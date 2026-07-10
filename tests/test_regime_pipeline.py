from datetime import datetime

import numpy as np
import pytest

from mahdi.data import db
from mahdi.data.collector import MinuteBar
from mahdi.engines import regime_pipeline
from mahdi.engines.regime import FEATURE_NAMES, RegimeLabel, RegimeState
from mahdi.engines.regime_pipeline import RegimeFeatureBuilder, RegimeStateMachine, _MIN_WARMUP_BARS


def _bar(close: float, high: float, low: float, spread: float = 0.5) -> MinuteBar:
    return MinuteBar(
        minute=datetime(2026, 7, 10, 9, 30),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        vwap=close,
        ofi=0.0,
        microprice=close,
        bid_ask_spread=spread,
        buy_volume=50.0,
        sell_volume=50.0,
        quality_flag=0,
    )


def test_feature_builder_returns_features_in_declared_order():
    builder = RegimeFeatureBuilder()
    for i in range(5):
        builder.update_bar(_bar(close=100.0 + i, high=100.5 + i, low=99.5 + i))
    builder.update_iv(0.2)

    features = builder.build(daily_closes=[])
    assert len(features) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in features)


def test_compute_macro_score_proxy_uses_foreign_net_sign(monkeypatch):
    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: (500.0, -100.0, 100.0))
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 1.0

    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: (-500.0, 100.0, -100.0))
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == -1.0

    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: None)
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 0.0


def test_latest_prior_close_regime_falls_back_to_range_balanced(monkeypatch):
    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: None)
    assert regime_pipeline.latest_prior_close_regime(conn=None) == RegimeLabel.RANGE_BALANCED

    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: int(RegimeLabel.CRISIS_DEFENSE))
    assert regime_pipeline.latest_prior_close_regime(conn=None) == RegimeLabel.CRISIS_DEFENSE


def test_state_machine_uses_warmup_fallback_when_no_model(monkeypatch, tmp_path):
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )
    assert machine.engine is None

    monkeypatch.setattr(db, "daily_closes", lambda conn, symbol, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda conn, ts, symbol, features, version: None)
    monkeypatch.setattr(regime_pipeline, "compute_gap_zscore", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "compute_macro_score_proxy", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "latest_prior_close_regime", lambda conn: RegimeLabel.VOL_COMPRESSION)

    machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))
    state = machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert state.is_warmup is True
    assert state.regime == RegimeLabel.VOL_COMPRESSION


def test_state_machine_switches_to_predict_after_warmup(monkeypatch, tmp_path):
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )

    class _StubEngine:
        def predict(self, features_1m):
            return RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple([1.0] + [0.0] * 7))

    machine.engine = _StubEngine()

    monkeypatch.setattr(db, "daily_closes", lambda conn, symbol, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda conn, ts, symbol, features, version: None)

    for _ in range(_MIN_WARMUP_BARS):
        machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))

    state = machine.step(conn=None, timestamp=datetime(2026, 7, 10, 11, 0))
    assert state.regime == RegimeLabel.TREND_UP_STRONG


class _FakeCursor:
    def __init__(self, results: list):
        self._results = results  # 같은 리스트 참조 — 커넥션당 여러 cursor() 호출이 큐를 공유해야 함
        self._current = None

    def execute(self, query, params=None):
        self._current = self._results.pop(0)

    def fetchone(self):
        return self._current

    def fetchall(self):
        return self._current or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, results: list):
        self._results = results

    def cursor(self):
        return _FakeCursor(self._results)


def test_compute_gap_zscore_uses_prev_close_and_atm_iv():
    conn = _FakeConnection(
        [
            (datetime(2026, 7, 9, 15, 45), 350.0),  # 전일 마지막 스팟
            (355.0,),  # 오늘 첫 스팟
            [(0.2,), (0.2,)],  # ATM IV(콜/풋)
        ]
    )
    z = regime_pipeline.compute_gap_zscore(conn, "KOSPI200")
    expected_move = 350.0 * 0.2 * (1 / 365) ** 0.5
    assert z == pytest.approx((355.0 - 350.0) / expected_move)


def test_compute_gap_zscore_no_prior_day_returns_zero():
    conn = _FakeConnection([None, (355.0,)])
    assert regime_pipeline.compute_gap_zscore(conn, "KOSPI200") == 0.0
