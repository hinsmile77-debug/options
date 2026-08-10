"""저장 게이트 3겹 — 2026-08-10 HMM 상수 출력 사고의 재발 방지.

그날 두 가드가 **둘 다 다른 것을 재고 있었다**:
  - 방문 상태 검사는 전 이력을 한 시퀀스로 배치 Viterbi해 "8/8 방문"을 냈는데, 같은 모델을
    라이브 축(길이 1 호출)으로 재면 1/8이었다.
  - 수렴 가드(`monitor_.converged`)는 hmmlearn 구현상 `iter == n_iter`(반복 소진 = 비수렴)와
    음수 delta(발산)에서 **정확히 True**를 낸다.

아래 테스트는 그 두 실패 모드를 각각 재현해 게이트가 거부하는지 확인한다.
"""

from datetime import datetime

import numpy as np

from scripts.fit_regime_engine import (
    build_feature_matrix,
    check_live_diversity,
    check_startprob,
    split_sessions,
)
from mahdi.engines.regime import (
    FEATURE_NAMES,
    RegimeLabel,
    RegimeState,
    convergence_report as check_convergence,
)
from mahdi.engines.regime_pipeline import _MIN_WARMUP_BARS


class _Monitor:
    def __init__(self, history, iter_, n_iter=200, tol=0.01):
        self.history = history
        self.iter = iter_
        self.n_iter = n_iter
        self.tol = tol

    @property
    def converged(self):
        """hmmlearn 0.3.3의 구현을 그대로 옮긴 것 — 이 테스트의 대조군이다."""
        return self.iter == self.n_iter or (
            len(self.history) >= 2 and self.history[-1] - self.history[-2] < self.tol
        )


class _Model:
    def __init__(self, monitor=None, startprob=None):
        self.monitor_ = monitor
        if startprob is not None:
            self.startprob_ = np.asarray(startprob, dtype=float)


# --- 수렴 게이트 ------------------------------------------------------------


def test_convergence_gate_rejects_decreasing_log_likelihood():
    # 08-10 실행에서 실제로 나온 delta(-0.064). hmmlearn은 이것을 "수렴"이라 부른다.
    monitor = _Monitor(history=[12903.924438, 12903.860264], iter_=57)
    assert monitor.converged is True, "대조군: 옛 가드는 이것을 통과시킨다"

    ok, reason = check_convergence(_Model(monitor))
    assert ok is False
    assert "감소" in reason


def test_convergence_gate_rejects_exhausted_iterations():
    # 로그우도는 계속 오르고 있는데 반복을 다 썼다 = 아직 안 끝난 것인데 converged는 True.
    monitor = _Monitor(history=[100.0, 200.0], iter_=200, n_iter=200)
    assert monitor.converged is True, "대조군: 옛 가드는 이것을 통과시킨다"

    ok, reason = check_convergence(_Model(monitor))
    assert ok is False
    assert "다 쓰고" in reason


def test_convergence_gate_accepts_a_genuinely_converged_fit():
    # 08-10에 실제 저장된 모델의 마지막 두 값 — 증가하면서 tol 미만.
    monitor = _Monitor(history=[16282.324427, 16282.332897], iter_=92)
    ok, reason = check_convergence(_Model(monitor))
    assert ok is True
    assert "수렴" in reason


def test_convergence_gate_rejects_missing_history():
    ok, _ = check_convergence(_Model(monitor=None))
    assert ok is False


# --- startprob 게이트 -------------------------------------------------------


def test_startprob_gate_rejects_the_one_hot_collapse():
    # 08-10 재학습이 실제로 낸 값.
    ok, reason = check_startprob(_Model(startprob=[0, 0, 0, 0, 0, 1.0, 0, 0]))
    assert ok is False
    assert "one-hot" in reason


def test_startprob_gate_accepts_a_session_aware_fit():
    # 같은 데이터에 lengths=(21세션)만 넘겨 재학습했을 때 실제로 나온 분포.
    ok, _ = check_startprob(_Model(startprob=[0, 0.1928, 0, 0, 0.0476, 0.2857, 0.331, 0.1429]))
    assert ok is True


