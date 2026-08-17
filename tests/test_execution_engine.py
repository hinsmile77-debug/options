from datetime import datetime, time

from mahdi.execution.engine import EntryRequest, ExecutionEngine
from mahdi.execution.entry import EntryContext
from mahdi.execution.exit_stack import BeliefState, ExitLayer, MarketStructureState, PositionState
from mahdi.execution.hybrid_mode import GateAction, HybridMode
from mahdi.risk.circuit_breaker import MarketConditions
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
_STRATEGY_PARAMS = {"exit_rules": {"TREND_STRONG": {"time_stop": 120}}}


def _account(**overrides) -> AccountState:
    base = dict(
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, drawdown_pct=0.0,
        same_direction_positions=0, daily_trades_by_strategy={}, pending_trade_loss_pct=0.0,
    )
    base.update(overrides)
    return AccountState(**base)


def _market_conditions(**overrides) -> MarketConditions:
    base = dict(vpin=0.0, vix=0.0, usdkrw_daily_change_pct=0.0, data_quality_ok=True, model_drift_detected=False)
    base.update(overrides)
    return MarketConditions(**base)


def _sizing_input(**overrides) -> PositionSizingInput:
    base = dict(
        base_size=100.0, regime_confidence=1.0, signal_quality=1.0, target_vol=0.01,
        realized_vol=0.01, liquidity_score=1.0, drawdown_pct=0.0, portfolio_capacity_remaining_pct=1.0,
    )
    base.update(overrides)
    return PositionSizingInput(**base)


def _entry_request(**overrides) -> EntryRequest:
    base = dict(
        entry_context=EntryContext(symbol="B01603955", side="BUY", qty=1, reference_price=100.0, now=time(10, 0)),
        sizing_input=_sizing_input(),
        account_state=_account(),
        strategy_id="vrp_harvest",
        market_conditions=_market_conditions(),
    )
    base.update(overrides)
    return EntryRequest(**base)


def _engine() -> ExecutionEngine:
    return ExecutionEngine(risk_engine=RiskEngine(risk_limits=_RISK_LIMITS), strategy_params=_STRATEGY_PARAMS)


def test_full_auto_entry_approved_produces_entry_plan():
    outcome = _engine().evaluate_entry(_entry_request(), HybridMode.FULL_AUTO)
    assert outcome.approved
    assert outcome.entry_plan is not None
    assert outcome.approved_size > 0


def test_advisory_mode_approves_signal_but_no_entry_plan():
    outcome = _engine().evaluate_entry(_entry_request(), HybridMode.ADVISORY)
    assert outcome.approved
    assert outcome.entry_plan is None
    assert outcome.gate_decision.action == GateAction.ADVISORY_ONLY


def test_risk_engine_rejection_blocks_entry_regardless_of_mode():
    outcome = _engine().evaluate_entry(
        _entry_request(account_state=_account(daily_pnl_pct=-0.03)), HybridMode.FULL_AUTO
    )
    assert not outcome.approved
    assert any(r.startswith("circuit_breaker:daily_loss_pct") for r in outcome.reject_reasons)
    assert outcome.entry_plan is None


# --- 2026-08-17 — 실행 경로에서 조용히 빠져 있던 게이트 둘 -------------------------------------
#
# `main.py`의 그림자 게이트가 이 결함을 미리 적어 뒀다: *"이 인자가 비어 있으면 Phase 2에서
# 실행 엔진이 같은 호출을 복사해 갈 때 시각 게이트가 조용히 빠진다."* 복사해 간 쪽이 실제로
# 그 상태였다. 아래 셋이 그 회귀를 막는다.


def test_entry_after_the_1450_cutoff_is_rejected_by_the_execution_path_too():
    """v6 §4.2 신규 진입 컷오프 — 판단 층에만 있으면 실행 층이 그것을 우회한다."""
    outcome = _engine().evaluate_entry(
        _entry_request(now=datetime(2026, 8, 17, 14, 51)), HybridMode.FULL_AUTO
    )
    assert not outcome.approved
    assert outcome.reject_reasons == ["entry_cutoff"]
    assert outcome.entry_plan is None


def test_entry_just_before_the_cutoff_still_passes():
    outcome = _engine().evaluate_entry(
        _entry_request(now=datetime(2026, 8, 17, 14, 49)), HybridMode.FULL_AUTO
    )
    assert outcome.approved


def test_a_halted_market_blocks_the_execution_path():
    outcome = _engine().evaluate_entry(_entry_request(market_halted=True), HybridMode.FULL_AUTO)
    assert not outcome.approved
    assert outcome.reject_reasons == ["market_halt"]


def test_averaging_down_blocked_before_risk_engine_is_even_consulted():
    outcome = _engine().evaluate_entry(
        _entry_request(has_open_position_same_direction=True, is_new_signal=False), HybridMode.FULL_AUTO
    )
    assert not outcome.approved
    assert outcome.reject_reasons == ["averaging_down_forbidden"]


def _position(**overrides) -> PositionState:
    base = dict(
        symbol="B01603955", side="BUY", entry_price=100.0, current_price=100.0,
        entry_time_minutes=0.0, now_minutes=10.0, regime="TREND_STRONG",
    )
    base.update(overrides)
    return PositionState(**base)


def _belief(**overrides) -> BeliefState:
    base = dict(win_probability=0.6, avg_win=2.0, avg_loss=1.0)
    base.update(overrides)
    return BeliefState(**base)


def test_evaluate_exit_hold_when_nothing_triggered():
    decision, gate = _engine().evaluate_exit(
        _position(), MarketStructureState(), _belief(), _account(), _market_conditions(), HybridMode.FULL_AUTO
    )
    assert decision.action == "HOLD"
    assert gate.action == GateAction.ADVISORY_ONLY


def test_circuit_breaker_forces_full_exit_even_when_exit_stack_holds():
    decision, gate = _engine().evaluate_exit(
        _position(), MarketStructureState(), _belief(), _account(drawdown_pct=-0.15),
        _market_conditions(), HybridMode.ADVISORY,
    )
    assert decision.action == "FULL_EXIT"
    assert decision.reason == "circuit_breaker_emergency_flatten"
    # 방어 조치는 모드 무관 항상 자동 -> gate_exit(mode, "circuit_breaker")는 always_automatic 처리
    assert gate.action == GateAction.AUTO_SUBMIT


def test_hard_stop_exit_is_auto_even_in_advisory_mode():
    position = _position(entry_price=100.0, current_price=97.0)
    decision, gate = _engine().evaluate_exit(
        position, MarketStructureState(), _belief(), _account(), _market_conditions(), HybridMode.ADVISORY
    )
    assert decision.triggered_layer == ExitLayer.HARD_STOP
    assert gate.action == GateAction.AUTO_SUBMIT


def test_structure_stop_in_advisory_mode_is_advisory_only():
    position = _position(current_price=99.0)
    decision, gate = _engine().evaluate_exit(
        position, MarketStructureState(vwap=100.0), _belief(), _account(), _market_conditions(), HybridMode.ADVISORY
    )
    assert decision.triggered_layer == ExitLayer.STRUCTURE_STOP
    assert gate.action == GateAction.ADVISORY_ONLY
