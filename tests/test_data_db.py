import json
from datetime import date, datetime

from mahdi.data import db


def test_local_now_returns_naive_wall_clock_time():
    # 2026-07-19 타임스탬프 정책 명문화(§5-3): DB에 쓰이는 시각은 전부 이 함수를 거쳐야 한다.
    # naive(tzinfo 없음)인 것 자체가 "정책"이다 — tz-aware로 바뀌면 psycopg가 다른 방식으로
    # 직렬화해 기존에 쌓인 "가짜 UTC" 라벨 데이터와 갑자기 섞이므로, 이 성질이 깨지면 안 된다.
    before = datetime.now()
    result = db.local_now()
    after = datetime.now()

    assert result.tzinfo is None
    assert before <= result <= after


class FakeCursor:
    def __init__(self, store: dict):
        self.store = store

    def execute(self, query: str, params=None) -> None:
        self.store["query"] = query
        self.store["params"] = params

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self):
        self.store: dict = {}
        self.committed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def commit(self) -> None:
        self.committed = True


def test_insert_market_raw_1m_upserts_on_timestamp_symbol():
    conn = FakeConnection()
    row = {
        "timestamp": datetime(2026, 7, 5, 9, 5),
        "symbol": "201W09",
        "open": 350.0,
        "high": 351.0,
        "low": 349.5,
        "close": 350.5,
        "volume": 100,
        "vwap": 350.2,
        "vpin": 0.3,
        "ofi": 12.0,
        "microprice": 350.3,
        "bid_ask_spread": 0.1,
        "buy_volume": 60,
        "sell_volume": 40,
        "usdkrw": 1380.0,
        "quality_flag": 0,
    }

    db.insert_market_raw_1m(conn, row)

    assert conn.committed is True
    assert "INSERT INTO market_raw_1m" in conn.store["query"]
    assert "ON CONFLICT (timestamp, symbol) DO UPDATE" in conn.store["query"]
    assert conn.store["params"][0] == row["timestamp"]
    assert conn.store["params"][1] == "201W09"
    assert len(conn.store["params"]) == len(db._MARKET_RAW_1M_COLUMNS)


def test_insert_feature_store_serializes_features_to_json():
    conn = FakeConnection()
    ts = datetime(2026, 7, 5, 9, 5)

    db.insert_feature_store(conn, ts, "201W09", {"ofi": 12.0, "vpin": 0.3}, feature_version="v1")

    assert conn.committed is True
    params = conn.store["params"]
    assert params[0] == ts
    assert params[1] == "201W09"
    assert json.loads(params[2]) == {"ofi": 12.0, "vpin": 0.3}
    assert params[3] == "v1"
    assert "feature_store" in conn.store["query"]


def test_insert_regime_state_upserts_on_timestamp_only():
    conn = FakeConnection()
    ts = datetime(2026, 7, 5, 9, 5)

    db.insert_regime_state(conn, ts, regime=4, prob_vector=[0.1] * 8, higher_tf_regime=None, stability_flag=True)

    assert "ON CONFLICT (timestamp) DO UPDATE" in conn.store["query"]
    assert conn.store["params"][0] == ts
    assert conn.store["params"][1] == 4


def test_insert_option_analysis_1m_upserts_on_full_leg_key():
    conn = FakeConnection()
    row = {col: None for col in db._OPTION_ANALYSIS_1M_COLUMNS}
    row.update(
        timestamp=datetime(2026, 7, 6, 9, 31),
        underlying="KOSPI200",
        expiry=date(2026, 7, 9),
        strike=1340.0,
        option_type="C",
        gamma=0.0047,
        gex=123.4,
    )

    db.insert_option_analysis_1m(conn, row)

    assert "ON CONFLICT (timestamp, underlying, expiry, strike, option_type) DO UPDATE" in conn.store["query"]


