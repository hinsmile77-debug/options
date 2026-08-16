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


# 2026-08-16 (Block C) — `execution_logs` 적재용 행 변환.
#
# `Order` docstring이 *"execution_logs 테이블 1행에 대응"* 이라고 적어 두었는데 **그 행을 만드는
# 함수가 없어서** 주문을 내도 남길 곳이 없었다. 변환을 `Order` 옆에 두는 이유는 이 dataclass가
# 컬럼 집합의 소유자이기 때문이다(사실을 아는 쪽이 상수를 소유한다 — 08-06 §7 원칙).
_EXECUTION_LOG_KEYS = (
    "order_id", "timestamp", "symbol", "side", "order_type",
    "intended_px", "filled_px", "qty", "state", "slippage_ticks", "latency_ms",
)


def order_to_execution_log_row(order: Order) -> dict:
    """
    입력: Order.
    계산: `db.insert_execution_log()`에 바로 넘길 dict. `state`는 enum이 아니라 **문자열 값**으로
         내린다(DB 컬럼이 VARCHAR(15)이고, enum을 그대로 넘기면 psycopg가 어댑터를 못 찾는다).
    해석: PK가 `order_id`라 같은 주문의 상태 변화는 **같은 행을 갱신**한다 — 주문의 생애가
         한 행에 누적되고, 이력이 필요하면 `filled_qty` 누적과 `state`로 읽는다.
    실패 조건: 없음.
    """
    return {
        "order_id": order.order_id,
        "timestamp": order.timestamp,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "intended_px": order.intended_px,
        "filled_px": order.filled_px,
        "qty": order.qty,
        "state": order.state.value,
        "slippage_ticks": order.slippage_ticks,
        "latency_ms": order.latency_ms,
    }
