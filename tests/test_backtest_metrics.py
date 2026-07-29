import pytest

from mahdi.backtest.metrics import SimulatedTrade, compute_metrics


def test_empty_trades_yields_all_zero_metrics():
    metrics = compute_metrics([])
    assert metrics.n_signals == 0
    assert metrics.win_rate == 0.0
    assert metrics.ev_after_cost == 0.0
    assert metrics.max_dd == 0.0
    assert metrics.cvar95 == 0.0


def test_all_winning_trades():
    metrics = compute_metrics([SimulatedTrade(net_pnl=1.0), SimulatedTrade(net_pnl=2.0)])
    assert metrics.n_signals == 2
    assert metrics.win_rate == 1.0
    assert metrics.ev_after_cost == 1.5
    assert metrics.max_dd == 0.0  # 계속 신고점 갱신 -> 낙폭 없음


def test_win_rate_counts_only_strictly_positive_pnl():
    metrics = compute_metrics([SimulatedTrade(net_pnl=0.0), SimulatedTrade(net_pnl=-1.0)])
    assert metrics.win_rate == 0.0


def test_max_dd_reflects_drawdown_from_running_peak():
    # 누적: +5, +2(peak5,dd-3), +6(peak6, dd0), +1(peak6, dd-5)
    trades = [SimulatedTrade(net_pnl=p) for p in (5.0, -3.0, 4.0, -5.0)]
    metrics = compute_metrics(trades)
    assert metrics.max_dd == -5.0


def test_cvar95_averages_worst_five_percent_with_minimum_one():
    # n=4 -> tail_count = max(1, round(0.2)) = 1 -> 최악 1건 평균
    trades = [SimulatedTrade(net_pnl=p) for p in (10.0, -1.0, -8.0, 3.0)]
    metrics = compute_metrics(trades)
    assert metrics.cvar95 == -8.0


def test_ev_after_cost_is_mean_pnl():
    trades = [SimulatedTrade(net_pnl=p) for p in (1.0, -1.0, 3.0)]
    metrics = compute_metrics(trades)
    assert metrics.ev_after_cost == pytest.approx(1.0)
