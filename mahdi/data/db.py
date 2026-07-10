"""TimescaleDB 커넥션·삽입 헬퍼 — db/migrations/001_init.sql 스키마와 대응.

실시간 수집과 백테스트 재처리가 같은 삽입 경로를 쓰도록, INSERT는 모두 PK 충돌 시
갱신(ON CONFLICT DO UPDATE)해 재처리에도 멱등성을 보장한다.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Protocol

import psycopg

from mahdi.config.settings import DBSettings, get_db_settings

_MARKET_RAW_1M_COLUMNS = (
    "timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap",
    "vpin", "ofi", "microprice", "bid_ask_spread", "buy_volume", "sell_volume",
    "usdkrw", "quality_flag",
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


def latest_expiry_liquidity(conn: ConnectionLike, underlying: str) -> list[dict]:
    """
    입력: 기초자산 라벨.
    계산: series(regular/weekly_mon/weekly_thu)별로 가장 최근 timestamp 1건씩만 골라 반환한다 — 폴링 주기(5분)
         중 두 북의 조회 시각이 조금씩 어긋날 수 있어 북별 최신값을 취한다.
    해석: 반환된 dict는 COCKPIT 만기 유동성 비교 패널(Phase 1.5-④)이 바로 렌더링에 쓸 수 있는
         키를 가진다. 아직 폴링이 한 번도 안 돌았으면 빈 리스트.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (series)
                series, expiry, atm_spread_pct, depth, volume, days_to_expiry
            FROM expiry_liquidity_1m
            WHERE underlying=%s
            ORDER BY series, timestamp DESC
            """,
            (underlying,),
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
