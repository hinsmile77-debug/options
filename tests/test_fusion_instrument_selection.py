"""§11.5 Instrument Selection — 전략명이 실제 종목이 되는 지점의 계약.

이 파일이 지키는 것은 "좋은 행사가를 골랐는가"가 아니다(체결이 0건인 동안 그것은 정의상
관측 불가다). **규칙대로 골랐는가, 못 고른 이유가 남는가** 둘뿐이다.
"""

from datetime import date

import pytest

from mahdi.fusion.instrument_selection import (
    REASON_NO_CHAIN,
    REASON_NO_ELIGIBLE_BOOK,
    REASON_NO_LIQUID_INSTRUMENT,
    REASON_NO_SELECTION_RULE,
    REASON_NO_STRIKE_MATCH,
    REASON_SPOT_UNAVAILABLE,
    ChainLeg,
    LiquidityThresholds,
    is_expiry_day,
    legs_from_chain_snapshot,
    liquidity_reject_reason,
    select_book,
    select_instruments,
)

TODAY = date(2026, 8, 17)
NEAR = date(2026, 8, 18)  # 화요일 만기(대체공휴일 이월) — 요일 규칙으로는 안 나오는 날짜
FAR = date(2026, 9, 10)


def _chain(expiry=NEAR, strikes=(340.0, 345.0, 350.0, 355.0, 360.0), *, delta_scale=True, **overrides):
    """ATM 350 근처의 단순 체인. 델타는 행사가에서 단조 감소하도록 만든다."""
    legs = []
    for strike in strikes:
        for option_type in ("C", "P"):
            if delta_scale:
                moneyness = (350.0 - strike) / 50.0 if option_type == "C" else (strike - 350.0) / 50.0
                delta = max(0.02, min(0.98, 0.5 + moneyness))
            else:
                delta = None
            legs.append(
                ChainLeg(
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    delta=delta,
                    oi=overrides.get("oi", 1000),
                    volume=overrides.get("volume", 500),
                    spread_state=overrides.get("spread_state", 1),
                )
            )
    return legs


def test_atm_long_picks_the_strike_nearest_the_futures_price():
    result = select_instruments(["atm_long"], _chain(), spot=349.0, today=TODAY)
    assert result.reason is None
    (candidate,) = result.candidates
    (leg,) = candidate.legs
    assert leg.strike == 350.0
    assert leg.option_type == "C"  # direction 기본값 +1 -> 콜
    assert leg.side == "BUY"
    assert leg.rule == "atm@350"


def test_negative_direction_picks_a_put_for_directional_strategies():
    result = select_instruments(["atm_long"], _chain(), spot=349.0, today=TODAY, direction=-1)
    (leg,) = result.candidates[0].legs
    assert leg.option_type == "P"


def test_the_expiry_day_book_is_excluded_because_it_is_0dte_only():
    """만기 당일 북은 §11.4가 0DTE 플레이북 전용으로 못박은 것 — 일반 전략 후보가 될 수 없다."""
    legs = _chain(expiry=TODAY) + _chain(expiry=FAR)
    result = select_instruments(["atm_long"], legs, spot=350.0, today=TODAY)
    assert result.book_expiry == FAR
    assert all(leg.expiry == FAR for c in result.candidates for leg in c.legs)


def test_only_the_expiry_day_book_left_is_a_verdict_not_a_crash():
    result = select_instruments(["atm_long"], _chain(expiry=TODAY), spot=350.0, today=TODAY)
    assert result.candidates == ()
    assert result.reason == REASON_NO_ELIGIBLE_BOOK


def test_nearest_non_expiry_book_wins_over_the_far_one():
    expiry, reason = select_book(_chain(expiry=FAR) + _chain(expiry=NEAR), TODAY)
    assert (expiry, reason) == (NEAR, None)


def test_is_expiry_day_is_the_single_source_shared_with_the_exit_stack():
    """`exit_rules_key(is_expiry_day=...)`에 먹일 값 — 두 곳이 따로 판정하면 안 된다."""
    assert is_expiry_day(_chain(expiry=TODAY), TODAY) is True
    assert is_expiry_day(_chain(expiry=NEAR), TODAY) is False


def test_empty_chain_and_missing_spot_are_different_reasons():
    assert select_instruments(["atm_long"], [], spot=350.0, today=TODAY).reason == REASON_NO_CHAIN
    assert select_instruments(["atm_long"], _chain(), spot=None, today=TODAY).reason == REASON_SPOT_UNAVAILABLE


