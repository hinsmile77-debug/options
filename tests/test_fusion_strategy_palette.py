from mahdi.engines.regime import RegimeLabel
from mahdi.fusion.strategy_palette import enforce_daily_strategy_cap, select_strategies


def test_defensive_regimes_always_return_empty():
    for regime in (RegimeLabel.LIQUIDITY_THIN, RegimeLabel.CRISIS_DEFENSE):
        result = select_strategies(regime, vrp=0.1)
        assert result.allowed_strategies == []
        assert result.reason == "defensive_regime_no_new_entries"


def test_trend_strong_underpriced_allows_atm_long():
    result = select_strategies(RegimeLabel.TREND_UP_STRONG, vrp=-0.05)
    assert result.allowed_strategies == ["atm_long"]


def test_trend_strong_overpriced_allows_debit_spread():
    result = select_strategies(RegimeLabel.TREND_DOWN_STRONG, vrp=0.05)
    assert result.allowed_strategies == ["debit_spread"]


def test_vol_expansion_overpriced_forbids_short_gamma_even_with_gates_open():
    result = select_strategies(
        RegimeLabel.VOL_EXPANSION,
        vrp=0.05,
        highest_confidence=True,
        positive_gex=True,
        stable_regime=True,
    )
    assert result.allowed_strategies == []
    assert result.reason == "no_strategy_for_this_cell"


def test_range_tight_overpriced_premium_sell_blocked_without_gates():
    result = select_strategies(RegimeLabel.RANGE_BALANCED, vrp=0.05)
    assert result.allowed_strategies == []
    assert result.reason == "short_gamma_requires_not_met"


def test_range_tight_overpriced_premium_sell_allowed_with_all_gates_open():
    result = select_strategies(
        RegimeLabel.RANGE_BALANCED,
        vrp=0.05,
        highest_confidence=True,
        positive_gex=True,
        stable_regime=True,
    )
    assert result.allowed_strategies == ["limited_premium_sell"]


def test_vrp_within_neutral_band_is_fair():
    result = select_strategies(RegimeLabel.VOL_EXPANSION, vrp=0.01)
    assert result.allowed_strategies == ["long_gamma"]


def test_enforce_daily_strategy_cap_prioritizes_continuing_strategies():
    result = enforce_daily_strategy_cap(
        ["fresh_a", "fresh_b", "continuing_c"],
        already_used_today=frozenset({"continuing_c"}),
        cap=2,
    )
    assert result == ["continuing_c", "fresh_a"]


def test_enforce_daily_strategy_cap_zero_cap_blocks_everything():
    result = enforce_daily_strategy_cap(["a"], already_used_today=frozenset({"a"}), cap=0)
    assert result == []
