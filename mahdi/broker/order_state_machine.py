"""주문 상태머신 — execution_logs 스키마와 1:1 대응 (v6 §18.1).

PENDING → PARTIAL/FILLED/CANCELLED/REJECTED. 상태 전이 규칙을 한 곳에서 강제해
Execution Engine이 잘못된 전이(예: FILLED 이후 재체결)를 만들지 못하게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OrderState(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


_TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}

_ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.PENDING: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.PARTIAL: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED},
    OrderState.FILLED: set(),
    OrderState.CANCELLED: set(),
    OrderState.REJECTED: set(),
}


@dataclass
class Order:
    """execution_logs 테이블 1행에 대응."""

    order_id: str
    symbol: str
    side: str  # BUY/SELL
    order_type: str  # LIMIT/MARKET 등
    intended_px: float
    qty: int
    timestamp: datetime
    state: OrderState = OrderState.PENDING
    filled_px: float | None = None
    filled_qty: int = 0
    slippage_ticks: float | None = None
    latency_ms: int | None = None


class InvalidTransitionError(Exception):
    pass


class OrderStateMachine:
    """단일 주문의 상태 전이를 강제하는 헬퍼. 영속화(execution_logs 반영)는 호출측(Data Layer) 책임."""

    def __init__(self, order: Order) -> None:
        self.order = order

    def transition(
        self,
        new_state: OrderState,
        *,
        filled_px: float | None = None,
        filled_qty: int | None = None,
    ) -> Order:
        """
        입력: 목표 상태, (체결 시) 체결가와 이번 체결분 수량.
        계산: _ALLOWED_TRANSITIONS 규칙에 부합하면 상태 갱신 (filled_qty는 누적 가산).
        해석: PARTIAL 상태는 스스로에게도 재전이 가능(추가 부분체결 누적).
        실패 조건: 종결 상태(FILLED/CANCELLED/REJECTED)에서는 어떤 전이도 InvalidTransitionError.
        """
        current = self.order.state
        if new_state not in _ALLOWED_TRANSITIONS[current]:
            raise InvalidTransitionError(f"{current} -> {new_state} 전이는 허용되지 않습니다")

        if filled_px is not None:
            self.order.filled_px = filled_px
        if filled_qty is not None:
            self.order.filled_qty += filled_qty

        self.order.state = new_state
        return self.order

    @property
    def is_terminal(self) -> bool:
        return self.order.state in _TERMINAL_STATES
