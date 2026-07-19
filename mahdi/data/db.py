"""TimescaleDB 커넥션·삽입 헬퍼 — db/migrations/001_init.sql 스키마와 대응.

실시간 수집과 백테스트 재처리가 같은 삽입 경로를 쓰도록, INSERT는 모두 PK 충돌 시
갱신(ON CONFLICT DO UPDATE)해 재처리에도 멱등성을 보장한다.

**타임스탬프 정책(2026-07-19 명문화, §5-3)**: DB에 쓰이는 모든 시각은 반드시 이 모듈의
local_now()를 거쳐서 만들 것 — 자세한 내용은 그 함수의 docstring과
db/migrations/008_timestamp_policy_docs.sql 참고.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Protocol

import psycopg

from mahdi.config.settings import DBSettings, get_db_settings


def local_now() -> datetime:
    """
    이 프로젝트가 DB에 쓰는 모든 시각의 유일한 생성 지점 — 다른 곳에서 datetime.now()를
    직접 호출하지 말고 반드시 이 함수를 쓸 것(2026-07-19, 운영점검보고서 §3-4/§5-3 "타임스탬프
    정책 명문화"로 도입 — 동작은 datetime.now()와 완전히 동일하다, 정책 변경이 아니라 기존
    동작을 한 곳에 모아 문서화한 것).

    반환값은 naive(타임존 정보 없는) 서버 로컬 벽시계 시각(KST)이다. DB의 모든 timestamp
    컬럼은 TIMESTAMPTZ로 선언돼 있지만, naive datetime을 그대로 psycopg에 넘기면 Postgres가
    "세션 타임존"(이 프로젝트는 명시 설정이 없어 기본값 UTC — docker-compose.yml에 TZ 미설정)
    기준으로 해석해 저장한다. 즉 실제로는 KST 벽시계 시각인데 "UTC"라고 라벨링된 값이 저장된다
    — 2026-07-16 점검에서 14:20(KST)에 조회한 market_raw_1m.timestamp가
    "2026-07-16 14:20:00+00"으로 나온 것으로 확인(진짜 UTC라면 05:20이어야 함).

    애플리케이션 코드 전체가 이 규약을 일관되게 쓰는 한(비교·차집합 등 모든 시간 연산이 같은
    "가짜 UTC" 좌표계 안에서만 일어나는 한) self-consistent하고, 09:00~15:45 장중 판단 로직도
    전부 KST 벽시계 기준으로 정확히 동작한다 — 지금 당장 고장난 동작은 없다.

    잠재 위험(그대로 유효, 해결된 게 아니라 "문서화"만 한 상태):
    ① 해외선물(VIX/CNH/ZN, 미국·홍콩 거래시간 기준) 데이터와 시각을 교차분석하면 9시간 오차가
       실제 시차처럼 섞여 혼란을 준다.
    ② `CURRENT_DATE`/`NOW()` 같은 Postgres 서버 함수를 쓰는 쿼리는 진짜 UTC로 동작하므로, 이
       함수가 반환한 값과 섞어 쓰면(특히 00:00~09:00 KST 구간 — 진짜 UTC로는 전날 15:00~24:00)
       날짜 경계가 어긋난다.

    이 규약 자체를 바꾸려면(진짜 tz-aware로 전환, 또는 컬럼 타입을 TIMESTAMP로 바꿔 스키마가
    최소한 "거짓말"은 안 하게 하는 것) 이미 쌓인 과거 데이터의 보정이나 하이퍼테이블 파티션
    컬럼 타입 변경이 필요한 별도 마이그레이션 작업이다 — 사용자 확인 후 2026-07-19에 "지금은
    문서화만 하고 스키마/데이터는 건드리지 않는다"로 결정함([[SESSION_LOG]] 참고).
    """
    return datetime.now()


_MARKET_RAW_1M_COLUMNS = (
    "timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap",
    "vpin", "ofi", "microprice", "bid_ask_spread", "buy_volume", "sell_volume",
    "usdkrw", "quality_flag",
)

_MACRO_SNAPSHOT_5M_COLUMNS = (
    "timestamp", "vix_front", "vix_next", "vix_term_structure", "usdcnh", "us10y_yield", "zn_front", "quality_flag",
)

_OPTION_ANALYSIS_1M_COLUMNS = (
    "timestamp", "underlying", "expiry", "strike", "option_type",
    "delta", "gamma", "theta", "vega", "vanna", "charm",
    "iv", "rv_5d", "vrp", "skew_25d", "gex", "oi", "oi_change",
    "volume", "spread_state",
)


class CursorLike(Protocol):
    def execute(self, query: str, params: Any = None) -> Any: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...
    def commit(self) -> None: ...


@contextmanager
def get_connection(settings: DBSettings | None = None) -> Iterator[psycopg.Connection]:
    settings = settings or get_db_settings()
    conn = psycopg.connect(settings.dsn)
    try:
        yield conn
    finally:
        conn.close()


def _upsert(conn: ConnectionLike, table: str, columns: tuple[str, ...], conflict_keys: tuple[str, ...], row: dict) -> None:
    values = [row.get(c) for c in columns]
    placeholders = ", ".join(["%s"] * len(columns))
    update_cols = [c for c in columns if c not in conflict_keys]
    update_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
    query = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_keys)}) DO UPDATE SET {update_clause}"
    )
    with conn.cursor() as cur:
        cur.execute(query, values)
    conn.commit()


def insert_market_raw_1m(conn: ConnectionLike, row: dict) -> None:
    """
    입력: market_raw_1m 컬럼과 동일한 키를 가진 dict (예: MinuteBarAggregator 출력 + symbol).
    계산: INSERT ... ON CONFLICT (timestamp, symbol) DO UPDATE — 재처리에도 멱등.
    실패 조건: 필수 컬럼이 dict에 없으면 해당 값은 NULL로 삽입된다(스키마의 NOT NULL 제약이
              없는 컬럼에 한함) — 상위 레이어가 필수 필드를 채워야 한다.
    """
    _upsert(conn, "market_raw_1m", _MARKET_RAW_1M_COLUMNS, ("timestamp", "symbol"), row)


def insert_option_analysis_1m(conn: ConnectionLike, row: dict) -> None:
    """
    입력: option_analysis_1m 컬럼과 동일한 키를 가진 dict — KIS get_quote() 응답(그릭스/IV/OI)을
         REST 폴링 루프가 파싱한 결과 1레그(행사가+콜/풋 1건).
    계산: INSERT ... ON CONFLICT (timestamp, underlying, expiry, strike, option_type) DO UPDATE.
    """
    _upsert(
        conn, "option_analysis_1m", _OPTION_ANALYSIS_1M_COLUMNS,
        ("timestamp", "underlying", "expiry", "strike", "option_type"), row,
    )


def insert_macro_snapshot_5m(conn: ConnectionLike, row: dict) -> None:
    """
    입력: macro_snapshot_5m 컬럼과 동일한 키를 가진 dict(Cross-asset stress 원시값, v6 §7.3) —
         vix_front/vix_next(CBOE VX 선물 근월·차근월 현재가), vix_term_structure(vix_next/
         vix_front - 1, 양수면 콘탱고), usdcnh(HKEx CNH 선물 현재가), us10y_yield(해외주식
         국채구분 일봉 API — 실제 수익률(%) 레벨, 대부분의 5분 행에서 None일 수 있음),
         zn_front(2026-07-10 CBOT 거래소 신청 완료 후 추가 — CME 10년 국채선물 근월물 현재가,
         5분마다 갱신되는 "급변" 감지용. 가격은 수익률과 역상관이므로 us10y_yield와 단위가 다름).
    계산: INSERT ... ON CONFLICT (timestamp) DO UPDATE — 재처리에도 멱등.
    """
    _upsert(conn, "macro_snapshot_5m", _MACRO_SNAPSHOT_5M_COLUMNS, ("timestamp",), row)


def latest_macro_snapshot(conn: ConnectionLike) -> dict | None:
    """
    계산: us10y_yield는 하루 대부분 NULL이라(위 insert_macro_snapshot_5m 설명 참고), 최신 행에
         값이 없으면 값이 채워진 마지막 행에서 하나 더 가져와 LOCF(forward-fill)한다. zn_front는
         CBOT 신청 완료 후 5분마다 갱신되므로 별도 폴백이 필요 없다.
    해석: 대시보드/레짐 피처가 "지금 시점의 매크로 상태"를 한 번에 조회할 수 있게 한다.
    실패 조건: 폴링이 한 번도 안 돌았으면 None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, vix_front, vix_next, vix_term_structure, usdcnh, us10y_yield, zn_front "
            "FROM macro_snapshot_5m ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    timestamp, vix_front, vix_next, vix_term_structure, usdcnh, us10y_yield, zn_front = row
    if us10y_yield is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT us10y_yield FROM macro_snapshot_5m "
                "WHERE us10y_yield IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
            )
            fallback = cur.fetchone()
        us10y_yield = fallback[0] if fallback else None
    return {
        "timestamp": timestamp,
        "vix_front": float(vix_front) if vix_front is not None else None,
        "vix_next": float(vix_next) if vix_next is not None else None,
        "vix_term_structure": float(vix_term_structure) if vix_term_structure is not None else None,
        "usdcnh": float(usdcnh) if usdcnh is not None else None,
        "us10y_yield": float(us10y_yield) if us10y_yield is not None else None,
        "zn_front": float(zn_front) if zn_front is not None else None,
    }


