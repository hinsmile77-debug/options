from mahdi.engines.regime import RegimeLabel
from mahdi.fusion.strategy_palette import (
    NON_ENTRY_STRATEGIES,
    enforce_daily_strategy_cap,
    enforce_reentry_cooldown,
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


# --- 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 6 / Fix#5): 하루 상한이 실제로 걸리는가 ---


def test_daily_cap_blocks_a_third_fresh_strategy_once_the_cap_is_reached():
    """**이것이 08-05까지 안 걸리던 경우다.**

    종전 구현은 `(continuing + fresh)[:cap]`이라 **이번 호출의 목록 길이**만 잘랐다.
    그런데 §11.4 매트릭스 셀은 전부 원소 1개짜리라 `[:2]`가 자를 일이 없었다 —
    오늘 이미 A·B를 썼어도 세 번째 전략 C가 그대로 통과했다.
    """
    result = enforce_daily_strategy_cap(
        ["small_strangle_buy"], already_used_today=frozenset({"atm_long", "debit_spread"}), cap=2
    )
    assert result == []


def test_daily_cap_always_allows_continuing_an_already_used_strategy():
    """연속 사용은 새 알파 원천을 추가하는 행위가 아니다 — 상한에 걸리면 안 된다."""
    result = enforce_daily_strategy_cap(
        ["atm_long"], already_used_today=frozenset({"atm_long", "debit_spread"}), cap=2
    )
    assert result == ["atm_long"]


def test_daily_cap_leaves_room_for_exactly_the_unused_slots():
    result = enforce_daily_strategy_cap(
        ["fresh_a", "fresh_b"], already_used_today=frozenset({"used_c"}), cap=2
    )
    assert result == ["fresh_a"]  # 남은 슬롯 1개


def test_daily_cap_is_a_noop_when_nothing_used_yet():
    result = enforce_daily_strategy_cap(["atm_long"], already_used_today=frozenset(), cap=2)
    assert result == ["atm_long"]


def test_matrix_cells_are_single_strategy_which_is_why_the_old_cap_never_bound():
    """상한이 무력이었던 **구조적 이유**를 고정한다 — 셀이 1개짜리라 길이 자르기로는 못 막는다.
    나중에 셀이 여러 전략을 담게 되면 이 테스트가 깨지고, 그때 상한 설계를 다시 봐야 한다."""
    for regime in (RegimeLabel.TREND_UP_STRONG, RegimeLabel.RANGE_BALANCED,
                   RegimeLabel.VOL_EXPANSION, RegimeLabel.VOL_COMPRESSION):
        for vrp in (-0.05, 0.0, 0.1):
            assert len(select_strategies(regime, vrp).allowed_strategies) <= 1


# ===== 2026-08-11 고도화 D — 동일 전략 재진입 쿨다운 =====
#
# 08-11에 ENTER 281건/494분(56.9%)이 분 단위로 연속했다. 하루 상한은 **가짓수**를 막고
# 같은 전략의 연속 사용은 `continuing`으로 의도적으로 면제하므로, 이 패턴을 못 막는다.


def test_cooldown_is_a_no_op_when_disabled():
    """레버 OFF(0)는 **종전과 바이트 단위로 같아야** 한다 — 오늘 실린 것은 기계뿐이다."""
    allowed = ["straddle_accumulate", "small_strangle_buy"]
    assert enforce_reentry_cooldown(allowed, {"straddle_accumulate": 1.0}, 0) == allowed
    assert enforce_reentry_cooldown(allowed, {"straddle_accumulate": 1.0}, -5) == allowed


def test_cooldown_blocks_only_the_strategy_that_is_still_cooling():
    allowed = ["straddle_accumulate", "small_strangle_buy"]
    elapsed = {"straddle_accumulate": 3.0, "small_strangle_buy": 20.0}

    assert enforce_reentry_cooldown(allowed, elapsed, 15) == ["small_strangle_buy"]


def test_a_strategy_with_no_prior_entry_passes():
    """오늘 첫 진입은 막지 않는다 — 쿨다운은 «다시» 들어가는 것을 막는 규칙이다."""
    assert enforce_reentry_cooldown(["straddle_accumulate"], {}, 15) == ["straddle_accumulate"]


def test_elapsed_exactly_at_the_cooldown_passes():
    """경계는 통과다 — «15분 쿨다운»은 15분 뒤에 다시 들어갈 수 있다는 뜻이다."""
    assert enforce_reentry_cooldown(["s"], {"s": 15.0}, 15) == ["s"]
    assert enforce_reentry_cooldown(["s"], {"s": 14.99}, 15) == []
