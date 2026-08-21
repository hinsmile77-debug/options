from datetime import datetime

import pytest

from mahdi.data.collector import MinuteBarAggregator, Tick, VolumeBucketAggregator


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
    agg.add_tick(_tick(0, 350.0, volume=10, minute=5))  # 세션 첫 틱, 정확히 중간값 → 모름
    agg.add_tick(_tick(10, 351.0, volume=5, minute=5))   # 상승 → buy
    agg.add_tick(_tick(20, 349.0, volume=3, minute=5))   # 하락 → sell
    bar = agg.flush_final()
    assert bar.buy_volume == pytest.approx(5)
    assert bar.sell_volume == pytest.approx(3)
    # 가를 근거가 없던 첫 틱 10은 어느 쪽에도 안 들어간다 — 합이 volume보다 작을 수 있다.
    assert bar.volume == pytest.approx(18)


# ===== 2026-08-21: 틱 룰 편향 수정 회귀 =====
#
# 종전 규칙은 `p >= prev_price` + 봉마다 새로 잡는 baseline이었다. 동가 틱이 전부 매수로 가고
# 봉의 첫 틱도 자기 자신과 비교돼 항상 매수였다 — 08-21 실측 A01609 매수비율 59.7%, COCKPIT의
# CVD가 가격이 제자리인 한 시간 동안 부호 전환 없이 곧게 올랐다.


def test_unchanged_price_ticks_are_not_counted_as_buys():
    """가격이 한 번도 안 움직인 봉은 **매수 압력이 0이어야 한다.** 종전엔 100% 매수였다."""
    agg = MinuteBarAggregator()
    for sec in (0, 10, 20, 30, 40):
        agg.add_tick(_tick(sec, 350.0, volume=10, minute=5))

    bar = agg.flush_final()

    assert bar.buy_volume == pytest.approx(0)
    assert bar.sell_volume == pytest.approx(0)


def test_zero_tick_inherits_the_previous_direction():
    # 표준 틱 룰(zero-uptick/zero-downtick) — 동가 틱은 직전 분류를 승계한다.
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, volume=1, minute=5))    # 첫 틱(중간값) → 모름
    agg.add_tick(_tick(10, 349.0, volume=10, minute=5))  # 하락 → sell
    agg.add_tick(_tick(20, 349.0, volume=20, minute=5))  # 동가 → 직전(sell) 승계
    agg.add_tick(_tick(30, 350.0, volume=5, minute=5))   # 상승 → buy
    agg.add_tick(_tick(40, 350.0, volume=7, minute=5))   # 동가 → 직전(buy) 승계

    bar = agg.flush_final()

    assert bar.sell_volume == pytest.approx(30)
    assert bar.buy_volume == pytest.approx(12)


def test_tick_rule_baseline_carries_across_bar_boundaries():
    """봉의 첫 틱은 **직전 봉의 마지막 체결**과 비교한다 — 자기 자신과 비교하면 항상 매수다."""
    agg = MinuteBarAggregator()
    agg.add_tick(_tick(0, 350.0, volume=1, minute=5))
    agg.add_tick(_tick(10, 351.0, volume=1, minute=5))  # 상승 → buy
    completed = agg.add_tick(_tick(0, 349.0, volume=10, minute=6))  # 5분봉 마감, 6분봉 첫 틱

    assert completed is not None and completed.minute.minute == 5
    bar6 = agg.flush_final()

    # 349는 직전 봉 마지막 체결(351)보다 낮으므로 매도다. 종전 규칙에서는 매수였다.
    assert bar6.sell_volume == pytest.approx(10)
    assert bar6.buy_volume == pytest.approx(0)


def test_first_tick_of_session_is_split_by_the_quote_midpoint():
    # 비교할 직전 체결이 없을 때만 쓰는 quote test(Lee-Ready). 중간값보다 비싸면 매수.
    agg = MinuteBarAggregator()
    agg.add_tick(Tick(datetime(2026, 8, 21, 9, 0, 0), price=350.04, volume=9,
                      bid_px=350.0, bid_qty=100, ask_px=350.05, ask_qty=100))

    bar = agg.flush_final()

    assert bar.buy_volume == pytest.approx(9)  # 중간값 350.025보다 비싸게 체결
    assert bar.sell_volume == pytest.approx(0)


def test_volume_bucket_returns_none_until_bucket_size_reached():
    agg = VolumeBucketAggregator(bucket_size=50)
    assert agg.add_tick(price=100.0, volume=20) is None
    assert agg.add_tick(price=101.0, volume=20) is None


def test_volume_bucket_closes_and_resets_on_reaching_size():
    agg = VolumeBucketAggregator(bucket_size=50)
    agg.add_tick(price=100.0, volume=20)
    agg.add_tick(price=101.0, volume=20)
    bucket = agg.add_tick(price=102.0, volume=15)  # 누적 55 >= 50 → 마감

    assert bucket is not None
    assert bucket.open_to_close_return == pytest.approx((102.0 - 100.0) / 100.0)
    assert bucket.volume == pytest.approx(55)

    # 리셋 확인 — 다음 틱부터 새 버킷
    assert agg.add_tick(price=200.0, volume=10) is None


def test_volume_bucket_ignores_non_positive_volume_ticks():
    agg = VolumeBucketAggregator(bucket_size=10)
    assert agg.add_tick(price=100.0, volume=0) is None
    bucket = agg.add_tick(price=105.0, volume=10)  # 0짜리 틱은 시가에 영향 안 줌
    assert bucket is not None
    assert bucket.open_to_close_return == pytest.approx(0.0)  # 시가=종가=105.0


def test_volume_bucket_invalid_size_raises():
    with pytest.raises(ValueError):
        VolumeBucketAggregator(bucket_size=0)
