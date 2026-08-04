"""라이브 DB 하루치 → 지표 dict.

2026-08-01(운영점검보고서 2026-07-31 §5-2 Phase 2). 07-31 조사에서 손으로 돌린 SQL 25건을 고정한다.

**COCKPIT 배지(§5-5)와 계산 함수를 공유한다** — 리포트와 배지가 다른 답을 내면 어느 쪽을 믿을지
알 수 없다. `rest_demand()` / `monthly_book_coverage()`가 그 공유 지점이다.
"""

from __future__ import annotations

import logging
from datetime import date

from mahdi.data import db
from mahdi.data.db import ConnectionLike
from mahdi.fusion.signal_layer import IMPLEMENTED_MEMBER_FIELDS, MEMBER_FIELDS

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
        ("book_gamma_map", book_gamma_map),
        ("wide_oi_landscape", wide_oi_landscape),
        ("member_availability", member_availability),
        ("strike_window_quality", strike_window_quality),
        ("signal_decisions", _signal_decisions),
        ("signal_reach", signal_reach),
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
    expiry = monthly_book_expiry(conn, target, underlying)
    if expiry is None:
        return {"expiry": None, "minutes": None, "coverage_pct": None, "reason": "만기유동성 미적재(장전)"}
    if elapsed_minutes is None:
        elapsed_minutes = observed_span_minutes(conn, target, underlying)
    if not elapsed_minutes or elapsed_minutes <= 0:
        return {"expiry": expiry, "minutes": None, "coverage_pct": None, "reason": "관측 구간 없음"}
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
        members.append(
            {
                "member": name,
                "available_minutes": minutes - dead_minutes,
                "available_pct": round((minutes - dead_minutes) / minutes * 100, 1),
                "top_unavailable_reason": top_reason,
                "implemented": name in IMPLEMENTED_MEMBER_FIELDS,
            }
        )
    return {"available": True, "minutes": minutes, "members": members}


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
        return {"available": False, "reason": "만기유동성 미적재(먼슬리 북 식별 불가)"}

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
        return {"available": False, "reason": "먼슬리 체인 미적재"}
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
    design_strikes = side * 2 + 1
    median_strikes = float(jitter[0]) if jitter and jitter[0] is not None else None
    return {
        "available": True,
        "expiry": expiry,
        "minutes": minutes,
        "atm_covered_pct": round(atm_hit / minutes * 100, 1),
        "window_covered_pct": round(window_hit / minutes * 100, 1),
        "atm_offset_strikes_median": round(offset_median, 2) if offset_median is not None else None,
        "atm_offset_strikes_max": round(offset_max, 2) if offset_max is not None else None,
        "design_strikes": design_strikes,
        "snapshot_strikes_median": median_strikes,
        "snapshot_strikes_max": int(jitter[1]) if jitter and jitter[1] is not None else None,
        "width_jitter": round(median_strikes / design_strikes, 2) if median_strikes else None,
        "snapshot_window_minutes": db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES,
    }


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
    "gamma_flip_pct_min": 80.0,
    # db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES에 여유(1.5배)를 더한 값 — 넘으면 경계가 깨진 것이다.
    # 2026-08-04 Fix#6b로 창이 10분 → 5분이 됐으므로 임계도 함께 내려간다(상수에서 파생시켜
    # 두 값이 갈라지지 않게 한다 — 08-04 §2-1이 하드코딩된 임계 문자열로 겪은 문제와 같은 종류).
    "chain_age_seconds_max": db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES * 60 * 1.5,
}


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
            "       max(chain_oldest_leg_age_seconds) "
            "FROM signal_decisions WHERE timestamp::date=%s",
            (target,),
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
    }

    warnings: list[str] = []
    if member_max < SIGNAL_REACH_WARNINGS["member_count_max_min"]:
        warnings.append(
            f"앙상블 최대 가용 멤버 {member_max}개 — options_flow가 한 번도 활성화되지 않았다"
        )
    if flip_pct < SIGNAL_REACH_WARNINGS["gamma_flip_pct_min"]:
        warnings.append(f"감마플립 산출률 {flip_pct}% — 행사가 창이 스팟을 따라가고 있는지 확인")
    age_max = out["chain_age_seconds_max"]
    if age_max is not None and age_max > SIGNAL_REACH_WARNINGS["chain_age_seconds_max"]:
        warnings.append(
            f"체인 스냅샷 최고령 레그 {age_max / 60:.0f}분 — 신선도 경계가 깨졌다"
        )
    out["warnings"] = warnings
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
