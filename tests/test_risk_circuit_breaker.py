from mahdi.risk.circuit_breaker import CircuitBreaker, CircuitBreakerState, MarketConditions
from mahdi.risk.limits import AccountState

_RISK_LIMITS = {
    "circuit_breaker": {
        "daily_loss_pct": -0.02,
        "drawdown_pct": -0.10,
        "vpin_crisis": 0.90,
        "vix_spike": 40,
        "usdkrw_daily_change": 0.02,
        "data_quality_fail": True,
        "model_drift": True,
    }
}


def _account(**overrides) -> AccountState:
    base = dict(
        daily_pnl_pct=0.0,
        weekly_pnl_pct=0.0,
        drawdown_pct=0.0,
        same_direction_positions=0,
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


def test_normal_conditions_stay_normal():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(), _market())
    assert decision.state == CircuitBreakerState.NORMAL
    assert decision.triggered_conditions == []
    assert not decision.requires_gradual_delever
    assert not decision.requires_emergency_flatten


def test_daily_loss_breach_halts_and_requires_emergency_flatten():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(daily_pnl_pct=-0.03), _market())
    assert decision.state == CircuitBreakerState.HALTED
    assert "daily_loss_pct" in decision.triggered_conditions
    assert decision.requires_gradual_delever
    assert decision.requires_emergency_flatten


def test_drawdown_breach_halts_and_requires_emergency_flatten():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(drawdown_pct=-0.12), _market())
    assert decision.state == CircuitBreakerState.HALTED
    assert decision.requires_emergency_flatten


def test_vpin_crisis_halts_without_emergency_flatten():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(), _market(vpin=0.95))
    assert decision.state == CircuitBreakerState.HALTED
    assert "vpin_crisis" in decision.triggered_conditions
    assert not decision.requires_emergency_flatten


def test_vix_spike_halts():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(), _market(vix=45))
    assert decision.state == CircuitBreakerState.HALTED
    assert "vix_spike" in decision.triggered_conditions


def test_usdkrw_daily_change_halts():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(), _market(usdkrw_daily_change_pct=-0.03))
    assert decision.state == CircuitBreakerState.HALTED
    assert "usdkrw_daily_change" in decision.triggered_conditions


def test_data_quality_fail_halts_and_requires_emergency_flatten():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(), _market(data_quality_ok=False))
    assert decision.state == CircuitBreakerState.HALTED
    assert decision.requires_emergency_flatten


def test_model_drift_halts_without_emergency_flatten():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    decision = cb.evaluate(_account(), _market(model_drift_detected=True))
    assert decision.state == CircuitBreakerState.HALTED
    assert not decision.requires_emergency_flatten


def test_halt_persists_after_conditions_clear_until_reset_daily():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    cb.evaluate(_account(daily_pnl_pct=-0.03), _market())
    assert cb.state == CircuitBreakerState.HALTED

    # 조건이 해소돼도 자동 NORMAL 복귀 안 됨
    decision = cb.evaluate(_account(), _market())
    assert decision.state == CircuitBreakerState.HALTED

    cb.reset_daily()
    decision_after_reset = cb.evaluate(_account(), _market())
    assert decision_after_reset.state == CircuitBreakerState.NORMAL


def test_emergency_flatten_reflects_only_this_calls_breach_not_halt_history():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    first = cb.evaluate(_account(daily_pnl_pct=-0.03), _market())
    assert first.requires_emergency_flatten

    # HALT 상태(requires_gradual_delever)는 유지되지만, 이번 호출엔 새로 발동한
    # emergency 조건이 없으므로(계좌가 정상으로 들어옴) requires_emergency_flatten은
    # 과거 이력 때문에 계속 True로 고착되지 않는다 — 매번 그 호출 시점 조건만 본다.
    second = cb.evaluate(_account(), _market())
    assert second.state == CircuitBreakerState.HALTED
    assert second.requires_gradual_delever
    assert not second.requires_emergency_flatten


def test_triggered_conditions_accumulate_across_calls():
    cb = CircuitBreaker(risk_limits=_RISK_LIMITS)
    cb.evaluate(_account(daily_pnl_pct=-0.03), _market())
    decision = cb.evaluate(_account(daily_pnl_pct=-0.03), _market(vix=45))
    assert set(decision.triggered_conditions) == {"daily_loss_pct", "vix_spike"}
