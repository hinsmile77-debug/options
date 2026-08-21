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


# ===== 2026-08-21: 거래소 분류가 틱 룰 추정을 대체한다 =====
#
# KIS 실시간 체결 프레임에는 틱 단위 체결구분(공격자 플래그)이 없지만 **누적 매수/매도 수량**이
# 있다(H0IFCNT0 idx 41/42, H0IOCNT0 idx 48/49). 봉의 매수량 = 그 누적값의 차분이고, 그것은
# 추정이 아니라 거래소 판정이다.


def _cum_tick(second: int, price: float, volume: float, cum, minute: int = 5) -> Tick:
    """cum = (누적거래량, 누적매수, 누적매도). None을 주면 누적 필드가 없는 프레임을 흉내 낸다."""
    cv, cb, cs = cum if cum is not None else (None, None, None)
    return Tick(
        timestamp=datetime(2026, 8, 21, 9, minute, second),
        price=price, volume=volume,
        bid_px=price - 0.05, bid_qty=100, ask_px=price + 0.05, ask_qty=100,
        cum_volume=cv, cum_buy_volume=cb, cum_sell_volume=cs,
    )


def test_exchange_cumulative_volumes_replace_the_tick_rule():
    agg = MinuteBarAggregator()
    agg.add_tick(_cum_tick(0, 100.0, 10, (1000, 600, 400), minute=5))   # 기준선
    agg.add_tick(_cum_tick(0, 100.0, 90, (1090, 670, 420), minute=6))   # 6분봉

    agg.add_tick(_cum_tick(0, 100.0, 10, (1100, 675, 425), minute=7))   # 6분봉 마감
    bar = agg.flush_final()  # 7분봉

    # 6분봉의 Δ: 거래량 90 = 매수 70 + 매도 20. 가격이 전혀 안 움직였으므로 틱 룰이었다면 0/0이다.
    assert bar.volume_source == "exchange"


def test_exchange_delta_is_taken_across_the_bar_boundary():
    agg = MinuteBarAggregator()
    agg.add_tick(_cum_tick(0, 100.0, 10, (1000, 600, 400), minute=5))
    completed = agg.add_tick(_cum_tick(0, 100.0, 100, (1100, 670, 430), minute=6))

    assert completed is not None  # 5분봉
    bar6 = agg.flush_final()

    assert bar6.buy_volume == pytest.approx(70)   # 670 - 600
    assert bar6.sell_volume == pytest.approx(30)  # 430 - 400
    assert bar6.volume_source == "exchange"


def test_falls_back_to_the_tick_rule_when_the_frame_has_no_cumulative_fields():
    # 짧은 프레임·리플레이·과거 픽스처 — 누적 필드가 없다고 틱을 버리지 않는다.
    agg = MinuteBarAggregator()
    agg.add_tick(_cum_tick(0, 100.0, 10, None, minute=5))
    agg.add_tick(_cum_tick(0, 99.0, 10, None, minute=6))

    bar = agg.flush_final()

    assert bar.volume_source == "tick_rule"
    assert bar.sell_volume == pytest.approx(10)  # 하락 틱


def test_self_check_rejects_cumulative_fields_that_do_not_add_up():
    """`Δ매수 + Δ매도`가 `Δ누적거래량`과 크게 어긋나면 **우리가 잘못 읽은 것**이다.

    두 누적량은 같은 프레임에서 오므로 원리상 합이 맞아야 한다. 필드 인덱스가 틀리면 시각이나
    가격 같은 엉뚱한 숫자가 들어와 자릿수가 통째로 달라진다 — 그때 조용히 그 값을 쓰면
    2026-08-21 오전에 CVD를 직선으로 만든 사고가 재연된다. 티가 나게 추정으로 되돌아간다.
    """
    agg = MinuteBarAggregator()
    agg.add_tick(_cum_tick(0, 100.0, 10, (1000, 600, 400), minute=5))
    # 거래량은 100 늘었는데 매수/매도 합은 9밖에 안 는다 → 못 믿는다.
    agg.add_tick(_cum_tick(0, 99.0, 100, (1100, 604, 405), minute=6))

    bar = agg.flush_final()

    assert bar.volume_source == "tick_rule"


def test_a_session_reset_does_not_produce_negative_volumes():
    # 장이 바뀌면 누적이 0부터 다시 센다 — 차분이 음수가 되므로 그 봉만 추정으로 가고
    # 기준선은 새 값으로 갱신돼야 한다(안 그러면 다음 봉이 통째로 부풀어 오른다).
    agg = MinuteBarAggregator()
    agg.add_tick(_cum_tick(0, 100.0, 10, (9000, 5000, 4000), minute=5))
    agg.add_tick(_cum_tick(0, 100.0, 10, (50, 30, 20), minute=6))       # 리셋
    reset_bar = agg.add_tick(_cum_tick(0, 100.0, 10, (150, 80, 70), minute=7))

    assert reset_bar is not None and reset_bar.volume_source == "tick_rule"
    bar7 = agg.flush_final()
    assert bar7.buy_volume == pytest.approx(50)   # 80 - 30, 새 기준선에서 정상 재개
    assert bar7.sell_volume == pytest.approx(50)  # 70 - 20
    assert bar7.volume_source == "exchange"
