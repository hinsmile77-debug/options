import numpy as np
import pytest

from mahdi.features import regime_features

from mahdi.features.regime_features import (
    adx,
    book_thinning,
    cross_asset_stress,
    hurst_exponent,
    iv_change_rate,
    rv_ratio,
)


def _ar1_price_path(phi: float, n: int, seed: int) -> list[float]:
    """AR(1) 로그수익률(r_t = phi*r_{t-1} + noise)로 만든 가격 경로.

    phi>0 → 양의 자기상관(추세 지속, Hurst 상승 방향) / phi<0 → 음의 자기상관(평균회귀 방향).
    """
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.01, n)
    returns = [noise[0]]
    for i in range(1, n):
        returns.append(phi * returns[-1] + noise[i])
    price = 100.0
    prices = [price]
    for r in returns:
        price *= (1 + r)
        prices.append(price)
    return prices


def test_hurst_exponent_insufficient_data_returns_neutral():
    assert hurst_exponent([100.0, 101.0, 99.0]) == pytest.approx(0.5)


def test_hurst_exponent_distinguishes_trend_from_mean_reversion():
    trending = _ar1_price_path(phi=0.4, n=300, seed=1)
    mean_reverting = _ar1_price_path(phi=-0.4, n=300, seed=1)

    h_trend = hurst_exponent(trending)
    h_revert = hurst_exponent(mean_reverting)

    assert 0.0 <= h_trend <= 1.0
    assert 0.0 <= h_revert <= 1.0
    assert h_trend > h_revert


def test_adx_insufficient_data_returns_neutral():
    assert adx([1.0, 2.0], [0.5, 1.5], [0.8, 1.8]) == pytest.approx(20.0)


def test_adx_trending_series_scores_higher_than_sideways():
    n = 40
    trend_closes = [100.0 + i * 1.0 for i in range(n)]
    trend_highs = [c + 0.5 for c in trend_closes]
    trend_lows = [c - 0.5 for c in trend_closes]

    rng = np.random.default_rng(2)
    sideways_closes = [100.0 + float(rng.normal(0, 0.05)) for _ in range(n)]
    sideways_highs = [c + 0.5 for c in sideways_closes]
    sideways_lows = [c - 0.5 for c in sideways_closes]

    trend_adx = adx(trend_highs, trend_lows, trend_closes)
    sideways_adx = adx(sideways_highs, sideways_lows, sideways_closes)

    assert trend_adx > sideways_adx


def test_rv_ratio_insufficient_data_returns_neutral():
    assert rv_ratio([100.0] * 10) == pytest.approx(1.0)


def test_rv_ratio_recent_spike_exceeds_one():
    rng = np.random.default_rng(3)
    calm = 100 * np.cumprod(1 + rng.normal(0, 0.001, 16))
    volatile = calm[-1] * np.cumprod(1 + rng.normal(0, 0.02, 5))
    daily_closes = list(calm) + list(volatile)

    assert rv_ratio(daily_closes) > 1.3


def test_iv_change_rate_basic():
    assert iv_change_rate([0.20, 0.20, 0.24]) == pytest.approx(0.2)
    assert iv_change_rate([0.20]) == pytest.approx(0.0)
    assert iv_change_rate([0.0, 0.1]) == pytest.approx(0.0)


def test_book_thinning_flat_spread_returns_neutral():
    assert book_thinning([0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.0)


def test_book_thinning_spike_is_positive():
    spreads = [0.5, 0.51, 0.49, 0.5, 0.52, 3.0]
    assert book_thinning(spreads) > 2.0


def test_cross_asset_stress_neutral_when_no_data():
    assert cross_asset_stress() == 0.0
    assert cross_asset_stress([], [], []) == 0.0


def test_cross_asset_stress_neutral_when_all_series_flat():
    flat = [1.0, 1.0, 1.0, 1.0]
    assert cross_asset_stress(flat, flat, flat) == pytest.approx(0.0)


def test_cross_asset_stress_reacts_to_usdcnh_spike():
    usdcnh_spike = [6.78, 6.781, 6.779, 6.780, 6.900]
    assert cross_asset_stress([], usdcnh_spike, []) > 2.0


def test_cross_asset_stress_reacts_to_usdkrw_daily_spike():
    # USDKRW는 거래일 단위 값만 있으므로 day-over-day 급변이 실질적인 "급변" 단위다.
    usdkrw_spike = [1340.0, 1342.0, 1341.0, 1343.0, 1400.0]
    assert cross_asset_stress(usdkrw_spike, [], []) > 2.0


def test_cross_asset_stress_averages_across_available_series():
    # 세 시퀀스 중 하나만 데이터가 있으면 나머지는 중립(0.0)으로 취급해 평균을 낸다.
    usdcnh_spike = [6.78, 6.781, 6.779, 6.780, 6.900]
    only_usdcnh = cross_asset_stress([], usdcnh_spike, [])
    all_three_same_spike = cross_asset_stress(usdcnh_spike, usdcnh_spike, usdcnh_spike)
    assert only_usdcnh == pytest.approx(all_three_same_spike / 3)


# ===== 2026-08-03 §4 우선순위 6(HMM 드라이런에서 발견): z-score 폭발 =====


def test_series_zscore_treats_float_noise_variance_as_no_variance():
    """거의 상수인 계열에서 z-score가 폭발하면 안 된다.

    `std <= 0` 가드는 "완전히 불변"만 걸렀는데, 실제 데이터는 부동소수 잡음 때문에 std가 정확히
    0이 아니라 1e-13 같은 값이 된다 — 그러면 (x-mean)/1e-13 = 1e12가 그대로 통과했다.
    08-03 실측: `feature_store` 6,213행 중 13행의 cross_asset_stress가 1e11~1e12였고,
    대각 공분산 가우시안 HMM이 그 이상치 하나로 발산했다(6회 재시작 전부 비수렴, 로그우도 -2.2e22).
    us10y_yield/usdkrw는 KIS 일봉이라 하루 1~2개 값뿐이라 이 조건이 정상 운영에서 재현된다.
    """
    nearly_constant = [4.54, 4.54, 4.54 + 1e-13, 4.55]
    assert regime_features._series_zscore(nearly_constant) == 0.0


def test_series_zscore_is_clamped_to_a_sane_range():
    # z가 10을 넘는 순간 "얼마나 더 큰가"는 추가 정보가 아니라 잡음이다.
    exploding = [1.0, 1.0, 1.0000001, 500.0]
    z = regime_features._series_zscore(exploding)
    assert abs(z) <= regime_features._MAX_ABS_ZSCORE


def test_series_zscore_keeps_ordinary_values_untouched():
    # 클램프가 정상 범위 값까지 건드리면 피처가 뭉개진다.
    ordinary = [10.0, 12.0, 8.0, 11.0, 14.0]
    z = regime_features._series_zscore(ordinary)
    assert 0.0 < abs(z) < regime_features._MAX_ABS_ZSCORE


def test_cross_asset_stress_stays_bounded_for_near_constant_series():
    # 08-03 실측 최대 1.05e12 → 클램프 이후 상한은 _MAX_ABS_ZSCORE.
    stress = regime_features.cross_asset_stress(
        usdkrw_daily_series=[1352.3, 1352.3, 1352.3 + 1e-12, 1360.0],
        usdcnh_recent_series=[6.78, 6.78, 6.78, 6.79],
        us10y_daily_series=[4.54, 4.54, 4.54, 4.60],
    )
    assert 0.0 <= stress <= regime_features._MAX_ABS_ZSCORE
