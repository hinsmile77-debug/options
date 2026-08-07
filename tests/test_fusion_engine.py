import logging

from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.fusion.engine import MetaLabelContext, SignalFusionEngine
from mahdi.fusion.meta_label import TradePermission
from mahdi.fusion.signal_layer import SignalInputs

_STRATEGY_PARAMS = {
    "ensemble": {
        "regime_hmm": {"base_w": 0.20},
        "xgboost_tabular": {"base_w": 0.20},
        "lstm_temporal": {"base_w": 0.15},
        "options_flow": {"base_w": 0.20},
        "orderflow_ofi_vpin": {"base_w": 0.15},
        "flow_position": {"base_w": 0.10},
    },
    "meta_label_thresholds": {
        "no_trade_max": 0.15,
        "small_test_max": 0.35,
        "standard_max": 0.65,
        "slippage_penalty_factor": 0.7,
        "gamma_regime_penalty_factor": 0.85,
        "foreign_flow_penalty_factor": 0.8,
        "event_proximity_penalty_minutes": 15,
        "event_proximity_penalty_factor": 0.5,
    },
    "strategy_gates": {"max_priority_strategies_per_regime_day": 2},
}


def _regime_state(regime: RegimeLabel, stability_flag: bool = True, prob: float = 0.9) -> RegimeState:
    prob_vector = [0.0] * 8
    prob_vector[regime] = prob
    return RegimeState(regime=regime, prob_vector=tuple(prob_vector), stability_flag=stability_flag)


def test_no_signals_at_all_yields_no_trade_and_no_strategies():
    engine = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS)
    decision = engine.evaluate(SignalInputs(), MetaLabelContext())
    assert decision.trade_permission == TradePermission.NO_TRADE
    assert decision.allowed_strategies == []
    assert "meta_label:no_trade" in decision.reject_reasons


def test_strong_aligned_trend_signal_produces_allowed_strategy():
    inputs = SignalInputs(
        regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG, prob=1.0),
        gex=-1000.0,
        gamma_flip=100.0,
        spot=105.0,
        ofi=5.0,
        queue_imbalance=0.3,
        foreign_net_flow=500.0,
    )
    engine = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS)
    decision = engine.evaluate(inputs, MetaLabelContext(), vrp=-0.05)
    assert decision.direction > 0
    assert decision.trade_permission != TradePermission.NO_TRADE
    assert decision.reject_reasons == []
    assert decision.allowed_strategies == ["atm_long"]


def test_conflicting_signals_block_strategy_selection_even_with_high_confidence():
    inputs = SignalInputs(
        regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG, prob=1.0),
        gex=1000.0,
        gamma_flip=100.0,
        spot=105.0,  # options_flow reverts -> 음수 방향, regime_hmm은 양수 -> 충돌
    )
    engine = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS)
    decision = engine.evaluate(inputs, MetaLabelContext(), vrp=-0.05)
    if decision.trade_permission != TradePermission.NO_TRADE:
        assert "conflict_resolution:no_clear_consensus" in decision.reject_reasons
        assert decision.allowed_strategies == []


def test_daily_strategy_cap_is_applied_via_already_used_set():
    inputs = SignalInputs(
        regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG, prob=1.0),
        gex=-1000.0,
        gamma_flip=100.0,
        spot=105.0,
        ofi=5.0,
        queue_imbalance=0.3,
        foreign_net_flow=500.0,
    )
    engine = SignalFusionEngine(
        strategy_params={**_STRATEGY_PARAMS, "strategy_gates": {"max_priority_strategies_per_regime_day": 0}}
    )
    decision = engine.evaluate(inputs, MetaLabelContext(), vrp=-0.05)
    assert decision.allowed_strategies == []


# ===== 2026-08-03 §5-2: 판단 축 전이 로깅 =====