def insert_underlying_spot(conn: ConnectionLike, timestamp: datetime, underlying: str, spot: float) -> None:
    """입력: 기초자산(지수) 현재가 — REST 응답 output3(지수 자체)에서 추출, 어느 옵션을 조회해도 동일한 값."""
    row = {"timestamp": timestamp, "underlying": underlying, "spot": spot}
    _upsert(conn, "underlying_spot_1m", ("timestamp", "underlying", "spot"), ("timestamp", "underlying"), row)


def latest_underlying_spot(conn: ConnectionLike, underlying: str) -> float | None:
    """가장 최근 기초자산 스팟 1건. 폴링 루프가 아직 한 번도 못 돌았으면 None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT spot FROM underlying_spot_1m WHERE underlying=%s ORDER BY timestamp DESC LIMIT 1",
            (underlying,),
        )
        row = cur.fetchone()
    return float(row[0]) if row else None


def insert_investor_flow(
    conn: ConnectionLike,
    timestamp: datetime,
    underlying: str,
    foreign_net: float,
    institution_net: float,
    individual_net: float,
) -> None:
    """입력: KOSPI200 파생상품시장(선물+콜옵션+풋옵션 합산) 투자자별 순매수 거래대금 — 세션 누적치 스냅샷."""
    row = {
        "timestamp": timestamp,
        "underlying": underlying,
        "foreign_net": foreign_net,
        "institution_net": institution_net,
        "individual_net": individual_net,
    }
    _upsert(
        conn, "investor_flow_1m",
        ("timestamp", "underlying", "foreign_net", "institution_net", "individual_net"),
        ("timestamp", "underlying"), row,
    )


def latest_investor_flow(conn: ConnectionLike, underlying: str) -> tuple[float, float, float] | None:
    """가장 최근 투자자별 순매수(외국인, 기관계, 개인) 1건. 폴링 루프가 아직 안 돌았으면 None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT foreign_net, institution_net, individual_net FROM investor_flow_1m "
            "WHERE underlying=%s ORDER BY timestamp DESC LIMIT 1",
            (underlying,),
        )
        row = cur.fetchone()
    return (float(row[0]), float(row[1]), float(row[2])) if row else None


