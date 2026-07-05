from statistics import NormalDist

import pytest

from mahdi.features.orderflow import (
    BookSnapshot,
    absorption_score,
    calculate_ofi,
    calculate_vpin,
    microprice,
    queue_imbalance,
)


def test_ofi_empty_or_single_snapshot_is_zero():
    assert calculate_ofi([]) == 0.0
    assert calculate_ofi([BookSnapshot(100, 10, 101, 10)]) == 0.0


def test_ofi_bid_price_up_and_ask_price_down():
    snap0 = BookSnapshot(bid_px=100, bid_qty=10, ask_px=102, ask_qty=10)
    snap1 = BookSnapshot(bid_px=101, bid_qty=5, ask_px=101, ask_qty=7)
    # delta_bid = curr.bid_qty (price rose) = 5
    # delta_ask = curr.ask_qty (price fell) = 7
    assert calculate_ofi([snap0, snap1]) == pytest.approx(5 - 7)


def test_ofi_same_price_uses_qty_delta():
    snap0 = BookSnapshot(bid_px=100, bid_qty=10, ask_px=101, ask_qty=10)
    snap1 = BookSnapshot(bid_px=100, bid_qty=15, ask_px=101, ask_qty=10)
    assert calculate_ofi([snap0, snap1]) == pytest.approx(5.0)


def test_ofi_accumulates_over_window():
    snaps = [
        BookSnapshot(100, 10, 101, 10),
        BookSnapshot(100, 15, 101, 10),   # e = +5
        BookSnapshot(100, 15, 101, 12),   # e = -2
    ]
    assert calculate_ofi(snaps) == pytest.approx(5 - 2)


def test_microprice_symmetric_qty_is_midprice():
    assert microprice(bid_px=100, bid_qty=10, ask_px=102, ask_qty=10) == pytest.approx(101.0)


def test_microprice_leans_toward_ask_when_bid_qty_dominant():
    mp = microprice(bid_px=100, bid_qty=90, ask_px=102, ask_qty=10)
    assert mp > 101.0


def test_microprice_zero_liquidity_falls_back_to_midprice():
    assert microprice(100, 0, 102, 0) == pytest.approx(101.0)


def test_queue_imbalance_symmetric_is_zero():
    assert queue_imbalance(10, 10) == 0.0


def test_queue_imbalance_bid_heavy_is_positive():
    assert queue_imbalance(80, 20) == pytest.approx(0.6)


def test_queue_imbalance_no_liquidity_is_zero():
    assert queue_imbalance(0, 0) == 0.0


def test_vpin_zero_variance_returns_is_zero():
    # 모든 버킷 수익률이 동일(분산 0) → 매수비율 0.5 폴백 → 불균형 0
    assert calculate_vpin([1.0, 1.0, 1.0, 1.0], [100, 100, 100, 100]) == 0.0


def test_vpin_known_value_alternating_returns():
    returns = [1.0, -1.0, 1.0, -1.0]
    volumes = [10.0, 10.0, 10.0, 10.0]
    p_up = NormalDist().cdf(1.0)  # sigma=1 (population std of [1,-1,1,-1])
    expected_diff_per_bucket = 10.0 * abs(2 * p_up - 1)
    expected = (4 * expected_diff_per_bucket) / 40.0
    assert calculate_vpin(returns, volumes) == pytest.approx(expected)


def test_vpin_no_buckets_is_zero():
    assert calculate_vpin([], []) == 0.0


def test_absorption_score_flags_high_volume_flat_price():
    assert absorption_score(traded_volume=300, avg_volume=100, price_change=0.0001) == pytest.approx(3.0)


def test_absorption_score_zero_when_price_moves_too_much():
    assert absorption_score(traded_volume=300, avg_volume=100, price_change=0.01) == 0.0


def test_absorption_score_zero_when_no_baseline():
    assert absorption_score(traded_volume=300, avg_volume=0, price_change=0.0) == 0.0