def test_engine_logs_shape_transition_once_not_every_cycle(caplog):
    """매 분 찍으면 하루 495줄, 아무것도 안 찍으면 멤버가 조용히 죽어도 영영 모른다.

    08-03에 실제로 후자였다 — `options_flow`가 전 이력에서 한 번도 활성화된 적 없었는데
    로그에 흔적이 0줄이었다.
    """
    engine = SignalFusionEngine()
    inputs = SignalInputs(regime_state=None, foreign_net_flow=500.0)

    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        engine.evaluate(inputs, MetaLabelContext())
        first = [r for r in caplog.records if "판단 형태 전이" in r.getMessage()]
        engine.evaluate(inputs, MetaLabelContext())  # 같은 형태 — 로그 없음
        second = [r for r in caplog.records if "판단 형태 전이" in r.getMessage()]

    assert len(first) == 1
    assert len(second) == 1, "형태가 그대로면 다시 남기지 않는다"
    assert "flow_position" in first[0].getMessage()


def test_engine_logs_again_when_member_availability_changes(caplog):
    engine = SignalFusionEngine()

    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        engine.evaluate(SignalInputs(foreign_net_flow=500.0), MetaLabelContext())
        engine.evaluate(SignalInputs(foreign_net_flow=None), MetaLabelContext())  # 멤버 하나 사라짐

    records = [r for r in caplog.records if "판단 형태 전이" in r.getMessage()]
    assert len(records) == 2
    assert "직전 가용멤버" in records[1].getMessage()


# ==========================================================================================
# 2026-08-07 고도화#3 — 확신도 분모를 「실질 멤버 수」로 바꾸는 레버 (지금은 내려져 있다)
# ==========================================================================================


def _dead_axis_inputs():
    """`regime_hmm`이 중립(0점)인 상태 — 08-07 실측 212분 전량이 이랬다.

    레짐이 24영업일 연속 한 상태(학습 미완)라 방향 점수가 안 나온다. 그래도 `available`은
    그 멤버를 세므로 확신도의 분모가 부풀려진다.
    """
    return SignalInputs(
        regime_state=_regime_state(RegimeLabel.RANGE_BALANCED, prob=1.0),
        gex=-1000.0, gamma_flip=100.0, spot=105.0,
        ofi=5.0, queue_imbalance=0.3, foreign_net_flow=500.0,
    )


def test_effective_member_count_lever_is_down_by_default():
    """켜지 않은 상태에서는 08-06까지와 **1비트도 다르지 않아야** 한다.

    이 값은 확신도의 분모라(v6 §11.3) 정의를 바꾸면 그날부터 시계열이 과거와 비교 불가가 된다.
    """
    inputs = _dead_axis_inputs()
    base = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS).evaluate(inputs, MetaLabelContext())
    explicit_off = SignalFusionEngine(
        strategy_params={**_STRATEGY_PARAMS, "use_effective_member_count": False}
    ).evaluate(inputs, MetaLabelContext())

    assert base.conviction_score == explicit_off.conviction_score
    assert base.available_member_count == explicit_off.available_member_count
    assert base.available_member_count >= base.effective_member_count


def test_effective_member_count_lever_changes_the_confidence_denominator_when_raised():
    """올리면 죽은 축이 분모에서 빠진다 — 08-10 재학습 **다음** 영업일에 켠다.

    재학습과 같은 날 켜면 확신도 변화를 둘 중 무엇이 만들었는지 영영 못 가른다.
    """
    inputs = _dead_axis_inputs()
    off = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS).evaluate(inputs, MetaLabelContext())
    on = SignalFusionEngine(
        strategy_params={**_STRATEGY_PARAMS, "use_effective_member_count": True}
    ).evaluate(inputs, MetaLabelContext())

    # 기록되는 두 계측값 자체는 레버와 무관하게 그대로다(둘의 차이가 곧 죽은 축의 수다).
    assert on.available_member_count == off.available_member_count
    assert on.effective_member_count == off.effective_member_count
    # 전제를 조건문이 아니라 단언으로 둔다 — 죽은 축이 사라지면 이 테스트는 **깨져야** 한다
    # (조건문으로 두면 레버가 아무것도 안 하게 돼도 조용히 통과한다).
    assert off.available_member_count > off.effective_member_count, "죽은 축이 없으면 이 시나리오가 아니다"
    # 죽은 축이 분모에서 빠지므로 확신도가 올라간다(4개 중 3개 동조 → 3개 중 3개 동조).
    assert on.conviction_score > off.conviction_score
