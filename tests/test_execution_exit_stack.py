from mahdi.execution.exit_stack import (
    BeliefState,
    ExitLayer,
    MarketStructureState,
    PositionState,
    evaluate_exit_stack,
    reevaluate_position,
)

_EXIT_RULES = {
    "TREND_STRONG": {"time_stop": 120},
    "RANGE_TIGHT": {"time_stop": 30},
}


def _position(**overrides) -> PositionState:
    base = dict(
        symbol="B01603955",
        side="BUY",
        entry_price=100.0,
        current_price=100.0,
        entry_time_minutes=0.0,
        now_minutes=10.0,
        regime="TREND_STRONG",
    )
    base.update(overrides)
    return PositionState(**base)


def _belief(**overrides) -> BeliefState:
    base = dict(win_probability=0.6, avg_win=2.0, avg_loss=1.0)
    base.update(overrides)
    return BeliefState(**base)


def test_forced_flat_time_overrides_everything():
    position = _position(current_price=110.0)  # 이익 중이어도
    market = MarketStructureState(is_forced_flat_time=True)
    decision = evaluate_exit_stack(position, market, _belief(), _EXIT_RULES)
    assert decision.triggered_layer == ExitLayer.FORCED_FLAT
    assert decision.action == "FULL_EXIT"


def test_hard_stop_triggers_on_long_position_loss():
    position = _position(entry_price=100.0, current_price=97.0)  # -3% > -2% 한도
    decision = evaluate_exit_stack(position, MarketStructureState(), _belief(), _EXIT_RULES)
    assert decision.triggered_layer == ExitLayer.HARD_STOP


def test_structure_stop_triggers_when_long_breaks_below_vwap():
    position = _position(current_price=99.0)
    market = MarketStructureState(vwap=100.0)
    decision = evaluate_exit_stack(position, market, _belief(), _EXIT_RULES)
    assert decision.triggered_layer == ExitLayer.STRUCTURE_STOP


def test_flow_stop_triggers_on_foreign_flow_reversal():
    market = MarketStructureState(foreign_flow_reversed=True)
    decision = evaluate_exit_stack(_position(), market, _belief(), _EXIT_RULES)
    assert decision.triggered_layer == ExitLayer.FLOW_STOP


def test_time_stop_triggers_after_regime_time_stop_elapsed():
    position = _position(regime="RANGE_TIGHT", entry_time_minutes=0.0, now_minutes=31.0)
    decision = evaluate_exit_stack(position, MarketStructureState(), _belief(), _EXIT_RULES)
    assert decision.triggered_layer == ExitLayer.TIME_STOP


def test_no_trigger_returns_hold():
    decision = evaluate_exit_stack(_position(), MarketStructureState(), _belief(), _EXIT_RULES)
    assert decision.triggered_layer is None
    assert decision.action == "HOLD"


def test_reevaluate_position_holds_with_zero_or_one_degradation():
    # EV = 0.6*2.0 - 0.4*1.0 = 0.8 > 0 -> EV 악화 아님, 나머지 플래그도 전부 False
    decision = reevaluate_position(_belief())
    assert decision.action == "HOLD"


def test_reevaluate_position_two_degradations_yield_partial_exit():
    decision = reevaluate_position(_belief(regime_degraded=True, volatility_state_mismatch=True))
    assert decision.action == "PARTIAL_EXIT_50"
    assert decision.triggered_layer == ExitLayer.BELIEF_DECAY_STOP


def test_reevaluate_position_three_degradations_yield_full_exit():
    decision = reevaluate_position(
        _belief(regime_degraded=True, volatility_state_mismatch=True, slippage_worsened=True)
    )
    assert decision.action == "FULL_EXIT"


def test_reevaluate_position_negative_ev_counts_as_one_degradation():
    # EV = 0.1*1.0 - 0.9*5.0 = -4.4 <= 0 -> EV 악화 1개 + regime_degraded 1개 = 2개
    decision = reevaluate_position(
        BeliefState(win_probability=0.1, avg_win=1.0, avg_loss=5.0, regime_degraded=True)
    )
    assert decision.action == "PARTIAL_EXIT_50"
