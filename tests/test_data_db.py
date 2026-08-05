import json
from datetime import date, datetime, timedelta

import psycopg
import pytest

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


def test_is_slack_alerts_enabled_reads_stored_value():
    # 2026-07-19(§5-4): 저장된 값이 있으면(누군가 이미 토글했으면) 그 값을 그대로 반환.
    assert db.is_slack_alerts_enabled(FakeReadConnection([(True,)])) is True
    assert db.is_slack_alerts_enabled(FakeReadConnection([(False,)])) is False


def test_is_slack_alerts_enabled_falls_back_to_settings_default_when_no_row(monkeypatch):
    # 아무도 토글한 적 없어(최초 기동) 행이 없으면 SlackSettings.slack_alerts_enabled_default로 폴백.
    from mahdi.config import settings as settings_module

    class _FakeSlackSettings:
        slack_alerts_enabled_default = False

    monkeypatch.setattr(settings_module, "get_slack_settings", lambda: _FakeSlackSettings())

    assert db.is_slack_alerts_enabled(FakeReadConnection([])) is False


def test_set_slack_alerts_enabled_upserts_singleton_row():
    conn = FakeConnection()
    db.set_slack_alerts_enabled(conn, True)

    assert conn.committed is True
    assert "INSERT INTO slack_alert_settings" in conn.store["query"]
    assert "ON CONFLICT (id) DO UPDATE" in conn.store["query"]
    assert conn.store["params"][0] is True  # id
    assert conn.store["params"][1] is True  # enabled


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
        "usdkrw": 1352.30,
        "zn_front": 110.25,
        "zn_front_source": "kis",
        "es_front": 5123.25,
        "es_front_source": "kis",
        "move_index": 95.30,
        "move_index_source": "yfinance_fallback",
        "quality_flag": 0,
    }

    db.insert_macro_snapshot_5m(conn, row)

    assert conn.committed is True
    assert "ON CONFLICT (timestamp) DO UPDATE" in conn.store["query"]
    assert conn.store["params"][0] == ts
    assert len(conn.store["params"]) == len(db._MACRO_SNAPSHOT_5M_COLUMNS)


def test_macro_snapshot_columns_matches_insert_columns():
    # 2026-07-21: COCKPIT 스키마 정합성 헬스체크(_schema_integrity_check)가 이 목록을
    # information_schema.columns와 대조한다 — insert_macro_snapshot_5m()이 실제로 쓰는
    # 컬럼과 어긋나면 헬스체크가 무의미해지므로 단일 소스임을 고정한다.
    assert db.macro_snapshot_columns() == db._MACRO_SNAPSHOT_5M_COLUMNS


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
    conn = _FakeSequentialConnection(
        (ts, 17.50, 17.80, 0.017143, 6.7803, 4.54, 1352.30, 110.25, "kis", 5123.25, "kis", 95.30, "yfinance_fallback")
    )

    result = db.latest_macro_snapshot(conn)

    assert result == {
        "timestamp": ts,
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 0.017143,
        "usdcnh": 6.7803,
        "us10y_yield": 4.54,
        "usdkrw": 1352.30,
        "zn_front": 110.25,
        "zn_front_source": "kis",
        "es_front": 5123.25,
        "es_front_source": "kis",
        "move_index": 95.30,
        "move_index_source": "yfinance_fallback",
        # 2026-08-05(P1-4): 최신 행에 값이 다 있으면 LOCF를 안 타므로 관측 시각이 곧 행 시각이다.
        "us10y_yield_asof": ts,
        "usdkrw_asof": ts,
        "zn_front_asof": ts,
        "move_index_asof": ts,
    }


def test_latest_macro_snapshot_forward_fills_us10y_when_null():
    # 최신 5분 행은 US10Y(일봉 레벨)가 아직 안 갱신돼 NULL이지만, 그 전에 일봉으로 한 번 채워진
    # 값이 있으면 그 값을 LOCF로 들고 와야 한다. zn_front는 CBOT 신청 후(또는 yfinance 폴백으로)
    # 5분마다 갱신되므로 별도 폴백 없이 그대로 반환돼야 한다. usdkrw는 이 테스트에서 값이 있어
    # 별도 LOCF 쿼리가 안 나가는 케이스로 둔다(usdkrw 자체의 LOCF는 아래 별도 테스트에서 검증).
    ts = datetime(2026, 7, 10, 8, 10)
    locf_ts = datetime(2026, 7, 9, 15, 40)  # 전일 일봉으로 채워진 행
    conn = _FakeSequentialConnection(
        # 최신 행: us10y_yield NULL, zn_front는 yfinance 폴백으로 채워진 값
        (ts, 17.55, 17.85, 0.017094, 6.7810, None, 1352.35, 110.30, "yfinance_fallback", 5100.00, "kis", None, None),
        (locf_ts, 4.54),  # us10y_yield 폴백 쿼리 결과(2026-08-05 P1-4부터 관측 시각을 함께 읽는다)
    )

    result = db.latest_macro_snapshot(conn)

    assert result["us10y_yield"] == 4.54
    assert result["us10y_yield_asof"] == locf_ts  # 이 값은 어제 것이라는 사실이 함께 나와야 한다
    assert result["vix_front"] == 17.55
    assert result["usdkrw"] == 1352.35
    assert result["usdkrw_asof"] == ts  # LOCF를 안 탄 항목은 최신 행 시각
    assert result["zn_front"] == 110.30
    assert result["zn_front_source"] == "yfinance_fallback"


