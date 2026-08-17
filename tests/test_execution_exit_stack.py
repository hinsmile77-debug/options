import pytest

from mahdi.engines.regime import RegimeLabel
from mahdi.execution.exit_stack import (
    BeliefState,
    ExitLayer,
    ExitParams,
    MarketStructureState,
    PositionState,
    effective_stop_pct,
    evaluate_exit_stack,
    exit_rules_key,
    reevaluate_position,
    resolve_exit_params,
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


# ===== 2026-08-17 — 레짐 -> exit_rules 키 매핑(A) / 레짐별 손절(B) =====


def test_every_regime_label_maps_to_an_exit_rules_key():
    """8종 전부 매핑돼야 한다 — 빠진 레짐은 조용히 타임스톱 없는 포지션이 된다."""
    for label in RegimeLabel:
        assert exit_rules_key(label)


def test_expiry_day_overrides_regime_mapping():
    """만기 당일은 레짐과 무관하게 0DTE 전용 세트(v6 §11.4)."""
    assert exit_rules_key(RegimeLabel.VOL_COMPRESSION, is_expiry_day=True) == "EXPIRY_DAY_0DTE"
    assert exit_rules_key(RegimeLabel.TREND_UP_STRONG, is_expiry_day=True) == "EXPIRY_DAY_0DTE"


def test_trend_up_and_down_share_one_exit_rules_row():
    assert exit_rules_key(RegimeLabel.TREND_UP_STRONG) == exit_rules_key(RegimeLabel.TREND_DOWN_STRONG)


def test_unknown_regime_value_raises_instead_of_defaulting():
    with pytest.raises(ValueError):
        exit_rules_key(99)


def test_missing_exit_rules_row_is_reported_not_silent():
    """설정에 행이 없으면 defined=False — 종전에는 아무 표시 없이 타임스톱만 사라졌다."""
    params = resolve_exit_params("VOL_COMPRESSION", _EXIT_RULES)
    assert params.defined is False
    assert params.time_stop is None


def test_regime_stop_binds_before_absolute_hard_stop():
    """레짐 손절(-0.8%)이 절대한도(-2%)보다 타이트하면 그쪽이 먼저 문다."""
    assert effective_stop_pct(ExitParams(key="RANGE_TIGHT", stop=-0.008), -0.02) == pytest.approx(-0.008)


def test_regime_stop_never_loosens_the_absolute_limit():
    """레짐 손절이 절대한도보다 느슨해도 한도는 유지된다(안전 방향으로만 움직인다)."""
    assert effective_stop_pct(ExitParams(key="X", stop=-0.05), -0.02) == pytest.approx(-0.02)


def test_regime_stop_triggers_hard_stop_layer_with_regime_reason():
    cfg = {"RANGE_TIGHT": {"stop": -0.008, "time_stop": 30}}
    position = _position(regime="RANGE_TIGHT", entry_price=100.0, current_price=99.0)  # -1%
    decision = evaluate_exit_stack(position, MarketStructureState(), _belief(), cfg)
    assert decision.triggered_layer == ExitLayer.HARD_STOP
    assert "RANGE_TIGHT" in (decision.reason or "")


def test_regime_without_stop_keeps_previous_absolute_behaviour():
    """`stop`이 없는 레짐은 종전과 바이트 단위로 같은 동작이어야 한다."""
    position = _position(regime="TREND_STRONG", entry_price=100.0, current_price=99.0)  # -1%, -2% 미달
    decision = evaluate_exit_stack(position, MarketStructureState(), _belief(), _EXIT_RULES)
    assert decision.triggered_layer is None
