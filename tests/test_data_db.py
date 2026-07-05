import json
from datetime import datetime

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
