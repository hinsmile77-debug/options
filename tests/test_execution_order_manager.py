from datetime import datetime

import pytest

from mahdi.broker.order_state_machine import InvalidTransitionError, Order, OrderState
from mahdi.execution.order_manager import confirm_fill, submit

_NOW = datetime(2026, 7, 28, 10, 0, 0)


class _FakeBroker:
    def __init__(self, submit_response: dict, fill_status: dict | None = None):
        self._submit_response = submit_response
        self._fill_status = fill_status

    def submit_order(self, symbol, side, qty, price, order_dvsn_cd="01"):
        return self._submit_response

    def get_order_fill_status(self, order_id):
        return self._fill_status


def _order(state: OrderState = OrderState.PENDING) -> Order:
    return Order(
        order_id="ORD1", symbol="B01603955", side="BUY", order_type="LIMIT",
        intended_px=100.0, qty=1, timestamp=_NOW, state=state,
    )


def test_submit_success_keeps_order_pending():
    result = submit(_order(), _FakeBroker({"rt_cd": "0", "odno": "ORD1"}))
    assert result.order.state == OrderState.PENDING
    assert result.broker_response["odno"] == "ORD1"


def test_submit_rejection_transitions_to_rejected():
    result = submit(_order(), _FakeBroker({"rt_cd": "1", "msg1": "invalid"}))
    assert result.order.state == OrderState.REJECTED


def test_confirm_fill_still_pending_returns_unchanged():
    order = confirm_fill(_order(), _FakeBroker({}, {"state": "PENDING"}))
    assert order.state == OrderState.PENDING


def test_confirm_fill_transitions_to_filled():
    order = confirm_fill(
        _order(), _FakeBroker({}, {"state": "FILLED", "filled_px": 100.5, "filled_qty": 1})
    )
    assert order.state == OrderState.FILLED
    assert order.filled_px == 100.5
    assert order.filled_qty == 1


def test_confirm_fill_accumulates_partial_quantity():
    order = _order(state=OrderState.PARTIAL)
    order.filled_qty = 1
    updated = confirm_fill(order, _FakeBroker({}, {"state": "PARTIAL", "filled_qty": 1}))
    assert updated.state == OrderState.PARTIAL
    assert updated.filled_qty == 2


def test_confirm_fill_on_terminal_order_raises():
    with pytest.raises(InvalidTransitionError):
        confirm_fill(_order(state=OrderState.FILLED), _FakeBroker({}, {"state": "FILLED"}))
