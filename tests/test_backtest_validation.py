import pytest

from mahdi.backtest.validation import (
    deflated_sharpe_ratio,
    monte_carlo_resample,
    walk_forward_splits,
)


def test_walk_forward_splits_anchored_train_grows_each_fold():
    splits = walk_forward_splits(10, 3, embargo=0)
    assert len(splits) == 3
    assert splits[0].train_indices == range(0, 0)
    assert splits[0].test_indices == range(0, 3)
    assert splits[1].train_indices == range(0, 3)
    assert splits[1].test_indices == range(3, 6)
    assert splits[2].train_indices == range(0, 6)
    assert splits[2].test_indices == range(6, 10)  # 마지막 fold가 나머지 전부를 흡수


def test_walk_forward_splits_embargo_shrinks_train_end():
    splits = walk_forward_splits(10, 3, embargo=1)
    assert splits[1].train_indices == range(0, 2)  # embargo=1만큼 test_start(3)에서 뺌
    assert splits[2].train_indices == range(0, 5)


def test_walk_forward_splits_invalid_inputs_return_empty():
    assert walk_forward_splits(0, 3) == []
    assert walk_forward_splits(10, 0) == []


def test_monte_carlo_resample_empty_trades_returns_zeros():
    result = monte_carlo_resample([], 100, seed=1)
    assert result.mean_final_pnl == 0.0
    assert result.worst_max_dd == 0.0


def test_monte_carlo_resample_is_reproducible_with_same_seed():
    first = monte_carlo_resample([1.0, 2.0, 3.0], 500, seed=42)
    second = monte_carlo_resample([1.0, 2.0, 3.0], 500, seed=42)
    assert first == second


def test_monte_carlo_resample_different_seed_can_differ():
    first = monte_carlo_resample([1.0, 2.0, 3.0], 500, seed=42)
    second = monte_carlo_resample([1.0, 2.0, 3.0], 500, seed=7)
    assert first != second


def test_monte_carlo_resample_all_positive_pnls_never_draws_down():
    result = monte_carlo_resample([1.0, 2.0, 3.0], 200, seed=1)
    assert result.worst_max_dd == 0.0
    assert result.mean_final_pnl > 0


def test_deflated_sharpe_ratio_single_trial_equal_to_observed_is_half():
    # N=1 -> 팽창 없음(그 시행값 그대로 기준선) -> observed==기준선 -> Phi(0)=0.5
    assert deflated_sharpe_ratio([0.5], 0.5, n_observations=100) == pytest.approx(0.5)


def test_deflated_sharpe_ratio_zero_variance_trials_uses_mean_as_baseline():
    trials = [0.3, 0.3, 0.3, 0.3]  # 분산 0 -> 기준선(SR0)=평균=0.3, 팽창 없음
    assert deflated_sharpe_ratio(trials, 0.3, n_observations=100) == pytest.approx(0.5)
    assert deflated_sharpe_ratio(trials, 0.5, n_observations=100) > 0.5  # 관측이 기준선보다 높음
    assert deflated_sharpe_ratio(trials, 0.1, n_observations=100) < 0.5  # 관측이 기준선보다 낮음


def test_deflated_sharpe_ratio_decreases_as_number_of_trials_grows():
    # 같은 관측 Sharpe(0.5)라도 시행 횟수(다중검정)가 늘수록 "우연일 가능성"에 대한
    # 눈높이가 높아져 DSR이 낮아져야 한다 — 다중검정 보정의 핵심 성질.
    dsr_n2 = deflated_sharpe_ratio([0.1, 0.5], 0.5, n_observations=100)
    dsr_n10 = deflated_sharpe_ratio([0.1, 0.5] * 5, 0.5, n_observations=100)
    dsr_n100 = deflated_sharpe_ratio([0.1, 0.5] * 50, 0.5, n_observations=100)
    assert dsr_n2 > dsr_n10 > dsr_n100


def test_deflated_sharpe_ratio_no_trials_history_uses_zero_baseline():
    # trial_sharpe_ratios가 비어 있으면 다중검정 보정 없이 SR0=0 -> Phi(observed*sqrt(n-1))
    result = deflated_sharpe_ratio([], observed_sharpe=0.5, n_observations=100)
    assert result > 0.5  # 양의 관측 Sharpe는 SR0=0보다 높음


def test_deflated_sharpe_ratio_rejects_insufficient_observations():
    with pytest.raises(ValueError):
        deflated_sharpe_ratio([0.5], 0.5, n_observations=1)
