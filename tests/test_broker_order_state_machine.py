from datetime import datetime

import pytest

from mahdi.broker.order_state_machine import (
    InvalidTransitionError,
    Order,
    OrderState,
    OrderStateMachine,
)


def _new_order(state: OrderState = OrderState.PENDING) -> Order:
    return Order(
        order_id="ORD1",
        symbol="101W09",
        side="BUY",
        order_type="LIMIT",
        intended_px=350.0,
        qty=10,
        timestamp=datetime(2026, 7, 5, 9, 5),
        state=state,
    )


def test_pending_to_filled_records_fill():
    machine = OrderStateMachine(_new_order())
    order = machine.transition(OrderState.FILLED, filled_px=350.0, filled_qty=10)
    assert order.state == OrderState.FILLED
    assert order.filled_px == 350.0
    assert order.filled_qty == 10
    assert machine.is_terminal is True


def test_pending_to_partial_then_filled_accumulates_qty():
    machine = OrderStateMachine(_new_order())
    machine.transition(OrderState.PARTIAL, filled_px=350.0, filled_qty=4)
    order = machine.transition(OrderState.FILLED, filled_px=350.5, filled_qty=6)
    assert order.filled_qty == 10
    assert order.filled_px == 350.5
    assert order.state == OrderState.FILLED


def test_partial_can_accumulate_further_partials():
    machine = OrderStateMachine(_new_order())
    machine.transition(OrderState.PARTIAL, filled_qty=3)
    order = machine.transition(OrderState.PARTIAL, filled_qty=2)
    assert order.filled_qty == 5
    assert order.state == OrderState.PARTIAL


def test_pending_to_cancelled_and_rejected_allowed():
    assert OrderStateMachine(_new_order()).transition(OrderState.CANCELLED).state == OrderState.CANCELLED
    assert OrderStateMachine(_new_order()).transition(OrderState.REJECTED).state == OrderState.REJECTED


def test_terminal_states_reject_any_transition():
    for terminal in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED):
        machine = OrderStateMachine(_new_order(state=terminal))
        with pytest.raises(InvalidTransitionError):
            machine.transition(OrderState.PARTIAL)


def test_cannot_skip_backwards_from_partial_to_pending():
    machine = OrderStateMachine(_new_order(state=OrderState.PARTIAL))
    with pytest.raises(InvalidTransitionError):
        machine.transition(OrderState.PENDING)


def test_is_terminal_false_for_pending_and_partial():
    assert OrderStateMachine(_new_order(OrderState.PENDING)).is_terminal is False
    assert OrderStateMachine(_new_order(OrderState.PARTIAL)).is_terminal is False
