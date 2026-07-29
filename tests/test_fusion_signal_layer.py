from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.fusion.signal_layer import SignalInputs, build_member_scores


def _regime_state(regime: RegimeLabel, stability_flag: bool = True) -> RegimeState:
    prob_vector = [0.0] * 8
    prob_vector[regime] = 0.9
    return RegimeState(regime=regime, prob_vector=tuple(prob_vector), stability_flag=stability_flag)


def test_all_none_inputs_yield_all_none_scores():
    scores = build_member_scores(SignalInputs())
    assert scores.regime_hmm is None
    assert scores.xgboost_tabular is None
    assert scores.lstm_temporal is None
    assert scores.options_flow is None
    assert scores.orderflow_ofi_vpin is None
    assert scores.flow_position is None


def test_regime_hmm_trend_up_strong_scores_positive():
    scores = build_member_scores(SignalInputs(regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG)))
    assert scores.regime_hmm == 1.0


def test_regime_hmm_unstable_trend_is_dampened():
    scores = build_member_scores(
        SignalInputs(regime_state=_regime_state(RegimeLabel.TREND_DOWN_STRONG, stability_flag=False))
    )
    assert scores.regime_hmm == -0.5


def test_regime_hmm_range_regime_has_no_direction():
    scores = build_member_scores(SignalInputs(regime_state=_regime_state(RegimeLabel.RANGE_BALANCED)))
    assert scores.regime_hmm == 0.0


def test_xgboost_and_lstm_are_always_none():
    scores = build_member_scores(
        SignalInputs(regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG), ofi=10.0)
    )
    assert scores.xgboost_tabular is None
    assert scores.lstm_temporal is None


def test_options_flow_positive_gex_reverts_toward_flip():
    # spot(105) > gamma_flip(100), 양수 GEX -> flip 방향(하락)으로 되돌아감 = 음수 방향
    scores = build_member_scores(SignalInputs(gex=1000.0, gamma_flip=100.0, spot=105.0))
    assert scores.options_flow == -1.0


def test_options_flow_negative_gex_extends_away_from_flip():
    # spot(105) > gamma_flip(100), 음수 GEX -> flip에서 계속 멀어짐 = 양수 방향
    scores = build_member_scores(SignalInputs(gex=-1000.0, gamma_flip=100.0, spot=105.0))
    assert scores.options_flow == 1.0


def test_options_flow_charm_only_counted_when_active():
    inactive = build_member_scores(
        SignalInputs(total_charm=5.0, charm_active=False)
    )
    active = build_member_scores(SignalInputs(total_charm=5.0, charm_active=True))
    assert inactive.options_flow is None
    assert active.options_flow == 1.0


def test_orderflow_ofi_vpin_averages_ofi_and_queue_imbalance():
    scores = build_member_scores(SignalInputs(ofi=5.0, queue_imbalance=-0.2))
    assert scores.orderflow_ofi_vpin == 0.0  # +1과 -1의 평균


def test_orderflow_ofi_vpin_none_when_no_inputs():
    scores = build_member_scores(SignalInputs())
    assert scores.orderflow_ofi_vpin is None


def test_flow_position_from_foreign_net_flow_sign():
    assert build_member_scores(SignalInputs(foreign_net_flow=-500.0)).flow_position == -1.0
    assert build_member_scores(SignalInputs(foreign_net_flow=500.0)).flow_position == 1.0
    assert build_member_scores(SignalInputs()).flow_position is None
