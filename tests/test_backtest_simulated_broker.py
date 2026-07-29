from datetime import datetime

import pytest

from mahdi.backtest.simulated_broker import Bar, FillConfig, fill_market_at_next_open, try_fill_limit
from mahdi.broker.order_state_machine import Order, OrderState

_NOW = datetime(2026, 7, 28, 10, 0, 0)


def _limit_order(side: str, price: float) -> Order:
    return Order(
        order_id="O1", symbol="B01603955", side=side, order_type="LIMIT",
        intended_px=price, qty=1, timestamp=_NOW,
    )


def _market_order(side: str) -> Order:
    return Order(
        order_id="O1", symbol="B01603955", side=side, order_type="MARKET",
        intended_px=0.0, qty=1, timestamp=_NOW,
    )


def test_buy_limit_fills_when_low_touches_price():
    bar = Bar(timestamp_minutes=1.0, open=101.0, high=101.5, low=99.5, close=100.5)
    order = try_fill_limit(_limit_order("BUY", 100.0), bar)
    assert order.state == OrderState.FILLED
    assert order.filled_px == 100.0


def test_buy_limit_does_not_fill_when_low_stays_above_price():
    bar = Bar(timestamp_minutes=1.0, open=101.0, high=101.5, low=100.5, close=101.0)
    order = try_fill_limit(_limit_order("BUY", 100.0), bar)
    assert order.state == OrderState.PENDING


def test_sell_limit_fills_when_high_touches_price():
    bar = Bar(timestamp_minutes=1.0, open=99.0, high=100.5, low=98.5, close=99.5)
    order = try_fill_limit(_limit_order("SELL", 100.0), bar)
    assert order.state == OrderState.FILLED
    assert order.filled_px == 100.0


def test_try_fill_limit_rejects_market_order():
    with pytest.raises(ValueError):
        try_fill_limit(_market_order("BUY"), Bar(timestamp_minutes=1.0, open=1, high=1, low=1, close=1))


def test_market_buy_fills_above_open_with_slippage():
    bar = Bar(timestamp_minutes=1.0, open=100.0, high=100.5, low=99.5, close=100.2)
    order = fill_market_at_next_open(_market_order("BUY"), bar, FillConfig(slippage_ticks=2, tick_size=0.05))
    assert order.state == OrderState.FILLED
    assert order.filled_px == pytest.approx(100.1)


def test_market_sell_fills_below_open_with_slippage():
    bar = Bar(timestamp_minutes=1.0, open=100.0, high=100.5, low=99.5, close=100.2)
    order = fill_market_at_next_open(_market_order("SELL"), bar, FillConfig(slippage_ticks=2, tick_size=0.05))
    assert order.filled_px == pytest.approx(99.9)


def test_fill_market_at_next_open_rejects_limit_order():
    with pytest.raises(ValueError):
        fill_market_at_next_open(
            _limit_order("BUY", 100.0), Bar(timestamp_minutes=1.0, open=1, high=1, low=1, close=1), FillConfig()
        )
