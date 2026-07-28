from mahdi.risk.circuit_breaker import CircuitBreakerState, MarketConditions
from mahdi.risk.engine import RiskEngine
from mahdi.risk.limits import AccountState
from mahdi.risk.sizing import PositionSizingInput

_RISK_LIMITS = {
    "sizing": {"kelly_fraction": 0.25, "max_kelly_fraction": 0.25},
    "limits": {
        "per_trade_loss_pct": -0.005,
        "daily_loss_pct": -0.02,
        "weekly_loss_pct": -0.05,
        "max_drawdown_pct": -0.10,
        "max_same_direction_positions": 3,
        "max_daily_trades_per_strategy": None,
    },
    "circuit_breaker": {
        "daily_loss_pct": -0.02,
        "drawdown_pct": -0.10,
        "vpin_crisis": 0.90,
        "vix_spike": 40,
        "usdkrw_daily_change": 0.02,
        "data_quality_fail": True,
        "model_drift": True,
    },
}


def _account(**overrides) -> AccountState:
    base = dict(
        daily_pnl_pct=0.0,
        weekly_pnl_pct=0.0,
        drawdown_pct=0.0,
        same_direction_positions=0,
        daily_trades_by_strategy={},
        pending_trade_loss_pct=0.0,
    )
    base.update(overrides)
    return AccountState(**base)


def _market(**overrides) -> MarketConditions:
    base = dict(
        vpin=0.0,
        vix=0.0,
        usdkrw_daily_change_pct=0.0,
        data_quality_ok=True,
        model_drift_detected=False,
    )
    base.update(overrides)
    return MarketConditions(**base)


def _sizing_input(**overrides) -> PositionSizingInput:
    base = dict(
        base_size=100.0,
        regime_confidence=1.0,
        signal_quality=1.0,
        target_vol=0.01,
        realized_vol=0.01,
        liquidity_score=1.0,
        drawdown_pct=0.0,
        portfolio_capacity_remaining_pct=1.0,
    )
    base.update(overrides)
    return PositionSizingInput(**base)


def test_healthy_state_approves_and_sizes():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market()
    )
    assert decision.approved
    assert decision.approved_size > 0
    assert decision.reject_reasons == []


def test_circuit_breaker_halt_rejects_entirely_with_zero_size():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(daily_pnl_pct=-0.03), "vrp_harvest", _market()
    )
    assert not decision.approved
    assert decision.approved_size == 0.0
    assert any(r.startswith("circuit_breaker:daily_loss_pct") for r in decision.reject_reasons)


def test_limit_violation_rejects_even_when_circuit_breaker_normal():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(),
        _account(same_direction_positions=3),
        "vrp_harvest",
        _market(),
    )
    assert not decision.approved
    assert decision.approved_size == 0.0
    assert any(r.startswith("max_same_direction_positions") for r in decision.reject_reasons)


def test_both_circuit_breaker_and_limit_violations_reported_together():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(),
        _account(daily_pnl_pct=-0.03, same_direction_positions=3),
        "vrp_harvest",
        _market(),
    )
    assert not decision.approved
    reasons_joined = " ".join(decision.reject_reasons)
    assert "circuit_breaker:daily_loss_pct" in reasons_joined
    assert "max_same_direction_positions" in reasons_joined


def test_evaluate_ongoing_reflects_circuit_breaker_state():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_ongoing(_account(drawdown_pct=-0.15), _market())
    assert decision.state == CircuitBreakerState.HALTED
    assert decision.requires_emergency_flatten


def test_engine_reuses_same_circuit_breaker_instance_across_calls():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    engine.evaluate_entry(_sizing_input(), _account(daily_pnl_pct=-0.03), "vrp_harvest", _market())
    # 조건이 해소돼도 같은 엔진 인스턴스에서는 HALT가 유지되어야 한다
    decision = engine.evaluate_entry(_sizing_input(), _account(), "vrp_harvest", _market())
    assert not decision.approved
    assert engine.circuit_breaker.state == CircuitBreakerState.HALTED


def test_unconfigured_portfolio_greeks_check_surfaced_on_approval():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market()
    )
    assert decision.approved
    assert "portfolio_greeks" in decision.unconfigured_checks
