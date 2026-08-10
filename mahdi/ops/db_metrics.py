"""라이브 DB 하루치 → 지표 dict.

2026-08-01(운영점검보고서 2026-07-31 §5-2 Phase 2). 07-31 조사에서 손으로 돌린 SQL 25건을 고정한다.

**COCKPIT 배지(§5-5)와 계산 함수를 공유한다** — 리포트와 배지가 다른 답을 내면 어느 쪽을 믿을지
알 수 없다. `rest_demand()` / `monthly_book_coverage()`가 그 공유 지점이다.
"""

from __future__ import annotations

import logging
from datetime import date

from mahdi import session
from mahdi.data import db
from mahdi.data.db import ConnectionLike
from mahdi.features.options_intel import (
    GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS,
    GAMMA_FLIP_MIN_LEGS,
)
from mahdi.fusion.signal_layer import IMPLEMENTED_MEMBER_FIELDS, MEMBER_FIELDS

logger = logging.getLogger("mahdi.ops.db_metrics")

# HMM 학습에 필요한 최소 샘플(scripts/fit_regime_engine.DEFAULT_MIN_SAMPLES와 같은 값).
HMM_MIN_SAMPLES = 8000

# 2026-08-05(§2-6 / §2-7) — 한 사이클이 **설계상** 수집해야 하는 레그 수.
#
# `mahdi.main`을 import하지 않고 여기 다시 적는 이유는 `log_metrics`가 순수 파서로 남는 것과
# 같다 — `ops`는 관측 계층이고 오케스트레이터에 의존하면 리포트가 라이브 코드를 끌고 온다.
# 대신 **두 값이 갈라지지 않는 것은 계약 테스트가 지킨다**
# (`tests/test_ops_metric_conventions.py`가 `main.STRIKES_EACH_SIDE`에서 재계산해 대조한다).
# 값을 여기서 바꾸면 그 테스트가 깨진다 — 그것이 요점이다.
MONTHLY_LEGS_PER_CYCLE_DESIGN = 10  # (ATM±2 = 5행사가) x 콜/풋
CHAIN_LEGS_PER_CYCLE_DESIGN = 20  # 먼슬리 10 + 위클리 1북 10(격분)

# 2026-08-05(§2-8) — "시장 구조상 불가피한 미가용" 사유. `main.MEMBER_UNAVAILABLE_CLOSING_AUCTION`과
# 같은 문자열이어야 §14-1이 그 분들을 분리해 낼 수 있다(계약 테스트가 지킨다).
STRUCTURAL_UNAVAILABLE_REASON = "종가 단일가(연속체결 없음)"

# 2026-08-06(§2-5 / Fix#5) — 장전 스팟 부재(`main.MEMBER_UNAVAILABLE_PREOPEN`)도 같은 계열이다.
# 두 사유 모두 결함이 아니라 시장 구조/설계이므로 §14-1의 「그중 구조적」 열이 함께 세야 한다.
# 문자열 일치는 `tests/test_ops_metric_conventions.py`가 기계적으로 지킨다.
# 2026-08-07(§3-1 / Fix#2) — 현물 장 마감(15:20~) 스팟 부재도 같은 계열이다.
# 문자열 일치는 `tests/test_ops_metric_conventions.py`가 세 상수 모두에 대해 기계적으로 지킨다.
STRUCTURAL_UNAVAILABLE_REASONS = frozenset(
    {STRUCTURAL_UNAVAILABLE_REASON, "장전(스팟 미적재)", "현물 장마감(스팟 미적재)"}
)

# 2026-08-06(§2-2 / Fix#1) — v6 §4.2 신규 진입 컷오프로 막힌 판단의 사유.
# `main._REJECT_REASON_ENTRY_CUTOFF`·`RiskEngine`이 내는 문자열과 **같아야** 이 지표가 그 분들을
# 세어낸다(`tests/test_ops_metric_conventions.py`가 세 곳의 일치를 기계적으로 지킨다).
ENTRY_CUTOFF_REJECT_REASON = "entry_cutoff"

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
    # 2026-08-06 고도화#5 — 진입 판단의 사후 평가. §12에 함께 세워 **테이블이 자란다는 사실**이
    # 다른 적재량과 같은 자리에서 보이게 한다(그것이 이 고도화가 선언한 대가다).
    ("decision_outcomes", "timestamp", "판단 사후 평가"),
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
        ("chain_minute_coverage", chain_minute_coverage),
        ("monthly_leg_completeness", monthly_leg_completeness),
        ("spot_source_divergence", spot_source_divergence),
        ("book_coverage", _book_coverage),
        ("book_gamma_map", book_gamma_map),
        ("wide_oi_landscape", wide_oi_landscape),
        ("member_availability", member_availability),
        ("member_score_quality", member_score_quality),
        ("strike_window_quality", strike_window_quality),
        ("signal_decisions", _signal_decisions),
        # 2026-08-06 Fix#3 — 위 리스트는 표를 그리기 위한 것이고, 이쪽은 **가설이 지목할 수 있는**
        # 축별 dict다(`db.decisions.…`). 둘은 같은 테이블을 다르게 접은 것이라 값이 갈리면
        # `crosscheck`가 잡는다.
        ("decisions", decisions),
        ("signal_reach", signal_reach),
        # 2026-08-06 고도화#5 — 진입 판단의 사후 평가(ADVISORY 기준선). 계산은 장마감 배치가
        # 먼저 하고(`scripts/daily_ops_report.py`), 여기서는 읽기만 한다.
        ("decision_outcomes", _decision_outcomes),
        ("risk_gate_distinct", _risk_gate_distinct),
        ("regime", _regime),
        ("feature_store", _feature_store),
        ("macro", _macro),
        ("market_halt", _market_halt),
        # 2026-08-06 Fix#3 — `2026-08-03-p4`가 지목했지만 존재하지 않던 절.
        ("ws_status", _ws_status),
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


def monthly_book_expiry_with_source(
    conn: ConnectionLike, target: date, underlying: str = "KOSPI200"
) -> tuple[date | None, str | None]:
    """
    계산: 그날의 **먼슬리(최근월) 만기일**과 그것을 어디서 얻었는지를 함께 돌려준다.
         1순위 `expiry_liquidity_1m`(series='regular' — 유일하게 series를 실제로 가진 테이블),
         2순위 `signal_decisions.gex_expiry`(그날 최빈값).
    해석: 2026-08-06(운영점검 장전편 §2-3 / Fix#3) — 1순위만 있던 종전 구현은
         **만기유동성 폴러에 매달려 있었고, 그 폴러는 08:31 전에 한 행도 남기지 않는다**(Fix#2).
         그래서 이 함수에 걸려 있는 지표 **세 개가 매일 장전 내내 통째로 눈이 멀었다**:
         §12 먼슬리 절대 커버리지 / §14-2 행사가 창 품질 / `monthly_leg_completeness()`.
         실제 피해가 08-06 07:48에 났다 — EGW00201 1건이 먼슬리 북을 10 → 9레그로 깎았는데
         **그것을 재라고 08-05 Fix#3이 만든 지표가 못 봤다.**

         2순위를 `signal_decisions.gex_expiry`(마이그레이션 023)로 두는 이유가 핵심이다.
         그 컬럼은 **판단이 실제로 그 분에 GEX를 낸 북**이고(`signal_book_legs()`가 골라
         `_build_signal_inputs()`가 기록한다), 장전에도 매분 채워진다. 즉 판단 경로는 처음부터
         만기유동성 폴러 없이도 먼슬리를 옳게 식별하고 있었고 **그 답을 DB에 남기고 있었다.**

         > **지표의 입력은 감시 대상의 입력보다 약해서는 안 된다.** 2026-08-05 고도화#1이
         > *"지표는 감시 대상과 **독립한** 입력을 써야 한다"* 를 규약으로 올렸는데, 여기서 난
         > 것은 그 반대쪽 실패다 — 독립성만으로는 부족하다.

         1순위를 남겨두는 이유: `gex_expiry`는 `MAX(expiry)` 규칙의 산물이라 **월물 만기 주간에
         뒤집힐 수 있다**(`signal_book_legs()` 주석). `series`를 실제로 가진 1순위가 있으면
         그것이 늘 옳으므로 먼저 본다. 출처를 함께 돌려주는 것은 리포트가 **어느 쪽으로 답했는지
         보이게** 하기 위해서다 — 폴백이 조용히 쓰이면 1순위가 죽은 것을 영영 모른다.
    실패 조건: 둘 다 없으면 (None, None) — 호출측이 "집계 전"으로 표시한다(지어내지 않는다).
    """
    row = _fetchone(
        conn,
        "SELECT expiry FROM expiry_liquidity_1m "
        "WHERE underlying=%s AND series='regular' AND timestamp::date=%s "
        "ORDER BY timestamp DESC LIMIT 1",
        (underlying, target),
    )
    if row:
        return row[0], "expiry_liquidity"

    row = _fetchone(
        conn,
        "SELECT gex_expiry FROM signal_decisions "
        "WHERE timestamp::date=%s AND gex_expiry IS NOT NULL "
        "GROUP BY gex_expiry ORDER BY count(*) DESC, gex_expiry DESC LIMIT 1",
        (target,),
    )
    if row:
        return row[0], "signal_decisions"
    return None, None