def test_latest_macro_snapshot_forward_fills_usdkrw_when_null():
    # USDKRW도 US10Y와 동일하게 일봉이라 하루 중 대부분 NULL — 값이 채워진 마지막 행으로 LOCF.
    ts = datetime(2026, 7, 10, 8, 15)
    conn = _FakeSequentialConnection(
        (ts, 17.60, 17.90, 0.017045, 6.7820, 4.54, None, 110.40, "kis", None, None, None, None),
        (datetime(2026, 7, 9, 15, 40), 1352.30),  # usdkrw 폴백 쿼리 결과
    )

    result = db.latest_macro_snapshot(conn)

    assert result["usdkrw"] == 1352.30
    assert result["us10y_yield"] == 4.54


def test_latest_macro_snapshot_locf_does_not_reach_back_without_a_bound():
    """2026-08-05 P1-4 회귀 — LOCF 쿼리에 **시각 조건이 아예 없었다.**

    `latest_option_chain()`(08-03)·`latest_expiry_liquidity()`(08-04)에서 이미 두 번 고친 화석 행
    결함인데 이 경로만 남아 있었다: 3주 전 MOVE가 "지금 시점의 매크로 상태"로 실려 나왔다.
    """
    ts = datetime(2026, 7, 10, 8, 15)
    queries: list[tuple] = []

    class _RecordingCursor(_FakeSequentialCursor):
        def execute(self, query, params=None):
            queries.append((query, params))

    class _RecordingConnection(_FakeSequentialConnection):
        def cursor(self):
            cur = _RecordingCursor([self._queue.pop(0)] if self._queue else [])
            return cur

    conn = _RecordingConnection(
        (ts, 17.60, 17.90, 0.017045, 6.7820, None, 1352.30, 110.40, "kis", None, None, None, None),
        (datetime(2026, 7, 9, 15, 40), 4.54),
    )
    db.latest_macro_snapshot(conn, as_of=ts)

    locf_query, params = queries[1]
    assert "timestamp >= %s" in locf_query
    assert params == (ts - timedelta(days=db._MACRO_LOCF_MAX_AGE_DAYS),)


def test_latest_macro_snapshot_returns_none_when_older_than_max_age():
    """신호 경로(`regime_pipeline.macro_score`)만 켜는 경계 — 낡은 VIX 부호가 레짐에 흘러들면 안 된다."""
    ts = datetime(2026, 7, 10, 8, 15)
    conn = _FakeSequentialConnection(
        (ts, 17.60, 17.90, 0.017045, 6.7820, 4.54, 1352.30, 110.40, "kis", None, None, None, None)
    )

    assert db.latest_macro_snapshot(conn, as_of=ts + timedelta(minutes=16), max_age_minutes=15) is None


def test_latest_macro_snapshot_keeps_row_within_max_age():
    ts = datetime(2026, 7, 10, 8, 15)
    conn = _FakeSequentialConnection(
        (ts, 17.60, 17.90, 0.017045, 6.7820, 4.54, 1352.30, 110.40, "kis", None, None, None, None)
    )

    result = db.latest_macro_snapshot(conn, as_of=ts + timedelta(minutes=14), max_age_minutes=15)

    assert result is not None and result["vix_front"] == 17.60


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


def test_recent_usdkrw_daily_series_returns_oldest_first():
    # DB는 (날짜, 값) 튜플을 DESC로 반환하지만 함수는 rv_ratio/cross_asset_stress가 기대하는
    # 시간순(오래된 순) 값 리스트로 뒤집어 돌려줘야 한다(daily_closes와 동일 패턴).
    conn = FakeReadConnection(
        [(date(2026, 7, 10), 1352.30), (date(2026, 7, 9), 1351.00), (date(2026, 7, 8), 1350.00)]
    )
    assert db.recent_usdkrw_daily_series(conn, days=10) == [1350.00, 1351.00, 1352.30]


def test_recent_usdkrw_daily_series_empty_when_no_rows():
    conn = FakeReadConnection([])
    assert db.recent_usdkrw_daily_series(conn, days=10) == []


def test_recent_us10y_daily_series_returns_oldest_first():
    conn = FakeReadConnection(
        [(date(2026, 7, 10), 4.54), (date(2026, 7, 9), 4.52), (date(2026, 7, 8), 4.50)]
    )
    assert db.recent_us10y_daily_series(conn, days=10) == [4.50, 4.52, 4.54]


def test_recent_usdcnh_series_returns_oldest_first():
    conn = FakeReadConnection([(6.900,), (6.780,), (6.781,)])
    assert db.recent_usdcnh_series(conn, limit=24) == [6.781, 6.780, 6.900]


def test_recent_es_front_series_returns_oldest_first():
    conn = FakeReadConnection([(5100.00,), (5050.00,), (5000.00,)])
    assert db.recent_es_front_series(conn, limit=12) == [5000.00, 5050.00, 5100.00]


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
    rows = [(1340.0, "C", 363, 0.9, 0.0047, 123.4, date(2026, 7, 9), datetime(2026, 7, 6, 9, 31), 0.72)]
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
            "rv_5d": 0.72,
        }
    ]


