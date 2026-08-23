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
from datetime import datetime, time as dtime

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
    # ===== 2026-08-17 — 이 두 필드가 없어서 실행 경로에서 게이트 둘이 조용히 빠져 있었다 =====
    #
    # `RiskEngine.evaluate_entry()`의 `now`/`market_halted`는 기본값이 있고, 그 기본값의 뜻은
    # **"그 게이트를 건너뛴다"** 이다(`now=None` → 14:50 신규 진입 컷오프 미평가,
    # `market_halted=False` → 거래소 정지 미평가). 이 파사드는 둘 다 안 넘기고 있었다.
    #
    # `main.py`의 그림자 게이트 호출부가 정확히 이것을 예언해 뒀다(2026-08-06 Fix#1 주석):
    #     *"이 인자가 비어 있으면 Phase 2에서 실행 엔진이 같은 호출을 복사해 갈 때
    #       시각 게이트가 조용히 빠진다. **두 층 모두 채워져 있어야 한다.**"*
    # 복사해 간 쪽이 이미 그 상태였고, 라이브 미배선이라 아무도 안 밟았을 뿐이다.
    #
    # **기본값을 None/False로 두는 이유**: 시각과 무관한 한도만 보고 싶은 기존 테스트/백테스트를
    # 깨지 않기 위함이다 — RiskEngine이 같은 이유로 같은 기본값을 쓴다. 대신 **라이브 경로는
    # 반드시 채운다**(`test_execution_engine.py`가 그것을 강제한다).
    now: datetime | dtime | None = None
    market_halted: bool = False


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
        입력: EntryRequest(진입 컨텍스트 + Risk Engine 입력 + 물타기 판단용 플래그 + 판단 시각
             `now`과 거래정지 여부 `market_halted`), 현재 하이브리드 모드.
        계산: (1) 물타기 금지 규칙(entry.forbid_averaging_down) 선체크 — 위반이면 즉시 거부.
             (2) RiskEngine.evaluate_entry()로 **시각 컷오프(14:50)·거래정지**·사이징·한도·
             Circuit Breaker 통과 여부 확인 — 거부되면 그대로 전파(§12 "독립 거부권", 이
             파사드가 절대 우회하지 않음).
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
            request.sizing_input, request.account_state, request.strategy_id, request.market_conditions,
            # 2026-08-17 — 이 두 인자를 넘기지 않으면 §12의 거부권 중 **시각·거래정지 두 개가
            # 조용히 사라진다.** 자세한 근거는 `EntryRequest`의 두 필드 위 주석.
            market_halted=request.market_halted,
            now=request.now,
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
        # 2026-08-23 (실행 배선 ④) — **None이 정상값이다.** EV 입력(`trade_history`)이
        # 없는 동안 레이어 4는 평가되지 않는다(근거는 `exit_stack` 쪽 주석). 지어낸
        # 중립값을 넣으면 그 숫자가 그대로 청산 주문이 된다.
        belief: BeliefState | None,
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
