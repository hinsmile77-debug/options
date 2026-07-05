import numpy as np
import pytest

from mahdi.engines.regime import FEATURE_NAMES, RegimeEngine, RegimeLabel, warmup_fallback

# GaussianHMM의 EM 수렴은 BLAS 스레딩 등 실행 환경에 따라 지역해가 달라질 수 있어(비결정적),
# 라벨 매핑·확률벡터 구성·상위 레짐 충돌 해소 같은 "우리가 작성한 로직"은 가짜 모델(stub)을
# 주입해 결정론적으로 검증한다. 실제 GaussianHMM.fit()은 별도의 가벼운 스모크 테스트로만 확인.


class _StubModel:
    """RegimeEngine이 사용하는 hmmlearn 인터페이스(predict/predict_proba/n_components/means_)만 흉내."""

    def __init__(self, n_components: int, state_seq: np.ndarray, proba: np.ndarray):
        self.n_components = n_components
        self._state_seq = state_seq
        self._proba = proba
        self.means_ = np.zeros((n_components, len(FEATURE_NAMES)))

    def predict(self, features: np.ndarray) -> np.ndarray:
        return self._state_seq

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        return self._proba


def _engine_with_stub(state_seq: np.ndarray, features: np.ndarray, proba: np.ndarray | None = None) -> RegimeEngine:
    engine = RegimeEngine()
    n_components = len(RegimeLabel)
    if proba is None:
        proba = np.eye(n_components)[state_seq]
    engine.model = _StubModel(n_components, state_seq, proba)
    engine._state_to_label = engine._calibrate_labels(features)
    engine._fitted = True
    return engine


def _feature_row(**overrides: float) -> list[float]:
    base = {"hurst": 0.5, "adx": 20.0, "rv_ratio": 1.0, "iv_chg": 0.0, "cross_asset_stress": 1.0, "book_thinning": 1.0}
    base.update(overrides)
    return [base[name] for name in FEATURE_NAMES]


def test_calibrate_labels_maps_states_by_feature_semantics():
    # 상태 0~7에 각각 특징이 뚜렷한 피처 행을 배정해 계산된 매핑이 의미에 맞는지 확인.
    rows = [
        _feature_row(rv_ratio=5.0),                          # state 0 → VOL_EXPANSION
        _feature_row(rv_ratio=0.1),                          # state 1 → VOL_COMPRESSION
        _feature_row(cross_asset_stress=9.0),                # state 2 → CRISIS_DEFENSE
        _feature_row(book_thinning=9.0),                     # state 3 → LIQUIDITY_THIN
        _feature_row(hurst=0.9),                             # state 4 → TREND_UP_STRONG (hurst 1위)
        _feature_row(hurst=0.7),                             # state 5 → TREND_DOWN_STRONG (2위)
        _feature_row(hurst=0.5),                             # state 6 → RANGE_BALANCED (3위)
        _feature_row(hurst=0.3),                             # state 7 → RANGE_BREAK_PREP (4위)
    ]
    features = np.array(rows)
    state_seq = np.arange(8)
    engine = _engine_with_stub(state_seq, features)

    assert engine._state_to_label == {
        0: RegimeLabel.VOL_EXPANSION,
        1: RegimeLabel.VOL_COMPRESSION,
        2: RegimeLabel.CRISIS_DEFENSE,
        3: RegimeLabel.LIQUIDITY_THIN,
        4: RegimeLabel.TREND_UP_STRONG,
        5: RegimeLabel.TREND_DOWN_STRONG,
        6: RegimeLabel.RANGE_BALANCED,
        7: RegimeLabel.RANGE_BREAK_PREP,
    }


def _fully_specified_8state_features() -> np.ndarray:
    # 8개 상태 전부를 사용하도록 구성 (일부 상태가 미사용이면 폴백 means_(0)이 랭킹을
    # 오염시킬 수 있음 — _calibrate_labels의 실제 동작이므로 테스트 데이터에서 피한다).
    rows = [
        _feature_row(rv_ratio=5.0),             # state 0 → VOL_EXPANSION
        _feature_row(rv_ratio=0.1),              # state 1 → VOL_COMPRESSION
        _feature_row(cross_asset_stress=9.0),    # state 2 → CRISIS_DEFENSE
        _feature_row(book_thinning=9.0),         # state 3 → LIQUIDITY_THIN
        _feature_row(hurst=0.9),                 # state 4 → TREND_UP_STRONG
        _feature_row(hurst=0.7),                 # state 5 → TREND_DOWN_STRONG
        _feature_row(hurst=0.5),                 # state 6 → RANGE_BALANCED
        _feature_row(hurst=0.3),                 # state 7 → RANGE_BREAK_PREP
    ]
    return np.array(rows)


