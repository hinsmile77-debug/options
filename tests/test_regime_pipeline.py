import logging
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
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn, **kw: None)
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
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn, **kw: {"vix_term_structure": 0.02})
    assert regime_pipeline.compute_macro_score_proxy(conn=None, underlying="KOSPI200") == 1.0

    # 백워데이션(음수)=위험회피
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn, **kw: {"vix_term_structure": -0.02})
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
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn, **kw: {"vix_term_structure": -0.02})
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

    monkeypatch.setattr(db, "underlying_daily_closes", lambda conn, underlying, days: [])
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

    monkeypatch.setattr(db, "underlying_daily_closes", lambda conn, underlying, days: [])
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

    monkeypatch.setattr(db, "underlying_daily_closes", lambda conn, underlying, days: [])
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda conn, ts, symbol, features, version: None)

    for _ in range(_MIN_WARMUP_BARS):
        machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))

    state = machine.step(conn=None, timestamp=datetime(2026, 7, 10, 11, 0))
    assert state.regime == RegimeLabel.TREND_UP_STRONG


def test_step_caches_result_on_last_state_for_other_pollers_to_read(monkeypatch, tmp_path):
    # Signal Fusion 라이브 배선(poll_signal_fusion_cycle)이 재계산 없이 최신 레짐을 읽을 수 있도록
    # step()이 반환값을 last_state에도 남겨야 한다.
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )
    assert machine.last_state is None

    monkeypatch.setattr(db, "underlying_daily_closes", lambda conn, underlying, days: [])
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda conn, ts, symbol, features, version: None)
    monkeypatch.setattr(regime_pipeline, "compute_gap_zscore", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "compute_macro_score_proxy", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "latest_prior_close_regime", lambda conn: RegimeLabel.VOL_COMPRESSION)

    machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))
    state = machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert machine.last_state is state


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


# ===== 2026-08-03 §5-2: 피처 활성화/레짐 전이 로깅 =====


def _machine_for_logging(monkeypatch) -> RegimeStateMachine:
    machine = RegimeStateMachine(underlying="KOSPI200", futures_symbol="101S03")
    machine.engine = None  # warmup_fallback 경로 — 모델 파일 유무와 무관하게 결정론적
    monkeypatch.setattr(db, "underlying_daily_closes", lambda conn, underlying, days: [])
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda *a, **k: None)
    monkeypatch.setattr(db, "insert_regime_state", lambda *a, **k: None)
    monkeypatch.setattr(db, "latest_investor_flow", lambda conn, underlying: None)
    monkeypatch.setattr(db, "latest_macro_snapshot", lambda conn, **kw: None)
    monkeypatch.setattr(db, "recent_es_front_series", lambda conn, limit: [])
    return machine


def test_feature_activation_is_logged_once_when_it_first_leaves_neutral(monkeypatch, caplog):
    """rv_ratio가 실제로 살아나는 시점이 로그 한 줄로 남아야 한다.

    07-30에는 그 사실을 알아내려고 `feature_store` 전체 5,394행을 뒤져야 했다. 그리고
    **예상일(08-04)이 지나도 이 줄이 안 나오면 그 자체가 이상 신호**다(2026-08-03 §2-6).
    """
    machine = _machine_for_logging(monkeypatch)

    with caplog.at_level(logging.INFO, logger="mahdi.engines.regime_pipeline"):
        machine._log_neutral_escapes({"rv_ratio": 1.0, "book_thinning": 0.0})
        assert not [r for r in caplog.records if "피처 활성화" in r.getMessage()]

        machine._log_neutral_escapes({"rv_ratio": 1.34, "book_thinning": 0.0})
        first = [r for r in caplog.records if "피처 활성화" in r.getMessage()]
        machine._log_neutral_escapes({"rv_ratio": 1.41, "book_thinning": 0.0})
        second = [r for r in caplog.records if "피처 활성화" in r.getMessage()]

    assert len(first) == 1
    assert "rv_ratio" in first[0].getMessage()
    assert len(second) == 1, "피처당 평생 1건이라 두 번째부터는 남기지 않는다"


def test_feature_activation_uses_the_same_neutral_values_as_the_ops_report():
    # 리포트 지표와 로그가 다른 기준을 쓰면 어느 쪽을 믿을지 알 수 없다(README 규약).
    from mahdi.engines.regime_pipeline import _FEATURE_NEUTRAL_VALUES
    from mahdi.ops.db_metrics import _FEATURE_NEUTRAL

    for name, neutral in _FEATURE_NEUTRAL.items():
        assert _FEATURE_NEUTRAL_VALUES[name] == neutral


# ---------------------------------------------------------------------------
# 2026-08-10 — HMM 상수 출력 사고 회귀 방지.
#
# `step()`이 `predict(np.array([features]))`로 **길이 1**을 넘기고 있었다. 길이 1의 사후확률은
# `normalize(startprob ⊙ emission)`이라 전이행렬이 통째로 안 쓰이고, 그날 학습된 모델의
# `startprob_`가 one-hot이라 전 이력 8,241분이 단일 레짐으로 나왔다. 아래 테스트들은 그 호출이
# 다시 길이 1로 돌아가면 즉시 죽는다.
# ---------------------------------------------------------------------------


