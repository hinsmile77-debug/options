from datetime import datetime

import pytest

from mahdi.data.collector import MinuteBarAggregator, Tick


def _tick(second: int, price: float, volume: float = 10.0, minute: int = 5) -> Tick:
    return Tick(
        timestamp=datetime(2026, 7, 5, 9, minute, second),
        price=price,
        volume=volume,
        bid_px=price - 0.05,
        bid_qty=100,
        ask_px=price + 0.05,
        ask_qty=100,
    )


def test_add_tick_same_minute_returns_none_until_rollover():
    agg = MinuteBarAggregator()
    assert agg.add_tick(_tick(0, 350.0)) is None
    assert agg.add_tick(_tick(10, 350.5)) is None
    assert agg.add_tick(_tick(20, 350.2)) is None


def test_add_tick_rollover_produces_bar_with_correct_ohlcv():
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, volume=10, minute=5))
    agg.add_tick(_tick(10, 351.0, volume=20, minute=5))
    agg.add_tick(_tick(20, 349.5, volume=5, minute=5))
    bar = agg.add_tick(_tick(0, 352.0, volume=8, minute=6))  # 다음 분 진입 → 5분 봉 flush

    assert bar is not None
    assert bar.open == 350.0
    assert bar.high == 351.0
    assert bar.low == 349.5
    assert bar.close == 349.5
    assert bar.volume == 35
    expected_vwap = (350.0 * 10 + 351.0 * 20 + 349.5 * 5) / 35
    assert bar.vwap == pytest.approx(expected_vwap)


def test_quality_flag_low_when_too_few_ticks():
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, minute=5))
    bar = agg.add_tick(_tick(0, 350.0, minute=6))
    assert bar.quality_flag == 1


def test_quality_flag_normal_with_enough_ticks():
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, minute=5))
    agg.add_tick(_tick(10, 350.1, minute=5))
    agg.add_tick(_tick(20, 350.2, minute=5))
    bar = agg.add_tick(_tick(0, 350.0, minute=6))
    assert bar.quality_flag == 0


def test_late_tick_before_current_bucket_is_ignored():
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, minute=5))
    agg.add_tick(_tick(0, 351.0, minute=6))  # 버킷을 6분으로 이동
    late = agg.add_tick(_tick(0, 999.0, minute=5))  # 이미 지난 5분 틱 (지연 도착)
    assert late is None


def test_flush_final_returns_last_bucket():
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, minute=5))
    agg.add_tick(_tick(10, 351.0, minute=5))
    bar = agg.flush_final()
    assert bar is not None
    assert bar.close == 351.0
    assert agg.flush_final() is None  # 이미 비워졌으므로 이후 호출은 None


def test_buy_sell_volume_uses_tick_rule():
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, volume=10, minute=5))  # baseline
    agg.add_tick(_tick(10, 351.0, volume=5, minute=5))   # 상승 → buy
    agg.add_tick(_tick(20, 349.0, volume=3, minute=5))   # 하락 → sell
    bar = agg.flush_final()
    assert bar.buy_volume == pytest.approx(15)  # 350(자기자신 baseline,>=prev) + 351
    assert bar.sell_volume == pytest.approx(3)
