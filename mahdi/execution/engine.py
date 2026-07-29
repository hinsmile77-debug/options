"""Execution Engine — 파사드 (v6 §13 전체).

진입은 반드시 `RiskEngine.evaluate_entry()`를, 보유 중 재평가는 반드시
`RiskEngine.evaluate_ongoing()`을 거친다(v6 §12 "독립 거부권" 배선 요구사항 —
[[NEXT_TODO]] Phase 2 절 참고). `forced_flat.py`/`order_manager.py`는 실제 브로커
제출·폴링이 필요해 이 동기 파사드에 억지로 엮지 않고 독립 모듈로 남겨둔다 — main.py
라이브 루프 배선 시점에 이 파사드의 `evaluate_entry()`/`evaluate_exit()` 결과를
받아 `order_manager.submit()`/`confirm_fill()`로 실제 주문을 내고, 15:10에는
`forced_flat.build_forced_flat_orders()` + `verify_forced_flat()`로 자기검증한다.

이번 증분은 main.py에 연결하지 않는다 — Signal Fusion이 아직 검증되지 않은
휴리스틱 단계라 실시간으로 실제 주문(모의계좌라도)을 계속 내보내는 건 시기상조라는
판단([[DECISION_LOG]] 참고 예정).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mahdi.config.settings import get_strategy_params
from mahdi.execution.entry import EntryContext, EntryPlan, build_entry_plan, forbid_averaging_down
from mahdi.execution.exit_stack import (
    BeliefState,
    ExitDecision,
    MarketStructureState,
    PositionState,
    evaluate_exit_stack,
)
from mahdi.execution.hybrid_mode import GateAction, GateDecision, HybridMode, gate_entry, gate_exit
from mahdi.risk.circuit_breaker import MarketConditions
from mahdi.risk.engine import RiskEngine
from mahdi.risk.limits import AccountState
from mahdi.risk.sizing import PositionSizingInput


@dataclass(frozen=True, slots=True)
class EntryRequest:
    entry_context: EntryContext
    sizing_input: PositionSizingInput
    account_state: AccountState
    strategy_id: str
    market_conditions: MarketConditions
    has_open_position_same_direction: bool = False
    is_new_signal: bool = True


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    approved: bool
    approved_size: float = 0.0
    gate_decision: GateDecision | None = None
    entry_plan: EntryPlan | None = None
    reject_reasons: list[str] = field(default_factory=list)


class ExecutionEngine:
    def __init__(self, risk_engine: RiskEngine | None = None, strategy_params: dict | None = None) -> None:
        self._risk_engine = risk_engine if risk_engine is not None else RiskEngine()
        self._params = strategy_params if strategy_params is not None else get_strategy_params()

    def evaluate_entry(self, request: EntryRequest, mode: HybridMode) -> EntryOutcome:
        """
        입력: EntryRequest(진입 컨텍스트 + Risk Engine 입력 + 물타기 판단용 플래그), 현재
             하이브리드 모드.
        계산: (1) 물타기 금지 규칙(entry.forbid_averaging_down) 선체크 — 위반이면 즉시 거부.
             (2) RiskEngine.evaluate_entry()로 사이징/한도/Circuit Breaker 통과 여부 확인 —
             거부되면 그대로 전파(§12 "독립 거부권", 이 파사드가 절대 우회하지 않음).
             (3) 하이브리드 모드 게이트(hybrid_mode.gate_entry) — ADVISORY면 주문 계획을
             만들지 않고 신호만 승인(entry_plan=None).
             (4) 그 외(CONFIRM/FULL_AUTO)엔 Passive-first EntryPlan을 만든다.
        해석: approved=True이지만 entry_plan=None이면 "신호는 유효하나 수동 판단 대상"
             (ADVISORY)이라는 뜻 — 호출측이 실제 주문을 내면 안 된다.
        실패 조건: 없음 — 모든 거부 경로가 reject_reasons로 드러난다.
        """
        if forbid_averaging_down(request.has_open_position_same_direction, request.is_new_signal):
            return EntryOutcome(approved=False, reject_reasons=["averaging_down_forbidden"])

        risk_decision = self._risk_engine.evaluate_entry(
            request.sizing_input, request.account_state, request.strategy_id, request.market_conditions
        )
        if not risk_decision.approved:
            return EntryOutcome(approved=False, reject_reasons=list(risk_decision.reject_reasons))

        gate = gate_entry(mode)
        if gate.action == GateAction.ADVISORY_ONLY:
            return EntryOutcome(approved=True, approved_size=risk_decision.approved_size, gate_decision=gate)

        plan = build_entry_plan(request.entry_context)
        return EntryOutcome(
            approved=True, approved_size=risk_decision.approved_size, gate_decision=gate, entry_plan=plan
        )

    def evaluate_exit(
        self,
        position: PositionState,
        market: MarketStructureState,
        belief: BeliefState,
        account_state: AccountState,
        market_conditions: MarketConditions,
        mode: HybridMode,
    ) -> tuple[ExitDecision, GateDecision]:
        """
        입력: 포지션/시장구조/확신 상태 + RiskEngine.evaluate_ongoing() 입력 + 현재 모드.
        계산: RiskEngine.evaluate_ongoing()으로 Circuit Breaker를 먼저 재확인한다 —
             requires_emergency_flatten이면 exit_stack 결과와 무관하게 즉시 FULL_EXIT로
             강제한다(§12 독립 거부권이 §13 청산 로직보다 우선). 그 외엔
             exit_stack.evaluate_exit_stack()의 6-Layer 결과를 그대로 쓴다. 최종적으로
             hybrid_mode.gate_exit()로 자동/승인대기/권고 여부를 정한다(HOLD면 게이트 자체가
             무의미하므로 ADVISORY_ONLY로 둔다).
        해석: 반환된 ExitDecision.action이 "HOLD"가 아닐 때만 GateDecision.action을 실제로
             따른다.
        실패 조건: 없음.
        """
        cb_decision = self._risk_engine.evaluate_ongoing(account_state, market_conditions)
        exit_rules_cfg = self._params.get("exit_rules", {})
        decision = evaluate_exit_stack(position, market, belief, exit_rules_cfg)

        if cb_decision.requires_emergency_flatten and decision.action == "HOLD":
            decision = ExitDecision(
                triggered_layer=None, action="FULL_EXIT", reason="circuit_breaker_emergency_flatten"
            )

        if decision.action == "HOLD":
            return decision, GateDecision(action=GateAction.ADVISORY_ONLY)

        layer_name = decision.triggered_layer.value if decision.triggered_layer else "circuit_breaker"
        return decision, gate_exit(mode, layer_name)
