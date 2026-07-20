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


def _no_other_macro_signals(monkeypatch):
    """compute_macro_score_proxy가 foreign_net 외 신호를 전부 '데이터 없음'으로 보게 만든다 —
    개별 신호 하나만 골라 테스트할 때 나머지가 평균에 섞이지 않게 격리한다."""
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn: None)
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_es_front_series", lambda conn, limit: [])


def test_compute_macro_score_proxy_uses_foreign_net_sign(monkeypatch):
    _no_other_macro_signals(monkeypatch)

    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: (500.0, -100.0, 100.0))
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 1.0

    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: (-500.0, 100.0, -100.0))
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == -1.0

    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: None)
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 0.0


def test_compute_macro_score_proxy_uses_vix_term_structure_sign(monkeypatch):
    _no_other_macro_signals(monkeypatch)
    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: None)

    # 콘탱고(양수)=위험선호
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn: {"vix_term_structure": 0.02})
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 1.0

    # 백워데이션(음수)=위험회피
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn: {"vix_term_structure": -0.02})
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == -1.0


def test_compute_macro_score_proxy_uses_usdkrw_trend_inverted(monkeypatch):
    # 원화 약세(USDKRW 상승 추세)는 위험회피(-1)로 뒤집어 반영돼야 한다.
    _no_other_macro_signals(monkeypatch)
    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: None)
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [1340.0, 1350.0, 1360.0])

    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == -1.0


def test_compute_macro_score_proxy_uses_es_trend_direct(monkeypatch):
    # S&P500 선물(ES) 상승 추세는 위험선호(+1)로 그대로 반영돼야 한다.
    _no_other_macro_signals(monkeypatch)
    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: None)
    monkeypatch.setattr(db, "recent_es_front_series", lambda conn, limit: [5000.0, 5050.0, 5100.0])

    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 1.0


def test_compute_macro_score_proxy_averages_multiple_signals(monkeypatch):
    # foreign_net(+1)·VIX 기간구조 백워데이션(-1)이 섞이면 평균(0.0)이 나와야 한다.
    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn: {"vix_term_structure": -0.02})
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_es_front_series", lambda conn, limit: [])

    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == pytest.approx(0.0)


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
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda conn, ts, symbol, features, version: None)
    monkeypatch.setattr(regime_pipeline, "compute_gap_zscore", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "compute_macro_score_proxy", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "latest_prior_close_regime", lambda conn: RegimeLabel.VOL_COMPRESSION)

    machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))
    state = machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert state.is_warmup is True
    assert state.regime == RegimeLabel.VOL_COMPRESSION


def test_state_machine_feeds_real_macro_series_into_cross_asset_stress(monkeypatch, tmp_path):
    # 2026-07-20: cross_asset_stress()가 더 이상 고정 스텁이 아니라 DB의 USDKRW/USDCNH/US10Y
    # 실데이터로 계산돼야 한다 — step()이 db.recent_*_series를 실제로 호출해 급변(z-score)이
    # feature_store에 적재되는 피처 벡터에 반영되는지 확인한다.
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )

    monkeypatch.setattr(db, "daily_closes", lambda conn, symbol, days: [])
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [1350.0, 1351.0, 1352.0])
    # USDCNH가 최근 급등 — 마지막 값이 baseline 대비 확 튀도록 구성.
    monkeypatch.setattr(
        db, "recent_usdcnh_series", lambda conn, limit: [6.78, 6.781, 6.779, 6.780, 6.900]
    )
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [4.50, 4.51, 4.52])

    captured: dict = {}

    def _capture(conn, ts, symbol, features, version):
        captured.update(features)

    monkeypatch.setattr(db, "insert_feature_store", _capture)
    monkeypatch.setattr(regime_pipeline, "compute_gap_zscore", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "compute_macro_score_proxy", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "latest_prior_close_regime", lambda conn: RegimeLabel.RANGE_BALANCED)

    machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))
    machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert captured["cross_asset_stress"] > 0.5  # USDCNH 급등이 반영돼 중립값(0.0)보다 뚜렷이 커야 함


def test_state_machine_switches_to_predict_after_warmup(monkeypatch, tmp_path):
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )

    class _StubEngine:
        def predict(self, features_1m):
            return RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple([1.0] + [0.0] * 7))

    machine.engine = _StubEngine()

    monkeypatch.setattr(db, "daily_closes", lambda conn, symbol, days: [])
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [])
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
