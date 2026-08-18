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


# --- 2026-08-18 — 선물을 옵션으로 조회하고 있었다 -------------------------------------------
#
# `get_quote()`의 `market_div_code` 기본값이 옵션('O')인데 스크립트가 그 인자를 넘기지 않아
# 선물 코드를 'O'로 물었고, KIS는 **4xx가 아니라 전 필드 0**으로 답했다. 예행연습의
# `reference <= 0` 검사가 주문 전에 멈춰 세웠지만, 원인을 못 짚으면 매번 다시 밟는다.


def test_a_futures_code_is_quoted_as_futures():
    from mahdi.broker import tr_codes
    from scripts.measure_order_roundtrip import market_div_for

    assert market_div_for("A01609") == tr_codes.FID_MRKT_DIV_INDEX_FUTURES
    assert market_div_for("101S03") == tr_codes.FID_MRKT_DIV_INDEX_FUTURES


def test_an_option_code_is_quoted_as_options():
    from mahdi.broker import tr_codes
    from scripts.measure_order_roundtrip import market_div_for

    assert market_div_for("B09FAWA37") == tr_codes.FID_MRKT_DIV_INDEX_OPTION
    assert market_div_for("C01609A39") == tr_codes.FID_MRKT_DIV_INDEX_OPTION


def test_an_unknown_length_keeps_the_previous_default():
    """이 함수가 생기기 전 동작(옵션 기본값)을 바꾸지 않는다 — 옵션 경로는 원래 맞았다."""
    from mahdi.broker import tr_codes
    from scripts.measure_order_roundtrip import market_div_for

    assert market_div_for("???") == tr_codes.FID_MRKT_DIV_INDEX_OPTION


# --- 2026-08-18 — 30%는 일일 가격제한폭 밖이었다 ---------------------------------------------
#
# 첫 실주문이 `rt_cd=1 "모의투자 상/하한가 오류"`로 거부됐다. 실측: 현재가 1099.65 /
# 제한폭 1011.00~1186.80(기준가 ±8.0%) / 시도한 지정가 769.75 -> 하한보다 241.25 낮다.

_LOW, _HIGH = 1011.00, 1186.80


def test_the_limit_price_is_clamped_into_the_daily_band():
    """거래소는 제한폭 밖 지정가를 접수하지 않는다 — 30%를 낮추는 대신 밴드로 자른다."""
    buy = _away_price(1099.65, "BUY", DEFAULT_TICK_SIZE, _LOW, _HIGH)
    sell = _away_price(1099.65, "SELL", DEFAULT_TICK_SIZE, _LOW, _HIGH)

    assert _LOW <= buy <= _HIGH and _LOW <= sell <= _HIGH
    assert buy == _LOW and sell == _HIGH  # 허용 범위에서 가장 먼 값


def test_clamping_still_lands_on_the_tick_grid():
    """하한에 붙였을 때 또 내리면 밴드 밖으로 나가 다시 거부당한다 — 그때만 올림한다."""
    buy = _away_price(1000.0, "BUY", DEFAULT_TICK_SIZE, 933.33, 1100.0)
    assert _on_grid(buy, DEFAULT_TICK_SIZE)
    assert buy >= 933.33  # 밴드 밖으로 나가지 않았다


def test_a_wide_band_leaves_the_full_30_percent_intact():
    """옵션처럼 밴드가 넓으면 30% 거리가 그대로 살아 있어야 한다."""
    buy = _away_price(1099.65, "BUY", DEFAULT_TICK_SIZE, 1.0, 99999.0)
    assert buy == pytest.approx(1099.65 * 0.70, abs=DEFAULT_TICK_SIZE)


def test_without_a_known_band_the_behaviour_is_unchanged():
    """밴드를 모르면 자르지 않는다 — 이 인자가 생기기 전과 같아야 한다."""
    assert _away_price(1099.65, "BUY") == _away_price(1099.65, "BUY", DEFAULT_TICK_SIZE, None, None)
