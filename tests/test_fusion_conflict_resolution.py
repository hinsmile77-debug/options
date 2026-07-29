from mahdi.fusion.conflict_resolution import resolve_conflicts
from mahdi.fusion.ensemble import EnsembleResult
from mahdi.fusion.signal_layer import MemberScores


def test_zero_consensus_direction_has_no_agreement_or_disagreement():
    result = resolve_conflicts(
        MemberScores(regime_hmm=1.0, options_flow=-1.0),
        EnsembleResult(direction=0.0, available_member_count=2),
    )
    assert result.agreement_count == 0
    assert result.disagreement_count == 0
    assert not result.has_clear_consensus


def test_all_members_agree_with_consensus():
    result = resolve_conflicts(
        MemberScores(regime_hmm=1.0, options_flow=1.0, flow_position=1.0),
        EnsembleResult(direction=1.0, available_member_count=3),
    )
    assert result.agreement_count == 3
    assert result.disagreement_count == 0
    assert result.has_clear_consensus


def test_disagreement_at_least_equal_to_agreement_breaks_consensus():
    result = resolve_conflicts(
        MemberScores(regime_hmm=1.0, options_flow=-1.0),
        EnsembleResult(direction=1.0, available_member_count=2),  # 아주 작은 양의 가중 평균이라 가정
    )
    assert result.agreement_count == 1
    assert result.disagreement_count == 1
    assert not result.has_clear_consensus  # 동조와 반대가 같으면 뚜렷한 합의 아님


def test_neutral_member_scores_are_not_counted_either_way():
    result = resolve_conflicts(
        MemberScores(regime_hmm=1.0, options_flow=0.0),
        EnsembleResult(direction=1.0, available_member_count=2),
    )
    assert result.agreement_count == 1
    assert result.disagreement_count == 0
    assert result.has_clear_consensus
