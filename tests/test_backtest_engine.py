import pytest

from mahdi.backtest.engine import BacktestEngine, BacktestStep
from mahdi.backtest.simulated_broker import Bar
from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.execution.engine import ExecutionEngine
from mahdi.execution.hybrid_mode import HybridMode
from mahdi.fusion.engine import SignalFusionEngine
from mahdi.fusion.signal_layer import SignalInputs
from mahdi.risk.circuit_breaker import MarketConditions
from mahdi.risk.engine import RiskEngine
from mahdi.risk.limits import AccountState

_RISK_LIMITS = {
    "sizing": {"kelly_fraction": 0.25, "max_kelly_fraction": 0.25},
    "limits": {
        "per_trade_loss_pct": -0.005,
        "daily_loss_pct": -0.02,
        "weekly_loss_pct": -0.05,
        "max_drawdown_pct": -0.10,
        "max_same_direction_positions": 3,
        "max_daily_trades_per_strategy": None,
    },
    "circuit_breaker": {
        "daily_loss_pct": -0.02,
        "drawdown_pct": -0.10,
        "vpin_crisis": 0.90,
        "vix_spike": 40,
        "usdkrw_daily_change": 0.02,
        "data_quality_fail": True,
        "model_drift": True,
    },
}
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
    "exit_rules": {"TREND_STRONG": {"time_stop": 120}},
}


def _account() -> AccountState:
    return AccountState(
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, drawdown_pct=0.0,
        same_direction_positions=0, daily_trades_by_strategy={}, pending_trade_loss_pct=0.0,
    )


def _market_conditions() -> MarketConditions:
    return MarketConditions(
        vpin=0.0, vix=0.0, usdkrw_daily_change_pct=0.0, data_quality_ok=True, model_drift_detected=False
    )


def _strong_bullish_signal_inputs() -> SignalInputs:
    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 0.95
    regime_state = RegimeState(
        regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector), stability_flag=True
    )
    return SignalInputs(
        regime_state=regime_state,
        gex=-1000.0,
        gamma_flip=95.0,
        spot=100.0,
        ofi=5.0,
        queue_imbalance=0.3,
        foreign_net_flow=500.0,
    )


def _engine() -> BacktestEngine:
    fusion = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS)
    execution = ExecutionEngine(risk_engine=RiskEngine(risk_limits=_RISK_LIMITS), strategy_params=_STRATEGY_PARAMS)
    return BacktestEngine(fusion, execution, hybrid_mode=HybridMode.FULL_AUTO)


def test_strong_signal_enters_and_hard_stop_exits_with_loss():
    steps = [
        BacktestStep(
            bar=Bar(timestamp_minutes=0.0, open=100.0, high=100.5, low=99.5, close=100.0),
            signal_inputs=_strong_bullish_signal_inputs(),
            account_state=_account(),
            market_conditions=_market_conditions(),
        ),
        BacktestStep(
            # LIMIT(99.95) 터치 -> 체결, 이번 봉 종가로 즉시 청산 평가(작은 손실, HOLD 예상)
            bar=Bar(timestamp_minutes=1.0, open=99.9, high=100.0, low=99.4, close=99.6),
            signal_inputs=_strong_bullish_signal_inputs(),
            account_state=_account(),
            market_conditions=_market_conditions(),
        ),
        BacktestStep(
            # 큰 하락 -> hard stop(-2%) 이탈
            bar=Bar(timestamp_minutes=2.0, open=99.5, high=99.6, low=95.0, close=95.5),
            signal_inputs=_strong_bullish_signal_inputs(),
            account_state=_account(),
            market_conditions=_market_conditions(),
        ),
    ]
    trades = _engine().run(steps)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == "BUY"
    assert trade.entry_price == pytest.approx(99.95)
    assert trade.exit_price == pytest.approx(95.5)
    assert trade.net_pnl < 0
    assert "hard_stop" in trade.exit_reason or "이탈" in trade.exit_reason


def test_no_signal_produces_no_trades():
    steps = [
        BacktestStep(
            bar=Bar(timestamp_minutes=0.0, open=100.0, high=100.5, low=99.5, close=100.0),
            signal_inputs=SignalInputs(),
            account_state=_account(),
            market_conditions=_market_conditions(),
        ),
        BacktestStep(
            bar=Bar(timestamp_minutes=1.0, open=100.0, high=100.5, low=99.5, close=100.0),
            signal_inputs=SignalInputs(),
            account_state=_account(),
            market_conditions=_market_conditions(),
        ),
    ]
    trades = _engine().run(steps)
    assert trades == []