def upsert_active_futures_symbol(conn: ConnectionLike, underlying: str, symbol: str, updated_at: datetime) -> None:
    """
    입력: 현재 구독 중인 선물 단축코드(분기마다 바뀜).
    계산: INSERT ... ON CONFLICT (underlying) DO UPDATE — underlying당 현재값 1개만 유지.
    해석: 대시보드가 "이 종목이 선물인지 옵션인지"를 vpin 유무 같은 휴리스틱으로 추측하지 않고
         바로 조회할 수 있게 한다.
    """
    row = {"underlying": underlying, "symbol": symbol, "updated_at": updated_at}
    _upsert(conn, "active_futures_symbol", ("underlying", "symbol", "updated_at"), ("underlying",), row)


def is_slack_alerts_enabled(conn: ConnectionLike) -> bool:
    """
    입력: DB 커넥션.
    계산: slack_alert_settings(싱글턴 테이블, 2026-07-19 §5-4)의 enabled 값을 반환한다. COCKPIT
         (Streamlit)과 mahdi.main(관측 루프)은 서로 다른 프로세스라 메모리 전역변수로 On/Off를
         공유할 수 없다 — 이 함수가 양쪽이 항상 같은 값을 보게 하는 단일 진실 공급원(SSOT)이다.
    실패 조건: 아무도 토글한 적이 없어 행이 없으면(최초 기동)
              mahdi.config.settings.get_slack_settings().slack_alerts_enabled_default로 폴백한다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT enabled FROM slack_alert_settings LIMIT 1")
        row = cur.fetchone()
    if row is None:
        from mahdi.config.settings import get_slack_settings

        return get_slack_settings().slack_alerts_enabled_default
    return bool(row[0])


def set_slack_alerts_enabled(conn: ConnectionLike, enabled: bool) -> None:
    """
    입력: DB 커넥션, 새 On/Off 값(COCKPIT 체크박스 토글).
    계산: 싱글턴 행(id=TRUE 고정) upsert — mahdi.main의 notify()가 다음 알림 시도부터 바로
         반영해서 보므로 재시작이 필요 없다.
    """
    row = {"id": True, "enabled": enabled, "updated_at": local_now()}
    _upsert(conn, "slack_alert_settings", ("id", "enabled", "updated_at"), ("id",), row)


_EXPIRY_LIQUIDITY_1M_COLUMNS = (
    "timestamp", "underlying", "series", "expiry",
    "atm_spread_pct", "depth", "volume", "days_to_expiry",
)


def insert_expiry_liquidity_1m(conn: ConnectionLike, row: dict) -> None:
    """
    입력: 만기북(series="regular"|"weekly_mon"|"weekly_thu", 2026-07-10 위클리 분리)별 ATM±2
         구간 유동성 스냅샷 — % 호가스프레드(Cao-Wei 기준, 달러 스프레드 아님)·호가잔량 합(깊이)·
         누적거래량·잔존일수.
    계산: INSERT ... ON CONFLICT (timestamp, underlying, series, expiry) DO UPDATE — 장전 선발
         점수의 20거래일 기준선(전일 중앙값) 산출에 쓰인다(docs/Dev_md/RESEARCH_EXPIRY_SELECTION_v1.md).
    """
    _upsert(
        conn, "expiry_liquidity_1m", _EXPIRY_LIQUIDITY_1M_COLUMNS,
        ("timestamp", "underlying", "series", "expiry"), row,
    )


def get_active_futures_symbol(conn: ConnectionLike, underlying: str) -> str | None:
    """현재 구독 중인 선물 단축코드. 관측 루프가 아직 한 번도 안 돌았으면 None."""
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM active_futures_symbol WHERE underlying=%s", (underlying,))
        row = cur.fetchone()
    return row[0] if row else None


def latest_option_chain(conn: ConnectionLike, underlying: str) -> list[dict]:
    """
    계산: (strike, option_type) 레그별로 가장 최근 timestamp 1건씩만 골라 체인 스냅샷을 구성한다
         (폴링 주기 중 레그마다 조회 시각이 조금씩 어긋날 수 있어 레그별 최신값을 취함).
    해석: 반환된 dict는 mahdi.features.options_intel.OptionLeg 생성에 바로 쓸 수 있는 키를 가진다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (strike, option_type)
                strike, option_type, oi, iv, gamma, gex, expiry, timestamp
            FROM option_analysis_1m
            WHERE underlying=%s
            ORDER BY strike, option_type, timestamp DESC
            """,
            (underlying,),
        )
        rows = cur.fetchall()
    return [
        {
            "strike": float(strike),
            "option_type": option_type,
            "oi": float(oi) if oi is not None else 0.0,
            "iv": float(iv) if iv is not None else 0.0,
            "gamma": float(gamma) if gamma is not None else 0.0,
            "gex": float(gex) if gex is not None else 0.0,
            "expiry": expiry,
            "timestamp": timestamp,
        }
        for strike, option_type, oi, iv, gamma, gex, expiry, timestamp in rows
    ]


# 현재 코드가 실제로 기록하는 series 값만 조회한다 — 과거 버전이 쓰던 이름(예: 2026-07-10
# 위클리 월/목 분리 이전의 병합 라벨 "weekly")이 남아있으면 그 뒤로 아무도 안 써도 DISTINCT ON
# (series)에 계속 잡혀 COCKPIT에 화석 행으로 영원히 남는다(Flow Radar의 _LEGACY_MIXED_SYMBOL과
# 같은 패턴 — mahdi/dashboard/data_source.py 참고). 새 series를 추가하면 이 튜플도 함께 갱신할 것.
_VALID_EXPIRY_LIQUIDITY_SERIES = ("regular", "weekly_mon", "weekly_thu")


def latest_expiry_liquidity(conn: ConnectionLike, underlying: str) -> list[dict]:
    """
    입력: 기초자산 라벨.
    계산: series(regular/weekly_mon/weekly_thu, _VALID_EXPIRY_LIQUIDITY_SERIES로 고정)별로 가장
         최근 timestamp 1건씩만 골라 반환한다 — 폴링 주기(5분) 중 북마다 조회 시각이 조금씩
         어긋날 수 있어 북별 최신값을 취한다. 더 이상 코드가 쓰지 않는 옛 series 값(화석 데이터)은
         과거에 몇 건이 쌓여 있든 결과에서 제외한다.
    해석: 반환된 dict는 COCKPIT 만기 유동성 비교 패널(Phase 1.5-④)이 바로 렌더링에 쓸 수 있는
         키를 가진다. 아직 폴링이 한 번도 안 돌았으면 빈 리스트.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (series)
                series, expiry, atm_spread_pct, depth, volume, days_to_expiry
            FROM expiry_liquidity_1m
            WHERE underlying=%s AND series = ANY(%s)
            ORDER BY series, timestamp DESC
            """,
            (underlying, list(_VALID_EXPIRY_LIQUIDITY_SERIES)),
        )
        rows = cur.fetchall()
    return [
        {
            "series": series,
            "expiry": expiry,
            "atm_spread_pct": float(atm_spread_pct) if atm_spread_pct is not None else None,
            "depth": float(depth) if depth is not None else None,
            "volume": float(volume) if volume is not None else None,
            "days_to_expiry": int(days_to_expiry) if days_to_expiry is not None else None,
        }
        for series, expiry, atm_spread_pct, depth, volume, days_to_expiry in rows
    ]


def insert_feature_store(conn: ConnectionLike, timestamp: datetime, symbol: str, features: dict, feature_version: str) -> None:
    """
    입력: 타임스탬프, 종목코드, 피처 사전 결과(dict), 피처 버전 태그.
    계산: JSONB로 직렬화해 feature_store에 upsert.
    """
    row = {
        "timestamp": timestamp,
        "symbol": symbol,
        "features": json.dumps(features),
        "feature_version": feature_version,
    }
    _upsert(conn, "feature_store", ("timestamp", "symbol", "features", "feature_version"), ("timestamp", "symbol"), row)


def get_feature_history(conn: ConnectionLike, symbol: str, feature_version: str) -> list[tuple[datetime, dict]]:
    """
    입력: 심볼, 피처 버전 태그.
    계산: feature_store에서 해당 심볼·버전의 전체 이력을 시간순으로 반환한다 — 오프라인
         fit 배치(scripts/fit_regime_engine.py)가 RegimeEngine.fit() 입력 ndarray를 구성할 때 사용.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, features FROM feature_store "
            "WHERE symbol=%s AND feature_version=%s ORDER BY timestamp ASC",
            (symbol, feature_version),
        )
        rows = cur.fetchall()
    return [(ts, features if isinstance(features, dict) else json.loads(features)) for ts, features in rows]


