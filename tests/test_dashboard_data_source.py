from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest

from mahdi.dashboard.data_source import _synthetic_snapshot, load_snapshot
from mahdi.engines.regime import RegimeLabel


def test_synthetic_snapshot_is_flagged_not_live_and_internally_consistent():
    snap = _synthetic_snapshot(seed=42)

    assert snap.is_live is False
    assert len(snap.timestamps) == len(snap.ofi_series) == len(snap.vpin_series) == len(snap.price_series)
    assert all(0.0 <= v <= 1.0 for v in snap.vpin_series)
    assert abs(sum(snap.regime_prob.values()) - 1.0) < 1e-9
    assert snap.regime in RegimeLabel
    assert len(snap.chain) > 0


def test_synthetic_snapshot_is_deterministic_given_seed():
    a = _synthetic_snapshot(seed=7)
    b = _synthetic_snapshot(seed=7)
    assert a.spot == b.spot
    assert a.ofi_series == b.ofi_series


def test_load_snapshot_falls_back_to_synthetic_when_db_unavailable(monkeypatch):
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    snap = load_snapshot()

    assert snap.is_live is False


class _FakeCursor:
    """쿼리 문자열/파라미터로 어떤 조회인지 구분해 미리 준비한 결과를 돌려준다."""

    def __init__(self, responses: dict, query_log: list | None = None):
        self._responses = responses
        self._current: list = []
        self._query_log = query_log if query_log is not None else []

    def execute(self, query: str, params=None) -> None:
        self._query_log.append((query, params))
        if "regime_state" in query:
            self._current = self._responses["regime"]
        elif "underlying_spot_1m" in query:
            self._current = self._responses["spot"]
        elif "option_analysis_1m" in query:
            self._current = self._responses["chain"]
        elif "investor_flow_1m" in query:
            self._current = self._responses["investor_flow"]
        elif "active_futures_symbol" in query:
            self._current = self._responses["futures_symbol"]
        elif "GROUP BY symbol" in query:
            self._current = self._responses["option_symbol"]
        elif "expiry_liquidity_1m" in query:
            self._current = self._responses.get("expiry_liquidity", [])
        elif "market_raw_1m" in query and params and params[0] == self._responses.get("futures_symbol_value"):
            self._current = self._responses["futures_rows"]
        elif "market_raw_1m" in query:
            self._current = self._responses["option_rows"]
        else:
            self._current = []

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, responses: dict):
        self._responses = responses
        self.query_log: list = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._responses, self.query_log)


_BASE_RESPONSES = {
    "regime": [(datetime(2026, 7, 6, 9, 31), 2, [0.1] * 8, None, False)],
    "spot": [(1333.77,)],
    "chain": [],
    "futures_symbol": [],
    "futures_symbol_value": None,
    "futures_rows": [],
    "option_symbol": [],
    "option_rows": [],
    "investor_flow": [],
}