def test_latest_option_chain_keeps_missing_rv_as_none_not_zero():
    # 2026-08-05(Fix#1): rv 없음(NULL)과 실현변동성 0은 다르다 — 0.0으로 채우면
    # `atm_straddle_vrp()`가 "못 쟀다"를 "쟀는데 0"으로 오인해 VRP = IV가 되고,
    # 그 분은 항상 극단적 고평가로 판정된다(08-05 위클리 두 북이 실제로 rv_5d=0이었다).
    rows = [(1340.0, "C", 363, 0.9, 0.0047, 123.4, date(2026, 7, 9), datetime(2026, 7, 6, 9, 31), None)]

    chain = db.latest_option_chain(FakeReadConnection(rows), "KOSPI200")

    assert chain[0]["rv_5d"] is None


def test_expiry_liquidity_fossil_series_returns_empty_when_none_found():
    conn = FakeReadConnection([])
    assert db.expiry_liquidity_fossil_series(conn, "KOSPI200") == []


def test_expiry_liquidity_fossil_series_returns_values_outside_whitelist():
    # 2026-07-19(§5-6 "오늘의 점검 요약"): 2026-07-10 위클리 분리 이전 병합 라벨 "weekly"처럼
    # 화이트리스트 밖 series가 남아있으면 그 값들을 그대로 알려줘야 한다(latest_expiry_liquidity()는
    # 이런 화석 데이터를 조용히 걸러내기만 하지, "있는지 없는지"는 별도로 확인해야 함).
    conn = FakeReadConnection([("weekly",)])

    result = db.expiry_liquidity_fossil_series(conn, "KOSPI200")

    assert result == ["weekly"]
    assert conn.store["params"][0] == "KOSPI200"
    assert set(conn.store["params"][1]) == {"regular", "weekly_mon", "weekly_thu"}
    assert "series != ALL(%s)" in conn.store["query"]


def test_insert_signal_decision_is_plain_insert_not_upsert():
    # signal_decisions는 decision_id가 자동생성 UUID라 upsert 대상이 아니다 — 매 호출이 새 행.
    conn = FakeConnection()
    ts = datetime(2026, 7, 28, 10, 0)

    db.insert_signal_decision(
        conn, ts, conviction="HIGH_CONVICTION", decision="ENTER",
        reject_reason=None, risk_gate_state={"direction": 1.0}, exec_mode="ADVISORY",
    )

    assert conn.committed is True
    assert "INSERT INTO signal_decisions" in conn.store["query"]
    assert "ON CONFLICT" not in conn.store["query"]
    params = conn.store["params"]
    assert params[0] == ts
    assert params[1] == "HIGH_CONVICTION"
    assert params[2] == "ENTER"
    assert params[3] is None
    assert json.loads(params[4]) == {"direction": 1.0}
    assert params[5] == "ADVISORY"


