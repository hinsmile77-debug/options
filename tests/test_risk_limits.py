from mahdi.risk.limits import AccountState, check_limits

_RISK_LIMITS = {
    "limits": {
        "per_trade_loss_pct": -0.005,
        "daily_loss_pct": -0.02,
        "weekly_loss_pct": -0.05,
        "max_drawdown_pct": -0.10,
        "max_same_direction_positions": 3,
        "max_daily_trades_per_strategy": 5,
    }
}


def _account(**overrides) -> AccountState:
    base = dict(
        daily_pnl_pct=0.0,
        weekly_pnl_pct=0.0,
        drawdown_pct=0.0,
        same_direction_positions=0,
        daily_trades_by_strategy={},
        pending_trade_loss_pct=0.0,
    )
    base.update(overrides)
    return AccountState(**base)


def test_all_within_limits_passes():
    result = check_limits(_account(), "vrp_harvest", risk_limits=_RISK_LIMITS)
    assert result.passed
    assert result.violations == []


def test_per_trade_loss_violation():
    result = check_limits(
        _account(pending_trade_loss_pct=-0.006), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert not result.passed
    assert any(v.limit_name == "per_trade_loss_pct" for v in result.violations)


def test_per_trade_loss_exactly_at_limit_passes():
    # -0.005 == 한도이므로 위반 아님(엄격 부등호 <)
    result = check_limits(
        _account(pending_trade_loss_pct=-0.005), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert result.passed


def test_daily_loss_violation():
    result = check_limits(
        _account(daily_pnl_pct=-0.021), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert not result.passed
    assert any(v.limit_name == "daily_loss_pct" for v in result.violations)


def test_weekly_loss_violation():
    result = check_limits(
        _account(weekly_pnl_pct=-0.06), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert not result.passed
    assert any(v.limit_name == "weekly_loss_pct" for v in result.violations)


def test_max_drawdown_violation():
    result = check_limits(
        _account(drawdown_pct=-0.11), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert not result.passed
    assert any(v.limit_name == "max_drawdown_pct" for v in result.violations)


def test_same_direction_position_cap_violation():
    result = check_limits(
        _account(same_direction_positions=3), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert not result.passed
    assert any(v.limit_name == "max_same_direction_positions" for v in result.violations)


def test_same_direction_position_below_cap_passes():
    result = check_limits(
        _account(same_direction_positions=2), "vrp_harvest", risk_limits=_RISK_LIMITS
    )
    assert result.passed


def test_max_daily_trades_per_strategy_is_scoped_to_strategy_id():
    account = _account(daily_trades_by_strategy={"vrp_harvest": 5, "gamma_scalp": 0})
    result = check_limits(account, "vrp_harvest", risk_limits=_RISK_LIMITS)
    assert not result.passed
    assert any(v.limit_name == "max_daily_trades_per_strategy" for v in result.violations)

    result_other_strategy = check_limits(account, "gamma_scalp", risk_limits=_RISK_LIMITS)
    assert result_other_strategy.passed


def test_multiple_violations_are_all_reported_not_just_first():
    result = check_limits(
        _account(daily_pnl_pct=-0.03, weekly_pnl_pct=-0.06, drawdown_pct=-0.15),
        "vrp_harvest",
        risk_limits=_RISK_LIMITS,
    )
    violated_names = {v.limit_name for v in result.violations}
    assert violated_names == {"daily_loss_pct", "weekly_loss_pct", "max_drawdown_pct"}


def test_portfolio_greeks_unconfigured_is_flagged_not_silently_skipped():
    result = check_limits(_account(), "vrp_harvest", risk_limits=_RISK_LIMITS)
    assert "portfolio_greeks" in result.unconfigured_checks


def test_portfolio_greeks_violation_when_limits_provided():
    result = check_limits(
        _account(),
        "vrp_harvest",
        risk_limits=_RISK_LIMITS,
        portfolio_greeks_limits={"delta": 100.0},
        current_portfolio_greeks={"delta": 150.0},
    )
    assert not result.passed
    assert any(v.limit_name == "portfolio_delta" for v in result.violations)
    assert "portfolio_greeks" not in result.unconfigured_checks
