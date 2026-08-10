import logging

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


def test_save_before_fit_raises(tmp_path):
    engine = RegimeEngine()
    with pytest.raises(RuntimeError):
        engine.save(tmp_path / "model.pkl")


def test_save_load_roundtrip_predicts_identically(tmp_path):
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
    model_path = tmp_path / "regime_engine.pkl"
    engine.save(model_path)

    loaded = RegimeEngine.load(model_path)
    original_state = engine.predict(features[-5:])
    loaded_state = loaded.predict(features[-5:])

    assert loaded_state.regime == original_state.regime
    assert loaded_state.prob_vector == original_state.prob_vector


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RegimeEngine.load(tmp_path / "does_not_exist.pkl")


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


# ---------------------------------------------------------------------------
# 2026-08-10 — startprob one-hot 붕괴 회귀 방지.
# ---------------------------------------------------------------------------


def _synthetic_sessions(n_sessions=6, bars=60, seed=0):
    """레짐이 서로 다른 세션들 — 세션 경계가 살아 있으면 startprob이 여러 상태에 퍼진다."""
    rng = np.random.default_rng(seed)
    sessions = []
    for s in range(n_sessions):
        centre = np.array([0.3 + 0.1 * (s % 3), 20.0 + 15 * (s % 3), 0.8 + 0.2 * (s % 3),
                           0.0, 1.0 * (s % 3), 1.0 * (s % 2)])
        sessions.append(centre + rng.normal(0, 0.05, size=(bars, len(FEATURE_NAMES))))
    return sessions


def test_fit_with_lengths_keeps_startprob_from_collapsing_to_one_hot():
    """`lengths`가 startprob 추정의 표본 수다 — 없으면 시작점이 1개라 one-hot으로 붕괴한다."""
    sessions = _synthetic_sessions()
    features = np.vstack(sessions)
    lengths = [len(s) for s in sessions]

    engine = RegimeEngine(n_restarts=2, n_iter=50)
    engine.fit(features, lengths)

    nonzero = int((engine.model.startprob_ > 1e-12).sum())
    assert nonzero >= 2, f"startprob_ 비영이 {nonzero}개 — 08-10 사고와 같은 붕괴다"


def test_fit_rejects_lengths_that_do_not_sum_to_the_matrix():
    # 필터로 빠진 행을 안 다시 센 경우 — 조용히 틀리는 대신 즉시 죽어야 한다.
    features = np.zeros((10, len(FEATURE_NAMES)))
    with pytest.raises(ValueError, match="lengths 합"):
        RegimeEngine(n_restarts=1, n_iter=5).fit(features, [4, 4])


def test_fit_without_lengths_warns_that_startprob_may_collapse(caplog):
    # `lengths=None`은 테스트·단발 실험용 하위호환으로만 남긴다 — 운영 경로가 실수로 이리 오면
    # 로그에 흔적이 남아야 한다(08-10 사고는 이 경로로 학습된 모델이 만들었다).
    features = np.vstack(_synthetic_sessions())
    with caplog.at_level(logging.WARNING, logger="mahdi.engines.regime"):
        RegimeEngine(n_restarts=1, n_iter=20).fit(features)
    assert any("one-hot" in r.getMessage() for r in caplog.records)


def _single_row_warnings(caplog):
    return [r for r in caplog.records if "길이 1" in r.getMessage()]


def test_predict_warns_once_when_given_a_single_row(caplog):
    """길이 1 시퀀스는 전이행렬을 안 쓴다 — 라이브에서 이 경고가 뜨면 그 자체가 회귀 신호다."""
    rows = np.array([_feature_row(rv_ratio=1.0 + i) for i in range(len(RegimeLabel))])
    engine = _engine_with_stub(np.arange(len(RegimeLabel)), rows)

    with caplog.at_level(logging.WARNING, logger="mahdi.engines.regime"):
        engine.predict(rows[:1])
        engine.predict(rows[:1])

    assert len(_single_row_warnings(caplog)) == 1, "인스턴스당 1회만 — 매분 호출되는 경로다"


def test_predict_does_not_warn_for_a_window(caplog):
    rows = np.array([_feature_row(rv_ratio=1.0 + i) for i in range(len(RegimeLabel))])
    engine = _engine_with_stub(np.arange(len(RegimeLabel)), rows)

    with caplog.at_level(logging.WARNING, logger="mahdi.engines.regime"):
        engine.predict(rows)

    assert not _single_row_warnings(caplog)