def test_itm_debit_lands_inside_the_delta_band():
    result = select_instruments(["itm_debit"], _chain(), spot=350.0, today=TODAY)
    (leg,) = result.candidates[0].legs
    assert 0.60 <= abs(leg.delta) <= 0.70
    assert leg.rule == "delta_band[0.6,0.7]"


def test_a_leg_with_no_delta_never_satisfies_a_delta_band():
    """델타를 모르는 레그를 밴드 규칙으로 통과시키면 «규칙대로 골랐다»는 기록이 거짓이 된다."""
    result = select_instruments(["itm_debit"], _chain(delta_scale=False), spot=350.0, today=TODAY)
    assert result.candidates == ()
    assert result.reason == REASON_NO_STRIKE_MATCH


def test_debit_spread_buys_atm_and_sells_further_out_in_the_same_book():
    result = select_instruments(["debit_spread"], _chain(), spot=350.0, today=TODAY)
    buy, sell = result.candidates[0].legs
    assert (buy.side, buy.strike) == ("BUY", 350.0)
    assert (sell.side, sell.strike) == ("SELL", 360.0)  # 콜은 위쪽이 외가, 2칸 밖
    assert buy.expiry == sell.expiry  # §11.5 "양다리 동일 만기"
    assert sell.rule == "2_strikes_out@360"


def test_the_short_leg_falls_back_to_three_strikes_out_when_two_is_off_the_grid():
    legs = _chain(strikes=(340.0, 345.0, 350.0, 355.0))
    # ATM 350에서 콜 2칸 밖은 격자를 벗어난다 -> 3칸도 없으므로 매칭 실패가 정상.
    result = select_instruments(["debit_spread"], legs, spot=350.0, today=TODAY)
    assert result.reason == REASON_NO_STRIKE_MATCH


def test_put_side_spread_walks_down_the_grid():
    result = select_instruments(["debit_spread"], _chain(), spot=350.0, today=TODAY, direction=-1)
    buy, sell = result.candidates[0].legs
    assert (buy.strike, sell.strike) == (350.0, 340.0)  # 풋은 아래쪽이 외가


def test_straddle_takes_both_sides_of_the_same_strike():
    result = select_instruments(["atm_straddle"], _chain(), spot=351.0, today=TODAY)
    call_leg, put_leg = result.candidates[0].legs
    assert (call_leg.option_type, put_leg.option_type) == ("C", "P")
    assert call_leg.strike == put_leg.strike == 350.0


def test_strangle_takes_both_wings_near_25_delta():
    result = select_instruments(["small_strangle_buy"], _chain(), spot=350.0, today=TODAY)
    call_leg, put_leg = result.candidates[0].legs
    assert 0.20 <= abs(call_leg.delta) <= 0.30
    assert 0.20 <= abs(put_leg.delta) <= 0.30
    assert call_leg.strike != put_leg.strike


@pytest.mark.parametrize("strategy", ["long_gamma", "limited_premium_sell", "highly_limited_premium_sell"])
def test_strategies_the_spec_gave_no_strike_rule_for_produce_no_candidate(strategy):
    """§11.5 표에 행사가 규칙이 없는 전략 — 지어내지 않고 사유를 남기는 것이 설계다."""
    result = select_instruments([strategy], _chain(), spot=350.0, today=TODAY)
    assert result.candidates == ()
    assert result.reason == REASON_NO_SELECTION_RULE
    assert result.rejected[0]["strategy"] == strategy


def test_thresholds_are_all_off_by_default_so_nothing_is_filtered():
    """모의투자 실측 분포를 보기 전에는 임계를 안 건다 — 임계가 곧 결론이 되지 않게."""
    thresholds = LiquidityThresholds.from_params(None)
    assert thresholds.any_enabled is False
    starved = ChainLeg(expiry=NEAR, strike=350.0, option_type="C", oi=0, volume=0, spread_state=99)
    assert liquidity_reject_reason(starved, thresholds) is None


def test_configured_thresholds_reject_and_name_the_failing_condition():
    thresholds = LiquidityThresholds.from_params(
        {"min_oi": 100, "max_spread_state": 2, "require_traded_today": True}
    )
    leg = ChainLeg(expiry=NEAR, strike=350.0, option_type="C", oi=10, volume=5, spread_state=1)
    assert liquidity_reject_reason(leg, thresholds) == "oi_below_min"
    leg = ChainLeg(expiry=NEAR, strike=350.0, option_type="C", oi=1000, volume=5, spread_state=9)
    assert liquidity_reject_reason(leg, thresholds) == "spread_above_max"
    leg = ChainLeg(expiry=NEAR, strike=350.0, option_type="C", oi=1000, volume=0, spread_state=1)
    assert liquidity_reject_reason(leg, thresholds) == "not_traded_today"