# --- 라이브 리플레이 게이트 -------------------------------------------------


class _ConstantEngine:
    """어떤 창을 받아도 같은 레짐을 내는 엔진 — 08-10 사고의 재현."""

    def predict(self, window):
        return RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple([1.0] + [0.0] * 7))


class _AlternatingEngine:
    """창 길이에 따라 레짐이 바뀌는 엔진 — 정상 모델의 대역."""

    def predict(self, window):
        regime = RegimeLabel.TREND_UP_STRONG if len(window) % 2 else RegimeLabel.RANGE_BALANCED
        return RegimeState(regime=regime, prob_vector=tuple([1.0] + [0.0] * 7))


def _sessions(n_sessions=2, bars=_MIN_WARMUP_BARS + 10):
    return [np.zeros((bars, len(FEATURE_NAMES))) for _ in range(n_sessions)]


def test_live_diversity_gate_rejects_a_constant_model():
    ok, reason = check_live_diversity(_ConstantEngine(), _sessions())
    assert ok is False
    assert "1종" in reason


def test_live_diversity_gate_accepts_a_varying_model():
    ok, reason = check_live_diversity(_AlternatingEngine(), _sessions())
    assert ok is True
    assert "2종" in reason


def test_live_diversity_gate_rejects_when_there_is_nothing_to_replay():
    # 모든 세션이 워밍업 길이 이하 — "통과"로 셀 근거가 없다.
    short = [np.zeros((_MIN_WARMUP_BARS - 5, len(FEATURE_NAMES)))]
    ok, _ = check_live_diversity(_ConstantEngine(), short)
    assert ok is False


# --- 세션 분할 --------------------------------------------------------------


def _history_row(day, minute, value=1.0):
    return (
        datetime(2026, 8, day, 9, minute),
        {name: value for name in FEATURE_NAMES},
    )


def test_build_feature_matrix_counts_sessions_by_date():
    history = [_history_row(3, 0), _history_row(3, 1), _history_row(4, 0)]
    features, lengths = build_feature_matrix(history)
    assert len(features) == 3
    assert lengths == [2, 1]


def test_lengths_count_surviving_rows_only():
    """필터로 빠진 행을 세면 sum(lengths) != len(features)가 되어 fit()이 ValueError를 낸다.

    08-10 실측에서 8,241행 중 19행이 필터에 걸렸다 — 원본 날짜로 세면 그만큼 어긋난다.
    """
    bad = (datetime(2026, 8, 3, 9, 2), {name: float("inf") for name in FEATURE_NAMES})
    history = [_history_row(3, 0), bad, _history_row(3, 1), _history_row(4, 0)]
    features, lengths = build_feature_matrix(history)

    assert sum(lengths) == len(features), "이 항등식이 깨지면 fit()이 거부한다"
    assert lengths == [2, 1]


def test_lengths_skip_a_day_that_is_entirely_filtered_out():
    # 하루치가 통째로 걸리면 그 세션은 없는 것이지 길이 0인 세션이 아니다.
    bad_day = [
        (datetime(2026, 8, 4, 9, i), {name: float("nan") for name in FEATURE_NAMES}) for i in range(3)
    ]
    history = [_history_row(3, 0), *bad_day, _history_row(5, 0)]
    features, lengths = build_feature_matrix(history)

    assert lengths == [1, 1]
    assert 0 not in lengths
    assert sum(lengths) == len(features)


def test_split_sessions_round_trips_the_lengths():
    features = np.arange(12, dtype=float).reshape(6, 2)
    sessions = split_sessions(features, [4, 2])
    assert [len(s) for s in sessions] == [4, 2]
    assert np.array_equal(np.vstack(sessions), features)


