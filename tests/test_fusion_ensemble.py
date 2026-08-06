import pytest

from mahdi.fusion.ensemble import weighted_consensus
from mahdi.fusion.signal_layer import MemberScores

_ENSEMBLE_CFG = {
    "regime_hmm": {"base_w": 0.20},
    "xgboost_tabular": {"base_w": 0.20},
    "lstm_temporal": {"base_w": 0.15},
    "options_flow": {"base_w": 0.20},
    "orderflow_ofi_vpin": {"base_w": 0.15},
    "flow_position": {"base_w": 0.10},
}


def test_no_members_available_yields_neutral_zero():
    result = weighted_consensus(MemberScores(), _ENSEMBLE_CFG)
    assert result.direction == 0.0
    assert result.available_member_count == 0
    assert result.total_member_count == 6


def test_untrained_members_are_excluded_from_denominator():
    # xgboost_tabular/lstm_temporal은 항상 None — 나머지 4개(base_w 0.65)만으로 재정규화되어야 한다
    result = weighted_consensus(
        MemberScores(regime_hmm=1.0, options_flow=1.0, orderflow_ofi_vpin=1.0, flow_position=1.0),
        _ENSEMBLE_CFG,
    )
    assert result.direction == 1.0
    assert result.available_member_count == 4


def test_weighted_average_reflects_base_w_proportions():
    result = weighted_consensus(
        MemberScores(regime_hmm=1.0, options_flow=-1.0),
        _ENSEMBLE_CFG,
    )
    # (1.0*0.20 + -1.0*0.20) / (0.20+0.20) == 0.0
    assert result.direction == 0.0
    assert result.available_member_count == 2


def test_single_member_dominates_when_alone():
    result = weighted_consensus(MemberScores(flow_position=-1.0), _ENSEMBLE_CFG)
    assert result.direction == -1.0
    assert result.available_member_count == 1


def test_zero_weight_config_falls_back_to_zero_direction():
    result = weighted_consensus(MemberScores(regime_hmm=1.0), {"regime_hmm": {"base_w": 0.0}})
    assert result.direction == 0.0


# ===== 2026-08-06 고도화#2 — 실질 멤버 수(비영 점수를 낸 멤버) =====
#
# 08-06 §14-3: `regime_hmm`이 399분 전량 중립(평균 +0.0000, 강세 0 / 약세 0 / 중립 399)이었다.
# 그런데 판단 층은 `available_member_count = 4`를 세고 있었다 — 가중치를 바꿔도 답이 안 바뀌는
# 상태에서 "네 개 축을 본다"고 기록한 것이다.


def test_a_member_scoring_exactly_zero_is_available_but_not_effective():
    """**0은 중립이지 의견이 아니다.**"""
    scores = MemberScores(regime_hmm=0.0, options_flow=-0.5, flow_position=-0.7)
    result = weighted_consensus(scores, _ENSEMBLE_CFG)
    assert result.available_member_count == 3
    assert result.effective_member_count == 2


def test_effective_equals_available_when_every_member_speaks():
    scores = MemberScores(regime_hmm=0.3, options_flow=-0.5, flow_position=-0.7)
    result = weighted_consensus(scores, _ENSEMBLE_CFG)
    assert result.effective_member_count == result.available_member_count == 3


def test_none_members_count_in_neither():
    """미가용(None)과 중립(0.0)은 다른 사건이다 — 전자는 데이터가 없고 후자는 의견이 없다."""
    scores = MemberScores(regime_hmm=None, options_flow=0.0, flow_position=-0.7)
    result = weighted_consensus(scores, _ENSEMBLE_CFG)
    assert result.available_member_count == 2
    assert result.effective_member_count == 1


def test_all_neutral_members_yield_zero_effective():
    scores = MemberScores(regime_hmm=0.0, options_flow=0.0, flow_position=0.0)
    result = weighted_consensus(scores, _ENSEMBLE_CFG)
    assert result.available_member_count == 3
    assert result.effective_member_count == 0
    assert result.direction == 0.0


def test_effective_count_does_not_change_the_weighted_average():
    """이 지표는 **재기만 한다** — 방향 계산식은 손대지 않았다.

    중립 멤버가 분모(가중치 합)에는 들어가고 분자에는 0을 보태는 종전 동작 그대로여야 한다.
    여기서 값이 바뀌면 08-06 이전 확신도 시계열과 비교가 끊긴다.
    """
    scores = MemberScores(regime_hmm=0.0, options_flow=-0.5, flow_position=-0.7)
    # (0.0*0.20 + -0.5*0.20 + -0.7*0.10) / (0.20 + 0.20 + 0.10)
    assert weighted_consensus(scores, _ENSEMBLE_CFG).direction == pytest.approx(-0.34)