def test_insert_macro_snapshot_5m_upserts_on_timestamp():
    conn = FakeConnection()
    ts = datetime(2026, 7, 10, 8, 5)
    row = {
        "timestamp": ts,
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 0.017143,
        "usdcnh": 6.7803,
        "us10y_yield": 4.54,
        "zn_front": 110.25,
        "quality_flag": 0,
    }

    db.insert_macro_snapshot_5m(conn, row)

    assert conn.committed is True
    assert "ON CONFLICT (timestamp) DO UPDATE" in conn.store["query"]
    assert conn.store["params"][0] == ts
    assert len(conn.store["params"]) == len(db._MACRO_SNAPSHOT_5M_COLUMNS)


class _FakeSequentialCursor:
    def __init__(self, results: list):
        self._results = results

    def execute(self, query: str, params=None) -> None:
        pass

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSequentialConnection:
    """cursor() 호출마다 다른 결과를 순서대로 반환 — latest_macro_snapshot의 2단계(최신행 조회 →
    us10y_yield가 NULL이면 LOCF 폴백 조회) 쿼리를 서로 다른 응답으로 검증하는 데 쓴다."""

    def __init__(self, *fetchone_results):
        self._queue = list(fetchone_results)

    def cursor(self):
        return _FakeSequentialCursor([self._queue.pop(0)] if self._queue else [])


def test_latest_macro_snapshot_returns_none_when_no_rows():
    conn = _FakeSequentialConnection(None)
    assert db.latest_macro_snapshot(conn) is None


def test_latest_macro_snapshot_returns_row_when_us10y_present():
    ts = datetime(2026, 7, 10, 8, 5)
    conn = _FakeSequentialConnection((ts, 17.50, 17.80, 0.017143, 6.7803, 4.54, 110.25))

    result = db.latest_macro_snapshot(conn)

    assert result == {
        "timestamp": ts,
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 0.017143,
        "usdcnh": 6.7803,
        "us10y_yield": 4.54,
        "zn_front": 110.25,
    }


def test_latest_macro_snapshot_forward_fills_us10y_when_null():
    # 최신 5분 행은 US10Y(일봉 레벨)가 아직 안 갱신돼 NULL이지만, 그 전에 일봉으로 한 번 채워진
    # 값이 있으면 그 값을 LOCF로 들고 와야 한다. zn_front는 CBOT 신청 후 5분마다 갱신되므로
    # 별도 폴백 없이 그대로 반환돼야 한다.
    ts = datetime(2026, 7, 10, 8, 10)
    conn = _FakeSequentialConnection(
        (ts, 17.55, 17.85, 0.017094, 6.7810, None, 110.30),  # 최신 행: us10y_yield NULL, zn_front는 값 있음
        (4.54,),  # 폴백 쿼리 결과
    )

    result = db.latest_macro_snapshot(conn)

    assert result["us10y_yield"] == 4.54
    assert result["vix_front"] == 17.55
    assert result["zn_front"] == 110.30


def test_insert_underlying_spot_upserts_on_timestamp_underlying():
    conn = FakeConnection()
    ts = datetime(2026, 7, 6, 9, 31)

    db.insert_underlying_spot(conn, ts, "KOSPI200", 1333.77)

    assert "ON CONFLICT (timestamp, underlying) DO UPDATE" in conn.store["query"]
    assert conn.store["params"] == [ts, "KOSPI200", 1333.77]


class FakeReadCursor:
    def __init__(self, rows: list, store: dict | None = None):
        self._rows = rows
        self._store = store

    def execute(self, query: str, params=None) -> None:
        if self._store is not None:
            self._store["query"] = query
            self._store["params"] = params

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeReadConnection:
    def __init__(self, rows: list):
        self._rows = rows
        self.store: dict = {}

    def cursor(self) -> FakeReadCursor:
        return FakeReadCursor(self._rows, self.store)


def test_latest_underlying_spot_returns_value():
    conn = FakeReadConnection([(1333.77,)])
    assert db.latest_underlying_spot(conn, "KOSPI200") == 1333.77


