from datetime import datetime

from mahdi.broker.order_state_machine import Order, OrderState
from mahdi.execution.forced_flat import OpenPosition, build_forced_flat_orders, verify_forced_flat

_NOW = datetime(2026, 7, 28, 15, 10, 0)


def test_build_forced_flat_orders_reverses_side():
    orders = build_forced_flat_orders(
        [OpenPosition(symbol="B01603955", side="BUY", qty=2, current_price=100.0)], _NOW
    )
    assert len(orders) == 1
    assert orders[0].side == "SELL"
    assert orders[0].qty == 2
    assert orders[0].order_type == "MARKET"
    assert orders[0].state == OrderState.PENDING


def test_build_forced_flat_orders_empty_positions_yields_empty_list():
    assert build_forced_flat_orders([], _NOW) == []


def test_verify_forced_flat_all_filled_is_flat():
    orders = [
        Order(order_id="1", symbol="A", side="SELL", order_type="MARKET", intended_px=100.0, qty=1,
              timestamp=_NOW, state=OrderState.FILLED),
        Order(order_id="2", symbol="B", side="BUY", order_type="MARKET", intended_px=100.0, qty=1,
              timestamp=_NOW, state=OrderState.FILLED),
    ]
    result = verify_forced_flat(orders, _NOW)
    assert result.all_flat
    assert result.unconfirmed_order_ids == []


def test_verify_forced_flat_pending_order_is_not_flat():
    orders = [
        Order(order_id="1", symbol="A", side="SELL", order_type="MARKET", intended_px=100.0, qty=1,
              timestamp=_NOW, state=OrderState.PENDING),
    ]
    result = verify_forced_flat(orders, _NOW)
    assert not result.all_flat
    assert result.unconfirmed_order_ids == ["1"]


def test_verify_forced_flat_rejected_order_counts_as_unconfirmed():
    # REJECTED는 종결 상태지만 청산에 실패한 것 — 포지션이 그대로 남아있다는 뜻
    orders = [
        Order(order_id="1", symbol="A", side="SELL", order_type="MARKET", intended_px=100.0, qty=1,
              timestamp=_NOW, state=OrderState.REJECTED),
    ]
    result = verify_forced_flat(orders, _NOW)
    assert not result.all_flat
    assert result.unconfirmed_order_ids == ["1"]


def test_verify_forced_flat_no_positions_is_flat():
    result = verify_forced_flat([], _NOW)
    assert result.all_flat
