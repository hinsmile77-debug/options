"""주문 왕복 실측 스크립트의 지정가 계산 — **체결되지 않는 것**이 이 함수의 유일한 책임이다.

2026-08-17 실측: 종전 구현(`round(x, 2)`)은 현재가 2,000개 중 **1,600개(80%)** 에서 선물
호가단위(0.05)를 위반하는 지정가를 만들었다. 거래소가 거부하면 포지션은 안 생기지만 한 번뿐인
실측이 날아가고 «주문 API가 안 된다»로 오귀속되기 쉽다.
"""

import pytest

from mahdi.execution.entry import DEFAULT_TICK_SIZE
from scripts.measure_order_roundtrip import _PRICE_AWAY_RATIO, _away_price


def _on_grid(price: float, tick: float) -> bool:
    units = price / tick
    return abs(round(units) - units) < 1e-6


@pytest.mark.parametrize(
    "reference", [975.10, 975.15, 975.20, 975.25, 975.30, 1047.35, 1046.85, 350.05, 447.85]
)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_every_price_lands_on_the_tick_grid(reference, side):
    """종전 구현이 80% 확률로 깨뜨리던 불변식."""
    price = _away_price(reference, side)
    assert _on_grid(price, DEFAULT_TICK_SIZE), f"{price}가 {DEFAULT_TICK_SIZE} 격자를 벗어났다"


def test_the_default_tick_is_the_shared_constant_not_a_second_copy():
    """08-17까지 이 값이 두 곳에 따로 있었고, 그중 하나가 틀렸다."""
    import scripts.measure_order_roundtrip as script

    assert script._away_price.__defaults__[0] == DEFAULT_TICK_SIZE


@pytest.mark.parametrize("reference", [975.10, 1047.35, 350.05])
def test_snapping_never_moves_the_price_toward_the_market(reference):
    """가장 가까운 격자로 반올림하면 체결에 가까워질 수 있다 — 항상 멀어지는 쪽으로만 스냅한다."""
    buy = _away_price(reference, "BUY")
    sell = _away_price(reference, "SELL")
    # 스냅 전 원가격보다 각각 더 낮고/더 높아야 한다(같아도 안 된다면 아래 <=/>=가 잡는다).
    assert buy <= reference * (1 - _PRICE_AWAY_RATIO)
    assert sell >= reference * (1 + _PRICE_AWAY_RATIO)


@pytest.mark.parametrize("reference", [975.10, 1047.35, 350.05, 12.30])
def test_the_price_always_stays_on_the_safe_side_of_the_market(reference):
    assert _away_price(reference, "BUY") < reference
    assert _away_price(reference, "SELL") > reference


def test_a_finer_tick_is_honoured_for_cheap_options():
    """프리미엄이 낮은 옵션은 0.05 격자로 표현할 수 없다 — 그때만 사람이 내려 잡는다."""
    price = _away_price(0.50, "BUY", 0.01)
    assert _on_grid(price, 0.01)
    assert 0 < price < 0.50


def test_a_price_too_low_for_the_grid_stops_rather_than_inventing_one():
    """0을 내보내거나 현재가 위쪽 값을 지어내는 대신 멈춰서 --tick을 묻는다."""
    with pytest.raises(ValueError, match="--tick"):
        _away_price(0.05, "BUY", DEFAULT_TICK_SIZE)


def test_a_nonsense_tick_is_rejected_outright():
    with pytest.raises(ValueError, match="양수"):
        _away_price(975.10, "BUY", 0.0)


def test_the_snapped_price_survives_the_order_formatter():
    """격자에 맞춰도 전송 문자열이 지수 표기로 새면 거부당한다(`format_order_price`의 존재 이유)."""
    from mahdi.broker.rest_client import format_order_price

    for reference in (975.10, 1047.35, 350.05):
        text = format_order_price(_away_price(reference, "BUY"))
        assert "e" not in text.lower()
        assert float(text) == _away_price(reference, "BUY")


def test_the_whole_reference_range_is_grid_clean():
    """08-17에 80% 위반을 드러낸 그 스캔 — 이제 0건이어야 한다."""
    violations = [
        ref / 10.0
        for ref in range(9_000, 11_000)
        if not _on_grid(_away_price(ref / 10.0, "BUY"), DEFAULT_TICK_SIZE)
    ]
    assert violations == []
