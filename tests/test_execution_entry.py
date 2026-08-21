from datetime import time

import pytest

from mahdi.execution.entry import (
    DEFAULT_TICK_SIZE,
    EntryContext,
    build_entry_plan,
    forbid_averaging_down,
    tick_size_for_price,
)


def _ctx(**overrides) -> EntryContext:
    base = dict(
        symbol="B01603955",
        side="BUY",
        qty=1,
        reference_price=100.0,
        now=time(10, 0),
    )
    base.update(overrides)
    return EntryContext(**base)


def test_default_case_is_passive_limit_order():
    plan = build_entry_plan(_ctx())
    assert plan.order_type == "LIMIT"
    assert plan.urgency is False
    assert plan.limit_price == 100.0 - 0.05


def test_sell_side_offsets_limit_price_upward():
    plan = build_entry_plan(_ctx(side="SELL"))
    assert plan.limit_price == 100.0 + 0.05


def test_negative_gex_expansion_triggers_urgency_market_order():
    plan = build_entry_plan(_ctx(negative_gex_expansion=True))
    assert plan.order_type == "MARKET"
    assert plan.urgency is True
    assert plan.limit_price is None


def test_opening_five_minutes_dampens_urgency_even_with_negative_gex():
    plan = build_entry_plan(_ctx(negative_gex_expansion=True, now=time(9, 2)))
    assert plan.order_type == "LIMIT"
    assert plan.urgency is False


def test_event_proximity_dampens_urgency():
    plan = build_entry_plan(
        _ctx(negative_gex_expansion=True, now=time(11, 0), event_proximity_minutes=5.0)
    )
    assert plan.order_type == "LIMIT"
    assert plan.urgency is False


def test_event_far_enough_does_not_dampen_urgency():
    plan = build_entry_plan(
        _ctx(negative_gex_expansion=True, now=time(11, 0), event_proximity_minutes=30.0)
    )
    assert plan.order_type == "MARKET"


def test_forbid_averaging_down_blocks_same_direction_without_new_signal():
    assert forbid_averaging_down(has_open_position_same_direction=True, is_new_signal=False)


def test_forbid_averaging_down_allows_with_fresh_signal():
    assert not forbid_averaging_down(has_open_position_same_direction=True, is_new_signal=True)


def test_forbid_averaging_down_allows_when_no_existing_position():
    assert not forbid_averaging_down(has_open_position_same_direction=False, is_new_signal=False)


# ===== 2026-08-21: 호가단위 해석기 (흡수 판정이 실제 해상도를 필요로 해서 추가) =====


def test_tick_size_splits_at_premium_10():
    """실측(`market_raw_1m` 11,260행)이 확정한 경계 — 10.00 이상 0.05, 미만 0.01."""
    assert tick_size_for_price(10.0) == pytest.approx(0.05)
    assert tick_size_for_price(9.99) == pytest.approx(0.01)


def test_tick_size_needs_no_instrument_type():
    # 지수선물은 언제나 10.00을 훌쩍 넘으므로 가격만으로 0.05를 받는다 —
    # 호출측이 「선물인가 옵션인가」를 알아야 했다면 그 지식이 또 하나의 진실 공급원이 된다.
    assert tick_size_for_price(1080.0) == pytest.approx(DEFAULT_TICK_SIZE)
    assert tick_size_for_price(16.0) == pytest.approx(DEFAULT_TICK_SIZE)
    assert tick_size_for_price(2.5) == pytest.approx(0.01)


def test_default_tick_size_stays_the_safe_grid_for_orders():
    """`DEFAULT_TICK_SIZE`는 목적이 다르다 — 양쪽에서 거부되지 않는 주문 스냅용 격자다.

    저가 옵션(0.01 격자)에서 해상도가 거칠어지는 것은 의도된 대가이고, 이 함수가 그것을
    대체하지 않는다(08-18 결정).
    """
    assert DEFAULT_TICK_SIZE == pytest.approx(0.05)
