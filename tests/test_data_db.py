import json
from datetime import date, datetime

from mahdi.data import db


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


def test_insert_underlying_spot_upserts_on_timestamp_underlying():
    conn = FakeConnection()
    ts = datetime(2026, 7, 6, 9, 31)

    db.insert_underlying_spot(conn, ts, "KOSPI200", 1333.77)

    assert "ON CONFLICT (timestamp, underlying) DO UPDATE" in conn.store["query"]
    assert conn.store["params"] == [ts, "KOSPI200", 1333.77]


class FakeReadCursor:
    def __init__(self, rows: list):
        self._rows = rows

    def execute(self, query: str, params=None) -> None:
        pass

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

    def cursor(self) -> FakeReadCursor:
        return FakeReadCursor(self._rows)


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