def _mute_db(monkeypatch):
    monkeypatch.setattr(db, "underlying_daily_closes", lambda conn, underlying, days: [])
    monkeypatch.setattr(db, "recent_usdkrw_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "recent_usdcnh_series", lambda conn, limit: [])
    monkeypatch.setattr(db, "recent_us10y_daily_series", lambda conn, days: [])
    monkeypatch.setattr(db, "insert_feature_store", lambda conn, ts, symbol, features, version: None)
    # 워밍업 구간(_MIN_WARMUP_BARS 이전)은 폴백 경로를 타므로 그쪽 DB 호출도 막아야 한다.
    monkeypatch.setattr(regime_pipeline, "compute_gap_zscore", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "compute_macro_score_proxy", lambda conn, underlying: 0.0)
    monkeypatch.setattr(regime_pipeline, "latest_prior_close_regime", lambda conn: RegimeLabel.RANGE_BALANCED)


class _RecordingEngine:
    """predict()가 받은 배열의 shape을 전부 기록한다."""

    def __init__(self):
        self.shapes = []

    def predict(self, features_1m):
        self.shapes.append(np.asarray(features_1m).shape)
        return RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple([1.0] + [0.0] * 7))


def test_step_feeds_a_growing_window_not_a_single_row(monkeypatch, tmp_path):
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )
    engine = _RecordingEngine()
    machine.engine = engine
    _mute_db(monkeypatch)

    for i in range(_MIN_WARMUP_BARS + 5):
        machine.update_bar(_bar(close=100.0 + i * 0.1, high=100.5 + i * 0.1, low=99.5 + i * 0.1))
        machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert engine.shapes, "워밍업 이후에는 predict()가 불려야 한다"
    # 핵심 단언: 길이 1이 단 한 번도 없어야 한다.
    assert all(shape[0] > 1 for shape in engine.shapes), f"길이 1 호출이 있다: {engine.shapes}"
    # 창은 봉마다 자란다.
    assert [s[0] for s in engine.shapes] == sorted(s[0] for s in engine.shapes)
    assert engine.shapes[0][0] == _MIN_WARMUP_BARS, "첫 예측은 워밍업 봉 수만큼의 창을 받는다"
    assert engine.shapes[-1][1] == len(FEATURE_NAMES)


def test_predict_window_is_capped_at_the_declared_length(monkeypatch, tmp_path):
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )
    engine = _RecordingEngine()
    machine.engine = engine
    _mute_db(monkeypatch)

    over = regime_pipeline._PREDICT_WINDOW_MINUTES + 20
    for i in range(over):
        machine.update_bar(_bar(close=100.0 + i * 0.1, high=100.5 + i * 0.1, low=99.5 + i * 0.1))
        machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert max(s[0] for s in engine.shapes) == regime_pipeline._PREDICT_WINDOW_MINUTES


def test_non_finite_features_are_kept_out_of_the_predict_window(monkeypatch, tmp_path):
    # 학습(`build_feature_matrix`)이 거르는 종류의 값을 예측이 먹으면 라이브가 학습 분포 밖에서
    # 돈다. 08-03에 cross_asset_stress 1e11이 EM을 발산시킨 그 값들이다.
    machine = RegimeStateMachine(
        underlying="KOSPI200", futures_symbol="101S03", model_path=tmp_path / "missing.pkl"
    )
    engine = _RecordingEngine()
    machine.engine = engine
    _mute_db(monkeypatch)

    poisoned = [float("inf")] * len(FEATURE_NAMES)
    monkeypatch.setattr(RegimeFeatureBuilder, "build", lambda self, *a, **kw: list(poisoned))
    for _ in range(_MIN_WARMUP_BARS + 3):
        machine.update_bar(_bar(close=100.0, high=100.5, low=99.5))
        machine.step(conn=None, timestamp=datetime(2026, 7, 10, 9, 30))

    assert not engine.shapes, "비유한 피처만 들어온 세션에서는 모델을 부르지 않고 폴백해야 한다"
    assert len(machine._predict_window) == 0


def test_replay_live_predictions_matches_the_live_window_boundary():
    # 게이트가 라이브와 **같은 축**으로 재는지 — 리플레이가 만드는 창 길이가 step()과 같아야 한다.
    class _ShapeEngine:
        def __init__(self):
            self.lengths = []

        def predict(self, window):
            self.lengths.append(len(window))
            return RegimeState(regime=RegimeLabel.RANGE_BALANCED, prob_vector=tuple([1.0] + [0.0] * 7))

    engine = _ShapeEngine()
    session = np.zeros((_MIN_WARMUP_BARS + 5, len(FEATURE_NAMES)))
    labels = regime_pipeline.replay_live_predictions(engine, [session])

    assert len(labels) == 5 + 1  # 워밍업 봉에서 한 번, 이후 5봉
    assert engine.lengths[0] == _MIN_WARMUP_BARS
    assert engine.lengths == list(range(_MIN_WARMUP_BARS, _MIN_WARMUP_BARS + 6))


def test_replay_live_predictions_does_not_bleed_across_sessions():
    # 프로세스가 매일 재기동하므로 창은 세션에서 끊긴다 — 전날 마지막 봉이 오늘 첫 봉의 문맥이
    # 되면 게이트가 라이브에 없는 정보로 판정하게 된다.
    class _ShapeEngine:
        def __init__(self):
            self.lengths = []

        def predict(self, window):
            self.lengths.append(len(window))
            return RegimeState(regime=RegimeLabel.RANGE_BALANCED, prob_vector=tuple([1.0] + [0.0] * 7))

    engine = _ShapeEngine()
    sessions = [np.zeros((_MIN_WARMUP_BARS + 2, len(FEATURE_NAMES))) for _ in range(3)]
    regime_pipeline.replay_live_predictions(engine, sessions)

    # 세션마다 창이 워밍업 길이에서 다시 시작한다.
    assert engine.lengths.count(_MIN_WARMUP_BARS) == 3