def test_unknown_liquidity_fields_pass_because_unknown_is_not_bad():
    """None은 «모른다»다. 그것을 «나쁘다»로 읽으면 컬럼 하나 결손난 날 전 후보가 사라진다."""
    thresholds = LiquidityThresholds.from_params({"min_oi": 100, "require_traded_today": True})
    unknown = ChainLeg(expiry=NEAR, strike=350.0, option_type="C", oi=None, volume=None)
    assert liquidity_reject_reason(unknown, thresholds) is None


def test_all_candidates_filtered_out_is_recorded_as_no_liquid_instrument():
    thresholds = LiquidityThresholds.from_params({"min_oi": 100_000})
    result = select_instruments(
        ["atm_long"], _chain(), spot=350.0, today=TODAY, thresholds=thresholds
    )
    assert result.candidates == ()
    assert result.reason == REASON_NO_LIQUID_INSTRUMENT
    assert result.rejected[0]["detail"] == "oi_below_min"


def test_one_illiquid_leg_kills_the_whole_spread():
    """스프레드는 한 다리만 못 사면 그 전략이 실행 불가다 — 반쪽 후보를 만들면 안 된다."""
    legs = [
        leg if not (leg.strike == 360.0 and leg.option_type == "C") else ChainLeg(
            expiry=leg.expiry, strike=leg.strike, option_type=leg.option_type,
            delta=leg.delta, oi=1, volume=leg.volume, spread_state=leg.spread_state,
        )
        for leg in _chain()
    ]
    thresholds = LiquidityThresholds.from_params({"min_oi": 100})
    result = select_instruments(["debit_spread"], legs, spot=350.0, today=TODAY, thresholds=thresholds)
    assert result.candidates == ()
    assert result.reason == REASON_NO_LIQUID_INSTRUMENT


def test_symbol_resolver_fills_the_short_code_and_its_failure_keeps_the_candidate():
    """단축코드를 못 찾은 것은 선택의 실패가 아니다 — 두 원인을 한 사유로 뭉개지 않는다."""
    result = select_instruments(
        ["atm_long"], _chain(), spot=350.0, today=TODAY,
        symbol_resolver=lambda option_type, strike, expiry: f"2{option_type}{int(strike)}",
    )
    assert result.candidates[0].legs[0].symbol == "2C350"

    result = select_instruments(
        ["atm_long"], _chain(), spot=350.0, today=TODAY,
        symbol_resolver=lambda option_type, strike, expiry: None,
    )
    assert result.candidates[0].legs[0].symbol is None
    assert result.reason is None


def test_rationale_travels_with_the_candidate():
    """계산해 놓고 버리면 «무엇이 이 행사가를 골랐나»에 답할 수 없다(member_scores의 교훈)."""
    result = select_instruments(["atm_long"], _chain(), spot=350.0, today=TODAY)
    record = result.to_record()
    leg = record["candidates"][0]["legs"][0]
    assert leg["rule"] == "atm@350"
    assert leg["delta"] is not None and leg["oi"] == 1000 and leg["volume"] == 500
    assert record["book_expiry"] == "2026-08-18"  # JSONB 적재용 ISO 문자열


def test_legs_from_chain_snapshot_normalises_and_drops_unusable_rows():
    rows = [
        {"expiry": NEAR, "strike": 350.0, "option_type": "c", "delta": 0.5, "oi": 10,
         "volume": 3, "spread_state": 1},
        {"expiry": None, "strike": 350.0, "option_type": "C"},          # 만기 없음
        {"expiry": NEAR, "strike": None, "option_type": "C"},           # 행사가 없음
        {"expiry": NEAR, "strike": 355.0, "option_type": "X"},          # 콜/풋 아님
    ]
    legs = legs_from_chain_snapshot(rows)
    assert len(legs) == 1
    assert legs[0].option_type == "C"


def test_multiple_strategies_each_get_their_own_verdict():
    result = select_instruments(
        ["atm_long", "long_gamma"], _chain(), spot=350.0, today=TODAY
    )
    assert [c.strategy for c in result.candidates] == ["atm_long"]
    assert result.rejected == ({"strategy": "long_gamma", "reason": REASON_NO_SELECTION_RULE},)
    assert result.reason is None  # 후보가 하나라도 있으면 사유는 비어야 한다
