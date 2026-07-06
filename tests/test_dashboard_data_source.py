from contextlib import contextmanager
from datetime import date, datetime

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
    """쿼리 문자열 안의 테이블/조건 키워드로 어떤 조회인지 구분해 미리 준비한 결과를 돌려준다."""

    def __init__(self, responses: dict):
        self._responses = responses
        self._current: list = []

    def execute(self, query: str, params=None) -> None:
        if "regime_state" in query:
            self._current = self._responses["regime"]
        elif "underlying_spot_1m" in query:
            self._current = self._responses["spot"]
        elif "option_analysis_1m" in query:
            self._current = self._responses["chain"]
        elif "investor_flow_1m" in query:
            self._current = self._responses["investor_flow"]
        elif "GROUP BY symbol" in query:
            self._current = self._responses["flow_symbol"]
        elif "market_raw_1m" in query:
            self._current = self._responses["market_rows"]
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

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._responses)


def test_load_snapshot_builds_live_snapshot_with_real_spot_and_chain(monkeypatch):
    # 2026-07-06 발견한 버그의 회귀 테스트: 기초자산 현재가는 underlying_spot_1m에서,
    # Gamma Map은 option_analysis_1m 체인에서, Flow Radar는 가장 활발한 실제 종목에서 와야 한다
    # (예전엔 market_raw_1m의 고정 라벨 "KOSPI200_OPT"를 잘못 "기초자산"으로 표시했었음).
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "spot": [(1333.77,)],
        "chain": [
            (1340.0, "C", 363, 0.9, 0.0047, 1000.0, date(2026, 7, 9), ts),
            (1340.0, "P", 200, 0.85, 0.0040, -800.0, date(2026, 7, 9), ts),
        ],
        "flow_symbol": [("B01607B38",)],
        "market_rows": [(ts, 40.65, 12.0, 40.7)],
        "investor_flow": [(-150.0, 250.0, -40.0)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.is_live is True
    assert snap.spot == 1333.77  # market_raw_1m의 옵션 체결가가 아니라 진짜 지수 스팟
    assert snap.flow_radar_symbol == "B01607B38"
    assert len(snap.chain) == 1  # 같은 행사가의 콜/풋이 하나로 합산됨
    assert snap.chain[0].strike == 1340.0
    assert snap.chain[0].gex == pytest.approx(200.0)  # 1000.0 + (-800.0)
    assert snap.foreign_net == -150.0
    assert snap.institution_net == 250.0
    assert snap.individual_net == -40.0


def test_load_snapshot_defaults_investor_flow_to_zero_when_not_yet_polled(monkeypatch):
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "spot": [(1333.77,)],
        "chain": [],
        "flow_symbol": [],
        "market_rows": [],
        "investor_flow": [],  # poll_investor_flow가 아직 한 번도 안 돈 경우
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