# --- 두 게이트의 역할 분담 (2026-08-10 음성 대조에서 나온 것) ----------------
#
# 붕괴 모델(startprob one-hot)을 실제로 넣어 보면:
#   window=120 (R3 적용 후 라이브) → 8종, 리플레이 게이트 **통과**
#   window=1   (R3 회귀 = 그날 라이브) → 1종, 리플레이 게이트 **거부**
# 즉 startprob 게이트는 학습 결함을, 리플레이 게이트는 호출 경로 회귀를 잡는다.
# 어느 하나로 다른 쪽을 대신하려 들면 "한 축만 재고 통과"가 다시 생긴다.


class _CollapsedEngine:
    """startprob이 one-hot인 모델의 대역 — 창을 받으면 갈리고, 길이 1이면 상수다."""

    def predict(self, window):
        regime = (
            RegimeLabel.TREND_UP_STRONG
            if len(window) == 1
            else list(RegimeLabel)[len(window) % len(RegimeLabel)]
        )
        return RegimeState(regime=regime, prob_vector=tuple([1.0] + [0.0] * 7))


def test_replay_gate_catches_a_regression_back_to_single_row_calls():
    from mahdi.engines.regime_pipeline import replay_live_predictions

    sessions = _sessions()
    collapsed = _CollapsedEngine()

    windowed = {label.name for label in replay_live_predictions(collapsed, sessions)}
    single = {label.name for label in replay_live_predictions(collapsed, sessions, window=1)}

    assert len(windowed) > 1, "창을 넘기면 모델의 정보가 살아난다"
    assert single == {"TREND_UP_STRONG"}, "길이 1로 돌아가면 상수 — 게이트가 이것을 잡아야 한다"


def test_startprob_gate_is_the_one_that_catches_the_training_defect():
    # 리플레이 게이트가 통과시키는 모델도 startprob 게이트는 거부해야 한다 — 둘의 역할이 다르다.
    ok_replay, _ = check_live_diversity(_CollapsedEngine(), _sessions())
    ok_startprob, _ = check_startprob(_Model(startprob=[0, 0, 0, 0, 0, 1.0, 0, 0]))

    assert ok_replay is True
    assert ok_startprob is False


# --- iv_chg 재계산 (2026-08-10, 사용자 결정 (b)) -----------------------------
#
# `feature_store`는 **고치지 않는다.** 학습 입력에서만 먼슬리 단독 값으로 갈아 끼우고,
# 그 사실을 로그·모델 메타데이터·리포트 세 곳에 남긴다.


def test_build_feature_matrix_substitutes_iv_chg_without_touching_other_features():
    iv_index = FEATURE_NAMES.index("iv_chg")
    history = [_history_row(3, 0), _history_row(3, 1)]
    override = {history[0][0]: -0.25}

    features, _ = build_feature_matrix(history, override)

    assert features[0][iv_index] == -0.25, "대체된 분"
    assert features[1][iv_index] == 1.0, "대체 못 한 분은 DB 값 그대로"
    # 나머지 열은 손대지 않는다.
    for col in range(len(FEATURE_NAMES)):
        if col != iv_index:
            assert features[0][col] == features[1][col]


def test_build_feature_matrix_leaves_history_untouched():
    """DB에서 읽어 온 dict를 in-place로 고치면 호출측이 모르는 사이 원본이 바뀐다."""
    history = [_history_row(3, 0)]
    original = dict(history[0][1])
    build_feature_matrix(history, {history[0][0]: -0.9})
    assert history[0][1] == original


def test_substituted_iv_chg_still_passes_the_range_filter():
    # 재계산 값도 `_MAX_ABS_FEATURE_VALUE` 필터를 통과해야 행이 살아남는다 —
    # 대체가 조용히 행을 죽이면 lengths와 features가 어긋난다.
    history = [_history_row(3, 0), _history_row(3, 1)]
    features, lengths = build_feature_matrix(history, {history[0][0]: 1e9})

    assert len(features) == 1, "범위를 넘는 대체값은 그 행을 제외한다"
    assert sum(lengths) == len(features), "제외 후에도 항등식이 유지돼야 한다"