def monthly_book_expiry(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> date | None:
    """출처 없이 만기일만 필요한 호출측용 — 상세 규칙은 `monthly_book_expiry_with_source()` 참고."""
    return monthly_book_expiry_with_source(conn, target, underlying)[0]


def observed_span_minutes(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> int | None:
    """
    계산: 그날 옵션체인 폴러가 **실제로 돈 구간**의 분 수(첫 사이클 ~ 마지막 사이클, 양끝 포함).
    해석: 2026-08-03 COCKPIT 육안 점검에서 나온 요구. `monthly_book_coverage()`의 분자는 하루
         전체(장전 포함)를 세는데 COCKPIT 배지는 분모로 "09:00 이후 경과 분"을 넘기고 있어서
         **커버리지가 120.7%로 나왔다**(489분 / 405분). 기간이 어긋난 두 값을 나누고 있었던 것이다.
         분모를 이 함수로 구하면 분자와 같은 구간이 되어 100%를 넘을 수 없다.
    실패 조건: 그날 행이 없으면 None.
    """
    row = _fetchone(
        conn,
        "SELECT min(timestamp), max(timestamp) FROM option_analysis_1m "
        "WHERE underlying=%s AND timestamp::date=%s",
        (underlying, target),
    )
    if not row or row[0] is None:
        return None
    return int((row[1] - row[0]).total_seconds() // 60) + 1


def chain_minute_coverage(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 옵션체인 폴러가 돈 구간(`observed_span_minutes`와 같은 첫~끝) 안에서
         **`option_analysis_1m`에 행이 한 줄도 없는 분**을 센다.
    해석: 2026-08-05 §2-6 — 리포트 §4의 결손 지표는 **로그(사이클이 돌았는가)** 한 축뿐이라
         "사이클은 정상 실행됐는데 데이터가 0행"인 분을 **구조적으로 못 본다.**
         08-05 실측: 로그 기준 결손 1분(10:04)인데 DB 기준 0행 분은 **4분**
         (10:04 / 10:54 / 12:57 / 14:31)이었다. 14:31은 KIS가 53초간 전 레그를 타임아웃시켜
         `rows=0`으로 마감한 분이고(예산 초과 WARNING은 떴다), 10:54·12:57은 사이클이 `rows=19`를
         남겼는데 그 행이 **인접 분 타임스탬프로 적재된** 분이다(12:56이 22행 = 설계 상한 초과).
         **두 축의 값이 다르면 그 차이 자체가 신호다** — 리포트가 나란히 찍어 사람이 보게 한다.
    실패 조건: 그날 행이 없으면 `{"available": False}`.
    """
    row = _fetchone(
        conn,
        "SELECT min(date_trunc('minute', timestamp)), max(date_trunc('minute', timestamp)), "
        "       count(DISTINCT date_trunc('minute', timestamp)) "
        "FROM option_analysis_1m WHERE underlying=%s AND timestamp::date=%s",
        (underlying, target),
    )
    if not row or row[0] is None:
        return {"available": False}

    first, last, with_rows = row[0], row[1], int(row[2])
    span = int((last - first).total_seconds() // 60) + 1

    missing = _fetchall(
        conn,
        "SELECT to_char(m.t, 'HH24:MI') FROM generate_series(%s, %s, interval '1 minute') AS m(t) "
        "LEFT JOIN (SELECT DISTINCT date_trunc('minute', timestamp) AS t FROM option_analysis_1m "
        "           WHERE underlying=%s AND timestamp::date=%s) h ON h.t = m.t "
        "WHERE h.t IS NULL ORDER BY m.t",
        (first, last, underlying, target),
    )
    # 설계 상한(북 x 행사가 x 2)을 넘는 분 — 한 사이클의 행이 이웃 분 라벨로 들어갔다는 증거다.
    # 이것을 함께 내는 이유: 0행 분과 **같은 사건의 반대쪽 절반**이라 따로 보면 원인을 못 찾는다.
    over = _fetchall(
        conn,
        "SELECT to_char(t, 'HH24:MI'), n FROM ("
        "  SELECT date_trunc('minute', timestamp) AS t, count(*) AS n FROM option_analysis_1m "
        "  WHERE underlying=%s AND timestamp::date=%s GROUP BY 1"
        ") q WHERE n > %s ORDER BY t",
        (underlying, target, CHAIN_LEGS_PER_CYCLE_DESIGN),
    )
    return {
        "available": True,
        "span_minutes": span,
        "minutes_with_rows": with_rows,
        "zero_row_minutes": [r[0] for r in missing],
        "zero_row_count": len(missing),
        "over_design_minutes": [[r[0], int(r[1])] for r in over],
        "over_design_count": len(over),
    }


def spot_source_divergence(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 스팟의 **두 독립 소스**를 분 단위로 대조한다 —
         (A) `underlying_spot_1m.spot`: 옵션 조회에 얹혀 오는 KOSPI200 **지수**(REST).
         (B) `market_raw_1m.close`: 구독 중인 최근월 **선물** 1분봉 종가(WS).
         괴리율과, **지수가 얼어붙은 채 선물만 움직인 분 수**를 낸다.
    해석: 2026-08-05 §2-3 — 08-05 07:31~09:00에 (A)가 전일 종가 1000.03에 **75분간 고정**돼
         있는 동안 (B)는 1048까지 가 있었다. 두 값이 **48포인트(4.8%) 어긋난 채 15분을 갔는데
         아무도 비교하지 않았다.** ATM 롤링은 (B)를 보고 08:46에 옳게 움직였고, 신호 층은 (A)를
         써서 GEX를 스팟 1000.03 / 행사가 1042.5~1052.5로 계산했다.

         **괴리율에 임계를 걸지 않는다.** 08-05 실측으로 정규장 중 괴리는 0.5~0.9%가 정상이고
         (선물 베이시스는 실재하는 경제량이다), 보고서가 처음 적었던 "0.5% 2분 연속" 규칙은
         09:01·09:02·09:22·10:32에 오경보를 냈을 것이다. **정상 범위를 모르는 상태에서 임계를
         먼저 정하면 그 임계가 곧 결론이 된다** — 며칠 값을 쌓아 사람이 정한다.

         대신 `index_frozen_minutes`는 애매하지 않다: 지수가 **직전 분과 완전히 같은 값**인데
         선물은 움직인 분이다. 베이시스로는 설명되지 않는다(08-05 기준 그 값이 곧 사고다).
    실패 조건: 선물 심볼이나 어느 한쪽 데이터가 없으면 `{"available": False}`.
    """
    symbol_row = _fetchone(
        conn, "SELECT symbol FROM active_futures_symbol WHERE underlying=%s", (underlying,)
    )
    if not symbol_row or not symbol_row[0]:
        return {"available": False, "reason": "active_futures_symbol 없음"}

    row = _fetchone(
        conn,
        "WITH j AS ("
        "  SELECT s.timestamp AS t, s.spot AS idx, m.close AS fut, "
        "         lag(s.spot) OVER (ORDER BY s.timestamp) AS prev_idx, "
        "         lag(m.close) OVER (ORDER BY s.timestamp) AS prev_fut "
        "  FROM underlying_spot_1m s JOIN market_raw_1m m "
        "    ON m.timestamp = s.timestamp AND m.symbol = %s "
        "  WHERE s.timestamp::date = %s AND s.underlying = %s AND s.spot > 0"
        # 2026-08-07(§3-1 / Fix#1) — **지수가 살아 있는 구간만** 본다. 유가증권시장은
        # 15:20~15:30이 장 마감 동시호가이고 그 뒤로는 종가에 고정이라, 그 25분의 "정지"는
        # 사고가 아니라 시장 구조다. 08-04~08-07 나흘 내내 매일 정확히 9분이 이 지표에
        # 잡혀 원인 규명 대기 목록에 올라 있었다 — 규칙성 자체가 답이었다.
        # **2026-08-07 이후 데이터에는 그 구간 행이 아예 없지만**(main.py가 적재를 끊는다)
        # 과거 날짜를 재집계할 때를 위해 쿼리에도 경계를 건다. 두 곳이 갈리면 그날부터
        # 이 지표의 시계열이 조용히 두 가지가 된다.
        "    AND s.timestamp::time < %s"
        "), f AS ("
        "  SELECT t, (prev_idx = idx AND prev_fut IS DISTINCT FROM fut) AS frozen FROM j"
        "), g AS ("
        # 연속 구간(gaps-and-islands) — row_number 차이가 같으면 같은 덩어리다.
        "  SELECT frozen, row_number() OVER (ORDER BY t) "
        "         - row_number() OVER (PARTITION BY frozen ORDER BY t) AS grp FROM f"
        ") "
        "SELECT (SELECT count(*) FROM j), "
        "       (SELECT max(abs(idx - fut) / idx * 100) FROM j), "
        "       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(idx - fut) / idx * 100) FROM j), "
        "       (SELECT count(*) FROM f WHERE frozen), "
        "       COALESCE((SELECT max(n) FROM ("
        "           SELECT count(*) AS n FROM g WHERE frozen GROUP BY grp) q), 0)",
        (symbol_row[0], target, underlying, session.EQUITY_CONTINUOUS_TRADING_END),
    )
    minutes = int(row[0]) if row and row[0] is not None else 0
    if not minutes:
        return {"available": False, "reason": "지수/선물 공통 분 없음"}

    return {
        "available": True,
        "futures_symbol": symbol_row[0],
        # 2026-08-07 Fix#1 — 아래 값들이 재는 구간의 끝. 08-06 이전 리포트와 비교할 때
        # 이 경계가 없었다는 것을 알아야 한다(그때는 15:45까지 셌다).
        "live_window_end": session.EQUITY_CONTINUOUS_TRADING_END.strftime("%H:%M"),
        "minutes": minutes,
        "max_pct": round(float(row[1]), 3) if row[1] is not None else None,
        "median_pct": round(float(row[2]), 3) if row[2] is not None else None,
        "index_frozen_minutes": int(row[3]),
        # 총 건수보다 **연속 길이**가 판별력이 있다. 08-04(정상일)도 총 27분은 얼어 있는데
        # (지수는 옵션 조회에 얹혀 오므로 한 사이클 실패하면 값이 반복된다) 그것은 흩어진
        # 1~2분이다. 08-05의 사고는 **한 덩어리 16분**이었다.
        "index_frozen_max_run": int(row[4]),
    }


def monthly_leg_completeness(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 먼슬리(최근월) 북의 **분당 레그 수** 분포 — 설계값 미달/전무인 분 수.
    해석: 2026-08-05 §2-7 — §12의 먼슬리 커버리지는 *"그 분에 먼슬리 행이 있는가"* 만 보고
         **몇 개인지는 안 본다.** 08-05 실측: 커버리지 98.8%인데 레그 10개 미만이 489분 중
         **187분(38.2%)**, 0개인 분이 2개(14:31 / 14:46)였다. 먼슬리는 GEX/감마플립의 유일한
         입력이므로(v6 §11.4, 08-04 Fix#5) 이 값이 곧 **판단 주입력의 두께**다.
         커버리지와 반드시 나란히 읽는다 — 07-31 §5-5 원칙의 세 번째 재발 지점이다.
    실패 조건: 먼슬리 만기를 못 찾거나 행이 없으면 `{"available": False}`.
    """
    expiry = monthly_book_expiry(conn, target, underlying)
    if expiry is None:
        return {"available": False}

    row = _fetchone(
        conn,
        "SELECT count(*), "
        "       count(*) FILTER (WHERE n < %s), "
        "       count(*) FILTER (WHERE n < %s), "
        "       percentile_cont(0.5) WITHIN GROUP (ORDER BY n), min(n) "
        "FROM (SELECT date_trunc('minute', timestamp) AS t, count(*) AS n "
        "      FROM option_analysis_1m WHERE underlying=%s AND timestamp::date=%s AND expiry=%s "
        "      GROUP BY 1) q",
        (
            MONTHLY_LEGS_PER_CYCLE_DESIGN,
            GAMMA_FLIP_MIN_LEGS,
            underlying,
            target,
            expiry,
        ),
    )
    minutes = int(row[0]) if row else 0
    if not minutes:
        return {"available": False}

    below_design = int(row[1])
    return {
        "available": True,
        "expiry": expiry,
        "minutes": minutes,
        "design_legs": MONTHLY_LEGS_PER_CYCLE_DESIGN,
        "below_design_count": below_design,
        "below_design_pct": round(below_design / minutes * 100, 1),
        # BS 계산 자체가 불가능해지는 선 — 이 아래면 감마플립은 산출 시도조차 못 한다.
        "below_flip_minimum_count": int(row[2]),
        "legs_median": float(row[3]) if row[3] is not None else None,
        "legs_min": int(row[4]) if row[4] is not None else None,
    }


def monthly_book_coverage(
    conn: ConnectionLike, target: date, elapsed_minutes: int | None = None, underlying: str = "KOSPI200"
) -> dict:
    """
    입력: DB 커넥션, 대상 날짜, (선택) 분모로 쓸 경과 분, 기초자산 라벨.
    계산: 먼슬리 북의 **1분 연속성** — GEX/감마플립 입력이 몇 %의 분에 실제로 있었는지.
         `elapsed_minutes`를 안 넘기면 `observed_span_minutes()`로 **분자와 같은 구간**을 쓴다.
    해석: 2026-07-31에 "밀림 83→46건인데 먼슬리 커버리지 95.0%→90.5%"라는 사례가 있었다 —
         인프라 지표만 보면 판단 입력 품질 후퇴를 놓친다. §5-5 배지의 핵심 지표.

         **2026-08-03 COCKPIT 육안 점검**: 분자는 하루 전체(07:32~)를 세는데 COCKPIT이 분모로
         "09:00 이후 경과 분"을 넘겨 **120.7%**가 나오고 있었다. 그런데 배지는 `pct < 95`일 때만
         경고하므로 **지표가 고장났다는 사실 자체가 초록불로 표시**됐다 — 4주간 아무도 못 봤다.
         자동 리포트는 로그 span(하루 전체)을 넘겨 98.8%로 맞았으니, **배지와 리포트가 서로 다른
         답을 내고 있었다**(README 규약 정면 위반). 이제 인자를 안 넘기면 스스로 맞춘다.
    실패 조건: 만기유동성 미적재(장전)면 coverage_pct=None. 분모를 못 구해도 None.
              **100%를 넘으면 그 자체가 기간 불일치 신호**이므로 `over_100` 플래그를 세운다.
    """
    expiry, expiry_source = monthly_book_expiry_with_source(conn, target, underlying)
    if expiry is None:
        return {
            "expiry": None, "minutes": None, "coverage_pct": None,
            "reason": "먼슬리 북 식별 불가(만기유동성·판단 이력 모두 없음)",
        }
    if elapsed_minutes is None:
        elapsed_minutes = observed_span_minutes(conn, target, underlying)
    if not elapsed_minutes or elapsed_minutes <= 0:
        return {
            "expiry": expiry, "expiry_source": expiry_source,
            "minutes": None, "coverage_pct": None, "reason": "관측 구간 없음",
        }
    row = _fetchone(
        conn,
        "SELECT count(DISTINCT timestamp) FROM option_analysis_1m "
        "WHERE underlying=%s AND expiry=%s AND timestamp::date=%s",
        (underlying, expiry, target),
    )
    minutes = int(row[0]) if row else 0
    pct = minutes / elapsed_minutes * 100
    return {
        "expiry": expiry,
        # 2026-08-06 Fix#3 — 폴백이 조용히 쓰이면 1순위(만기유동성 폴러)가 죽은 것을 영영 모른다.
        "expiry_source": expiry_source,
        "minutes": minutes,
        "elapsed_minutes": elapsed_minutes,
        "coverage_pct": round(pct, 1),
        # 커버리지는 정의상 100%를 넘을 수 없다 — 넘었다면 분자와 분모의 기간이 어긋난 것이다.
        # 조용히 넘어가면 2026-08-03처럼 "고장난 지표가 초록불"이 된다.
        "over_100": pct > 100.0,
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


def book_gamma_map(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> list[dict]:
    """
    입력: DB 커넥션, 대상 날짜, 기초자산 라벨.
    계산: 그날 **장 마지막 스냅샷**을 만기별로 나눠 북마다 GEX / 감마플립 / 핀 리스크를 낸다.
    해석: 2026-08-03 §5-5 — 세 북을 합산하면 만기별 정보가 서로를 덮는다. 특히 **만기 당일 북은
         잔존만기가 0이라 감마플립이 정의되지 않는 반면 핀 리스크(v6 §A3)는 그 북에서만** 나온다.
         2026-08-03이 실제로 weekly_mon 만기일이었는데 그날 GEX는 세 북 합산 하나뿐이었다.

         2026-08-04(운영점검보고서 §2-7 / Fix#7) — **위 "장 마지막 스냅샷"은 거짓말이었다.**
         쿼리에 `timestamp::date=target`만 있고 **시각 경계가 없어서**, 그날 한 번이라도 방문한
         모든 행사가가 최신값으로 남았다. 실측 피해:
           - 보고된 레그 수 46/48/50 → 실제 장 마지막 창의 레그는 10/12/12(행사가 5~6개)
           - 만기 08-10의 `핀 행사가 957.5`는 **10시경에만 창에 있던 행사가**다(장 마감 창은
             995~1007.5). 즉 핀 리스크가 5시간 전 데이터로 계산됐다.
           - iv/gamma가 최대 7시간 차이 나는 값끼리 합산됐다.
         이것은 2026-08-03 §2-2가 `latest_option_chain()`에서 고친 결함과 **완전히 같은 패턴**이고,
         그 수정과 같은 날 작성된 새 파일에서 재발했다(`latest_expiry_liquidity()`까지 합치면
         세 번째다). 그래서 새 SQL을 쓰지 않고 **`db.option_chain_as_of()`를 그대로 재사용한다** —
         체인을 읽는 경로를 하나로 모으면 이 결함이 네 번째로 재발할 수 없다(고도화#1 규약 B).
    실패 조건: 그날 체인/스팟이 없으면 빈 목록 — 지어내지 않는다.
    """
    from mahdi.features.options_intel import calculate_gex, find_gamma_flip, legs_by_expiry, pin_risk

    spot_row = _fetchone(
        conn,
        "SELECT spot FROM underlying_spot_1m WHERE underlying=%s AND timestamp::date=%s "
        "ORDER BY timestamp DESC LIMIT 1",
        (underlying, target),
    )
    if not spot_row or spot_row[0] is None:
        return []
    spot = float(spot_row[0])

    last_chain_at = _fetchone(
        conn,
        "SELECT max(timestamp) FROM option_analysis_1m WHERE underlying=%s AND timestamp::date=%s",
        (underlying, target),
    )
    if not last_chain_at or last_chain_at[0] is None:
        return []
    # 라이브 판단이 보는 것과 **같은 함수·같은 신선도 창**으로 그날 마지막 스냅샷을 만든다.
    chain_rows = db.option_chain_as_of(conn, underlying, last_chain_at[0].replace(tzinfo=None))

    out = []
    for expiry, legs in legs_by_expiry(chain_rows, target).items():
        pin = pin_risk(legs, spot)
        out.append(
            {
                "expiry": expiry,
                "legs": len(legs),
                "gex": calculate_gex(legs, spot),
                "gamma_flip": find_gamma_flip(legs, spot),
                "pin_strike": pin["strike"] if pin else None,
                "pin_concentration_pct": round(pin["concentration"] * 100, 1) if pin else None,
                "expiry_today": expiry == target,
            }
        )
    return out


def wide_oi_landscape(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> list[dict]:
    """
    입력: DB 커넥션, 대상 날짜, 기초자산 라벨.
    계산: 그날 **한 번이라도 방문한 모든 행사가**로 북별 체인을 만들어,
         (a) 콜−풋 OI 편중(어느 쪽으로 몇 행사가나 쏠렸는가)과
         (b) **그 폭 전체를 탐색 구간으로 준 감마플립**(`find_gamma_flip(search_pct=방문폭)`)을 낸다.

         (b)가 이 함수의 핵심이다. 행사가별 C−P 부호가 국소적으로 바뀌는 것과 GEX(S) 부호가
         바뀌는 것은 **다른 사건**이다 — GEX(S)는 모든 행사가의 감마 가중 합이라, 08-04 먼슬리처럼
         작은 양수 행사가 4개(+11/+265/+15/+6)가 1000.0의 −3,847에 묻히면 국소 부호 전환이
         6번 있어도 GEX(S)는 전 구간 음수다. 국소 부호로 판정하면 "가능"이라는 **틀린 답**이 나온다.
    해석: 2026-08-04 §2-3 / 고도화#4 — 이 표가 08-04의 결정을 뒤집었다.

         `7716dd4`(08-04 07:49)는 *"감마플립 0%의 원인은 코드가 아니라 행사가 창"* 이라는 전제로
         먼슬리 광폭 체인(ATM±7)에 REST 예산을 얼마나 쓸지 정하려 했다. 그런데 그날 ATM 지터
         (§2-2)가 **공짜로 25행사가(952.5~1012.5, ±3%) 자연실험**을 만들어줬고, 그 결과는:
           먼슬리 25행사가 중 **20개에서 C−P가 음수**, 양수 5개의 합 +297 vs 1000.0 한 자리 −3,847.
         즉 이 북은 어느 폭으로 잘라도 GEX가 부호를 안 바꾼다 — **광폭 체인은 밀림만 늘린다.**

         그런데 Fix#6(ATM 히스테리시스)이 지터를 줄이면 **이 관측 능력도 함께 줄어든다.**
         그래서 같은 판정을 매일 자동으로 남긴다 — 추가 REST 호출은 0건이다(이미 있는 데이터다).
         `sign_flips`가 0에서 벗어나는 날이 **감마플립이 살아날 수 있는 첫 날**이고, 그때 비로소
         광폭 체인 안건을 다시 꺼낼 근거가 생긴다.
    실패 조건: 그날 체인이 없으면 빈 목록.
    """
    from mahdi.features.options_intel import find_gamma_flip, legs_by_expiry

    spot_row = _fetchone(
        conn,
        "SELECT spot FROM underlying_spot_1m WHERE underlying=%s AND timestamp::date=%s "
        "ORDER BY timestamp DESC LIMIT 1",
        (underlying, target),
    )
    if not spot_row or spot_row[0] is None:
        return []
    spot = float(spot_row[0])

    rows = _fetchall(
        conn,
        "SELECT DISTINCT ON (expiry, strike, option_type) "
        "       expiry, strike, option_type, oi, iv, gamma "
        "FROM option_analysis_1m WHERE underlying=%s AND timestamp::date=%s AND expiry >= %s "
        "ORDER BY expiry, strike, option_type, timestamp DESC",
        (underlying, target, target),
    )
    chain_rows = [
        {
            "expiry": expiry, "strike": float(strike), "option_type": option_type,
            "oi": float(oi or 0.0), "iv": float(iv or 0.0), "gamma": float(gamma or 0.0),
        }
        for expiry, strike, option_type, oi, iv, gamma in rows
    ]

    oi_by_strike: dict[date, dict[float, float]] = {}
    for row in chain_rows:
        sign = 1.0 if row["option_type"].lower() == "c" else -1.0
        book = oi_by_strike.setdefault(row["expiry"], {})
        book[row["strike"]] = book.get(row["strike"], 0.0) + sign * row["oi"]

    out = []
    for expiry, legs in legs_by_expiry(chain_rows, target).items():
        diffs = oi_by_strike.get(expiry, {})
        if not diffs:
            continue
        strikes = sorted(diffs)
        # 방문한 행사가 전체를 덮는 탐색 폭 — "체인을 이만큼 넓혔다면 어땠을까"에 답하는 값이다.
        reach = max(abs(strikes[0] - spot), abs(strikes[-1] - spot)) / spot if spot else 0.0
        wide_flip = find_gamma_flip(legs, spot, search_pct=reach) if reach > 0 else None
        out.append(
            {
                "expiry": expiry,
                "strikes": len(strikes),
                "strike_min": strikes[0],
                "strike_max": strikes[-1],
                "search_pct": round(reach * 100, 2),
                "net_call_put_oi": round(sum(diffs.values())),
                "call_heavy_strikes": sum(1 for d in diffs.values() if d > 0),
                "put_heavy_strikes": sum(1 for d in diffs.values() if d < 0),
                "wide_gamma_flip": wide_flip,
                "flip_possible": wide_flip is not None,
            }
        )
    return out


def member_availability(conn: ConnectionLike, target: date) -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 앙상블 멤버마다 **그날 몇 분이나 살아 있었는지**와, 죽어 있었다면 **가장 흔한 사유**를
         `signal_decisions.risk_gate_state`에서 집계한다.
    해석: 2026-08-04 고도화#2 — 종전 §14는 `앙상블 최대 가용 멤버 2개` 한 줄뿐이었다. 그 "2개"가
         어느 둘인지 알아내려고 08-04에 사람이 `signal_layer.py`를 읽어 역산해야 했고, 그 역산
         끝에 `orderflow_ofi_vpin`이 **데이터가 있는데도** 죽어 있다는 것이 나왔다(§2-5).
         사유까지 남으니 이제 표 한 줄로 끝난다.
    실패 조건: `member_unavailable` 키가 없는 날(2026-08-04 이전)은 `{"available": False}` —
              0%로 표시해 "전 멤버가 죽었다"는 거짓 신호를 만들지 않는다.
    """
    total_row = _fetchone(
        conn,
        "SELECT count(*) FROM signal_decisions "
        "WHERE timestamp::date=%s AND risk_gate_state ? 'member_unavailable'",
        (target,),
    )
    minutes = int(total_row[0]) if total_row else 0
    if not minutes:
        return {"available": False, "reason": "member_unavailable 미기록(2026-08-04 고도화#2 이전)"}

    rows = _fetchall(
        conn,
        "SELECT member, reason, count(*) FROM signal_decisions, "
        "     LATERAL jsonb_each_text(risk_gate_state->'member_unavailable') AS t(member, reason) "
        "WHERE timestamp::date=%s GROUP BY member, reason",
        (target,),
    )
    unavailable: dict[str, dict[str, int]] = {}
    for member, reason, count in rows:
        unavailable.setdefault(member, {})[reason] = int(count)

    members = []
    for name in MEMBER_FIELDS:
        by_reason = unavailable.get(name, {})
        dead_minutes = sum(by_reason.values())
        top_reason = max(by_reason.items(), key=lambda kv: kv[1])[0] if by_reason else None
        # 2026-08-05 §2-8 — 시장 구조상 불가피한 미가용은 **가용률과 분리해서** 낸다.
        # 섞어두면 08-05처럼 종가 단일가 9분이 `orderflow_ofi_vpin` 83.0%에 녹아들어
        # "이 멤버는 원래 좀 죽는다"로 읽힌다. 분자에서 빼지 않고 열을 따로 두는 이유는
        # 가용률의 정의를 조용히 바꾸지 않기 위해서다(전일 대비 델타가 의미를 잃는다).
        structural = sum(
            count for reason, count in by_reason.items() if reason in STRUCTURAL_UNAVAILABLE_REASONS
        )
        members.append(
            {
                "member": name,
                "available_minutes": minutes - dead_minutes,
                "available_pct": round((minutes - dead_minutes) / minutes * 100, 1),
                "top_unavailable_reason": top_reason,
                "structural_minutes": structural,
                "implemented": name in IMPLEMENTED_MEMBER_FIELDS,
            }
        )
    # 2026-08-06 §3-1 / Fix#3 — **멤버 이름으로 바로 지목할 수 있게** 펼쳐 둔다.
    # 08-05 `p12`가 적은 경로는 `db.member_availability.orderflow_ofi_vpin.structural_minutes`였고,
    # 실제 구조는 `…members`(리스트)라 그 가설은 주장 지표를 하나도 못 받았다. 표를 그리는 쪽은
    # 순서가 필요하니 리스트를 남기고, 지목하는 쪽을 위해 같은 dict를 이름으로도 건다
    # (`MEMBER_FIELDS`는 available/minutes/members와 겹치지 않는다 —
    # `tests/test_ops_db_metrics.py`가 그 불변식을 지킨다).
    out: dict = {"available": True, "minutes": minutes, "members": members}
    out.update({entry["member"]: entry for entry in members})
    return out


# 설계상 한 북이 유지하는 편측 행사가 수(`main.STRIKES_EACH_SIDE`). 여기에 두는 이유는
# db_metrics를 관측 루프에 의존시키지 않기 위함이며, 두 값이 갈라지면 테스트가 잡는다.
STRIKE_WINDOW_EACH_SIDE = 2
KOSPI200_STRIKE_INTERVAL = 2.5


def strike_window_quality(
    conn: ConnectionLike, target: date, underlying: str = "KOSPI200"
) -> dict:
    """
    입력: DB 커넥션, 대상 날짜, 기초자산 라벨.
    계산: **수집한 행사가가 스팟을 제대로 감싸고 있었는가**를 분 단위로 잰다.
         (a) **ATM 정합률** — 그 분의 스팟에서 계산한 ATM이 그 분에 실제로 수집된 먼슬리 행사가
             집합 안에 있었는가. **이것이 핵심 지표다**(08-03의 결함이면 0%가 나온다).
         (b) 창 정합률 — 설계 창(ATM±2, 5행사가)을 **전부** 덮었는가.
         (c) **ATM 이탈 거리** — 수집 창의 중심이 그 분의 진짜 ATM에서 몇 행사가나 떨어졌는가.
         (d) 창 폭 지터 — 체인 스냅샷(신선도 창) 안의 서로 다른 행사가 수 ÷ 설계값.

         **(b)를 합격/불합격으로 읽지 말 것.** 100% 밑이 정상이다: 재롤링은 선물 1분봉이 완성될
         때 일어나는데 그 분의 폴링은 이미 시작됐거나 끝났으므로 **구조적으로 한 틱 늦는다**.
         게다가 Fix#6(히스테리시스 0.75칸)은 **일부러** 창을 늦게 옮긴다 — (b)만 보면 그 fix가
         회귀로 보인다. (b)는 추세로 읽고(급락하면 무언가 바뀐 것), 판정은 (a)와 (c)로 한다.
         08-04 실측(히스테리시스 적용 전): (a) **96.1%** / (b) 35.5% / (c) 중앙 **1.0칸** ·
         최대 **7.0칸**(=17.5p). 최대값이 큰 것은 09시대 스팟 급변 구간이다 — (c)의 최대가
         편측 폭(2칸)을 넘는 분은 **그 분의 체인이 스팟을 아예 못 감쌌다**는 뜻이므로
         (a)와 함께 읽는다.
    해석: 2026-08-04 고도화#3 — 지표 사이에 빈 칸이 있었다.
           §12 커버리지  = "데이터가 DB에 있는가"
           §14 신호 도달률 = "그 데이터가 판단까지 갔는가"
         그 사이에 **"수집한 행사가가 애초에 맞는 행사가였는가"** 가 없었다. 08-03에 하루치
         체인 전체가 스팟에서 5.5% 떨어진 외가격에서 수집됐는데(재롤링이 WS 연결당 1회만 돌아
         행사가가 07:31 값에 고정됐다) 커버리지는 98.8%로 훌륭했고 감마플립 산출률도 0%라는
         같은 답만 냈다 — **이 지표 하나면 그날 바로 잡혔다.**
         (c)는 Fix#6b(신선도 창 10→5분)의 직접 지표다. 08-04 실측으로 10분 창에서는 먼슬리
         중앙 14레그(설계 10), 5분 창에서는 12레그였다.
    실패 조건: 그날 먼슬리 북을 식별할 수 없거나(장전) 스팟이 없으면 `{"available": False}`.
    """
    expiry = monthly_book_expiry(conn, target, underlying)
    if expiry is None:
        return {"available": False, "reason": "먼슬리 북 식별 불가(만기유동성·판단 이력 모두 없음)"}

    interval = KOSPI200_STRIKE_INTERVAL
    side = STRIKE_WINDOW_EACH_SIDE
    row = _fetchone(
        conn,
        """
        WITH per_minute AS (
            SELECT o.timestamp AS ts,
                   round((s.spot / %(interval)s)::numeric) * %(interval)s AS atm,
                   array_agg(DISTINCT o.strike) AS strikes
            FROM option_analysis_1m o
            JOIN underlying_spot_1m s
              ON s.underlying = o.underlying AND s.timestamp = o.timestamp
            WHERE o.underlying=%(underlying)s AND o.timestamp::date=%(target)s AND o.expiry=%(expiry)s
            GROUP BY o.timestamp, s.spot
        )
        SELECT count(*),
               count(*) FILTER (WHERE atm = ANY(strikes)),
               count(*) FILTER (WHERE (
                   SELECT bool_and(atm + g * %(interval)s = ANY(strikes))
                   FROM generate_series((-%(side)s)::int, (%(side)s)::int) AS g
               )),
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY abs(atm - (( (SELECT min(x) FROM unnest(strikes) x)
                                       + (SELECT max(x) FROM unnest(strikes) x) ) / 2)) / %(interval)s
               ),
               max(abs(atm - (( (SELECT min(x) FROM unnest(strikes) x)
                              + (SELECT max(x) FROM unnest(strikes) x) ) / 2)) / %(interval)s)
        FROM per_minute
        """,
        {"interval": interval, "side": side, "underlying": underlying, "target": target, "expiry": expiry},
    )
    if not row or not row[0]:
        # 2026-08-06 — 이 분기는 위 SQL이 `underlying_spot_1m`과 **조인**한 결과가 빈 경우다.
        # 종전 문구("먼슬리 체인 미적재")는 Fix#3 전에는 만기 식별 실패와 뭉뚱그려져 있어
        # 그럴듯했지만, 이제 체인이 976행 있는데도 이 문구가 나온다(08-06 08:20 실측).
        # **실제 원인은 거의 항상 스팟 쪽**이다 — 장전에는 설계상 스팟이 없다(`9ffcb9c`).
        return {"available": False, "reason": "스팟 미적재 — 체인과 겹치는 분이 없다(장전에는 정상)"}
    minutes, atm_hit, window_hit = int(row[0]), int(row[1]), int(row[2])
    offset_median = float(row[3]) if row[3] is not None else None
    offset_max = float(row[4]) if row[4] is not None else None

    jitter = _fetchone(
        conn,
        """
        WITH mins AS (
            SELECT DISTINCT timestamp AS m FROM option_analysis_1m
            WHERE underlying=%(underlying)s AND timestamp::date=%(target)s AND expiry=%(expiry)s
        )
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY c), max(c)
        FROM mins, LATERAL (
            SELECT count(DISTINCT o.strike) AS c FROM option_analysis_1m o
            WHERE o.underlying=%(underlying)s AND o.expiry=%(expiry)s
              AND o.timestamp <= mins.m
              AND o.timestamp > mins.m - (%(window)s || ' min')::interval
        ) x
        """,
        {"underlying": underlying, "target": target, "expiry": expiry,
         "window": db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES},
    )
    # 2026-08-05(고도화#1 / 규약 D) — **같은 지표를 독립 소스로 한 번 더 잰다.**
    #
    # 위 계산은 ATM을 `underlying_spot_1m`(REST 지수)에서 낸다. 그런데 그 스팟은 이 지표가
    # 감시하는 파이프라인(옵션체인 폴러)이 **같은 응답에서 뽑아 적재한 값**이다 — 감시자와
    # 감시 대상이 입력을 공유한다. 08-05에 그 결합이 90분짜리 사고를 통과시켰다: 지수가 전일
    # 종가에 얼어붙어 있어 스팟도 행사가도 틀렸는데 **둘이 서로 일치해서** 정합으로 세어졌고,
    # 지표는 88.1%를 냈다.
    #
    # 선물 WS 1분봉은 **완전히 다른 경로**(WebSocket 체결 스트림)로 들어온다. 두 값이 갈리면
    # 그 차이 자체가 신호다 — 어느 쪽이 옳은지는 이 함수가 정하지 않는다(사람이 §16의 스팟 소스
    # 괴리와 함께 읽는다).
    futures_row = _fetchone(
        conn,
        """
        WITH per_minute AS (
            SELECT round((m.close / %(interval)s)::numeric) * %(interval)s AS atm_fut,
                   round((s.spot / %(interval)s)::numeric) * %(interval)s AS atm_idx,
                   array_agg(DISTINCT o.strike) AS strikes
            FROM option_analysis_1m o
            JOIN active_futures_symbol f ON f.underlying = o.underlying
            JOIN market_raw_1m m ON m.timestamp = o.timestamp AND m.symbol = f.symbol
            JOIN underlying_spot_1m s
              ON s.underlying = o.underlying AND s.timestamp = o.timestamp
            WHERE o.underlying=%(underlying)s AND o.timestamp::date=%(target)s
              AND o.expiry=%(expiry)s AND m.close > 0 AND s.spot > 0
            GROUP BY o.timestamp, m.close, s.spot
        )
        SELECT count(*),
               count(*) FILTER (WHERE atm_fut = ANY(strikes)),
               count(*) FILTER (WHERE atm_idx = ANY(strikes))
        FROM per_minute
        """,
        {"interval": interval, "underlying": underlying, "target": target, "expiry": expiry},
    )
    # **두 값을 같은 분 집합에서 낸다.** 분모가 다르면 차이가 스팟 소스 때문인지 분 집합 때문인지
    # 구분되지 않는다 — 선물 1분봉은 WS 체결이 있어야 생기므로 장전 상당 구간이 빠진다.
    futures_minutes = int(futures_row[0]) if futures_row and futures_row[0] else 0
    atm_by_futures = atm_by_index_same_minutes = gap = None
    if futures_minutes:
        atm_by_futures = round(int(futures_row[1]) / futures_minutes * 100, 1)
        atm_by_index_same_minutes = round(int(futures_row[2]) / futures_minutes * 100, 1)
        gap = round(atm_by_futures - atm_by_index_same_minutes, 1)

    design_strikes = side * 2 + 1
    median_strikes = float(jitter[0]) if jitter and jitter[0] is not None else None
    return {
        "available": True,
        "expiry": expiry,
        "minutes": minutes,
        "atm_covered_pct": round(atm_hit / minutes * 100, 1),
        # 규약 D — 독립 소스(선물 WS) 기준의 같은 지표. `gap`이 이 교차 검증의 결론이다.
        "atm_covered_pct_by_futures": atm_by_futures,
        "atm_covered_pct_by_index_same_minutes": atm_by_index_same_minutes,
        "atm_source_gap_pt": gap,
        "futures_cross_check_minutes": futures_minutes,
        "window_covered_pct": round(window_hit / minutes * 100, 1),
        "atm_offset_strikes_median": round(offset_median, 2) if offset_median is not None else None,
        "atm_offset_strikes_max": round(offset_max, 2) if offset_max is not None else None,
        "design_strikes": design_strikes,
        "snapshot_strikes_median": median_strikes,
        "snapshot_strikes_max": int(jitter[1]) if jitter and jitter[1] is not None else None,
        "width_jitter": round(median_strikes / design_strikes, 2) if median_strikes else None,
        "snapshot_window_minutes": db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES,
    }


def member_score_quality(conn: ConnectionLike, target: date) -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: `risk_gate_state.member_scores`에서 **멤버가 무엇을 말했는가**를 집계한다 —
         멤버별 산출 분·평균·부호 분포와, 구현된 멤버 **쌍마다 부호가 같았던 비율**.
    해석: 2026-08-05 고도화#4 — 08-05까지의 모든 관측은 *"판단이 나오는가"* 를 쟀고 그날 답이
         Yes가 됐다(가용 멤버 4가 409분, 확신도 4종, 전이 83회). **다음 질문은 "그 판단이
         무엇에 반응하는가"다.**

         **부호 일치율이 핵심이다.** 4멤버가 항상 같은 부호면 앙상블은 실질 1멤버이고,
         가중 평균은 가중치를 바꿔도 답이 안 바뀐다 — 즉 `available_member_count`가 4라는 것이
         판단이 4개 축을 본다는 뜻이 아니다. 08-05의 `SMALL_TEST` 41건
         (`conflict_resolution:no_clear_consensus`)이 **멤버가 실제로 갈렸다는 첫 증거**였고,
         그중 36건이 14~15시(Charm 경로가 열리는 시각)에 몰려 있었다.

         **진입이 없어도 잴 수 있는 지표다.** ADVISORY 전용을 이유로 미루면 실거래 전환
         시점에 비교할 기준선이 없다.
    실패 조건: `member_scores` 키가 없는 날(2026-08-05 고도화#4 이전)은 `{"available": False}`.
    """
    rows = _fetchall(
        conn,
        "SELECT member, count(*), avg(score), "
        "       count(*) FILTER (WHERE score > 0), count(*) FILTER (WHERE score < 0), "
        "       count(*) FILTER (WHERE score = 0) "
        "FROM signal_decisions, "
        "     LATERAL jsonb_each_text(risk_gate_state->'member_scores') AS t(member, raw), "
        "     LATERAL (SELECT raw::double precision AS score) s "
        "WHERE timestamp::date=%s GROUP BY member",
        (target,),
    )
    if not rows:
        return {"available": False, "reason": "member_scores 미기록(2026-08-05 고도화#4 이전)"}

    members = [
        {
            "member": r[0],
            "scored_minutes": int(r[1]),
            "mean": round(float(r[2]), 4) if r[2] is not None else None,
            "positive": int(r[3]),
            "negative": int(r[4]),
            "zero": int(r[5]),
        }
        for r in rows
    ]
    members.sort(key=lambda m: MEMBER_FIELDS.index(m["member"]) if m["member"] in MEMBER_FIELDS else 99)

    # 쌍별 부호 일치율 — **둘 다 비영(非零)인 분**만 분모로 센다. 0은 "중립"이지 "동의"가
    # 아니므로, 0을 포함하면 아무 말도 안 한 멤버가 일치율을 부풀린다.
    pair_rows = _fetchall(
        conn,
        "SELECT a.member, b.member, count(*), "
        "       count(*) FILTER (WHERE sign(a.score) = sign(b.score)) "
        "FROM signal_decisions d, "
        "     LATERAL jsonb_each_text(d.risk_gate_state->'member_scores') AS ta(member, raw_a), "
        "     LATERAL (SELECT ta.member AS member, raw_a::double precision AS score) a, "
        "     LATERAL jsonb_each_text(d.risk_gate_state->'member_scores') AS tb(member, raw_b), "
        "     LATERAL (SELECT tb.member AS member, raw_b::double precision AS score) b "
        "WHERE d.timestamp::date=%s AND a.member < b.member AND a.score <> 0 AND b.score <> 0 "
        "GROUP BY a.member, b.member ORDER BY a.member, b.member",
        (target,),
    )
    pairs = [
        {
            "a": r[0], "b": r[1], "both_nonzero_minutes": int(r[2]),
            "same_sign_minutes": int(r[3]),
            "same_sign_pct": round(int(r[3]) / int(r[2]) * 100, 1) if int(r[2]) else None,
        }
        for r in pair_rows
    ]
    return {"available": True, "members": members, "pairs": pairs}


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


# ===== 2026-08-06 §3-1 / Fix#3 — 사람이 자연스럽게 적는 판단 지표 경로 =====
#
# `_signal_decisions()`는 **표를 그리기 위한** 리스트다(decision x conviction x reject_reason의
# 교차 집계). 그것만 있으면 가설 하나를 검정하기 위해 리스트를 순회해 합산해야 하는데,
# 08-05에 사람이 `hypotheses.yaml`에 실제로 적은 것은 이런 경로였다:
#
#     db.decisions.reject_reason.strategy_palette:wait_only
#     db.decisions.conviction.HIGH_CONVICTION
#     db.decisions.decision.ENTER
#     db.decisions.vrp.non_null_ratio
#     db.decisions.distinct_entry_strategies
#
# **그중 어느 것도 존재하지 않았다.** 13건 중 6건이 주장 지표를 하나도 못 받고 하루를 갔다.
# 경로를 사람에게 맞추는 편이 사람을 경로에 맞추는 것보다 낫다 — 위 다섯 개를 전부 여기서 만든다.
def decisions(conn: ConnectionLike, target: date) -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 그날 `signal_decisions`를 축별로 접어 dict로 낸다 — decision/conviction/reject_reason
         각각의 건수, `vrp` 채움 비율, 진입 전략 분포, 그리고 **진입 컷오프 불변식**(§14).
    해석: `enter_after_cutoff`/`enter_after_forced_flat`은 v6 §4.2를 어긴 판단의 수다.
         **0이어야 하는 불변식**이지 "낮으면 좋은 지표"가 아니다 — 08-06 실측은 21/18이었고,
         그 값이 0이 아닌 날은 게이트가 빠진 것이지 시장이 특이한 것이 아니다.
    실패 조건: 컬럼(`vrp`)이나 jsonb 키가 없는 과거 날짜에서도 죽지 않는다 — 없는 축은 키가
              빠지는 게 아니라 0/None으로 남는다(키가 사라지면 그 경로를 쓰는 가설이 다시
              「경로 없음」이 된다 — 이 함수가 고치려던 바로 그 문제다).
    """
    total_row = _fetchone(
        conn, "SELECT count(*) FROM signal_decisions WHERE timestamp::date=%s", (target,)
    )
    total = int(total_row[0]) if total_row else 0

    def _counts(column: str) -> dict[str, int]:
        # column은 아래 세 호출부에서만 오는 고정 리터럴이다(사용자 입력 아님).
        rows = _fetchall(
            conn,
            f"SELECT {column}, count(*) FROM signal_decisions"
            f" WHERE timestamp::date=%s AND {column} IS NOT NULL GROUP BY 1",
            (target,),
        )
        return {str(k): int(n) for k, n in rows}

    vrp_row = _fetchone(
        conn,
        "SELECT count(vrp) FROM signal_decisions WHERE timestamp::date=%s",
        (target,),
    )
    vrp_non_null = int(vrp_row[0]) if vrp_row else 0

    strategy_rows = _fetchall(
        conn,
        "SELECT s, count(*) FROM signal_decisions, "
        "LATERAL jsonb_array_elements_text(risk_gate_state->'entry_strategies') s "
        "WHERE timestamp::date=%s AND decision='ENTER' GROUP BY 1 ORDER BY 2 DESC",
        (target,),
    )
    entry_strategies = {str(s): int(n) for s, n in strategy_rows}

    # 컷오프 불변식 — `decision='ENTER'`인데 시각이 컷오프/평탄화를 넘긴 분.
    cutoff_row = _fetchone(
        conn,
        "SELECT "
        " count(*) FILTER (WHERE decision='ENTER' AND timestamp::time >= %s),"
        " count(*) FILTER (WHERE decision='ENTER' AND timestamp::time >= %s),"
        " count(*) FILTER (WHERE reject_reason=%s)"
        " FROM signal_decisions WHERE timestamp::date=%s",
        (session.NEW_ENTRY_CUTOFF, session.FORCED_FLAT_TIME, ENTRY_CUTOFF_REJECT_REASON, target),
    )
    after_cutoff, after_flat, blocked = (
        (int(cutoff_row[0]), int(cutoff_row[1]), int(cutoff_row[2])) if cutoff_row else (0, 0, 0)
    )

    # 2026-08-06 고도화#2 — 가용 멤버 수와 **실질(비영 점수) 멤버 수**의 차이.
    #
    # 08-06 §14-3이 `regime_hmm` 399분 전량 중립을 드러냈다. 그런데 판단 층은 여전히 4를 세고
    # 있었고, `available_member_count = 4`는 "판단이 네 개 축을 본다"는 뜻으로 읽혔다.
    # **차이 자체가 죽은 축의 수**이므로 그것을 직접 낸다.
    member_row = _fetchone(
        conn,
        "SELECT avg((risk_gate_state->>'available_member_count')::numeric),"
        "       avg((risk_gate_state->>'effective_member_count')::numeric),"
        "       min((risk_gate_state->>'effective_member_count')::int),"
        "       count(*) FILTER ("
        "           WHERE (risk_gate_state->>'effective_member_count')::int"
        "               < (risk_gate_state->>'available_member_count')::int)"
        " FROM signal_decisions"
        " WHERE timestamp::date=%s AND risk_gate_state ? 'effective_member_count'",
        (target,),
    )
    if member_row and member_row[0] is not None:
        available_mean, effective_mean = float(member_row[0]), float(member_row[1])
        member_count = {
            "available": True,
            "available_mean": round(available_mean, 2),
            "effective_mean": round(effective_mean, 2),
            "dead_axis_mean": round(available_mean - effective_mean, 2),
            "effective_min": int(member_row[2]) if member_row[2] is not None else None,
            "minutes_with_dead_axis": int(member_row[3]),
        }
    else:
        # 2026-08-06 이전 판단에는 이 키가 없다 — 0으로 채우면 "전 축이 죽었다"는 거짓 신호가 된다.
        member_count = {"available": False, "reason": "effective_member_count 미기록(2026-08-06 고도화#2 이전)"}

    return {
        "total": total,
        "member_count": member_count,
        "decision": _counts("decision"),
        "conviction": _counts("conviction"),
        "reject_reason": _counts("reject_reason"),
        "vrp": {
            "non_null": vrp_non_null,
            "non_null_ratio": round(vrp_non_null / total, 3) if total else None,
        },
        "entry_strategies": entry_strategies,
        "distinct_entry_strategies": len(entry_strategies),
        # 2026-08-06 Fix#1 — v6 §4.2 진입 컷오프. `blocked`는 *진입할 뻔했는데 막힌* 분이고
        # `enter_after_*`는 *막지 못한* 분이다. 둘을 함께 봐야 "게이트가 걸렸다"와 "진입 후보가
        # 애초에 없었다"가 구분된다.
        "entry_cutoff": {
            "cutoff_time": session.NEW_ENTRY_CUTOFF.strftime("%H:%M"),
            "forced_flat_time": session.FORCED_FLAT_TIME.strftime("%H:%M"),
            "blocked_count": blocked,
            "enter_after_cutoff": after_cutoff,
            "enter_after_forced_flat": after_flat,
        },
    }


# ===== 2026-08-03 §5-1 — "신호 도달률" =====
#
# 07-31 §5-5는 옳은 원칙을 세웠다: *"인프라 지표가 좋아져도 판단 입력은 나빠질 수 있으므로 반드시
# 나란히 읽는다."* 그래서 `먼슬리 분 커버리지`를 만들었고, 08-03에 그 값은 98.8%로 훌륭했다.
# **그런데 같은 날 감마플립 산출률은 0%였다.** 커버리지는 *"데이터가 DB에 있는가"* 만 재고
# *"그 데이터가 신호까지 도달했는가"* 는 재지 않는다 — 한 칸이 비어 있었다.
#
# 아래 임계는 08-03 실측을 기준선으로 잡았다(전부 그날 경고를 냈을 값들이다).
SIGNAL_REACH_WARNINGS = {
    # 2026-08-04(§2-5 / 고도화#2) — 종전 값은 **3이었고 그 근거 주석이 틀렸다**:
    #   "orderflow는 파이프라인 미구현이라 이론상 최대는 3이다"
    # 그런데 `market_raw_1m.ofi`는 08-04에 선물 410분 **전부** 채워져 있었다(404분이 0 아님).
    # `orderflow_ofi_vpin`은 데이터가 없어서가 아니라 `main._build_signal_inputs()`가
    # `ofi=None`으로 하드코딩해서 죽어 있었다(Fix#2). 즉 임계 3은 **죽은 멤버 하나를 분모 안에
    # 숨기고 있었다** — 08-04 리포트가 `2개 / 이론 최대 3개`로 출력한 것이 그 결과다.
    # 이제 구현 여부를 아는 쪽(`fusion.signal_layer`)에서 가져온다. 하드코딩하지 않는다.
    "member_count_max_min": len(IMPLEMENTED_MEMBER_FIELDS),
    # 탐색 범위 밖이라 정상적으로 못 구하는 분이 있으므로 100%를 요구하지 않는다.
    #
    # 2026-08-05(§2-5) 경고: **이 임계는 "낮으면 나쁘다"를 전제하는데 그 전제가 이 북에서는
    # 거짓이다.** 08-04 §2-3이 확정했듯 먼슬리 북은 전 구간에서 GEX 부호가 안 바뀌므로
    # 산출률 0%가 정상이고, 08-05에 4.5%로 "올라온" 22건은 21건이 레그 범위 밖 허수였다.
    # 그래서 이 경고 문구는 더 이상 "행사가 창을 확인하라"고 말하지 않는다 —
    # 판정은 아래 `gamma_flip_out_of_range_count`(불변식, 0이어야 한다)로 한다.
    "gamma_flip_pct_min": 80.0,
    # 2026-08-06(§2-4 / Fix#4) — 종전 값은 `db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES * 60 * 1.5`
    # = 450초(7.5분)였다. 그 파생은 **재는 대상이 전 북일 때**의 것이다: 스냅샷 창(5분)이 허용하는
    # 최대 나이에 여유를 더한 값이라, 창 자체가 상한이므로 사실상 울릴 수 없었다.
    #
    # Fix#4로 이 값이 **먼슬리 북 하나**의 나이가 됐고, 먼슬리는 설계상 **매 분** 폴링된다.
    # 판단은 분의 ~10초 지점에서 돌므로 건강한 나이는 70~130초다(08-06 장전 실측 70.05초).
    # 180초 = "먼슬리가 2사이클 이상 밀렸다" — 그때는 GEX/감마플립의 유일한 입력이 늙은 것이므로
    # 경고가 맞다.
    #
    # **임계는 물리적 상한 아래에 있어야 한다**(2026-08-05 §2-4의 교훈 — Fix#8이 read를 4초로
    # 내렸는데 느린 호출 임계가 5초에 남아 자기를 정당화한 계측을 침묵시켰다). 여기서 물리적
    # 상한은 스냅샷 창 `db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES * 60` = 300초이고 180 < 300이다.
    # 이 부등식은 `tests/test_ops_db_metrics.py`의 불변식 테스트가 기계적으로 지킨다.
    #
    # 2026-08-07(§B-3 / Fix#2) — **값은 그대로 두되, 이 지표가 재던 것이 오늘부터 바뀐다.**
    #
    # 08-06의 위 문단은 임계 180초를 "먼슬리가 2사이클 이상 밀렸다"로 정의했다. 그런데 08-07
    # 실측에서 이 값은 **폴링 지연이 아니라 ATM 롤 잔상**을 재고 있었다:
    #
    #   스냅샷 레그   분(장중)   최고령 평균
    #      10(설계)      45        62초      ← 롤 없이 한 창. 임계 아래
    #      12            88       185초
    #      14            45       191초
    #      16            20       229초
    #      18             3       250초      ← 250초는 12레그 이상 분에서만 난다
    #
    # 즉 250초는 "먼슬리가 밀렸다"가 아니라 "창 밖으로 빠진 행사가가 5분 창에 남아 있다"였고,
    # 그래서 08-06(250.086초)과 08-07(250.139초)이 **창 길이에 붙어 고정**됐다. 임계가 재려던
    # 것과 지표가 재던 것이 달랐다 — 08-06 §2-4가 고친 것과 **같은 계열의 오류가 한 겹 더** 있었다.
    #
    # `db._restrict_to_latest_cycle_window()`(08-07 Fix#1)가 그 잔상을 걷어냈으므로, 이제 이
    # 값은 주석이 처음부터 주장하던 것 — 먼슬리 폴링이 얼마나 늙었는가 — 을 실제로 잰다.
    # 08-07 리플레이 기준 중앙 0초 / 최대 60초이고, 임계 180초는 그 위에 2사이클의 여유를 둔다.
    #
    # **08-06 이전 값과 비교하지 말 것**: 모집단이 바뀌었다(전 북 → 먼슬리는 08-06, 전 행사가
    # → 현재 창은 08-07). 시계열의 단절 지점이 두 곳이다.
    "chain_age_seconds_max": 180.0,
}


def _gamma_flip_out_of_range_count(conn: ConnectionLike, target: date) -> int | None:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 적재된 감마플립 중 **그 판단이 실제로 본 체인의 행사가 범위 밖**인 건수.
         범위는 판단 시각 기준 `db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES` 창 안에서, 그 판단이 쓴
         북(`gex_expiry`)의 행사가 min/max다 — **라이브 판단이 본 것과 같은 창·같은 북**이다.
         허용치도 `find_gamma_flip()`과 **같은 규칙**(행사가 간격 x
         `GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS`)을 쓴다. 두 곳이 다른 경계를 쓰면 이
         불변식은 **정상적으로 통과한 flip을 위반으로 신고한다** — 규약 B(같은 것은 한 곳에서)의
         정신을 SQL 쪽에도 적용한 것이다. SQL에서 간격은 균등 격자를 가정해
         (max-min)/(행사가 수-1)로 낸다(파이썬 쪽 최소 간격과 균등 격자에서 일치한다).
    해석: **불변식이다. 0이 아니면 2026-08-05 Fix#1이 뚫린 것이다.**
         `find_gamma_flip()`이 레그 범위 밖 flip을 기각하므로 정상 상태에서는 0이 나온다.
         08-05 실측(fix 이전)으로는 22건 중 **21건**이 여기 걸린다.
    실패 조건: `gex_expiry`(마이그레이션 023) 부재 등으로 조회가 실패하면 None —
              **0이 아니라 None이다.** 0으로 돌려주면 "검사했고 깨끗했다"와 "검사 못 했다"가
              같은 값이 되고, 그것이 08-04 §2-1이 겪은 실패(계측이 꺼졌는데 개선으로 보임)다.
    """
    try:
        row = _fetchone(
            conn,
            "SELECT count(*) FROM signal_decisions d "
            "JOIN LATERAL ("
            "    SELECT min(o.strike) AS lo, max(o.strike) AS hi, "
            "           count(DISTINCT o.strike) AS n "
            "    FROM option_analysis_1m o "
            "    WHERE o.expiry = d.gex_expiry "
            "      AND o.timestamp <= d.timestamp "
            "      AND o.timestamp > d.timestamp - make_interval(mins => %s)"
            ") w ON TRUE "
            "CROSS JOIN LATERAL ("
            "    SELECT CASE WHEN w.n > 1 THEN (w.hi - w.lo) / (w.n - 1) ELSE 0 END * %s AS tol"
            ") t "
            "WHERE d.timestamp::date = %s AND d.gamma_flip IS NOT NULL "
            "  AND d.gex_expiry IS NOT NULL AND w.lo IS NOT NULL "
            "  AND (d.gamma_flip < w.lo - t.tol OR d.gamma_flip > w.hi + t.tol)",
            (
                db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES,
                GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS,
                target,
            ),
        )
    except Exception:
        conn.rollback()
        logger.warning(
            "감마플립 범위 밖 건수 집계 실패 — 마이그레이션 023(gex_expiry) 적용 전일 수 있다",
            exc_info=True,
        )
        return None
    return int(row[0]) if row else 0


def signal_reach(conn: ConnectionLike, target: date) -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 그날의 판단 행에서 **신호가 실제로 도달했는지**를 센다 — 앙상블 최대 가용 멤버 수,
         감마플립 산출률, 체인 스냅샷의 레그 수/최고령 나이(중앙값·최대).
    해석: 상세 근거는 위 `SIGNAL_REACH_WARNINGS` 주석. `warnings` 키에 담긴 문자열이 비어 있지
         않으면 그날 사람이 반드시 읽어야 할 항목이다.
    실패 조건: 마이그레이션 022 적용 전(컬럼 없음)이면 `{"available": False}`만 돌려준다 —
              0%로 표시해 "오늘 신호가 죽었다"는 거짓 신호를 만들지 않는다.
    """
    try:
        row = _fetchone(
            conn,
            "SELECT count(*), "
            "       max((risk_gate_state->>'available_member_count')::int), "
            "       count(gamma_flip), "
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY chain_leg_count), "
            "       max(chain_leg_count), "
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY chain_oldest_leg_age_seconds), "
            "       max(chain_oldest_leg_age_seconds), "
            # 2026-08-07 고도화#2 — Fix#1(창 자르기)의 회귀 감시. 설계보다 두꺼운 분은
            # 서로 다른 ATM 창을 섞어 GEX를 낸 분이다(08-07 장중 157/202분).
            "       count(*) FILTER (WHERE chain_leg_count > %s), "
            "       max(chain_leg_count - %s) "
            "FROM signal_decisions WHERE timestamp::date=%s",
            (MONTHLY_LEGS_PER_CYCLE_DESIGN, MONTHLY_LEGS_PER_CYCLE_DESIGN, target),
        )
    except Exception:
        conn.rollback()
        logger.warning("신호 도달률 집계 실패 — 마이그레이션 022 적용 전일 수 있다", exc_info=True)
        return {"available": False}

    decisions = int(row[0]) if row else 0
    if not decisions:
        return {"available": False}

    member_max = int(row[1]) if row[1] is not None else 0
    flip_count = int(row[2])
    flip_pct = round(flip_count / decisions * 100, 1)
    out = {
        "available": True,
        "decisions": decisions,
        "member_count_max": member_max,
        "gamma_flip_count": flip_count,
        "gamma_flip_pct": flip_pct,
        "chain_leg_median": float(row[3]) if row[3] is not None else None,
        "chain_leg_max": int(row[4]) if row[4] is not None else None,
        "chain_age_seconds_median": float(row[5]) if row[5] is not None else None,
        "chain_age_seconds_max": float(row[6]) if row[6] is not None else None,
        # 2026-08-07 고도화#2 — **ATM 롤의 잔상**. 08-07 이전에는 이 값이 매일 100분 단위였고
        # 아무도 그걸 못 봤다(레그 수 중앙/최대만 보면 "좀 두껍다"로 읽힌다). Fix#1 이후
        # 0이 정상이며, 0이 아니면 창 자르기가 깨졌거나 한 사이클이 두 분에 걸쳐 적재된 것이다.
        "chain_leg_over_design_minutes": int(row[7]) if row[7] is not None else None,
        "chain_leg_excess_max": max(int(row[8]), 0) if row[8] is not None else None,
    }
    out["gamma_flip_out_of_range_count"] = _gamma_flip_out_of_range_count(conn, target)

    # 2026-08-06(§2-5 / Fix#5) — 표본이 **전부 장전**이면 아래 두 경고는 잴 대상이 아직 없다.
    # 장전에는 스팟이 설계상 없어(`mahdi.session.is_preopen`) `options_flow`가 미가용인 것이
    # 정상이고, 그러면 `member_count_max`도 감마플립 산출률도 구조적으로 낮게 나온다.
    # 그것을 경고로 내면 매일 07:31~09:00에 **설계대로 동작한 것을 장애로 신고**하게 된다
    # (08-06 장전 COCKPIT이 실제로 그랬다). 판정을 유예할 뿐 숨기지 않는다 — note로 남긴다.
    row_open = _fetchone(
        conn,
        "SELECT count(*) FROM signal_decisions WHERE timestamp::date=%s AND timestamp::time >= %s",
        (target, session.TRADING_DAY_START),
    )
    out["decisions_after_open"] = int(row_open[0]) if row_open else 0
    preopen_only = out["decisions_after_open"] == 0

    warnings: list[str] = []
    notes: list[str] = []
    if member_max < SIGNAL_REACH_WARNINGS["member_count_max_min"]:
        message = f"앙상블 최대 가용 멤버 {member_max}개 — options_flow가 한 번도 활성화되지 않았다"
        if preopen_only:
            notes.append(
                f"앙상블 최대 가용 멤버 {member_max}개 — 아직 장전 표본뿐이다"
                "(스팟 미적재로 options_flow가 미가용인 것은 설계된 정상)"
            )
        else:
            warnings.append(message)
    if flip_pct < SIGNAL_REACH_WARNINGS["gamma_flip_pct_min"]:
        message = (
            f"감마플립 산출률 {flip_pct}% — 이 북에서는 0%가 정상일 수 있다"
            "(08-04 §2-3: 전 구간 단조). 판정은 아래 범위 밖 건수로 한다"
        )
        (notes if preopen_only else warnings).append(message)
    out_of_range = out["gamma_flip_out_of_range_count"]
    if out_of_range is None:
        # "검사 못 했다"를 조용히 넘기지 않는다 — 이 침묵이 08-04 §2-1의 본체였다.
        warnings.append("감마플립 범위 밖 검사 불가 — 마이그레이션 023 적용 여부를 확인할 것")
    elif out_of_range:
        warnings.append(
            f"감마플립 {out_of_range}건이 수집 행사가 범위 밖이다 — 2026-08-05 Fix#1이 뚫렸다"
            "(레그 없는 구간의 외삽에서 주운 부호 전환일 수 있다)"
        )
    age_max = out["chain_age_seconds_max"]
    if age_max is not None and age_max > SIGNAL_REACH_WARNINGS["chain_age_seconds_max"]:
        warnings.append(
            f"체인 스냅샷 최고령 레그 {age_max / 60:.0f}분 — 신선도 경계가 깨졌다"
        )
    # 2026-08-07 고도화#2 — Fix#1(창 자르기)의 회귀 감시. **0이 불변식이다.**
    over_design = out["chain_leg_over_design_minutes"]
    if over_design:
        warnings.append(
            f"설계({MONTHLY_LEGS_PER_CYCLE_DESIGN}레그)보다 두꺼운 체인 {over_design}분 "
            f"(최대 +{out['chain_leg_excess_max']}레그) — 서로 다른 ATM 창을 섞어 GEX를 냈다. "
            "08-07 Fix#1(창 자르기)이 뚫렸거나 한 사이클이 두 분에 걸쳐 적재된 것이다"
        )
    out["warnings"] = warnings
    out["notes"] = notes
    return out


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
        "model": _regime_model_provenance(),
    }


def _regime_model_provenance() -> dict | None:
    """
    계산: 배포된 레짐 모델(`regime_pipeline.DEFAULT_MODEL_PATH`)의 학습 출처 메타데이터를 읽는다.
    해석: 2026-08-10 — 이날부터 학습은 `iv_chg`를 **DB가 기록한 값이 아니라 재계산한 값**으로
         쓴다(`feature_store`는 그대로 두고 학습 시점에만 대체). 리포트 §13이 `feature_store`
         누적 행수를 내면서 그 사실을 안 적으면, **읽는 사람은 학습 입력이 그 표와 같다고
         가정한다.** 학습이 쓴 값과 DB가 기록한 값이 다르다는 것은 조용히 넘어가면 안 된다.
         메타데이터가 없으면(08-10 이전 모델) `{}`가 아니라 그 사실을 그대로 낸다 —
         「메타데이터 없음」과 「대체 없었음」은 다른 사실이다.
    실패 조건: 파일이 없거나 못 읽으면 None(모델 미배포 — 리포트가 줄을 생략한다).
    """
    try:
        from mahdi.engines.regime import RegimeEngine
        from mahdi.engines.regime_pipeline import DEFAULT_MODEL_PATH

        engine = RegimeEngine.load(DEFAULT_MODEL_PATH)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("레짐 모델 학습 출처 조회 실패", exc_info=True)
        return None
    return dict(engine.metadata) or {"note": "메타데이터 없음(2026-08-10 이전 모델)"}


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


def _decision_outcomes(conn: ConnectionLike, target: date) -> dict:
    """진입 판단의 방향 적중률 — 상세 근거는 `mahdi/ops/decision_outcomes.py`."""
    from mahdi.ops import decision_outcomes

    return decision_outcomes.summarize(conn, target)


def _ws_status(conn: ConnectionLike, _target: date) -> dict:
    """WS 구독/연결 상태 싱글턴 — `2026-08-03-p4`가 지목한 `market_op_subscribed_at`이 여기 있다.

    2026-08-06 §3-1 / Fix#3에 붙여 만든 절이다. 그 가설은 `db.ws_status.market_op_subscribed_at`을
    예측 지표로 걸었는데 **집계에 `ws_status` 절 자체가 없었다** — 08-05에 사람이 DB를 직접
    조회해 손으로 확정했고, 자동 대조는 그 값을 한 번도 본 적이 없다. 경로를 사람에게 맞춘다.
    """
    row = _fetchone(
        conn,
        "SELECT updated_at, connected_since, last_message_at, reconnect_count_today,"
        " market_op_subscribed_at, atm_roll_count_today, atm_roll_dropped_subs_today"
        " FROM ws_status LIMIT 1",
    )
    if not row:
        return {
            "updated_at": None, "connected_since": None, "last_message_at": None,
            "reconnect_count_today": None, "market_op_subscribed_at": None,
            "atm_roll_count_today": None,
            # 2026-08-07 고도화#2 — 롤의 대가(마이그레이션 028).
            "atm_roll_dropped_subs_today": None,
        }

    def _hms(value):
        return value.strftime("%H:%M:%S") if value else None

    return {
        "updated_at": _hms(row[0]),
        "connected_since": _hms(row[1]),
        "last_message_at": _hms(row[2]),
        "reconnect_count_today": int(row[3]) if row[3] is not None else None,
        "market_op_subscribed_at": _hms(row[4]),
        "atm_roll_count_today": int(row[5]) if row[5] is not None else None,
        # 2026-08-07 고도화#2 — 판정 축은 횟수가 아니라 이쪽이다(마이그레이션 028).
        "atm_roll_dropped_subs_today": int(row[6]) if row[6] is not None else None,
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
