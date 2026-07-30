from mahdi.engines.regime import RegimeLabel
from mahdi.fusion.strategy_palette import (
    NON_ENTRY_STRATEGIES,
    enforce_daily_strategy_cap,
    entry_strategies,
    select_strategies,
)


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


# --- 2026-07-30(운영점검보고서 §2-2/§4 Fix#1): 관망 전략 계수 오류 회귀 방지 ---


def test_range_balanced_fair_vrp_returns_wait_and_see_which_is_not_an_entry():
    # 07-30 하루 419건의 잘못된 ENTER가 정확히 이 셀에서 나왔다 — 팔레트는 여전히
    # wait_and_see를 반환해야 하지만(v6 §11.4 정당한 셀 값), 진입 후보로는 세지 않아야 한다.
    result = select_strategies(RegimeLabel.RANGE_BALANCED, vrp=0.0)
    assert result.allowed_strategies == ["wait_and_see"]
    assert entry_strategies(result.allowed_strategies) == []


def test_vol_compression_fair_vrp_breakout_wait_is_not_an_entry_either():
    result = select_strategies(RegimeLabel.VOL_COMPRESSION, vrp=0.0)
    assert result.allowed_strategies == ["breakout_wait"]
    assert entry_strategies(result.allowed_strategies) == []


def test_entry_strategies_keeps_real_strategies_and_preserves_order():
    assert entry_strategies(["wait_and_see", "atm_long", "breakout_wait", "debit_spread"]) == [
        "atm_long", "debit_spread",
    ]


def test_entry_strategies_on_empty_palette_is_empty():
    assert entry_strategies([]) == []


def test_non_entry_strategies_are_all_actually_produced_by_the_matrix():
    # NON_ENTRY_STRATEGIES에 매트릭스에 없는 오타가 들어가면 필터가 조용히 무력해진다 —
    # 두 값 모두 실제 팔레트 셀에서 나오는지 확인한다.
    produced = set(select_strategies(RegimeLabel.RANGE_BALANCED, vrp=0.0).allowed_strategies)
    produced |= set(select_strategies(RegimeLabel.VOL_COMPRESSION, vrp=0.0).allowed_strategies)
    assert NON_ENTRY_STRATEGIES == produced
