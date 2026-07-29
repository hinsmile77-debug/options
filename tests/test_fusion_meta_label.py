from mahdi.fusion.meta_label import MetaLabelInputs, TradePermission, classify

_THRESHOLDS = {
    "no_trade_max": 0.15,
    "small_test_max": 0.35,
    "standard_max": 0.65,
    "slippage_penalty_factor": 0.7,
    "gamma_regime_penalty_factor": 0.85,
    "foreign_flow_penalty_factor": 0.8,
    "event_proximity_penalty_minutes": 15,
    "event_proximity_penalty_factor": 0.5,
}


def _inputs(**overrides) -> MetaLabelInputs:
    base = dict(
        regime_confidence=1.0,
        signal_agreement_count=4,
        available_member_count=4,
    )
    base.update(overrides)
    return MetaLabelInputs(**base)


def test_perfect_agreement_and_confidence_yields_high_conviction():
    result = classify(_inputs(), _THRESHOLDS)
    assert result.conviction_score == 1.0
    assert result.trade_permission == TradePermission.HIGH_CONVICTION


def test_zero_available_members_yields_no_trade():
    result = classify(_inputs(signal_agreement_count=0, available_member_count=0), _THRESHOLDS)
    assert result.conviction_score == 0.0
    assert result.trade_permission == TradePermission.NO_TRADE


def test_partial_agreement_ratio_lands_in_standard_band():
    # regime_confidence=0.5, agreement_ratio=1.0 -> score=0.5 -> STANDARD ([0.35, 0.65))
    result = classify(_inputs(regime_confidence=0.5), _THRESHOLDS)
    assert result.conviction_score == 0.5
    assert result.trade_permission == TradePermission.STANDARD


def test_slippage_penalty_reduces_score():
    result = classify(_inputs(recent_slippage_elevated=True), _THRESHOLDS)
    assert result.conviction_score == 0.7


def test_multiple_penalties_compound_multiplicatively():
    result = classify(
        _inputs(recent_slippage_elevated=True, gamma_regime_stable=False, foreign_flow_aligned=False),
        _THRESHOLDS,
    )
    assert result.conviction_score == 1.0 * 0.7 * 0.85 * 0.8


def test_event_close_in_time_applies_penalty():
    result = classify(_inputs(event_proximity_minutes=5.0), _THRESHOLDS)
    assert result.conviction_score == 0.5


def test_event_far_in_time_has_no_penalty():
    result = classify(_inputs(event_proximity_minutes=30.0), _THRESHOLDS)
    assert result.conviction_score == 1.0


def test_recent_setup_win_rate_scales_between_half_and_full():
    zero_win_rate = classify(_inputs(recent_same_setup_win_rate=0.0), _THRESHOLDS)
    full_win_rate = classify(_inputs(recent_same_setup_win_rate=1.0), _THRESHOLDS)
    no_history = classify(_inputs(recent_same_setup_win_rate=None), _THRESHOLDS)
    assert zero_win_rate.conviction_score == 0.5
    assert full_win_rate.conviction_score == 1.0
    assert no_history.conviction_score == 1.0


def test_low_score_yields_no_trade():
    result = classify(_inputs(regime_confidence=0.1), _THRESHOLDS)
    assert result.trade_permission == TradePermission.NO_TRADE


def test_small_test_band_boundary():
    # regime_confidence=0.3 -> score=0.3 -> SMALL_TEST ([0.15, 0.35))
    result = classify(_inputs(regime_confidence=0.3), _THRESHOLDS)
    assert result.trade_permission == TradePermission.SMALL_TEST