def test_save_and_load_round_trip_training_metadata(tmp_path):
    """학습 출처는 모델 **안에** 남아야 한다 — 로그는 지워지고 사람의 기억은 흐려진다."""
    rows = np.array([_feature_row(rv_ratio=1.0 + i) for i in range(len(RegimeLabel))])
    engine = _engine_with_stub(np.arange(len(RegimeLabel)), rows)
    path = tmp_path / "m.pkl"

    engine.save(path, metadata={"iv_chg_source": "먼슬리 단독 재계산", "db_rows_modified": False})
    restored = RegimeEngine.load(path)

    assert restored.metadata["iv_chg_source"] == "먼슬리 단독 재계산"
    assert restored.metadata["db_rows_modified"] is False


def test_load_tolerates_models_saved_before_metadata_existed(tmp_path):
    # 2026-08-10 이전 pickle에는 "metadata" 키가 없다 — 없다고 죽으면 안 되고,
    # 지어내서도 안 된다(빈 dict = "모른다").
    import pickle

    rows = np.array([_feature_row(rv_ratio=1.0 + i) for i in range(len(RegimeLabel))])
    engine = _engine_with_stub(np.arange(len(RegimeLabel)), rows)
    path = tmp_path / "old.pkl"
    with open(path, "wb") as f:
        pickle.dump({"model": engine.model, "state_to_label": engine._state_to_label}, f)

    assert RegimeEngine.load(path).metadata == {}


class _FitProbeHMM:
    """`fit()`의 후보 선택만 검증하기 위한 가짜 GaussianHMM.

    seed에 따라 (점수, 수렴 여부)를 정해 두고, `fit()`이 **점수 최고**가 아니라
    **수렴한 것 중 점수 최고**를 고르는지 본다.
    """

    plan: dict = {}

    def __init__(self, n_components, covariance_type, random_state, n_iter):
        self.n_components = n_components
        self.random_state = random_state
        self.n_iter = n_iter
        score, converged = self.plan[random_state]
        self._score = score
        history = [10.0, 10.0 + (0.001 if converged else -0.5)]
        self.monitor_ = type("M", (), {
            "history": history, "iter": 5, "n_iter": n_iter, "tol": 0.01,
        })()
        self.startprob_ = np.full(n_components, 1 / n_components)
        self.means_ = np.zeros((n_components, len(FEATURE_NAMES)))

    def fit(self, features, lengths=None):
        return self

    def score(self, features, lengths=None):
        return self._score

    def predict(self, features, lengths=None):
        return np.arange(len(features)) % self.n_components


def _fit_with_plan(monkeypatch, plan, n_restarts):
    from mahdi.engines import regime as regime_module

    _FitProbeHMM.plan = plan
    monkeypatch.setattr(regime_module, "GaussianHMM", _FitProbeHMM)
    engine = RegimeEngine(random_state=0, n_restarts=n_restarts, n_iter=50)
    engine.fit(np.zeros((len(RegimeLabel), len(FEATURE_NAMES))), [len(RegimeLabel)])
    return engine


def test_fit_prefers_a_converged_candidate_over_a_higher_scoring_diverged_one(monkeypatch):
    """2026-08-10 — 재계산 피처로 학습했을 때 **승자가 delta=−0.32로 발산한 후보**였다.

    점수만 보면 발산 중인 후보가 이길 수 있다(로그우도가 줄어드는 중이라 마지막 값이 우연히
    높을 수 있다). 그러면 저장 게이트가 런 전체를 거부한다 — 멀쩡한 후보가 있었는데도.
    """
    engine = _fit_with_plan(monkeypatch, {0: (100.0, False), 1: (50.0, True)}, n_restarts=2)
    assert engine.model.random_state == 1, "수렴한 후보를 골라야 한다"


def test_fit_falls_back_to_the_best_diverged_candidate_when_none_converged(monkeypatch, caplog):
    # 물러설 때는 조용히 하지 않는다 — 저장 게이트가 거부할 것이라고 미리 말한다.
    with caplog.at_level(logging.WARNING, logger="mahdi.engines.regime"):
        engine = _fit_with_plan(monkeypatch, {0: (10.0, False), 1: (99.0, False)}, n_restarts=2)

    assert engine.model.random_state == 1, "비수렴끼리는 점수 최고"
    assert any("수렴한 후보가 없다" in r.getMessage() for r in caplog.records)


def test_fit_still_picks_the_best_score_among_converged_candidates(monkeypatch):
    engine = _fit_with_plan(monkeypatch, {0: (10.0, True), 1: (99.0, True), 2: (50.0, True)}, n_restarts=3)
    assert engine.model.random_state == 1
