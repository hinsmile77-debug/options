"""Execution Engine — 하이브리드 3모드 (v6 §13.1).

모드는 "진입 자유"의 정도만 조절한다 — Hard Stop/Circuit Breaker/Forced Flat은
`strategy_params.yaml`의 `hybrid_mode.always_automatic` 목록에 있으므로 모드와
무관하게 항상 자동이다(v6 §13.1 불변 규칙: "수동 모드는 공격의 자유이지 방어의
자유가 아니다").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_ALWAYS_AUTOMATIC_EXIT_LAYERS = frozenset({"hard_stop", "circuit_breaker", "forced_flat_15_10"})


class HybridMode(str, Enum):
    ADVISORY = "ADVISORY"
    CONFIRM = "CONFIRM"
    FULL_AUTO = "FULL_AUTO"


class GateAction(str, Enum):
    AUTO_SUBMIT = "AUTO_SUBMIT"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    ADVISORY_ONLY = "ADVISORY_ONLY"


@dataclass(frozen=True, slots=True)
class GateDecision:
    action: GateAction
    confirmation_timeout_seconds: int | None = None


def gate_entry(mode: HybridMode, confirmation_timeout_seconds: int = 60) -> GateDecision:
    """
    입력: 현재 하이브리드 모드, CONFIRM 모드 대시보드 승인 타임아웃(초).
    계산: FULL_AUTO -> AUTO_SUBMIT. CONFIRM -> PENDING_CONFIRMATION(타임아웃 포함 — 타임아웃 시
         자동 취소는 Order Manager 책임). ADVISORY -> ADVISORY_ONLY(주문 없음, 신호만 표시).
    실패 조건: 없음 — 3가지 모드 전부 매핑된다.
    """
    if mode == HybridMode.FULL_AUTO:
        return GateDecision(action=GateAction.AUTO_SUBMIT)
    if mode == HybridMode.CONFIRM:
        return GateDecision(
            action=GateAction.PENDING_CONFIRMATION,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
        )
    return GateDecision(action=GateAction.ADVISORY_ONLY)


def gate_exit(
    mode: HybridMode,
    exit_layer: str,
    confirmation_timeout_seconds: int = 3,
    always_automatic: frozenset[str] = _ALWAYS_AUTOMATIC_EXIT_LAYERS,
) -> GateDecision:
    """
    입력: 현재 모드, 청산을 촉발한 레이어 이름("hard_stop"/"structure_stop"/"flow_stop"/
         "belief_decay_stop"/"time_stop"/"forced_flat_15_10").
    계산: exit_layer가 always_automatic에 있으면 모드 무관 AUTO_SUBMIT(v6 §13.1 불변 규칙).
         그 외 레이어는 gate_entry()와 같은 모드별 규칙을 따르되, CONFIRM 모드는 §13.3
         "Structure Stop: 3초 경고 후 자동"에 맞춰 기본 타임아웃을 3초로 짧게 둔다(진입 확인
         타임아웃보다 훨씬 짧음 — 청산 지연은 진입 지연보다 비용이 크다).
    실패 조건: 없음.
    """
    if exit_layer in always_automatic:
        return GateDecision(action=GateAction.AUTO_SUBMIT)
    if mode == HybridMode.FULL_AUTO:
        return GateDecision(action=GateAction.AUTO_SUBMIT)
    if mode == HybridMode.CONFIRM:
        return GateDecision(
            action=GateAction.PENDING_CONFIRMATION,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
        )
    return GateDecision(action=GateAction.ADVISORY_ONLY)
