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