def test_market_bars_between_maps_rows_to_bar_dicts():
    rows = [(datetime(2026, 7, 28, 9, 0), 100.0, 101.0, 99.0, 100.5)]
    conn = FakeReadConnection(rows)

    bars = db.market_bars_between(
        conn, "101S03", datetime(2026, 7, 28, 9, 0), datetime(2026, 7, 28, 9, 5)
    )

    assert bars == [
        {"timestamp": datetime(2026, 7, 28, 9, 0), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
    ]


def test_market_bars_between_empty_range_is_empty():
    assert db.market_bars_between(FakeReadConnection([]), "101S03", datetime(2026, 1, 1), datetime(2026, 1, 2)) == []


def test_option_chain_as_of_matches_latest_option_chain_shape():
    rows = [(1340.0, "C", 363, 0.9, 0.0047, 123.4, date(2026, 7, 9), datetime(2026, 7, 6, 9, 31), 0.72)]
    conn = FakeReadConnection(rows)

    chain = db.option_chain_as_of(conn, "KOSPI200", datetime(2026, 7, 6, 9, 31))

    assert chain == [
        {
            "strike": 1340.0, "option_type": "C", "oi": 363.0, "iv": 0.9, "gamma": 0.0047,
            "gex": 123.4, "expiry": date(2026, 7, 9), "timestamp": datetime(2026, 7, 6, 9, 31),
            "rv_5d": 0.72,
        }
    ]
    assert "timestamp <= %s" in conn.store["query"]


def test_chain_snapshot_bounds_freshness_and_expiry():
    """2026-08-03 §2-2 회귀 — 체인 스냅샷에 신선도 창과 만기 경계가 반드시 걸려야 한다.

    수정 전에는 `DISTINCT ON (strike, option_type)`만 있고 시각/만기 조건이 없어, ATM 창 밖으로
    빠진 행사가가 그때의 값 그대로 영원히 남았다 — 라이브 실측 246레그 중 오늘 수집분은 10개,
    156레그(63%)가 이미 만기가 지난 것이었고 최고령은 4주 전이었다. GEX 부호까지 뒤집혔다.
    """
    conn = FakeReadConnection([])
    as_of = datetime(2026, 8, 3, 13, 0)

    db.option_chain_as_of(conn, "KOSPI200", as_of)

    query, params = conn.store["query"], conn.store["params"]
    # 만기가 다른 3개 북(regular/weekly_mon/weekly_thu)이 같은 행사가에서 서로를 덮어쓰지 않도록
    # DISTINCT ON에 expiry가 들어가야 한다.
    assert "DISTINCT ON (expiry, strike, option_type)" in query
    assert "timestamp >= %s" in query and "expiry >= %s" in query
    assert params == (
        "KOSPI200",
        as_of,
        as_of - timedelta(minutes=db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES),
        as_of.date(),
    )


def test_latest_option_chain_uses_same_bounds_as_backtest_path(monkeypatch):
    # 라이브와 백테스트가 다른 체인을 보면 백테스트 결과를 라이브에 적용할 수 없다.
    now = datetime(2026, 8, 3, 13, 0)
    monkeypatch.setattr(db, "local_now", lambda: now)
    live, replay = FakeReadConnection([]), FakeReadConnection([])

    db.latest_option_chain(live, "KOSPI200")
    db.option_chain_as_of(replay, "KOSPI200", now)

    assert live.store == replay.store


def test_investor_flow_as_of_returns_tuple():
    conn = FakeReadConnection([(500.0, -200.0, -300.0)])
    result = db.investor_flow_as_of(conn, "KOSPI200", datetime(2026, 7, 6, 9, 31))
    assert result == (500.0, -200.0, -300.0)


def test_investor_flow_as_of_returns_none_when_no_rows():
    assert db.investor_flow_as_of(FakeReadConnection([]), "KOSPI200", datetime(2026, 7, 6)) is None


def test_get_trade_history_maps_rows_and_filters_nulls():
    rows = [
        (0, 0.8, 15.0),
        (None, 0.5, -3.0),  # regime_entry 없음 -> 제외
    ]
    conn = FakeReadConnection(rows)

    result = db.get_trade_history(conn)

    assert result == [{"regime_entry": 0, "confidence_entry": 0.8, "net_pnl": 15.0}]


def test_get_trade_history_filters_by_strategy_id():
    conn = FakeReadConnection([])
    db.get_trade_history(conn, strategy_id="vrp_harvest")
    assert "WHERE strategy_id=%s" in conn.store["query"]
    assert conn.store["params"] == ("vrp_harvest",)


def test_get_trade_history_empty_when_no_trades():
    assert db.get_trade_history(FakeReadConnection([])) == []


_ACCOUNT_SNAPSHOT_ROW = (
    datetime(2026, 7, 28, 10, 0), 50000000.0, 0.0, 0.0, 50000000.0, 50000000.0, 0.0, 0, 0,
)


def test_insert_account_balance_snapshot_upserts_on_timestamp():
    conn = FakeConnection()
    row = {
        "timestamp": datetime(2026, 7, 28, 10, 0), "prsm_dpast": 50000000.0,
        "evlu_pfls_amt_smtl": 0.0, "trad_pfls_amt_smtl": 0.0, "dnca_cash": 50000000.0,
        "ord_psbl_cash": 50000000.0, "mgna_tota": 0.0,
        "same_direction_buy_count": 0, "same_direction_sell_count": 0,
    }
    db.insert_account_balance_snapshot(conn, row)

    assert conn.committed is True
    assert "INSERT INTO account_balance_snapshots" in conn.store["query"]
    assert "ON CONFLICT (timestamp) DO UPDATE" in conn.store["query"]


def test_latest_account_balance_snapshot_maps_row_to_dict():
    conn = FakeReadConnection([_ACCOUNT_SNAPSHOT_ROW])
    result = db.latest_account_balance_snapshot(conn)
    assert result["prsm_dpast"] == 50000000.0
    assert result["same_direction_buy_count"] == 0


def test_latest_account_balance_snapshot_none_when_empty():
    assert db.latest_account_balance_snapshot(FakeReadConnection([])) is None


def test_account_balance_snapshot_before_queries_strict_less_than():
    conn = FakeReadConnection([_ACCOUNT_SNAPSHOT_ROW])
    before = datetime(2026, 7, 28, 0, 0)

    result = db.account_balance_snapshot_before(conn, before)

    assert result["prsm_dpast"] == 50000000.0
    assert "timestamp < %s" in conn.store["query"]
    assert conn.store["params"] == (before,)


def test_account_balance_snapshot_before_none_when_no_prior_snapshot():
    assert db.account_balance_snapshot_before(FakeReadConnection([]), datetime(2026, 7, 28)) is None


def test_max_account_balance_ever_returns_float():
    assert db.max_account_balance_ever(FakeReadConnection([(51000000.0,)])) == 51000000.0


def test_max_account_balance_ever_none_when_no_snapshots():
    assert db.max_account_balance_ever(FakeReadConnection([(None,)])) is None


def test_daily_trade_counts_by_strategy_groups_by_strategy_id():
    conn = FakeReadConnection([("vrp_harvest", 3), ("gamma_scalp", 1)])
    result = db.daily_trade_counts_by_strategy(conn, date(2026, 7, 28))
    assert result == {"vrp_harvest": 3, "gamma_scalp": 1}
    assert conn.store["params"] == (date(2026, 7, 28),)


def test_daily_trade_counts_by_strategy_empty_when_no_trades():
    assert db.daily_trade_counts_by_strategy(FakeReadConnection([]), date(2026, 7, 28)) == {}


def test_recent_signal_decisions_maps_rows_and_respects_limit():
    # 2026-07-29 신규 — COCKPIT "마흐디 판단 현황" 패널이 최근 이력을 최신순으로 보여주는 데 씀.
    rows = [
        (
            datetime(2026, 7, 29, 9, 5), "STANDARD", "ENTER", None,
            {"direction": 1.0, "risk_engine": {"approved": True, "approved_size": 1.0, "reject_reasons": []}},
            "ADVISORY",
        ),
    ]
    conn = FakeReadConnection(rows)

    result = db.recent_signal_decisions(conn, limit=5)

    assert result == [
        {
            "timestamp": datetime(2026, 7, 29, 9, 5), "conviction": "STANDARD", "decision": "ENTER",
            "reject_reason": None,
            "risk_gate_state": {
                "direction": 1.0, "risk_engine": {"approved": True, "approved_size": 1.0, "reject_reasons": []},
            },
            "exec_mode": "ADVISORY",
        }
    ]
    assert conn.store["params"] == (5,)


def test_recent_signal_decisions_empty_when_no_rows():
    assert db.recent_signal_decisions(FakeReadConnection([])) == []


def test_append_rate_limiter_status_history_is_plain_insert():
    # 2026-07-29(운영점검보고서 §2-5/Fix#3) — rate_limiter_status_log(싱글턴)와 달리 이 테이블은
    # append-only라 매 호출이 새 행을 남겨야 한다(upsert 아님).
    conn = FakeConnection()
    ts = datetime(2026, 7, 29, 9, 0)

    db.append_rate_limiter_status_history(conn, ts, backoff_multiplier=1.5, last_cycle_overrun_seconds=10.0)

    assert conn.committed is True
    assert "INSERT INTO rate_limiter_status_history" in conn.store["query"]
    assert "ON CONFLICT" not in conn.store["query"]
    # 2026-08-01(마이그레이션 019): total_calls가 뒤에 붙었다(미지정이면 NULL).
    assert conn.store["params"] == (ts, 1.5, 10.0, None)


def test_rate_limiter_status_history_since_maps_rows():
    conn = FakeReadConnection([(datetime(2026, 7, 29, 9, 0), 1.5, 10.0)])

    result = db.rate_limiter_status_history_since(conn, datetime(2026, 7, 29, 7, 30))

    assert result == [
        {"recorded_at": datetime(2026, 7, 29, 9, 0), "backoff_multiplier": 1.5, "last_cycle_overrun_seconds": 10.0}
    ]
    assert "recorded_at >= %s" in conn.store["query"]


def test_rate_limiter_status_history_since_empty_when_no_rows():
    assert db.rate_limiter_status_history_since(FakeReadConnection([]), datetime(2026, 7, 29)) == []


# ===== 2026-07-30 운영점검 Fix#5: rv_ratio 입력을 선물 종목코드 → 지수로 =====


def test_underlying_daily_closes_returns_oldest_first_from_index_table():
    # 선물은 분기마다 종목코드가 바뀌어 일별 이력이 리셋되지만 지수는 롤오버가 없다 —
    # rv_ratio(21개 필요)가 롤오버 때마다 중립값 1.0으로 되돌아가던 원인 제거.
    conn = FakeReadConnection(
        [(date(2026, 7, 30), 421.5), (date(2026, 7, 29), 420.0), (date(2026, 7, 28), 418.25)]
    )
    assert db.underlying_daily_closes(conn, "KOSPI200", days=30) == [418.25, 420.0, 421.5]


def test_underlying_daily_closes_queries_underlying_spot_not_market_raw():
    conn = FakeReadConnection([])
    db.underlying_daily_closes(conn, "KOSPI200", days=30)
    query = conn.store["query"]
    assert "underlying_spot_1m" in query
    assert "market_raw_1m" not in query  # 종목코드 기준으로 되돌아가면 롤오버 결함이 재발한다
    # 라이브 DB 왕복에서 발견: 장외 폴링 행이 그 날짜의 "종가"로 잡혀 07-16/17/19가 전부 같은
    # 값(1080.36)이 됐다 — 정규장(09:00~15:45) 필터가 빠지면 일간 수익률에 가짜 0이 섞인다.
    assert "timestamp::time BETWEEN '09:00' AND '15:45'" in query
    assert conn.store["params"] == ("KOSPI200", 30)


def test_underlying_daily_closes_empty_when_no_rows():
    assert db.underlying_daily_closes(FakeReadConnection([]), "KOSPI200", days=30) == []


# ===== 2026-07-30 운영점검 Fix#6: risk_snapshots 적재 =====


def test_insert_risk_snapshot_upserts_by_timestamp_and_serializes_jsonb():
    conn = FakeConnection()
    ts = datetime(2026, 7, 31, 9, 30)

    db.insert_risk_snapshot(
        conn, ts,
        greeks={"scope": "market", "gex": 1234.5},
        loss_buffer=0.02,
        cb_state={"circuit_breaker_state": "NORMAL", "market_halted": False},
    )

    assert conn.committed is True
    assert "INSERT INTO risk_snapshots" in conn.store["query"]
    # 같은 분에 두 번 들어와도 마지막 값으로 덮어써야 한다(폴러가 분 단위로 자른 시각을 씀).
    assert "ON CONFLICT (timestamp) DO UPDATE" in conn.store["query"]
    params = conn.store["params"]
    assert params[0] == ts
    assert json.loads(params[1]) == {"scope": "market", "gex": 1234.5}
    assert params[2] == 0.02
    assert json.loads(params[3]) == {"circuit_breaker_state": "NORMAL", "market_halted": False}


def test_insert_risk_snapshot_accepts_null_loss_buffer():
    # 계좌 스냅샷이 아직 없으면 손실 여유를 지어내지 않고 NULL로 남긴다.
    conn = FakeConnection()
    db.insert_risk_snapshot(conn, datetime(2026, 7, 31, 9, 30), greeks={}, loss_buffer=None, cb_state={})
    assert conn.store["params"][2] is None


# ===== 2026-07-31 운영점검 §2-2/§4 우선순위 4: CB 감지 생존 신호와 수신 사실의 분리 =====


def test_upsert_market_halt_state_does_not_touch_last_message_at():
    # 하트비트(300초)가 상태 행을 덮어쓸 때 last_message_at까지 NULL로 밀면 "감지기가 최근
    # 무언가를 봤는가"라는 별개 신호가 사라진다 — 컬럼 목록에 없어야 ON CONFLICT가 보존한다.
    conn = FakeConnection()
    db.upsert_market_halt_state(conn, datetime(2026, 7, 31, 9, 5), False, None, "정상", None)

    assert "last_message_at" not in conn.store["query"]
    assert "ON CONFLICT (id) DO UPDATE" in conn.store["query"]


def test_mark_market_halt_message_seen_updates_only_the_timestamp():
    # 반대로 수신 기록은 상태 값(is_halted/mkop_cls_code/label)을 건드리면 안 된다 —
    # 진행 중인 차단을 덮어쓸 위험이 있기 때문이다.
    conn = FakeConnection()
    seen_at = datetime(2026, 7, 31, 9, 0, 5)
    db.mark_market_halt_message_seen(conn, seen_at)

    query = conn.store["query"]
    assert query.startswith("UPDATE market_halt_status SET last_message_at=")
    assert "is_halted" not in query and "mkop_cls_code" not in query
    assert conn.store["params"] == (seen_at,)


def test_latest_market_halt_state_returns_both_liveness_signals():
    # COCKPIT은 updated_at("관측 루프가 살아있다")과 last_message_at("감지기가 최근 뭔가를 봤다")을
    # 나눠 표시해야 한다 — 07-30 설계는 둘을 한 값에 섞어 정상일에도 6시간 45분 묵은 값을 보여줬다.
    conn = FakeReadConnection(
        [(datetime(2026, 7, 31, 15, 43), False, None, "정상", None, datetime(2026, 7, 31, 9, 0, 5))]
    )
    state = db.latest_market_halt_state(conn)

    assert state["updated_at"] == datetime(2026, 7, 31, 15, 43)
    assert state["last_message_at"] == datetime(2026, 7, 31, 9, 0, 5)
    assert state["is_halted"] is False


def test_latest_market_halt_state_returns_none_when_detector_never_recorded():
    assert db.latest_market_halt_state(FakeReadConnection([])) is None


# ===== 2026-08-01(운영점검보고서 2026-07-31 §5-4/§5-5) WS 생존 신호 + 레이트리밋 호출 수 =====


def test_upsert_ws_status_writes_the_singleton_row():
    conn = FakeConnection()
    db.upsert_ws_status(conn, datetime(2026, 8, 3, 15, 43), datetime(2026, 8, 3, 7, 31), None, 0)

    assert "INSERT INTO ws_status" in conn.store["query"]
    assert "ON CONFLICT (id) DO UPDATE" in conn.store["query"]
    assert conn.store["params"][0] is True  # id


def test_latest_ws_status_returns_all_six_signals():
    conn = FakeReadConnection(
        [(
            datetime(2026, 8, 3, 15, 43), datetime(2026, 8, 3, 7, 31), datetime(2026, 8, 3, 15, 33), 0,
            datetime(2026, 8, 3, 7, 31, 4), 77,
        )]
    )
    state = db.latest_ws_status(conn)

    assert state["updated_at"] == datetime(2026, 8, 3, 15, 43)
    assert state["connected_since"] == datetime(2026, 8, 3, 7, 31)
    assert state["last_message_at"] == datetime(2026, 8, 3, 15, 33)
    assert state["reconnect_count_today"] == 0
    # 2026-08-03 §4 우선순위 4 — "구독이 성립했는가"는 "데이터가 왔는가"와 별개 신호다.
    assert state["market_op_subscribed_at"] == datetime(2026, 8, 3, 7, 31, 4)
    # 2026-08-05 P2-12(마이그레이션 026) — 관측 연속성의 선행지표. 08-05 실측 77회.
    assert state["atm_roll_count_today"] == 77


def test_latest_ws_status_keeps_unknown_atm_roll_count_as_none():
    """마이그레이션 026 미적용 구간에서는 None — 0으로 지어내면 "롤이 없었다"는 거짓말이 된다.

    호출측(`_atm_roll_churn_check`)이 이 None을 "집계 전"으로 표시하고, 미적용 사실 자체는
    스키마 정합성 배지가 따로 경고한다.
    """
    conn = FakeReadConnection(
        [(datetime(2026, 8, 3, 15, 43), None, None, 0, None, None)]
    )
    assert db.latest_ws_status(conn)["atm_roll_count_today"] is None


def test_latest_ws_status_returns_none_when_observation_loop_never_ran():
    assert db.latest_ws_status(FakeReadConnection([])) is None


# --- _select_with_optional_columns (2026-08-05, P2-10 검증 중 실측한 사고의 회귀) ----------------
#
# 마이그레이션 025/026을 커밋한 직후 라이브 DB에 적용 전 상태로 COCKPIT을 띄우니, **표시용 컬럼
# 하나 때문에 `_load_from_db()` 전체가 실패해 화면이 합성(가짜) 데이터로 떨어졌다.** 2026-07-21에
# 010/011로 겪은 사고와 같은 형태이며, 그때의 대응(장전 전체 재적용 + 스키마 배지)이 못 덮는
# 구간 — 커밋 시점 ~ 다음 기동 — 이 정확히 이 사고 구간이다.


class _UndefinedColumnOnceCursor:
    """`optional` 컬럼이 포함된 첫 쿼리만 42703으로 실패시키고, 두 번째(없는 버전)는 성공시킨다."""

    def __init__(self, marker: str, row: tuple, log: list):
        self._marker = marker
        self._row = row
        self._log = log
        self._failed = False

    def execute(self, query: str, params=None) -> None:
        self._log.append(query)
        if self._marker in query:
            self._failed = True
            raise psycopg.errors.UndefinedColumn(f'column "{self._marker}" does not exist')

    def fetchone(self):
        return None if self._failed else self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _UndefinedColumnOnceConnection:
    def __init__(self, marker: str, row: tuple):
        self._marker = marker
        self._row = row
        self.queries: list[str] = []
        self.rollback_calls = 0

    def cursor(self):
        return _UndefinedColumnOnceCursor(self._marker, self._row, self.queries)

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_select_with_optional_columns_retries_without_the_missing_column():
    conn = _UndefinedColumnOnceConnection("is_warmup", (datetime(2026, 8, 5, 12, 12), 2, [0.1] * 8, None, False))

    row = db._select_with_optional_columns(
        conn,
        base="SELECT timestamp, regime, prob_vector, higher_tf_regime, stability_flag",
        optional=("is_warmup",),
        tail=" FROM regime_state ORDER BY timestamp DESC LIMIT 1",
    )

    assert len(conn.queries) == 2  # 있는 버전 → 실패 → 없는 버전
    assert "is_warmup" in conn.queries[0]
    assert "is_warmup" not in conn.queries[1]
    assert conn.rollback_calls == 1  # 실패한 트랜잭션을 정리하지 않으면 이후 조회가 전부 죽는다
    # 반환 길이는 optional을 포함한 형태로 맞춰야 호출측 언패킹이 안 깨진다.
    assert len(row) == 6
    assert row[5] is None


def test_select_with_optional_columns_passes_through_other_errors():
    """42703이 아닌 오류는 삼키지 않는다 — 진짜 문제를 "컬럼 없음"으로 오해하면 안 된다."""

    class _BrokenCursor:
        def execute(self, query, params=None):
            raise psycopg.errors.SyntaxError("구문 오류")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _BrokenConn:
        def cursor(self):
            return _BrokenCursor()

    with pytest.raises(psycopg.errors.SyntaxError):
        db._select_with_optional_columns(
            _BrokenConn(), base="SELECT a", optional=("b",), tail=" FROM t"
        )


def test_select_with_optional_columns_returns_none_when_no_rows():
    class _EmptyCursor:
        def execute(self, query, params=None):
            pass

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _EmptyConn:
        def cursor(self):
            return _EmptyCursor()

    assert db._select_with_optional_columns(
        _EmptyConn(), base="SELECT a", optional=("b",), tail=" FROM t"
    ) is None


def test_rate_limiter_status_records_total_calls_for_demand_calculation():
    # 마이그레이션 019 — 두 행의 total_calls 차이로만 "초당 수요"가 나온다.
    conn = FakeConnection()
    db.record_rate_limiter_status(conn, datetime(2026, 8, 3, 10, 0), 1.2, 0.0, 5000)
    assert "total_calls" in conn.store["query"]
    assert conn.store["params"][-1] == 5000

    conn = FakeConnection()
    db.append_rate_limiter_status_history(conn, datetime(2026, 8, 3, 10, 0), 1.2, 0.0, 5000)
    assert "total_calls" in conn.store["query"]
    assert conn.store["params"][-1] == 5000


def test_rate_limiter_status_tolerates_missing_total_calls():
    # 019 적용 전 호출부(테스트 더블 등)와의 하위호환 — NULL 허용이라 그대로 들어간다.
    conn = FakeConnection()
    db.record_rate_limiter_status(conn, datetime(2026, 8, 3, 10, 0), 1.2, 0.0)
    assert conn.store["params"][-1] is None


def test_latest_expiry_liquidity_only_returns_today(monkeypatch):
    """2026-08-04 COCKPIT 육안 점검 — 어제 행을 오늘 것처럼 보여주면 안 된다.

    08-04 07:35 화면이 weekly_mon 만기를 2026-08-03(잔존 0일)로 표시했다. 그날 실제 북은 이미
    2026-08-10으로 롤오버돼 있었고, 만기유동성 폴러가 08:31부터 도니 그 시각엔 오늘 행이 없어
    어제 마지막 행이 나온 것이다. "잔존 0일"은 만기 당일이라는 뜻이라 오해를 부른다.
    """
    conn = FakeReadConnection([])
    monkeypatch.setattr(db, "local_now", lambda: datetime(2026, 8, 4, 7, 35))

    db.latest_expiry_liquidity(conn, "KOSPI200")

    assert "timestamp::date=%s" in conn.store["query"]
    assert conn.store["params"][-1] == date(2026, 8, 4)


def test_mark_market_op_subscribed_touches_only_its_own_column():
    # 하트비트가 쓰는 다른 컬럼(updated_at/connected_since/...)을 덮으면 안 된다.
    conn = FakeConnection()
    db.mark_market_op_subscribed(conn, datetime(2026, 8, 4, 7, 31, 3))

    query = conn.store["query"]
    assert "UPDATE ws_status SET market_op_subscribed_at=%s" in query
    assert "updated_at" not in query and "connected_since" not in query
    assert conn.committed is True


# ===== 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 6 / Fix#5) — 오늘 사용한 전략 =====


def test_entry_strategies_used_today_returns_distinct_names():
    conn = FakeReadConnection([("atm_long",), ("debit_spread",)])

    result = db.entry_strategies_used_today(conn, date(2026, 8, 6))

    assert result == frozenset({"atm_long", "debit_spread"})
    assert conn.store["params"] == (date(2026, 8, 6),)


def test_entry_strategies_used_today_counts_entries_not_allowances():
    """허용(`allowed_strategies`)이 아니라 진입(`entry_strategies`)을 세야 한다.

    허용을 세면 `wait_and_see`가 열려 있던 분까지 "전략을 썼다"로 계수돼 상한이 장 시작
    몇 분 만에 소진된다.
    """
    conn = FakeReadConnection([])
    db.entry_strategies_used_today(conn, date(2026, 8, 6))

    query = conn.store["query"]
    assert "risk_gate_state->'entry_strategies'" in query
    assert "decision = 'ENTER'" in query
    assert "allowed_strategies" not in query


def test_entry_strategies_used_today_skips_rows_without_a_json_array():
    """구버전 기록(키 없음/배열 아님)이 오늘 판단을 죽이면 안 된다 — SQL에서 걸러낸다."""
    conn = FakeReadConnection([])
    db.entry_strategies_used_today(conn, date(2026, 8, 6))

    assert "jsonb_typeof(risk_gate_state->'entry_strategies') = 'array'" in conn.store["query"]


def test_entry_strategies_used_today_is_empty_when_no_entries():
    assert db.entry_strategies_used_today(FakeReadConnection([]), date(2026, 8, 6)) == frozenset()


# ===== 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 8) — 스팟 신선도 경계 =====


def test_latest_underlying_spot_has_no_age_bound_by_default():
    """기본값은 종전 그대로다 — COCKPIT은 장전에도 전일 종가를 그 시각과 함께 보여줘야 하고,
    여기서 None을 돌려주면 `_load_from_db`가 합성 리플레이(가짜 데이터)로 빠질 위험이 있다
    (2026-07-21에 실제로 겪은 사고)."""
    conn = FakeReadConnection([(1333.77, datetime(2026, 8, 4, 15, 44))])

    assert db.latest_underlying_spot(conn, "KOSPI200") == 1333.77


def test_latest_underlying_spot_rejects_a_stale_value_when_the_bound_is_on():
    """장전에는 마지막 행이 전일 종가(약 17시간 전)다 — 신호 경로는 그것을 쓰면 안 된다."""
    conn = FakeReadConnection([(1000.03, datetime(2026, 8, 4, 15, 44))])

    result = db.latest_underlying_spot(
        conn, "KOSPI200", as_of=datetime(2026, 8, 5, 8, 50), max_age_minutes=5
    )
    assert result is None


def test_latest_underlying_spot_accepts_a_fresh_value_when_the_bound_is_on():
    conn = FakeReadConnection([(1042.91, datetime(2026, 8, 5, 9, 1))])

    result = db.latest_underlying_spot(
        conn, "KOSPI200", as_of=datetime(2026, 8, 5, 9, 3), max_age_minutes=5
    )
    assert result == 1042.91


def test_underlying_spot_bound_matches_the_chain_snapshot_window():
    """스팟과 체인은 같은 GEX 계산에 함께 들어간다 — 두 창이 어긋나면 서로 다른 시각의
    시장을 본 값으로 하나의 감마 지형을 그리게 된다."""
    assert db.UNDERLYING_SPOT_MAX_AGE_MINUTES == db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES
