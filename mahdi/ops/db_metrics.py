"""라이브 DB 하루치 → 지표 dict.

2026-08-01(운영점검보고서 2026-07-31 §5-2 Phase 2). 07-31 조사에서 손으로 돌린 SQL 25건을 고정한다.

**COCKPIT 배지(§5-5)와 계산 함수를 공유한다** — 리포트와 배지가 다른 답을 내면 어느 쪽을 믿을지
알 수 없다. `rest_demand()` / `monthly_book_coverage()`가 그 공유 지점이다.
"""

from __future__ import annotations

import logging
from datetime import date

from mahdi.data.db import ConnectionLike

logger = logging.getLogger("mahdi.ops.db_metrics")

# HMM 학습에 필요한 최소 샘플(scripts/fit_regime_engine.DEFAULT_MIN_SAMPLES와 같은 값).
HMM_MIN_SAMPLES = 8000

# 하루치 적재량을 볼 테이블 — (테이블, 시각 컬럼, 비고).
_DAILY_TABLES: list[tuple[str, str, str]] = [
    ("option_analysis_1m", "timestamp", "옵션체인 그릭스"),
    ("underlying_spot_1m", "timestamp", "기초자산 스팟"),
    ("market_raw_1m", "timestamp", "WS 체결 1분봉"),
    ("investor_flow_1m", "timestamp", "투자자 수급"),
    ("expiry_liquidity_1m", "timestamp", "만기 유동성"),
    ("macro_snapshot_5m", "timestamp", "매크로"),
    ("account_balance_snapshots", "timestamp", "계좌 잔고"),
    ("signal_decisions", "timestamp", "판단"),
    ("risk_snapshots", "timestamp", "리스크 스냅샷"),
    ("regime_state", "timestamp", "레짐"),
    ("feature_store", "timestamp", "레짐 피처"),
    ("rate_limiter_status_history", "recorded_at", "레이트리밋 이력"),
    ("market_halt_event_history", "recorded_at", "CB 전이 이력"),
]

# 매크로 컬럼별 non-null/고유값 — 항목별 갱신 주기 분리(2026-07-31)가 의도대로 도는지 본다.
_MACRO_COLUMNS = [
    "vix_front", "vix_next", "vix_term_structure", "usdcnh",
    "es_front", "zn_front", "us10y_yield", "usdkrw", "move_index",
]

# 레짐 피처의 "중립값" — 이 값이면 그 피처는 아직 살아있지 않다(2026-07-31 §2-4의 rv_ratio 1.0).
_FEATURE_NEUTRAL = {"rv_ratio": 1.0, "book_thinning": 0.0}


def collect(conn: ConnectionLike, target: date, elapsed_minutes: int | None = None) -> dict:
    """
    입력: DB 커넥션, 대상 날짜, (선택) 그날 관측이 돌아간 총 분 수 — 로그 지표에서 온다.
    계산: D1~D9을 한 번에 모은다. `elapsed_minutes`가 있으면 **먼슬리 절대 커버리지**도 함께
         낸다(§5-5 배지와 같은 함수를 쓴다).
    실패 조건: 지표 그룹마다 독립적으로 try/except 한다 — 하나가 죽어도 나머지는 낸다
              (`get_health_summary()`와 같은 원칙). 실패한 그룹은 키 자체가 빠진다.
    """
    out: dict = {}
    if elapsed_minutes:
        try:
            out["monthly_coverage"] = monthly_book_coverage(conn, target, elapsed_minutes)
        except Exception:
            conn.rollback()
            logger.warning("DB 지표 집계 실패: monthly_coverage", exc_info=True)
    for key, fn in (
        ("tables", _tables),
        ("book_coverage", _book_coverage),
        ("signal_decisions", _signal_decisions),
        ("risk_gate_distinct", _risk_gate_distinct),
        ("regime", _regime),
        ("feature_store", _feature_store),
        ("macro", _macro),
        ("market_halt", _market_halt),
        ("remaining_processes", _remaining_processes),
        ("rate_limiter", _rate_limiter),
    ):
        try:
            out[key] = fn(conn, target)
        except Exception:
            conn.rollback()
            logger.warning("DB 지표 집계 실패: %s", key, exc_info=True)
    return out


def _fetchall(conn: ConnectionLike, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _fetchone(conn: ConnectionLike, sql: str, params: tuple = ()) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _tables(conn: ConnectionLike, target: date) -> list[dict]:
    rows = []
    for table, ts_column, note in _DAILY_TABLES:
        try:
            result = _fetchone(
                conn,
                f"SELECT count(*), count(DISTINCT {ts_column}) FROM {table} WHERE {ts_column}::date=%s",
                (target,),
            )
        except Exception:
            conn.rollback()
            rows.append({"table": table, "rows": 0, "minutes": None, "note": "조회 실패"})
            continue
        rows.append(
            {"table": table, "rows": int(result[0]), "minutes": int(result[1]), "note": note}
        )
    return rows


def monthly_book_expiry(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> date | None:
    """
    계산: 그날의 **먼슬리(regular) 만기일**을 `expiry_liquidity_1m`에서 찾는다.
    해석: `option_analysis_1m`에는 `series` 컬럼이 없어 만기일만으로는 어느 북인지 모른다.
         `MAX(expiry)`를 먼슬리로 간주하는 방법은 **월물 만기 주간에 뒤집힌다**(먼슬리가 다음
         위클리(월)보다 가까워질 수 있다) — 그래서 `series`를 실제로 가진 테이블에서 끌어온다.
    실패 조건: 만기유동성 폴러의 첫 행은 08:31 부근이라 **장전에는 None**이다 — 호출측이
              "집계 전"으로 표시해야 한다(지어내지 않는다).
    """
    row = _fetchone(
        conn,
        "SELECT expiry FROM expiry_liquidity_1m "
        "WHERE underlying=%s AND series='regular' AND timestamp::date=%s "
        "ORDER BY timestamp DESC LIMIT 1",
        (underlying, target),
    )
    return row[0] if row else None


def monthly_book_coverage(
    conn: ConnectionLike, target: date, elapsed_minutes: int, underlying: str = "KOSPI200"
) -> dict:
    """
    계산: 먼슬리 북의 **1분 연속성** — GEX/감마플립 입력이 몇 %의 분에 실제로 있었는지.
    해석: 2026-07-31에 "밀림 83→46건인데 먼슬리 커버리지 95.0%→90.5%"라는 사례가 있었다 —
         인프라 지표만 보면 판단 입력 품질 후퇴를 놓친다. §5-5 배지의 핵심 지표.
    """
    expiry = monthly_book_expiry(conn, target, underlying)
    if expiry is None:
        return {"expiry": None, "minutes": None, "coverage_pct": None, "reason": "만기유동성 미적재(장전)"}
    row = _fetchone(
        conn,
        "SELECT count(DISTINCT timestamp) FROM option_analysis_1m "
        "WHERE underlying=%s AND expiry=%s AND timestamp::date=%s",
        (underlying, expiry, target),
    )
    minutes = int(row[0]) if row else 0
    pct = minutes / elapsed_minutes * 100 if elapsed_minutes > 0 else None
    return {
        "expiry": expiry,
        "minutes": minutes,
        "elapsed_minutes": elapsed_minutes,
        "coverage_pct": round(pct, 1) if pct is not None else None,
    }


def _book_coverage(conn: ConnectionLike, target: date) -> list[dict]:
    """북(series)별 만기와 적재 분 — 먼슬리/위클리 커버리지를 나란히 본다."""
    series_rows = _fetchall(
        conn,
        "SELECT DISTINCT ON (series) series, expiry FROM expiry_liquidity_1m "
        "WHERE timestamp::date=%s ORDER BY series, timestamp DESC",
        (target,),
    )
    span = _fetchone(
        conn,
        "SELECT count(DISTINCT timestamp) FROM option_analysis_1m WHERE timestamp::date=%s",
        (target,),
    )
    observed_minutes = int(span[0]) if span else 0
    out = []
    for series, expiry in series_rows:
        row = _fetchone(
            conn,
            "SELECT count(DISTINCT timestamp) FROM option_analysis_1m "
            "WHERE expiry=%s AND timestamp::date=%s",
            (expiry, target),
        )
        minutes = int(row[0]) if row else 0
        out.append(
            {
                "series": series,
                "expiry": expiry,
                "minutes": minutes,
                # 분모는 **그날 옵션체인이 실제로 돈 분 수**다(경과 분이 아니다) — 북 사이의 상대
                # 비교용이라 위클리가 설계대로 격분인지(≈50%)를 본다. "경과 분 대비 절대
                # 커버리지"는 monthly_book_coverage()가 따로 낸다 — 두 분모를 섞으면 안 된다.
                "coverage_pct": round(minutes / observed_minutes * 100, 1) if observed_minutes else None,
            }
        )
    return out


def _signal_decisions(conn: ConnectionLike, target: date) -> list[dict]:
    rows = _fetchall(
        conn,
        "SELECT decision, conviction, reject_reason, count(*) FROM signal_decisions "
        "WHERE timestamp::date=%s GROUP BY 1,2,3 ORDER BY 4 DESC",
        (target,),
    )
    return [
        {"decision": d, "conviction": c, "reject_reason": r, "count": int(n)} for d, c, r, n in rows
    ]


def _risk_gate_distinct(conn: ConnectionLike, target: date) -> int:
    """`risk_gate_state`의 고유값 수 — 1~2종이면 판단이 사실상 고정 출력이다(07-31: 4종)."""
    row = _fetchone(
        conn,
        "SELECT count(DISTINCT risk_gate_state::text) FROM signal_decisions WHERE timestamp::date=%s",
        (target,),
    )
    return int(row[0]) if row else 0


def _regime(conn: ConnectionLike, target: date) -> list[dict]:
    today = dict(
        (int(r[0]), int(r[1]))
        for r in _fetchall(
            conn, "SELECT regime, count(*) FROM regime_state WHERE timestamp::date=%s GROUP BY 1", (target,)
        )
    )
    rows = _fetchall(
        conn,
        "SELECT regime, count(*), count(DISTINCT timestamp::date) FROM regime_state GROUP BY 1 ORDER BY 1",
    )
    return [
        {"regime": str(regime), "today": today.get(int(regime), 0), "total": int(total), "days": int(days)}
        for regime, total, days in rows
    ]


def _feature_store(conn: ConnectionLike, target: date) -> dict:
    total_row = _fetchone(conn, "SELECT count(*) FROM feature_store")
    today_row = _fetchone(
        conn, "SELECT count(*) FROM feature_store WHERE timestamp::date=%s", (target,)
    )
    total = int(total_row[0]) if total_row else 0
    today = int(today_row[0]) if today_row else 0
    non_neutral = {}
    for feature, neutral in _FEATURE_NEUTRAL.items():
        row = _fetchone(
            conn,
            "SELECT count(*) FILTER (WHERE (features->>%s)::float <> %s), count(*) "
            "FROM feature_store WHERE timestamp::date=%s",
            (feature, neutral, target),
        )
        if row and int(row[1]):
            non_neutral[feature] = round(int(row[0]) / int(row[1]) * 100, 1)
    return {
        "today": today,
        "total": total,
        "hmm_threshold": HMM_MIN_SAMPLES,
        "hmm_progress_pct": round(total / HMM_MIN_SAMPLES * 100, 1),
        "non_neutral_pct": non_neutral,
    }


def _macro(conn: ConnectionLike, target: date) -> dict:
    selects = ", ".join(f"count({c}), count(DISTINCT {c})" for c in _MACRO_COLUMNS)
    row = _fetchone(conn, f"SELECT {selects} FROM macro_snapshot_5m WHERE timestamp::date=%s", (target,))
    if not row:
        return {}
    return {
        column: {"non_null": int(row[i * 2]), "distinct": int(row[i * 2 + 1])}
        for i, column in enumerate(_MACRO_COLUMNS)
    }


def _market_halt(conn: ConnectionLike, _target: date) -> dict:
    row = _fetchone(conn, "SELECT updated_at, last_message_at FROM market_halt_status LIMIT 1")
    if not row:
        return {"updated_at": None, "last_message_at": None}
    return {
        "updated_at": row[0].strftime("%H:%M:%S") if row[0] else None,
        "last_message_at": row[1].strftime("%H:%M:%S") if row[1] else None,
    }


def _remaining_processes(conn: ConnectionLike, _target: date) -> int | None:
    row = _fetchone(conn, "SELECT remaining_process_count FROM shutdown_check_log LIMIT 1")
    return int(row[0]) if row else None


def _rate_limiter(conn: ConnectionLike, target: date) -> dict:
    row = _fetchone(
        conn,
        "SELECT count(*), count(*) FILTER (WHERE last_cycle_overrun_seconds > 0), "
        "max(backoff_multiplier), avg(backoff_multiplier) "
        "FROM rate_limiter_status_history WHERE recorded_at::date=%s",
        (target,),
    )
    if not row or not row[0]:
        return {"rows": 0, "overrun_rows": 0, "max_multiplier": None, "mean_multiplier": None}
    return {
        "rows": int(row[0]),
        "overrun_rows": int(row[1]),
        "max_multiplier": round(float(row[2]), 3),
        "mean_multiplier": round(float(row[3]), 3),
    }


def rest_demand(conn: ConnectionLike, target: date) -> dict:
    """
    계산: `rate_limiter_status_history.total_calls`(마이그레이션 019) 증분으로 **초당 REST 수요**를
         구한다. COCKPIT 배지(§5-5)와 일일 리포트가 공유하는 함수다.
    해석: 누적 카운터는 프로세스 기동 이래 값이라 **재시작하면 되감긴다** — 감소를 감지하면 그
         지점에서 구간을 끊고 그 앞뒤를 따로 더한다(07-27 이후 장중 재시작은 없었지만 구조적으로
         대비해야 한다. 되감김을 무시하면 수요가 음수가 되거나 크게 과소평가된다).
    실패 조건: 표본이 2개 미만이거나 total_calls가 전부 NULL(019 적용 전)이면 None을 돌려준다.
    """
    rows = _fetchall(
        conn,
        "SELECT recorded_at, total_calls FROM rate_limiter_status_history "
        "WHERE recorded_at::date=%s AND total_calls IS NOT NULL ORDER BY recorded_at",
        (target,),
    )
    if len(rows) < 2:
        return {"calls": None, "calls_per_second": None, "capacity_pct": None,
                "deficit_threshold_multiplier": None}
    calls = 0
    for (_t_prev, c_prev), (_t_cur, c_cur) in zip(rows, rows[1:]):
        delta = int(c_cur) - int(c_prev)
        if delta >= 0:
            calls += delta
        # delta < 0 = 프로세스 재시작으로 카운터 리셋 — 그 구간은 건너뛴다(음수 누적 방지).
    span = (rows[-1][0] - rows[0][0]).total_seconds()
    if span <= 0:
        return {"calls": calls, "calls_per_second": None, "capacity_pct": None,
                "deficit_threshold_multiplier": None}
    from mahdi.ops.log_metrics import PACER_CAPACITY_CALLS_PER_SECOND

    per_second = calls / span
    return {
        "calls": calls,
        "span_seconds": round(span, 1),
        "calls_per_second": round(per_second, 3),
        "capacity_pct": round(per_second / PACER_CAPACITY_CALLS_PER_SECOND * 100, 1),
        "deficit_threshold_multiplier": round(PACER_CAPACITY_CALLS_PER_SECOND / per_second, 2)
        if per_second > 0
        else None,
    }
