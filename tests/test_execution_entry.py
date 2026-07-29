from datetime import time

from mahdi.execution.entry import EntryContext, build_entry_plan, forbid_averaging_down


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