def latest_regime_before(conn: ConnectionLike, before: datetime) -> int | None:
    """
    입력: 기준 시각(보통 오늘 자정) — 이 시각 이전(전일까지)의 마지막 레짐을 찾는다.
    계산: SELECT ... WHERE timestamp < before ORDER BY timestamp DESC LIMIT 1.
    해석: 실거래 파이프라인의 워밍업 폴백(warmup_fallback)이 하드코딩된 prior_close_regime
         대신 실제 전일 마감 레짐을 쓸 수 있게 한다.
    실패 조건: 이전 기록이 없으면(첫 실행일) None — 호출측이 기본 레짐으로 폴백해야 함.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT regime FROM regime_state WHERE timestamp < %s ORDER BY timestamp DESC LIMIT 1",
            (before,),
        )
        row = cur.fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def daily_closes(conn: ConnectionLike, symbol: str, days: int) -> list[float]:
    """
    입력: 선물 심볼, 조회할 최근 거래일 수(넉넉히, 예: 30 — rv_ratio가 21개를 요구).
    계산: market_raw_1m을 날짜별로 묶어 각 날짜의 마지막 체결가(종가)를 뽑는다.
    해석: mahdi.features.regime_features.rv_ratio 입력 — 시간순(오래된 순)으로 반환한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (timestamp::date) timestamp::date AS d, close
            FROM market_raw_1m
            WHERE symbol=%s
            ORDER BY d DESC, timestamp DESC
            LIMIT %s
            """,
            (symbol, days),
        )
        rows = cur.fetchall()
    return [float(close) for _, close in reversed(rows)]


def insert_regime_state(
    conn: ConnectionLike,
    timestamp: datetime,
    regime: int,
    prob_vector: list[float],
    higher_tf_regime: int | None,
    stability_flag: bool,
) -> None:
    """입력: RegimeEngine.predict() 결과를 그대로 매핑."""
    row = {
        "timestamp": timestamp,
        "regime": regime,
        "prob_vector": prob_vector,
        "higher_tf_regime": higher_tf_regime,
        "stability_flag": stability_flag,
    }
    _upsert(
        conn,
        "regime_state",
        ("timestamp", "regime", "prob_vector", "higher_tf_regime", "stability_flag"),
        ("timestamp",),
        row,
    )