def test_latest_underlying_spot_returns_none_when_no_rows():
    conn = FakeReadConnection([])
    assert db.latest_underlying_spot(conn, "KOSPI200") is None


def test_insert_investor_flow_upserts_on_timestamp_underlying():
    conn = FakeConnection()
    ts = datetime(2026, 7, 6, 10, 30)

    db.insert_investor_flow(
        conn, ts, "KOSPI200", foreign_net=-682279.0, institution_net=678405.0, individual_net=54565.0
    )

    assert "ON CONFLICT (timestamp, underlying) DO UPDATE" in conn.store["query"]
    assert conn.store["params"] == [ts, "KOSPI200", -682279.0, 678405.0, 54565.0]


def test_latest_investor_flow_returns_tuple():
    conn = FakeReadConnection([(-682279.0, 678405.0, 54565.0)])
    assert db.latest_investor_flow(conn, "KOSPI200") == (-682279.0, 678405.0, 54565.0)


def test_latest_investor_flow_returns_none_when_no_rows():
    conn = FakeReadConnection([])
    assert db.latest_investor_flow(conn, "KOSPI200") is None


def test_upsert_active_futures_symbol_upserts_on_underlying():
    conn = FakeConnection()
    ts = datetime(2026, 7, 6, 12, 0)

    db.upsert_active_futures_symbol(conn, "KOSPI200", "A01609", ts)

    assert "ON CONFLICT (underlying) DO UPDATE" in conn.store["query"]
    assert conn.store["params"] == ["KOSPI200", "A01609", ts]


def test_get_active_futures_symbol_returns_value():
    conn = FakeReadConnection([("A01609",)])
    assert db.get_active_futures_symbol(conn, "KOSPI200") == "A01609"


def test_get_active_futures_symbol_returns_none_when_no_rows():
    conn = FakeReadConnection([])
    assert db.get_active_futures_symbol(conn, "KOSPI200") is None


def test_latest_expiry_liquidity_filters_query_to_valid_series_only():
    # 2026-07-10: 위클리를 weekly_mon/weekly_thu로 분리하며, 그 전 버전이 쓰던 병합 라벨
    # "weekly"처럼 더 이상 아무도 안 쓰는 series 값이 DB에 화석으로 남아 있어도 COCKPIT에
    # 영원히 다시 나타나지 않도록 쿼리 자체가 유효한 series로 필터링하는지 검증한다.
    conn = FakeReadConnection([])

    db.latest_expiry_liquidity(conn, "KOSPI200")

    assert conn.store["params"][0] == "KOSPI200"
    assert set(conn.store["params"][1]) == {"regular", "weekly_mon", "weekly_thu"}
    assert "series = ANY(%s)" in conn.store["query"]


def test_latest_expiry_liquidity_maps_rows_to_dicts():
    rows = [("weekly_thu", date(2026, 7, 16), 0.4136, 36.0, 0.0, 6)]
    conn = FakeReadConnection(rows)

    result = db.latest_expiry_liquidity(conn, "KOSPI200")

    assert result == [
        {
            "series": "weekly_thu",
            "expiry": date(2026, 7, 16),
            "atm_spread_pct": 0.4136,
            "depth": 36.0,
            "volume": 0.0,
            "days_to_expiry": 6,
        }
    ]


def test_latest_option_chain_maps_rows_to_dicts():
    rows = [(1340.0, "C", 363, 0.9, 0.0047, 123.4, date(2026, 7, 9), datetime(2026, 7, 6, 9, 31))]
    conn = FakeReadConnection(rows)

    chain = db.latest_option_chain(conn, "KOSPI200")

    assert chain == [
        {
            "strike": 1340.0,
            "option_type": "C",
            "oi": 363.0,
            "iv": 0.9,
            "gamma": 0.0047,
            "gex": 123.4,
            "expiry": date(2026, 7, 9),
            "timestamp": datetime(2026, 7, 6, 9, 31),
        }
    ]
