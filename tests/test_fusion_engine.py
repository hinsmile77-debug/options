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
