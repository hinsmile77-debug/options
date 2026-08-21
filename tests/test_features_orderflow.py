from statistics import NormalDist

import pytest

from mahdi.features.orderflow import (
    BookSnapshot,
    absorption_score,
    flat_range_limit,
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
    # 범위 0.2가 정체 상한 0.5 이내 → 거래량 배수 그대로.
    assert absorption_score(traded_volume=300, avg_volume=100, price_range=0.2, flat_limit=0.5) == pytest.approx(3.0)


def test_absorption_score_zero_when_price_moves_too_much():
    assert absorption_score(traded_volume=300, avg_volume=100, price_range=1.2, flat_limit=0.5) == 0.0


def test_absorption_score_zero_when_no_baseline():
    assert absorption_score(traded_volume=300, avg_volume=0, price_range=0.0, flat_limit=0.5) == 0.0


def test_absorption_gate_uses_the_full_bar_range_not_the_net_change():
    """2026-08-21 회귀 — 봉 안에서 왕복한 봉을 「정체」로 세면 안 된다.

    A01609 실측에서 종전 규칙이 정체로 판정한 28봉이 **28봉 전부** 봉 안에서는 문턱보다 크게
    움직이고 있었다(예: 시가=종가 1072.60인데 고가 1073.35 / 저가 1070.85). 왕복은 가격 충격
    계수 λ가 낮았다는 뜻이 아니다.
    """
    # 시가와 종가가 같아 순변화는 0이지만, 봉 범위는 상한의 5배다.
    assert absorption_score(traded_volume=300, avg_volume=100, price_range=2.5, flat_limit=0.5) == 0.0


def test_flat_range_limit_scales_with_the_instruments_own_recent_ranges():
    # 문턱은 종목 자신의 최근 변동성에서 나온다 — 고정 상수는 두 상품 중 한쪽에서 무의미해진다.
    busy = flat_range_limit([2.0, 2.6, 3.0, 2.4, 2.6], tick_size=0.05)
    quiet = flat_range_limit([0.2, 0.3, 0.25, 0.2, 0.3], tick_size=0.05)

    assert busy == pytest.approx(1.3)   # 중앙값 2.6 x 0.5
    assert quiet == pytest.approx(0.125)  # 중앙값 0.25 x 0.5
    assert busy > quiet


def test_flat_range_limit_never_falls_below_the_tick_floor():
    """시장이 죽어 중앙값이 0이어도 문턱이 0이 되면 어떤 봉도 통과 못 한다 — 2틱이 하한이다."""
    assert flat_range_limit([0.0, 0.0, 0.0], tick_size=0.05) == pytest.approx(0.1)
    assert flat_range_limit([], tick_size=0.01) == pytest.approx(0.02)
