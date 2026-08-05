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
from datetime import date, datetime, timedelta
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
    "timestamp", "vix_front", "vix_next", "vix_term_structure", "usdcnh", "us10y_yield", "usdkrw",
    "zn_front", "zn_front_source", "es_front", "es_front_source", "move_index", "move_index_source",
    "quality_flag",
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
    입력: macro_snapshot_5m 컬럼과 동일한 키를 가진 dict(Cross-asset stress 원시값, v6 §7.3/§8) —
         vix_front/vix_next(CBOE VX 선물 근월·차근월 현재가), vix_term_structure(vix_next/
         vix_front - 1, 양수면 콘탱고), usdcnh(HKEx CNH 선물 현재가), us10y_yield/usdkrw(해외주식
         종목_지수_환율기간별시세 일봉 API — 국채구분 I / 환율구분 X, 둘 다 대부분의 5분 행에서
         None일 수 있음, 계좌 게이트 없이 무료), zn_front/es_front(CME 10년 국채선물·E-mini
         S&P500 선물 근월물 현재가, 5분마다 갱신되는 "급변" 감지용 — 가격은 수익률과 역상관이므로
         us10y_yield와 단위가 다름), zn_front_source/es_front_source/move_index_source(2026-07-20
         추가 — 각각 "kis"|"yfinance_fallback"|None. CME 계열 실시간시세가 KIS 유료 항목(월
         228.8불)이라 모의투자 개발 단계에서는 미구독 상태이고, 값이 mahdi/data/
         yfinance_fallback.py의 폴백값일 수 있다 — 이 필드로 실제 출처를 구분한다),
         move_index(ICE BofA MOVE Index — 장외 파생 인덱스라 KIS 경로 자체가 없어 항상
         yfinance_fallback에서만 옴).
    계산: INSERT ... ON CONFLICT (timestamp) DO UPDATE — 재처리에도 멱등.
    """
    _upsert(conn, "macro_snapshot_5m", _MACRO_SNAPSHOT_5M_COLUMNS, ("timestamp",), row)


def macro_snapshot_columns() -> tuple[str, ...]:
    """
    해석: insert_macro_snapshot_5m()이 실제로 쓰는 컬럼 목록을 그대로 노출한다 — COCKPIT 스키마
         정합성 헬스체크(2026-07-21)가 이 목록을 information_schema.columns와 대조해 라이브 DB에
         db/migrations/*.sql이 전부 적용됐는지 판단한다. 목록을 따로 하드코딩하면 두 곳이 어긋날
         수 있어 단일 소스(_MACRO_SNAPSHOT_5M_COLUMNS)를 그대로 반환한다.
    """
    return _MACRO_SNAPSHOT_5M_COLUMNS


def latest_macro_snapshot(conn: ConnectionLike) -> dict | None:
    """
    계산: 최신 행에 값이 없는 컬럼은 값이 채워진 마지막 행에서 하나 더 가져와 LOCF(forward-fill)한다.
    해석: 대시보드/레짐 피처가 "지금 시점의 매크로 상태"를 한 번에 조회할 수 있게 한다.
         *_source 필드도 함께 반환해 COCKPIT이 "kis"(실제 체결가)와 "yfinance_fallback"(근사치,
         mahdi/data/yfinance_fallback.py 참고)를 구분해 보여줄 수 있게 한다.
         **LOCF 대상(2026-07-31 확대)**: 원래는 일봉 전용이라 하루 대부분 NULL인 us10y_yield/usdkrw
         둘뿐이었다. 같은 날 매크로 항목별 갱신 주기를 분리하면서(main.py `MACRO_ITEM_REFRESH_SECONDS`
         — ZN 1시간 / MOVE·일봉 6시간) zn_front·move_index도 대부분의 5분 행에서 NULL이 됐으므로
         함께 LOCF한다. 그러지 않으면 COCKPIT 매크로 패널이 조회 주기 사이에 빈칸으로 보인다.
         값과 짝인 `*_source`는 **반드시 같은 행에서** 가져와야 한다 — 값만 이월하고 출처를 놓치면
         "yfinance 폴백인데 출처 미상"으로 표시돼 CBOT 승인 여부 판정이 어긋난다.
         es_front는 여전히 매 사이클 갱신되므로(ES는 macro_score의 실제 입력) LOCF 대상이 아니다.
    실패 조건: 폴링이 한 번도 안 돌았으면 None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, vix_front, vix_next, vix_term_structure, usdcnh, us10y_yield, usdkrw, "
            "zn_front, zn_front_source, es_front, es_front_source, move_index, move_index_source "
            "FROM macro_snapshot_5m ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    (
        timestamp, vix_front, vix_next, vix_term_structure, usdcnh, us10y_yield, usdkrw,
        zn_front, zn_front_source, es_front, es_front_source, move_index, move_index_source,
    ) = row

    def _locf(columns: tuple[str, ...]) -> tuple:
        """columns를 한 묶음으로(같은 행에서) 최근 non-null 값을 가져온다 — 값+출처 짝 유지용."""
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(columns)} FROM macro_snapshot_5m "
                f"WHERE {columns[0]} IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
            )
            fallback = cur.fetchone()
        return fallback if fallback else (None,) * len(columns)

    if us10y_yield is None:
        (us10y_yield,) = _locf(("us10y_yield",))
    if usdkrw is None:
        (usdkrw,) = _locf(("usdkrw",))
    if zn_front is None:
        zn_front, zn_front_source = _locf(("zn_front", "zn_front_source"))
    if move_index is None:
        move_index, move_index_source = _locf(("move_index", "move_index_source"))
    return {
        "timestamp": timestamp,
        "vix_front": float(vix_front) if vix_front is not None else None,
        "vix_next": float(vix_next) if vix_next is not None else None,
        "vix_term_structure": float(vix_term_structure) if vix_term_structure is not None else None,
        "usdcnh": float(usdcnh) if usdcnh is not None else None,
        "us10y_yield": float(us10y_yield) if us10y_yield is not None else None,
        "usdkrw": float(usdkrw) if usdkrw is not None else None,
        "zn_front": float(zn_front) if zn_front is not None else None,
        "zn_front_source": zn_front_source,
        "es_front": float(es_front) if es_front is not None else None,
        "es_front_source": es_front_source,
        "move_index": float(move_index) if move_index is not None else None,
        "move_index_source": move_index_source,
    }


def recent_usdkrw_daily_series(conn: ConnectionLike, days: int) -> list[float]:
    """
    입력: 조회할 최근 거래일 수(예: 10).
    계산: macro_snapshot_5m을 날짜별로 묶어 각 날짜의 마지막 usdkrw 값을 뽑는다(daily_closes와
         동일 패턴) — USDKRW는 KIS 해외주식 일봉 API로만 얻어 거래일당 값이 하나뿐이므로, 5분
         스냅샷이 아니라 거래일 단위 이력이 "급변"을 측정하는 실질적인 단위다.
    해석: mahdi.features.regime_features.cross_asset_stress의 usdkrw_daily_series 입력 —
         시간순(오래된 순)으로 반환한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (timestamp::date) timestamp::date AS d, usdkrw
            FROM macro_snapshot_5m
            WHERE usdkrw IS NOT NULL
            ORDER BY d DESC, timestamp DESC
            LIMIT %s
            """,
            (days,),
        )
        rows = cur.fetchall()
    return [float(usdkrw) for _, usdkrw in reversed(rows)]


def recent_us10y_daily_series(conn: ConnectionLike, days: int) -> list[float]:
    """US10Y_yield 버전 — recent_usdkrw_daily_series와 동일 패턴(US10Y도 일봉 전용)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (timestamp::date) timestamp::date AS d, us10y_yield
            FROM macro_snapshot_5m
            WHERE us10y_yield IS NOT NULL
            ORDER BY d DESC, timestamp DESC
            LIMIT %s
            """,
            (days,),
        )
        rows = cur.fetchall()
    return [float(us10y_yield) for _, us10y_yield in reversed(rows)]


def recent_usdcnh_series(conn: ConnectionLike, limit: int) -> list[float]:
    """
    입력: 조회할 최근 5분 스냅샷 행 수(예: 24 — 2시간).
    계산: macro_snapshot_5m에서 usdcnh가 non-null인 최근 limit개 행을 시간순(오래된 순)으로
         반환한다 — usdcnh는 5분마다 실제로 갱신되는 선물가라(계좌 게이트 없음) us10y_yield/
         usdkrw와 달리 장중 인트라데이 변동을 그대로 반영한다.
    해석: mahdi.features.regime_features.cross_asset_stress의 usdcnh_recent_series 입력.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT usdcnh FROM macro_snapshot_5m WHERE usdcnh IS NOT NULL ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [float(usdcnh) for (usdcnh,) in reversed(rows)]


def recent_es_front_series(conn: ConnectionLike, limit: int) -> list[float]:
    """
    입력: 조회할 최근 5분 스냅샷 행 수.
    계산: macro_snapshot_5m에서 es_front가 non-null인 최근 limit개 행을 시간순(오래된 순)으로
         반환한다 — recent_usdcnh_series와 동일 패턴. es_front는 KIS(구독 시)든 yfinance
         폴백이든 5분마다 갱신되므로 출처와 무관하게 같은 방식으로 추세를 볼 수 있다.
    해석: mahdi.engines.regime_pipeline.compute_macro_score_proxy의 S&P500 선물 추세 신호 입력.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT es_front FROM macro_snapshot_5m WHERE es_front IS NOT NULL ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [float(es_front) for (es_front,) in reversed(rows)]


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


# 2026-08-04(운영점검보고서 §2-5 / Fix#2) — 미시구조 값의 신선도 경계.
# 체인 스냅샷과 같은 원칙을 쓴다: 오래된 값을 조용히 들고 오면 "지금 시장"이 아닌 것으로 판단한다.
# 선물 1분봉은 매분 완성되므로 3분이면 결손 2분을 견디고도 충분하다(08-04 market_raw_1m 996행 /
# 410분, 선물 A01609만 410/410분).
MICROSTRUCTURE_MAX_AGE_MINUTES = 3


def latest_market_microstructure(
    conn: ConnectionLike, symbol: str, as_of: datetime | None = None
) -> dict | None:
    """
    입력: DB 커넥션, 종목코드(선물 단축코드), (선택) 기준 시각 — 없으면 `local_now()`.
    계산: `MICROSTRUCTURE_MAX_AGE_MINUTES`분 이내에 완성된 그 종목의 1분봉에서 주문흐름 지표
         (`ofi`/`microprice`/`bid_ask_spread`/`vpin`)를 꺼낸다.
    해석: 2026-08-04 §2-5 — `_build_signal_inputs()`가 `ofi=None`으로 **하드코딩**돼 있었고
         docstring은 *"아직 라이브 집계 파이프라인이 없어(체결 틱 기반 실시간 호가 집계 미구현)"*
         라고 적혀 있었다. **사실이 아니었다**: `MinuteBarAggregator`가 이 값들을 이미 매분
         계산해 `market_raw_1m`에 적재하고 있고, 08-04 실측으로 선물 A01609의 410분 전부
         `ofi`가 non-null(그중 404분이 0이 아님)이었다. 즉 앙상블 멤버 `orderflow_ofi_vpin`은
         데이터가 없어서가 아니라 **읽어서 넘기지 않아서** 하루 종일 죽어 있었다.
    실패 조건: 신선도 창 안에 봉이 없으면 None — 호출측이 그 멤버만 비운다(다른 폴러의
              "부분 실패 허용"과 같은 원칙). 오래된 값을 대신 돌려주지 않는다.
    """
    now = as_of or local_now()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ofi, microprice, bid_ask_spread, vpin, timestamp FROM market_raw_1m "
            "WHERE symbol=%s AND timestamp <= %s AND timestamp >= %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol, now, now - timedelta(minutes=MICROSTRUCTURE_MAX_AGE_MINUTES)),
        )
        row = cur.fetchone()
    if row is None:
        return None
    ofi, microprice, spread, vpin, timestamp = row
    return {
        "ofi": float(ofi) if ofi is not None else None,
        "microprice": float(microprice) if microprice is not None else None,
        "bid_ask_spread": float(spread) if spread is not None else None,
        "vpin": float(vpin) if vpin is not None else None,
        "timestamp": timestamp,
    }


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


def record_shutdown_check(conn: ConnectionLike, checked_at: datetime, remaining_process_count: int) -> None:
    """
    입력: DB 커넥션, 확인 시각, taskkill/PowerShell fallback kill 이후에도 커맨드라인 기준으로
         남아있는 마흐디 프로세스 수(0이면 정상 종료).
    계산: 싱글턴 행(id=TRUE 고정) upsert — scripts/log_marketclose_stop.py가 장마감 종료
         시도마다(성공/실패 무관하게 항상) 호출해 COCKPIT이 "직전 장마감이 실제로 깨끗했는지"를
         재시작 없이 바로 볼 수 있게 한다(2026-07-21, 운영점검보고서 §5-3).
    """
    row = {"id": True, "checked_at": checked_at, "remaining_process_count": remaining_process_count}
    _upsert(conn, "shutdown_check_log", ("id", "checked_at", "remaining_process_count"), ("id",), row)


def latest_shutdown_check(conn: ConnectionLike) -> tuple[datetime, int] | None:
    """
    계산: 가장 최근 record_shutdown_check() 기록을 반환한다.
    실패 조건: 아직 아무도 기록한 적이 없으면(최초 기동, 이 마이그레이션 적용 전 등) None —
              호출측(COCKPIT 헬스체크)이 "정보 없음"으로 구분해서 보여줘야 한다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT checked_at, remaining_process_count FROM shutdown_check_log LIMIT 1")
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


def record_rate_limiter_status(
    conn: ConnectionLike, checked_at: datetime, backoff_multiplier: float,
    last_cycle_overrun_seconds: float, total_calls: int | None = None,
) -> None:
    """
    입력: DB 커넥션, 기록 시각, 공유 _RateLimiter의 현재 배율(1.0=정상), 직전 옵션체인 폴링
         사이클이 60초 주기를 넘겨 밀린 초(0이면 정상), 페이서를 통과한 누적 호출 수
         (`KISRestClient.rate_limit_total_calls`, 2026-08-01 신규 — 마이그레이션 019).
    계산: 싱글턴 행(id=TRUE 고정) upsert — mahdi.main의 poll_option_chain이 매 사이클(60초)마다
         호출해 COCKPIT이 재시작 없이 "지금 레이트리밋에 얼마나 근접했는지"를 바로 볼 수 있게
         한다(2026-07-23, 운영점검보고서 §2-1/§4 Fix#4).
    해석(2026-08-01): total_calls는 **누적 카운터**라 그 자체로는 의미가 없고, 두 시점의 차이로만
         수요(건/초)가 나온다 — 계산은 `mahdi.ops.db_metrics.rest_demand()`가 전담한다
         (COCKPIT 배지와 일일 리포트가 그 함수를 공유해 서로 다른 답을 내지 않게 한다).
    """
    row = {
        "id": True, "checked_at": checked_at,
        "backoff_multiplier": backoff_multiplier, "last_cycle_overrun_seconds": last_cycle_overrun_seconds,
        "total_calls": total_calls,
    }
    _upsert(
        conn, "rate_limiter_status_log",
        ("id", "checked_at", "backoff_multiplier", "last_cycle_overrun_seconds", "total_calls"), ("id",), row,
    )


def latest_rate_limiter_status(conn: ConnectionLike) -> tuple[datetime, float, float] | None:
    """
    계산: 가장 최근 record_rate_limiter_status() 기록을 반환한다.
    실패 조건: 아직 아무도 기록한 적이 없으면(최초 기동, 이 마이그레이션 적용 전 등) None —
              호출측(COCKPIT 헬스체크)이 "정보 없음"으로 구분해서 보여줘야 한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT checked_at, backoff_multiplier, last_cycle_overrun_seconds FROM rate_limiter_status_log LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], float(row[1]), float(row[2])


def append_rate_limiter_status_history(
    conn: ConnectionLike, recorded_at: datetime, backoff_multiplier: float,
    last_cycle_overrun_seconds: float, total_calls: int | None = None,
) -> None:
    """
    입력: record_rate_limiter_status()와 동일한 인자 4종.
    계산: `rate_limiter_status_log`(싱글턴, "현재 상태"만 보존)와 달리 이 테이블은 append-only라
         매 호출이 새 행을 남긴다(2026-07-29, 운영점검보고서 §2-5/Fix#3) — 배율이 시간에 따라
         어떻게 변했는지 시계열로 되짚어볼 수 있게 한다. `poll_option_chain`이 매 사이클
         `record_rate_limiter_status()` 바로 다음에 같은 값으로 호출한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rate_limiter_status_history "
            "(recorded_at, backoff_multiplier, last_cycle_overrun_seconds, total_calls) "
            "VALUES (%s, %s, %s, %s)",
            (recorded_at, backoff_multiplier, last_cycle_overrun_seconds, total_calls),
        )
    conn.commit()


def rate_limiter_status_history_since(conn: ConnectionLike, since: datetime) -> list[dict]:
    """
    입력: 조회 시작 시각(이 시각 이상만 반환).
    계산: `since` 이후 기록을 시간순으로 반환한다 — COCKPIT 추세 차트, 운영점검 시 "언제부터
         배율이 상승했는지" 같은 분석에 쓴다.
    실패 조건: 없음 — 기록이 없으면 빈 목록.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recorded_at, backoff_multiplier, last_cycle_overrun_seconds "
            "FROM rate_limiter_status_history WHERE recorded_at >= %s ORDER BY recorded_at ASC",
            (since,),
        )
        rows = cur.fetchall()
    return [
        {"recorded_at": recorded_at, "backoff_multiplier": float(mult), "last_cycle_overrun_seconds": float(overrun)}
        for recorded_at, mult, overrun in rows
    ]


_MARKET_HALT_STATUS_COLUMNS = ("id", "updated_at", "is_halted", "mkop_cls_code", "label", "halted_since")


def upsert_market_halt_state(
    conn: ConnectionLike,
    updated_at: datetime,
    is_halted: bool,
    mkop_cls_code: str | None,
    label: str | None,
    halted_since: datetime | None,
) -> None:
    """
    입력: 서킷브레이커/거래정지 실시간 감지(mahdi.risk.market_halt.MarketHaltMonitor)의 상태
         전이 시점 값.
    계산: 싱글턴 행(id=TRUE 고정) upsert — `rate_limiter_status_log`와 동일한 패턴. main.py의
         WS 핸들러가 MarketHaltMonitor.update()의 `changed=True`일 때, 그리고
         `poll_market_halt_heartbeat()`가 300초마다 호출한다.
         2026-07-31: `last_message_at`은 여기서 건드리지 않는다 — 컬럼 목록에 없으므로 ON CONFLICT
         DO UPDATE가 그 값을 보존한다. 수신 시각은 `mark_market_halt_message_seen()` 전담이다
         (두 신호를 분리한 이유는 마이그레이션 018 주석 참고).
    """
    row = {
        "id": True, "updated_at": updated_at, "is_halted": is_halted,
        "mkop_cls_code": mkop_cls_code, "label": label, "halted_since": halted_since,
    }
    _upsert(conn, "market_halt_status", _MARKET_HALT_STATUS_COLUMNS, ("id",), row)


def mark_market_halt_message_seen(conn: ConnectionLike, seen_at: datetime) -> None:
    """
    입력: H0UNMKO0 장운영정보를 실제로 수신한 시각.
    계산: 싱글턴 행의 `last_message_at`만 갱신한다 — 상태 값(is_halted/mkop_cls_code/label)은
         건드리지 않으므로 진행 중인 차단을 덮어쓸 위험이 없다.
    해석: 2026-07-31 §2-2 — `updated_at`("관측 루프가 살아있다", 독립 하트비트가 갱신)과
         `last_message_at`("감지기가 최근 무언가를 봤다")을 분리한다. 이 TR은 세션 전이 시에만
         오므로 정상일에도 수 시간 공백이 정상이며, 그래서 이 컬럼에는 임계 경보를 두지 않는다.
    실패 조건: 아직 행이 없으면(구독 직후 기준행 upsert 전) UPDATE가 0행을 건드리고 조용히 끝난다 —
              기준행은 구독 직후 반드시 생기므로 실사용에서는 발생하지 않는다.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE market_halt_status SET last_message_at=%s WHERE id IS TRUE", (seen_at,))


def mark_market_op_subscribed(conn: ConnectionLike, subscribed_at: datetime) -> None:
    """
    입력: H0UNMKO0 구독이 성립한 시각(KIS SUBSCRIBE 응답 rt_cd=0 수신 시점).
    계산: `ws_status` 싱글턴 행의 `market_op_subscribed_at`만 갱신한다 — 하트비트가 쓰는 다른
         컬럼(updated_at/connected_since/last_message_at/reconnect_count_today)은 건드리지 않는다.
    해석: 2026-08-04(COCKPIT 육안 점검). 이 값을 하트비트(300초)에만 맡기면 **기동 직후 5분간
         NULL이 남는다** — 첫 하트비트가 구독 ACK보다 먼저 돌기 때문이다(08-04 실측: 하트비트
         07:31:02.7, ACK 07:31:03.2). 그 5분 동안 COCKPIT이 "구독 미성립" 경고를 띄웠고 실제로는
         구독이 성립해 있었다. ACK은 기동/재연결 시에만 오는 드문 이벤트라 즉시 써도 부담이 없다.
    실패 조건: 아직 행이 없으면(첫 하트비트 전) UPDATE가 0행을 건드리고 조용히 끝난다 —
              곧 이어질 하트비트가 메모리에 남은 값을 싣고 간다.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE ws_status SET market_op_subscribed_at=%s WHERE id IS TRUE", (subscribed_at,))
    conn.commit()


def latest_market_halt_state(conn: ConnectionLike) -> dict | None:
    """
    계산: 가장 최근 upsert_market_halt_state() 기록을 반환한다. 2026-07-31부터 `last_message_at`
         (마지막 H0UNMKO0 수신 시각)을 함께 돌려주며, 이는 `updated_at`(관측 루프 생존 하트비트)과
         **의미가 다르다** — COCKPIT은 둘을 나눠 표시해야 한다(마이그레이션 018 주석 참고).
    실패 조건: 아직 CB/거래정지 이벤트가 한 번도 없었으면(정상적인 대부분의 거래일) None —
              호출측(RiskEngine 게이팅, COCKPIT)이 "정상"으로 취급해야 한다(지어내지 않음 —
              is_halted=False로 만든 가짜 행을 반환하지 않는다).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT updated_at, is_halted, mkop_cls_code, label, halted_since, last_message_at "
            "FROM market_halt_status LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    updated_at, is_halted, mkop_cls_code, label, halted_since, last_message_at = row
    return {
        "updated_at": updated_at, "is_halted": bool(is_halted),
        "mkop_cls_code": mkop_cls_code, "label": label, "halted_since": halted_since,
        "last_message_at": last_message_at,
    }


_WS_STATUS_COLUMNS = (
    "id", "updated_at", "connected_since", "last_message_at", "reconnect_count_today",
    "market_op_subscribed_at",
)


def upsert_ws_status(
    conn: ConnectionLike,
    updated_at: datetime,
    connected_since: datetime | None,
    last_message_at: datetime | None,
    reconnect_count_today: int,
    market_op_subscribed_at: datetime | None = None,
) -> None:
    """
    입력: 하트비트 시각과 `mahdi.main.WsLiveness`의 현재 값.
    계산: 싱글턴 행(id=TRUE) upsert — `market_halt_status`와 동일 패턴.
    해석: 2026-08-01 §5-4. `updated_at`은 **메시지 수신과 무관한 독립 하트비트**(300초)가 갱신하며
         "관측 루프의 WS 파트가 살아있다"를 뜻한다. 07-31 재연결 0회처럼 아무 일도 없는 날에도
         이 값이 갱신되는 것이 생존의 증거다(마이그레이션 020 주석 참고).
    """
    row = {
        "id": True, "updated_at": updated_at, "connected_since": connected_since,
        "last_message_at": last_message_at, "reconnect_count_today": reconnect_count_today,
        "market_op_subscribed_at": market_op_subscribed_at,
    }
    _upsert(conn, "ws_status", _WS_STATUS_COLUMNS, ("id",), row)


def latest_ws_status(conn: ConnectionLike) -> dict | None:
    """
    계산: 가장 최근 upsert_ws_status() 기록.
    실패 조건: 관측 루프가 아직 안 돌았으면 None — 호출측(COCKPIT)이 "미기록"으로 구분해야 한다
              (지어내지 않는다).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT updated_at, connected_since, last_message_at, reconnect_count_today, "
            "market_op_subscribed_at FROM ws_status LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "updated_at": row[0], "connected_since": row[1],
        "last_message_at": row[2], "reconnect_count_today": int(row[3]),
        "market_op_subscribed_at": row[4],
    }


def append_market_halt_event_history(
    conn: ConnectionLike, recorded_at: datetime, mkop_cls_code: str | None, label: str | None, is_halted: bool
) -> None:
    """
    입력: upsert_market_halt_state()와 동일한 전이 시점 값.
    계산: `market_halt_status`(싱글턴)와 달리 append-only라 매 상태 전이마다 새 행을 남긴다
         (`rate_limiter_status_history`와 동일 패턴) — 그날 CB가 몇 번, 언제 발동·해제됐는지
         시계열로 되짚어볼 수 있게 한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO market_halt_event_history (recorded_at, mkop_cls_code, label, is_halted) "
            "VALUES (%s, %s, %s, %s)",
            (recorded_at, mkop_cls_code, label, is_halted),
        )
    conn.commit()


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


# 2026-08-03(운영점검보고서 §2-2) — 체인 스냅샷이 인정하는 레그의 최대 나이(분).
#
# **배경**: 종전 `latest_option_chain()`은 `DISTINCT ON (strike, option_type)`만 걸고 시각 조건도
# 만기 조건도 없었다. ATM±2 창은 스팟을 따라 움직이므로 한때 창 안에 있다가 빠져나간 행사가가
# **그때의 값 그대로 영원히 스냅샷에 남는다.** 2026-08-03 15:45 라이브 실측: 반환 246레그 중
# 오늘 수집분은 10개뿐이고, 156레그(63%)가 이미 만기가 지난 것이며, 최고령 레그는 4주 전
# (2026-07-06 10:45)이었다. GEX가 전체 -563억 / 오늘 수집분만 +28억으로 **부호가 뒤집혔다** —
# GEX 부호는 v6 §7의 회귀/증폭 판정과 §11.4 프리미엄 매도 게이트(positive GEX 요구)의 입력이다.
#
# **왜 10분인가**: `main.py:120` 주석이 위클리 격분(2분) 전환 근거로 "안 조회한 분에는 직전 값이
# 이월돼 구멍이 안 생긴다"고 적었는데, 그 의도는 **1~2분 이월**이었다. 여기에 밀림·결손이 겹쳐
# 최대 몇 분 더 벌어질 수 있는 여유를 더해 10분으로 잡는다(2026-08-03 실측 최대 밀림 48.2초,
# 미회수 결손 6분). 이보다 오래된 값은 "이월"이 아니라 "유령"이다.
# 2026-08-04(운영점검보고서 §2-2 / Fix#6b) — 10분 → 5분.
#
# §2-2는 체인 스냅샷 오염(설계 30레그인데 09시 실측 50레그, 최대 72)을 ATM 지터 탓으로 봤고
# Fix#6(히스테리시스)이 고칠 것으로 예측했다. **구현 후 08-04 스팟 시계열로 리플레이해보니
# 그 예측이 틀렸다** — 히스테리시스는 왕복(36 → 10회)과 롤링 횟수(182 → 131회)를 줄이지만
# 10분 창 안의 행사가 수는 **7개로 전혀 안 변한다**(ratio 0.0/0.75/1.0 전부 동일).
#
# 오염의 실제 원인은 왕복이 아니라 **스팟의 실제 이동거리**다. 08-04에 스팟이 954~1008(54p =
# 행사가 21칸)을 움직였고, 10분이면 어떤 롤링 정책이든 여러 칸을 지난다. 그리고
# `DISTINCT ON (expiry, strike, option_type)`은 **ATM 창 밖으로 빠진 행사가를 다시 폴링하지 않으므로
# 그 마지막 값이 창 만료까지 남는다** — 창 길이가 곧 오염 길이다.
#
# 08-04 실측(먼슬리 단독 기준, Fix#5로 판단 입력이 먼슬리 전용이 된 뒤의 지표):
#
#   창    중앙 레그   최대 레그   GAMMA_FLIP_MIN_LEGS 미달 분
#   2분      10          16              0
#   3분      12          16              0
#   5분      12          20              0
#   10분     14          30              0        ← 현행. 설계값 10의 3배까지 벌어진다
#
# 5분을 고른 이유: 중앙 레그가 3분과 같으면서(12) 위클리 2북을 **각각 2회씩** 포함한다
# (위클리는 격분 폴링이라 3분 창은 1회만 포함 — 그 1회가 실패하면 그 북이 통째로 빠진다.
# 08-04에 옵션체인 폴링 실패가 38건 있었으므로 이건 가정이 아니라 실측된 위험이다).
# 2분은 중앙 10레그로 설계값과 정확히 같지만 결손 분(08-04 48분) 2회 연속에 빈 체인이 된다.
CHAIN_SNAPSHOT_MAX_AGE_MINUTES = 5

_CHAIN_SNAPSHOT_COLUMNS = ("strike", "option_type", "oi", "iv", "gamma", "gex", "expiry", "timestamp")

# `DISTINCT ON`에 **expiry를 포함한다**(2026-08-03 §2-2). 종전에는 (strike, option_type)으로만 묶어
# 만기가 다른 3개 북(regular/weekly_mon/weekly_thu)이 같은 행사가에서 서로를 덮어썼다 — 마지막으로
# 폴링된 북 하나만 남아, 반환된 감마가 어느 만기의 것인지 알 수 없었고 북별 GEX도 볼 수 없었다.
_CHAIN_SNAPSHOT_SQL = """
    SELECT DISTINCT ON (expiry, strike, option_type)
        strike, option_type, oi, iv, gamma, gex, expiry, timestamp, rv_5d
    FROM option_analysis_1m
    WHERE underlying=%s
      AND timestamp <= %s
      AND timestamp >= %s
      AND expiry >= %s
    ORDER BY expiry, strike, option_type, timestamp DESC
"""


def _chain_snapshot(conn: ConnectionLike, underlying: str, as_of: datetime) -> list[dict]:
    """
    입력: DB 커넥션, 기초자산 라벨, 스냅샷 기준 시각.
    계산: `as_of` 이전 `CHAIN_SNAPSHOT_MAX_AGE_MINUTES`분 이내에 수집됐고 `as_of` 시점에 아직
         만기가 남은(당일 만기 포함) 레그만 모아, (expiry, strike, option_type)별 최신 1건으로
         체인 스냅샷을 구성한다.
    해석: 반환 dict는 `mahdi.features.options_intel.OptionLeg` 생성에 바로 쓸 수 있는 키를 가진다.
         `latest_option_chain()`(라이브)과 `option_chain_as_of()`(백테스트 리플레이)가 **이 함수를
         공유한다** — 두 경로가 다른 체인을 보면 백테스트 결과를 라이브에 적용할 수 없다.

         2026-08-05(Fix#1): `rv_5d`를 함께 싣는다. `atm_straddle_vrp()`가 v6 §11.4 매트릭스의
         열(저평가/적정/고평가)을 정하는 데 IV와 **같은 스냅샷의** 실현변동성이 필요하기 때문이다.
         별도 조회로 빼지 않는 이유는 위의 "라이브와 백테스트가 같은 체인을 본다"는 성질을
         VRP에도 그대로 물려주기 위해서다 — 다른 경로로 rv를 읽으면 두 경로가 갈린다.
         `OptionLeg`에는 rv 필드가 없으므로 `legs_from_chain_rows()`는 이 키를 무시한다(키를
         명시적으로 골라 읽는 구조라 추가해도 안전).
    실패 조건: 조건을 만족하는 행이 없으면 빈 목록(호출측이 GEX/flip을 건너뛴다).
    """
    oldest = as_of - timedelta(minutes=CHAIN_SNAPSHOT_MAX_AGE_MINUTES)
    with conn.cursor() as cur:
        cur.execute(_CHAIN_SNAPSHOT_SQL, (underlying, as_of, oldest, as_of.date()))
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
            # None을 0.0으로 바꾸지 않는다 — `atm_straddle_vrp()`가 "rv 없음"을 산출 불가로
            # 판정해야 하는데, 0.0으로 채우면 "실현변동성이 0"과 구분되지 않는다.
            "rv_5d": float(rv_5d) if rv_5d is not None else None,
        }
        for strike, option_type, oi, iv, gamma, gex, expiry, timestamp, rv_5d in rows
    ]


def latest_option_chain(conn: ConnectionLike, underlying: str) -> list[dict]:
    """
    계산: 지금(`local_now()`) 기준 체인 스냅샷 — 상세 규칙은 `_chain_snapshot()` 참고.
    해석: 반환된 dict는 mahdi.features.options_intel.OptionLeg 생성에 바로 쓸 수 있는 키를 가진다.
    """
    return _chain_snapshot(conn, underlying, local_now())


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
         키를 가진다. 아직 **오늘** 폴링이 한 번도 안 돌았으면 빈 리스트.

         2026-08-04(COCKPIT 육안 점검): 종전에는 시각 조건이 없어 `DISTINCT ON (series)`가
         **어제 행이든 4주 전 행이든** 최신 1건을 그대로 돌려줬다 — `latest_option_chain()`이
         2026-08-03 §2-2에서 고친 것과 **완전히 같은 결함 패턴**인데 이쪽은 안 고쳐져 있었다.
         실제 피해: 08-04 07:35 화면이 weekly_mon 만기를 **2026-08-03(잔존 0일)** 로 표시했다.
         그날 실제 북은 이미 2026-08-10으로 롤오버돼 있었고(`option_analysis_1m` 확인),
         만기유동성 폴러는 08:31부터 도니 그 시각엔 오늘 행이 없어 어제 마지막 행이 나온 것이다.
         **"잔존 0일"은 만기 당일이라는 뜻이라, 만기가 지난 북을 아직 보고 있다는 오해를 부른다.**
         어제 값은 오늘 판단에 쓰이지 않으므로 오늘 것만 본다(`monthly_book_expiry()`와 같은 규칙).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (series)
                series, expiry, atm_spread_pct, depth, volume, days_to_expiry
            FROM expiry_liquidity_1m
            WHERE underlying=%s AND series = ANY(%s) AND timestamp::date=%s
            ORDER BY series, timestamp DESC
            """,
            (underlying, list(_VALID_EXPIRY_LIQUIDITY_SERIES), local_now().date()),
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


def expiry_liquidity_fossil_series(conn: ConnectionLike, underlying: str) -> list[str]:
    """
    입력: 기초자산 라벨.
    계산: _VALID_EXPIRY_LIQUIDITY_SERIES 화이트리스트 밖의 series 값이 expiry_liquidity_1m에
         남아있는지 확인한다 — 2026-07-10 위클리 분리 이전 구코드가 남긴 series='weekly' 화석
         데이터(179건)가 COCKPIT에 영구 노출됐던 사고([[DECISION_LOG]])와 같은 패턴이 재발하는지
         COCKPIT "오늘의 점검 요약" 패널(2026-07-19, §5-6)에서 사람이 매번 DB를 직접 조회하지
         않고도 상시 확인할 수 있게 한다.
    해석: latest_expiry_liquidity()는 이미 이 화이트리스트로 걸러서 반환하므로 COCKPIT 정상
         패널에는 화석 데이터가 안 보인다 — 하지만 그건 "숨긴 것"이지 "없는 것"은 아니므로,
         실제로 그런 화석 행이 쌓이고 있는지는 별도로 확인해야 한다.
    실패 조건: 화석 series가 없으면 빈 리스트.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT series FROM expiry_liquidity_1m WHERE underlying=%s AND series != ALL(%s)",
            (underlying, list(_VALID_EXPIRY_LIQUIDITY_SERIES)),
        )
        rows = cur.fetchall()
    return [row[0] for row in rows]


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


def underlying_daily_closes(conn: ConnectionLike, underlying: str, days: int) -> list[float]:
    """
    입력: 기초자산 라벨("KOSPI200"), 조회할 최근 거래일 수(rv_ratio가 21개를 요구).
    계산: `underlying_spot_1m`을 날짜별로 묶어 각 날짜의 마지막 지수값(종가)을 뽑는다.
         시간순(오래된 순)으로 반환한다 — `daily_closes()`와 동일한 계약.
    해석: 2026-07-30(운영점검보고서 §2-3/§4 Fix#5) — 종전에는 `daily_closes(futures_symbol)`로
         **선물 종목코드별** 이력을 셌다. 선물은 분기마다 종목코드가 바뀌므로(A01609 → 다음 월물)
         롤오버 직후 이력이 0일로 리셋되고, 21일 임계를 다시 채울 때까지 한 달 가까이 rv_ratio가
         중립값 1.0으로 고정된다 — 실제로 07-30까지 `feature_store` 전체 5,394건의 rv_ratio가
         **단 한 건도 1.0이 아닌 적이 없었다**(A01609 누적 18일 < 21일). 지수는 롤오버가 없어
         이력이 끊기지 않으므로 rv_ratio 입력으로 더 적합하다.
         **정규장 시간대(09:00~15:45)로 제한**하는 이유: 개발 세션 재시작 등으로 장외 시간에
         폴러가 돌면 그 시각의 행이 그 날짜의 마지막 행이 되어 "종가"로 잡힌다. 실제로 라이브 DB
         왕복 검증에서 07-16/07-17/07-19 세 날짜의 종가가 전부 정확히 같은 값(1080.36)으로
         나왔는데, 이는 장외 폴링이 직전 세션의 마지막 값을 그대로 되돌려준 결과였다 — 그대로
         두면 일간 수익률에 인위적인 0이 섞여 rv_ratio가 실제보다 낮게 나온다.
         v6 §16.1 거래시간(09:00~15:45) 기준이며, 이 프로젝트의 timestamp는 KST 벽시계 값이
         그대로 저장돼 있어(local_now() docstring 참고) `timestamp::time` 비교가 곧 KST 비교다.
    실패 조건: 없음 — 데이터가 부족하면 짧은 리스트를 그대로 반환하고, 21개 미만일 때의 중립값
              처리는 `mahdi.features.regime_features.rv_ratio`가 담당한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (timestamp::date) timestamp::date AS d, spot
            FROM underlying_spot_1m
            WHERE underlying=%s AND timestamp::time BETWEEN '09:00' AND '15:45'
            ORDER BY d DESC, timestamp DESC
            LIMIT %s
            """,
            (underlying, days),
        )
        rows = cur.fetchall()
    return [float(spot) for _, spot in reversed(rows)]


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


def insert_signal_decision(
    conn: ConnectionLike,
    timestamp: datetime,
    conviction: str,
    decision: str,
    reject_reason: str | None,
    risk_gate_state: dict,
    exec_mode: str,
    chain_inputs: dict | None = None,
) -> None:
    """
    입력: 시각, conviction(v6 §11.1 4단계 문자열), decision("ENTER"/"HOLD"/"REJECT"),
         거절 사유(있으면), 리스크/신호 게이트 상태 요약(JSONB 직렬화), 실행 모드
         ("ADVISORY"/"CONFIRM"/"AUTO"), (선택) 판단 시점의 옵션 체인 입력
         (`gamma_flip`/`gex`/`chain_leg_count`/`chain_oldest_leg_age_seconds` 키, 마이그레이션 022;
         `gex_expiry` 키는 마이그레이션 023 — 어느 북으로 GEX를 냈는지, 2026-08-04 §2-8/Fix#5;
         `vrp` 키는 마이그레이션 024 — 팔레트 열을 정한 값, 2026-08-05 §2 이상점 1/Fix#1).
    계산: `signal_decisions`는 `decision_id`가 자동생성 UUID라 upsert 대상이 아니다 — 매 호출이
         새 행을 남기는 append-only 로그(§18.2 "거절된 신호도 기록한다")라 단순 INSERT만 한다.
    해석: 2026-08-03 §5-1 — 체인 입력을 판단 행에 함께 남겨야 "신호 도달률"을 사후 집계할 수
         있다. 08-03에 먼슬리 커버리지는 98.8%인데 감마플립 산출률은 0%였고, 그것을 알아낼
         지표가 하나도 없었다. `chain_inputs`가 None이면 네 컬럼은 NULL로 남는다 — 없는 값을
         0으로 채우면 "계산했는데 0"과 구분되지 않는다.
    실패 조건: reject_reason이 50자를 넘으면 DB가 자르지 않고 에러를 낼 수 있다 — 호출측이
              미리 축약해서 넘겨야 한다.
    """
    chain = chain_inputs or {}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signal_decisions (timestamp, conviction, decision, reject_reason, "
            "risk_gate_state, exec_mode, gamma_flip, gex, chain_leg_count, "
            "chain_oldest_leg_age_seconds, gex_expiry, vrp) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                timestamp, conviction, decision, reject_reason, json.dumps(risk_gate_state), exec_mode,
                chain.get("gamma_flip"), chain.get("gex"),
                chain.get("chain_leg_count"), chain.get("chain_oldest_leg_age_seconds"),
                chain.get("gex_expiry"), chain.get("vrp"),
            ),
        )
    conn.commit()


def entry_strategies_used_today(conn: ConnectionLike, on_date: date) -> frozenset[str]:
    """
    입력: DB 커넥션, 기준 날짜(보통 `local_now().date()`).
    계산: 그날 `decision='ENTER'`로 기록된 판단 행들의 `risk_gate_state.entry_strategies`를
         모아 **중복 없는 전략 이름 집합**으로 돌려준다.
    해석: 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 6 / Fix#5) —
         `SignalFusionEngine.evaluate()`의 `already_used_strategies_today` 인자가 **한 번도
         전달된 적이 없어** 항상 `frozenset()`이었다. 그 결과 v6 §11.4의 "하루 레짐당 우선
         전략군 2개 이하" 상한(`enforce_daily_strategy_cap`)이 전 이력 무력이었다.
         지금은 ADVISORY라 실손실이 없지만, **"안전장치는 죽었는지 알 수 있어야 한다"**는
         §5-4 원칙에 어긋난다(07-30 CB 하트비트에서 같은 종류의 문제를 겪었다).

         **`allowed_strategies`가 아니라 `entry_strategies`를 세는 이유**: 허용은 "이 셀이
         열려 있다"이고 사용은 "실제로 들어갔다"이다. 허용을 세면 `wait_and_see`가 열려 있던
         분까지 "전략을 썼다"로 계수돼 상한이 장 시작 몇 분 만에 소진된다.
         `entry_strategies`는 `entry_strategies()`가 관망 계열을 걸러낸 뒤의 목록이다.

         **알려진 한계(실행 배선 시 교체할 것)**: 지금 이 값의 출처는 `signal_decisions`,
         즉 **"권고된 진입"**이지 체결이 아니다. `trade_history`가 0행이라 그것 말고 셀 것이
         없다. 실주문 경로가 배선되면 출처를 `trade_history`로 옮겨야 한다 — 권고와 체결이
         갈리기 시작하는 순간부터 이 집합은 사실과 달라진다.
    실패 조건: 그날 ENTER가 없으면 빈 frozenset. `entry_strategies` 키가 없거나 배열이 아닌
              행(구버전 기록)은 조용히 건너뛴다 — 과거 기록 때문에 오늘 판단이 죽으면 안 된다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT jsonb_array_elements_text(risk_gate_state->'entry_strategies')
            FROM signal_decisions
            WHERE timestamp::date = %s
              AND decision = 'ENTER'
              AND jsonb_typeof(risk_gate_state->'entry_strategies') = 'array'
            """,
            (on_date,),
        )
        return frozenset(row[0] for row in cur.fetchall() if row[0])


def insert_risk_snapshot(
    conn: ConnectionLike,
    timestamp: datetime,
    greeks: dict,
    loss_buffer: float | None,
    cb_state: dict,
) -> None:
    """
    입력: 시각, 그릭스/시장 감마 구조 요약(JSONB), 일간 손실한도까지 남은 여유(비율, 예: 0.02 =
         2%p 남음 — 한도를 이미 넘겼으면 음수), Circuit Breaker/거래정지 상태 요약(JSONB).
    계산: `risk_snapshots`(timestamp PK 하이퍼테이블)에 upsert한다 — Signal Fusion 폴러가 매
         사이클(60초) 같은 분에 한 번씩 남기므로, 같은 분에 두 번 들어와도 마지막 값으로 덮어쓴다.
    해석: 2026-07-30(운영점검보고서 §2-9/§4 Fix#6) — RiskEngine이 하루 419회 평가를 수행했는데도
         `risk_snapshots`가 0행이라 사후에 "그 시점 리스크 상태가 어땠는지"를 재구성할 수 없었다.
         `signal_decisions.risk_gate_state`에는 승인여부/사이즈만 남아 CB 상태·손실 여유가 빠져 있다.
    실패 조건: 없음 — 호출측이 예외를 잡아 폴링 루프를 막지 않도록 처리한다(다른 폴러와 동일).

    **읽는 함수를 나중에 추가할 때 주의**: `loss_buffer`는 DECIMAL 컬럼이라 psycopg가
    `decimal.Decimal`로 돌려준다(라이브 왕복으로 확인) — 이 프로젝트의 다른 `latest_*` 함수들처럼
    반드시 `float()`로 변환할 것([[DECISION_LOG]] 2026-07-28 8차 "Decimal/float 혼합 버그" 참고).
    """
    _upsert(
        conn, "risk_snapshots", ("timestamp", "greeks", "loss_buffer", "cb_state"), ("timestamp",),
        {
            "timestamp": timestamp, "greeks": json.dumps(greeks),
            "loss_buffer": loss_buffer, "cb_state": json.dumps(cb_state),
        },
    )


def recent_signal_decisions(conn: ConnectionLike, limit: int = 20) -> list[dict]:
    """
    입력: 반환할 최대 건수(최신순).
    계산: COCKPIT "마흐디 판단 현황" 패널(2026-07-29)이 최근 진입 판단 이력을 보여주는 데
         쓴다 — `insert_signal_decision()`이 남긴 append-only 로그를 그대로 최신순으로 읽는다.
         `risk_gate_state`는 JSONB 컬럼이라 psycopg3가 자동으로 dict로 역직렬화해 돌려준다
         (별도 `json.loads` 불필요 — 라이브 DB로 직접 확인함).
    실패 조건: 없음 — 기록이 없으면 빈 목록.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, conviction, decision, reject_reason, risk_gate_state, exec_mode "
            "FROM signal_decisions ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "timestamp": timestamp, "conviction": conviction, "decision": decision,
            "reject_reason": reject_reason, "risk_gate_state": risk_gate_state, "exec_mode": exec_mode,
        }
        for timestamp, conviction, decision, reject_reason, risk_gate_state, exec_mode in rows
    ]


def market_bars_between(conn: ConnectionLike, symbol: str, start: datetime, end: datetime) -> list[dict]:
    """
    입력: 선물/옵션 단축코드, 조회 구간(start 이상 end 미만, 시간순).
    계산: `market_raw_1m`에서 해당 구간의 1분봉(OHLC)을 시간순으로 반환한다 — 백테스트
         데이터 어댑터(`mahdi/backtest/data_adapter.py`)가 `Bar` 시퀀스를 만드는 데 쓴다.
    해석: `market_raw_1m`은 실시간 수집만 하고(백테스트 재처리 대상 아님) 이 함수는 순수 조회다.
    실패 조건: 구간에 데이터가 없으면 빈 목록.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, open, high, low, close FROM market_raw_1m "
            "WHERE symbol=%s AND timestamp >= %s AND timestamp < %s ORDER BY timestamp ASC",
            (symbol, start, end),
        )
        rows = cur.fetchall()
    return [
        {"timestamp": ts, "open": float(o), "high": float(h), "low": float(low), "close": float(c)}
        for ts, o, h, low, c in rows
    ]


def option_chain_as_of(conn: ConnectionLike, underlying: str, as_of: datetime) -> list[dict]:
    """
    `latest_option_chain()`과 완전히 같은 형태·같은 규칙(신선도 창 + 만기 경계)을 `as_of` 시각
    기준으로 적용한다 — 백테스트 과거 리플레이 전용.

    2026-08-03(§4 우선순위 1): 종전에는 이 함수만 `timestamp <= as_of`를 걸고 신선도/만기
    경계가 없었다. 라이브와 백테스트가 다른 체인을 보면 백테스트 결과를 라이브에 적용할 수
    없으므로 `_chain_snapshot()`으로 통일한다.
    """
    return _chain_snapshot(conn, underlying, as_of)


def investor_flow_as_of(conn: ConnectionLike, underlying: str, as_of: datetime) -> tuple[float, float, float] | None:
    """`latest_investor_flow()`와 같은 형태를 `as_of` 시각 기준으로 반환(백테스트 리플레이 전용)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT foreign_net, institution_net, individual_net FROM investor_flow_1m "
            "WHERE underlying=%s AND timestamp <= %s ORDER BY timestamp DESC LIMIT 1",
            (underlying, as_of),
        )
        row = cur.fetchone()
    return (float(row[0]), float(row[1]), float(row[2])) if row else None


def get_trade_history(conn: ConnectionLike, strategy_id: str | None = None) -> list[dict]:
    """
    입력: 전략 ID(None이면 전체 전략).
    계산: `trade_history`에서 ML 학습에 필요한 컬럼(regime_entry/confidence_entry/net_pnl)만
         뽑는다 — `scripts/fit_signal_fusion_meta_label.py`가 이 함수로 읽어 학습 매트릭스를
         만든다(`mahdi/fusion/trainer.py`의 `build_training_matrix()` 참고).
    해석: 아직 아무 곳도 `trade_history`에 쓰지 않으므로(실주문 실행이 없음) 현재는 항상 빈
         목록을 반환한다 — 그 자체가 "학습 스크립트가 오늘은 데이터 부족으로 종료돼야 한다"는
         기대 동작과 일치한다.
    실패 조건: 없음 — 행이 없으면 빈 목록.
    """
    query = "SELECT regime_entry, confidence_entry, net_pnl FROM trade_history"
    params: tuple = ()
    if strategy_id is not None:
        query += " WHERE strategy_id=%s"
        params = (strategy_id,)
    query += " ORDER BY entry_time ASC"
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        {"regime_entry": regime_entry, "confidence_entry": float(confidence_entry), "net_pnl": float(net_pnl)}
        for regime_entry, confidence_entry, net_pnl in rows
        if regime_entry is not None and confidence_entry is not None and net_pnl is not None
    ]


_ACCOUNT_BALANCE_SNAPSHOT_COLUMNS = (
    "timestamp", "prsm_dpast", "evlu_pfls_amt_smtl", "trad_pfls_amt_smtl",
    "dnca_cash", "ord_psbl_cash", "mgna_tota",
    "same_direction_buy_count", "same_direction_sell_count",
)
_ACCOUNT_BALANCE_SNAPSHOT_DECIMAL_COLUMNS = (
    "prsm_dpast", "evlu_pfls_amt_smtl", "trad_pfls_amt_smtl", "dnca_cash", "ord_psbl_cash", "mgna_tota",
)


def _account_balance_snapshot_row_to_dict(row: tuple) -> dict:
    """DECIMAL 컬럼(psycopg가 `decimal.Decimal`로 반환)을 float으로 변환한다 — 라이브 DB
    실측에서 `account_tracker.build_account_state()`가 다른 float 값과 섞어 연산할 때
    `Decimal`/`float` 혼합 TypeError를 내는 걸 확인해 수정함(2026-07-28 8차)."""
    values = dict(zip(_ACCOUNT_BALANCE_SNAPSHOT_COLUMNS, row))
    for col in _ACCOUNT_BALANCE_SNAPSHOT_DECIMAL_COLUMNS:
        if values[col] is not None:
            values[col] = float(values[col])
    return values


def insert_account_balance_snapshot(conn: ConnectionLike, row: dict) -> None:
    """
    입력: `account_balance_snapshots` 컬럼과 동일한 키를 가진 dict(`mahdi.execution.
         account_tracker.BalanceSnapshot`을 dict로 펼친 것 — `parse_balance_response()` 참고).
    계산: INSERT ... ON CONFLICT (timestamp) DO UPDATE — 재처리에도 멱등.
    """
    _upsert(conn, "account_balance_snapshots", _ACCOUNT_BALANCE_SNAPSHOT_COLUMNS, ("timestamp",), row)


def latest_account_balance_snapshot(conn: ConnectionLike) -> dict | None:
    """가장 최근 계좌 잔고 스냅샷 1건. 계좌 추적기 폴러가 아직 한 번도 안 돌았으면 None."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_ACCOUNT_BALANCE_SNAPSHOT_COLUMNS)} FROM account_balance_snapshots "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _account_balance_snapshot_row_to_dict(row)


def account_balance_snapshot_before(conn: ConnectionLike, before: datetime) -> dict | None:
    """
    입력: 기준 시각.
    계산: `before` 이전의 마지막 스냅샷 1건을 반환한다 — `latest_regime_before()`/
         `compute_gap_zscore()`와 동일한 as-of 패턴. 호출측이 `before`에 오늘 자정을 넣으면
         "어제 종가 기준"(일간 손익 baseline), 이번주 월요일 자정을 넣으면 "지난주 금요일 종가
         기준"(주간 손익 baseline)이 된다 — 별도 함수를 두지 않고 하나로 재사용한다.
    실패 조건: 그 이전에 스냅샷이 없으면(운영 첫 날 등) None — 호출측이 "baseline 없음"으로
              처리해야 한다(`build_account_state()`는 이 경우 0.0으로 폴백).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_ACCOUNT_BALANCE_SNAPSHOT_COLUMNS)} FROM account_balance_snapshots "
            "WHERE timestamp < %s ORDER BY timestamp DESC LIMIT 1",
            (before,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _account_balance_snapshot_row_to_dict(row)


def max_account_balance_ever(conn: ConnectionLike) -> float | None:
    """계산: 역대 최고 `prsm_dpast` — 드로우다운(`drawdown_pct`) 계산의 기준 피크값."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(prsm_dpast) FROM account_balance_snapshots")
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def daily_trade_counts_by_strategy(conn: ConnectionLike, day: date) -> dict[str, int]:
    """
    입력: 집계할 날짜(거래일).
    계산: `trade_history`를 `entry_time::date`로 필터링해 전략별 거래 횟수를 센다 —
         `RiskEngine`의 `max_daily_trades_per_strategy` 한도 체크(`AccountState.
         daily_trades_by_strategy`) 재료.
    해석: 아직 아무도 `trade_history`에 쓰지 않으므로(실주문 실행이 없음) 현재는 항상 빈
         dict를 반환한다.
    실패 조건: 없음 — 행이 없으면 빈 dict.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id, count(*) FROM trade_history WHERE entry_time::date=%s GROUP BY strategy_id",
            (day,),
        )
        rows = cur.fetchall()
    return {strategy_id: int(count) for strategy_id, count in rows if strategy_id is not None}