def test_load_snapshot_builds_live_snapshot_with_real_spot_and_chain(monkeypatch):
    # 2026-07-06 발견한 버그의 회귀 테스트: 기초자산 현재가는 underlying_spot_1m에서,
    # Gamma Map은 option_analysis_1m 체인에서 와야 한다(예전엔 market_raw_1m의 고정 라벨
    # "KOSPI200_OPT"를 잘못 "기초자산"으로 표시했었음).
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "chain": [
            (1340.0, "C", 363, 0.9, 0.0047, 1000.0, date(2026, 7, 9), ts),
            (1340.0, "P", 200, 0.85, 0.0040, -800.0, date(2026, 7, 9), ts),
        ],
        "investor_flow": [(-150.0, 250.0, -40.0)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.is_live is True
    assert snap.spot == 1333.77  # market_raw_1m의 옵션 체결가가 아니라 진짜 지수 스팟
    assert len(snap.chain) == 1  # 같은 행사가의 콜/풋이 하나로 합산됨
    assert snap.chain[0].strike == 1340.0
    assert snap.chain[0].gex == pytest.approx(200.0)  # 1000.0 + (-800.0)
    assert snap.foreign_net == -150.0
    assert snap.institution_net == 250.0
    assert snap.individual_net == -40.0


def test_load_snapshot_splits_futures_and_option_flow_series(monkeypatch):
    # 2026-07-06 발견: 선물이 WS 구독 덕에 거의 매분 체결돼 "가장 최근 활동"만으로 대표 종목을
    # 뽑으면 옵션이 영원히 안 뽑힌다 — Flow Radar는 선물/옵션 계열을 각각 따로 조회해야 한다.
    # 선물 식별은 active_futures_symbol 레지스트리로 명시적으로 한다(vpin 유무 휴리스틱은
    # 옵션에도 VPIN을 적용하면서 깨졌음).
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "futures_symbol": [("A01609",)],
        "futures_symbol_value": "A01609",
        "futures_rows": [(ts, 1271.15, 92.0, 1270.89, 0.62)],
        "option_symbol": [("B01607B38",)],
        "option_rows": [(ts, 40.65, 12.0, 40.7, 0.55)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.futures_flow_symbol == "A01609"
    assert snap.price_series == [1271.15]
    assert snap.vpin_series == [0.62]

    assert snap.option_flow_symbol == "B01607B38"
    assert snap.option_price_series == [40.65]
    assert snap.option_ofi_series == [12.0]
    assert snap.option_microprice_series == [40.7]
    assert snap.option_vpin_series == [0.55]  # 2026-07-06: 옵션도 VPIN이 실제로 계산됨


def test_load_snapshot_picks_option_flow_symbol_by_windowed_volume_with_deterministic_tiebreak(monkeypatch):
    # 2026-07-06 위클리 북 추가 후 실측: 여러 위클리 종목이 같은 1분봉 timestamp로 동시에 찍혀서
    # "ORDER BY max(timestamp) DESC"만 쓰면 동률 처리가 비결정적이라 COCKPIT 리런(10초)마다
    # 뽑히는 종목이 계속 바뀌었다(차트가 매번 다른 종목으로 바뀌어 보임). 최근 룩백 윈도 누적
    # 거래량 + symbol 오름차순 타이브레이커로 쿼리가 바뀌었는지 검증한다.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
    }
    conn = _FakeConnection(responses)

    @contextmanager
    def fake_get_connection(settings=None):
        yield conn

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    load_snapshot()

    option_queries = [(q, p) for q, p in conn.query_log if "GROUP BY symbol" in q]
    assert len(option_queries) == 1
    query, params = option_queries[0]
    assert "sum(volume) DESC" in query
    assert "symbol ASC" in query  # 동률(거래량·시각 모두 같음)까지 결정론적으로 고정하는 최종 타이브레이커
    assert "timestamp >=" in query  # 단일 최근 틱이 아니라 룩백 윈도 내 누적 활동 기준
    # 룩백 기준 시각은 datetime.now()가 아니라 스냅샷 자체의 시각(regime_state.timestamp)이어야
    # 리플레이/재현 시나리오에서도 윈도가 항상 실제 데이터 시각 기준으로 맞는다.
    assert params[-1] == ts - timedelta(minutes=10)


def test_load_snapshot_defaults_vpin_to_zero_when_null(monkeypatch):
    # 아직 등거래량 버킷이 한 번도 안 닫혔으면 vpin은 NULL — 0.0으로 안전하게 처리돼야 한다.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "futures_symbol": [("A01609",)],
        "futures_symbol_value": "A01609",
        "futures_rows": [(ts, 1271.15, 92.0, 1270.89, None)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.vpin_series == [0.0]


def test_load_snapshot_reads_expiry_liquidity_per_series(monkeypatch):
    # Phase 1.5-④(2026-07-06 추가): 먼슬리/위클리 두 북의 최신 유동성 스냅샷이 그대로 실려야 함.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "expiry_liquidity": [
            ("regular", date(2026, 7, 30), 0.041, 220.0, 480.0, 24),
            ("weekly", date(2026, 7, 9), 0.093, 70.0, 140.0, 3),
        ],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert len(snap.expiry_liquidity) == 2
    by_series = {row["series"]: row for row in snap.expiry_liquidity}
    assert by_series["regular"]["expiry"] == date(2026, 7, 30)
    assert by_series["regular"]["atm_spread_pct"] == pytest.approx(0.041)
    assert by_series["regular"]["days_to_expiry"] == 24
    assert by_series["weekly"]["depth"] == pytest.approx(70.0)
    assert by_series["weekly"]["volume"] == pytest.approx(140.0)


def test_load_snapshot_defaults_investor_flow_to_zero_when_not_yet_polled(monkeypatch):
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.is_live is True
    assert snap.foreign_net == 0.0
    assert snap.institution_net == 0.0
    assert snap.individual_net == 0.0