def test_predict_reorders_proba_into_prob_vector_and_picks_argmax():
    features = _fully_specified_8state_features()
    state_seq = np.arange(8)
    engine = _engine_with_stub(state_seq, features)

    # state 0(VOL_EXPANSION)일 확률 0.9, state 1(VOL_COMPRESSION)일 확률 0.1인 상황을 가정
    proba = np.zeros((1, 8))
    proba[0, 0] = 0.9
    proba[0, 1] = 0.1
    engine.model._proba = proba
    state = engine.predict(np.zeros((1, 6)))

    assert state.regime == RegimeLabel.VOL_EXPANSION
    assert state.prob_vector[RegimeLabel.VOL_EXPANSION] == pytest.approx(0.9)
    assert state.prob_vector[RegimeLabel.VOL_COMPRESSION] == pytest.approx(0.1)
    assert sum(state.prob_vector) == pytest.approx(1.0)


def test_predict_stability_flag_false_below_threshold():
    rows = [_feature_row(rv_ratio=5.0), _feature_row(rv_ratio=0.1)]
    features = np.array(rows)
    engine = _engine_with_stub(np.array([0, 1]), features)

    proba = np.full((1, 8), 1 / 8)  # 완전히 불확실 → 최고 확률이 임계값 미만
    engine.model._proba = proba
    state = engine.predict(np.zeros((1, 6)))

    assert state.stability_flag is False


def test_predict_higher_timeframe_conflict_prefers_higher_tf():
    rows = [_feature_row(rv_ratio=5.0), _feature_row(cross_asset_stress=9.0)]
    features = np.array(rows)
    engine = _engine_with_stub(np.array([0, 1]), features)

    proba_1m = np.zeros((1, 8))
    proba_1m[0, 0] = 1.0  # VOL_EXPANSION
    proba_15m = np.zeros((1, 8))
    proba_15m[0, 1] = 1.0  # CRISIS_DEFENSE

    call_count = {"n": 0}
    real_predict_proba = engine.model.predict_proba

    def switching_predict_proba(features):
        call_count["n"] += 1
        return proba_1m if call_count["n"] == 1 else proba_15m

    engine.model.predict_proba = switching_predict_proba
    state = engine.predict(np.zeros((1, 6)), features_15m=np.zeros((1, 6)))

    assert state.higher_tf_regime == RegimeLabel.CRISIS_DEFENSE
    assert state.regime == RegimeLabel.CRISIS_DEFENSE


def test_predict_before_fit_raises():
    engine = RegimeEngine()
    with pytest.raises(RuntimeError):
        engine.predict(np.zeros((1, 6)))


def test_fit_empty_raises():
    engine = RegimeEngine()
    with pytest.raises(ValueError):
        engine.fit(np.empty((0, 6)))


def test_fit_smoke_runs_end_to_end_on_separable_data():
    # EM 지역해에 따라 정확한 클러스터-라벨 매칭은 보장되지 않지만(비결정적 아님, 단지 실행
    # 환경에 민감), fit()+predict()가 예외 없이 동작하고 결과 구조가 유효한지는 확인한다.
    rng = np.random.default_rng(0)
    centers = [
        (0.5, 25, 3.0, 0.05, 1.0, 1.0),
        (0.5, 15, 0.3, -0.02, 1.0, 1.0),
        (0.4, 30, 1.2, 0.10, 5.0, 1.5),
        (0.4, 15, 1.0, 0.0, 1.0, 5.0),
        (0.9, 35, 1.0, 0.0, 0.5, 0.5),
        (0.75, 30, 1.0, 0.0, 0.7, 0.7),
        (0.45, 14, 1.0, 0.0, 0.4, 0.4),
        (0.05, 6, 1.0, 0.0, 0.9, 1.3),
    ]
    blocks = [np.asarray(c) + rng.normal(scale=1e-6, size=(10, len(c))) for c in centers]
    features = np.vstack(blocks)

    engine = RegimeEngine(random_state=0, n_restarts=3, n_iter=50)
    engine.fit(features)
    state = engine.predict(features[-5:])

    assert isinstance(state.regime, RegimeLabel)
    assert len(state.prob_vector) == 8
    assert sum(state.prob_vector) == pytest.approx(1.0, abs=1e-6)


def test_warmup_fallback_returns_prior_when_gap_small():
    state = warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=0.5, gap_zscore=0.3)
    assert state.regime == RegimeLabel.RANGE_BALANCED
    assert state.is_warmup is True
    assert state.stability_flag is False


def test_warmup_fallback_large_positive_gap_risk_on_is_trend_up():
    state = warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=1.0, gap_zscore=2.5)
    assert state.regime == RegimeLabel.TREND_UP_STRONG


def test_warmup_fallback_large_negative_gap_risk_on_is_trend_down():
    state = warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=1.0, gap_zscore=-2.5)
    assert state.regime == RegimeLabel.TREND_DOWN_STRONG


def test_warmup_fallback_large_gap_risk_off_is_vol_expansion():
    state = warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=-1.0, gap_zscore=2.5)
    assert state.regime == RegimeLabel.VOL_EXPANSION


def test_warmup_fallback_extreme_gap_risk_off_is_crisis():
    state = warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=-1.0, gap_zscore=3.5)
    assert state.regime == RegimeLabel.CRISIS_DEFENSE
