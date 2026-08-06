from datetime import datetime, time as dtime, timedelta

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


def test_market_halted_rejects_entirely_before_circuit_breaker_or_limits():
    # 거래소 서킷브레이커/거래정지 중에는 사이징/한도 계산 자체가 무의미하므로 즉시 전량 거부하고,
    # 내부 CircuitBreaker(daily_loss 등)나 한도 위반 여부는 아예 평가하지 않는다(reject_reasons가
    # "market_halt" 단일 사유여야 함 — circuit_breaker.py의 상태를 오염시키지 않는지도 확인).
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(), market_halted=True
    )
    assert not decision.approved
    assert decision.approved_size == 0.0
    assert decision.reject_reasons == ["market_halt"]
    assert engine.circuit_breaker.state == CircuitBreakerState.NORMAL


def test_market_halted_false_falls_through_to_normal_evaluation():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(), market_halted=False
    )
    assert decision.approved
    assert decision.approved_size > 0


def test_unconfigured_portfolio_greeks_check_surfaced_on_approval():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market()
    )
    assert decision.approved
    assert "portfolio_greeks" in decision.unconfigured_checks


# ===== 2026-08-06 §2-2 / Fix#1 — v6 §4.2 신규 진입 컷오프(14:50) =====
#
# 08-06 실측: 14:50 초과 ENTER 21건, 그중 15:10 강제 평탄화 이후 18건(마지막 15:30).
# 청산 쪽 15:10은 `execution/exit_stack.py`에 있었는데 진입 쪽 게이트는 코드에 없었다.


def test_entry_after_cutoff_is_rejected_before_anything_else():
    """컷오프는 `market_halted`보다도 앞이다 — halt 상태에 따라 사유가 바뀌면 안 된다.

    사유가 흔들리면 `signal_decisions.reject_reason` 시계열로 이 게이트를 셀 수 없다.
    """
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(daily_pnl_pct=-0.03), "vrp_harvest", _market(),
        market_halted=True, now=dtime(15, 30),
    )
    assert not decision.approved
    assert decision.approved_size == 0.0
    assert decision.reject_reasons == ["entry_cutoff"]
    # 내부 CircuitBreaker를 건드리지 않았는지 — market_halt와 같은 계약이다.
    assert engine.circuit_breaker.state == CircuitBreakerState.NORMAL


def test_entry_at_cutoff_boundary_is_rejected():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    assert engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(), now=dtime(14, 50)
    ).reject_reasons == ["entry_cutoff"]


def test_entry_before_cutoff_falls_through_to_normal_evaluation():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(), now=dtime(14, 49)
    )
    assert decision.approved
    assert decision.approved_size > 0


def test_entry_cutoff_accepts_datetime():
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    decision = engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(),
        now=datetime(2026, 8, 6, 15, 11),
    )
    assert decision.reject_reasons == ["entry_cutoff"]


def test_omitting_now_skips_the_time_gate():
    """`now=None`은 시각 게이트를 건너뛴다 — 기존 호출측/백테스트를 깨지 않기 위한 계약이다.

    **라이브 경로가 이 기본값에 기대면 안 된다**: `tests/test_main.py`가 관측 루프에서
    `now`가 실제로 넘어가는지 별도로 강제한다.
    """
    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    assert engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market()
    ).approved


def test_cutoff_uses_the_session_module_constant():
    """규약 B — 14:50을 아는 곳은 `mahdi.session` 하나다.

    엔진이 자기 상수를 따로 들면 08-06의 재발이다(같은 사실이 두 곳에 적히면 갈라진다).
    """
    from mahdi import session

    engine = RiskEngine(risk_limits=_RISK_LIMITS)
    just_before = (
        datetime(2026, 8, 6).replace(
            hour=session.NEW_ENTRY_CUTOFF.hour, minute=session.NEW_ENTRY_CUTOFF.minute
        ) - timedelta(minutes=1)
    )
    assert engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(), now=just_before
    ).approved
    assert engine.evaluate_entry(
        _sizing_input(), _account(), "vrp_harvest", _market(), now=session.NEW_ENTRY_CUTOFF
    ).reject_reasons == ["entry_cutoff"]
