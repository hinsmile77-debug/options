from datetime import date, datetime

from mahdi.backtest.data_adapter import load_backtest_steps_from_db
from mahdi.data import db
from mahdi.engines.regime import RegimeLabel

_START = datetime(2026, 7, 28, 9, 0)
_END = datetime(2026, 7, 28, 9, 5)


def test_load_backtest_steps_from_db_builds_one_step_per_bar(monkeypatch):
    bars = [
        {"timestamp": datetime(2026, 7, 28, 9, 0), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2},
        {"timestamp": datetime(2026, 7, 28, 9, 1), "open": 100.2, "high": 100.6, "low": 99.8, "close": 100.4},
    ]
    monkeypatch.setattr(db, "market_bars_between", lambda conn, symbol, start, end: bars)
    monkeypatch.setattr(db, "option_chain_as_of", lambda conn, underlying, as_of: [])
    monkeypatch.setattr(db, "investor_flow_as_of", lambda conn, underlying, as_of: None)
    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: None)

    steps = load_backtest_steps_from_db(conn=object(), underlying="KOSPI200", futures_symbol="101S03",
                                         start=_START, end=_END)

    assert len(steps) == 2
    assert steps[0].bar.open == 100.0
    assert steps[0].bar.close == 100.2
    assert steps[1].bar.close == 100.4


def test_load_backtest_steps_from_db_empty_range_is_empty(monkeypatch):
    monkeypatch.setattr(db, "market_bars_between", lambda conn, symbol, start, end: [])

    steps = load_backtest_steps_from_db(conn=object(), underlying="KOSPI200", futures_symbol="101S03",
                                         start=_START, end=_END)
    assert steps == []


def test_load_backtest_steps_from_db_computes_gex_from_chain_as_of(monkeypatch):
    bars = [{"timestamp": datetime(2026, 7, 28, 9, 0), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}]
    chain_rows = [
        {"strike": 95.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02, "gex": 0.0,
         "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr(db, "market_bars_between", lambda conn, symbol, start, end: bars)
    monkeypatch.setattr(db, "option_chain_as_of", lambda conn, underlying, as_of: chain_rows)
    monkeypatch.setattr(db, "investor_flow_as_of", lambda conn, underlying, as_of: (300.0, -50.0, -20.0))
    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: int(RegimeLabel.TREND_UP_STRONG))

    steps = load_backtest_steps_from_db(conn=object(), underlying="KOSPI200", futures_symbol="101S03",
                                         start=_START, end=_END)

    assert len(steps) == 1
    signal_inputs = steps[0].signal_inputs
    assert signal_inputs.gex is not None
    assert signal_inputs.foreign_net_flow == 300.0
    assert signal_inputs.regime_state.regime == RegimeLabel.TREND_UP_STRONG
    assert signal_inputs.regime_state.prob_vector[RegimeLabel.TREND_UP_STRONG] == 1.0
    assert signal_inputs.ofi is None
    assert signal_inputs.queue_imbalance is None


def test_load_backtest_steps_from_db_missing_regime_label_yields_none_regime_state(monkeypatch):
    bars = [{"timestamp": datetime(2026, 7, 28, 9, 0), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}]
    monkeypatch.setattr(db, "market_bars_between", lambda conn, symbol, start, end: bars)
    monkeypatch.setattr(db, "option_chain_as_of", lambda conn, underlying, as_of: [])
    monkeypatch.setattr(db, "investor_flow_as_of", lambda conn, underlying, as_of: None)
    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: None)

    steps = load_backtest_steps_from_db(conn=object(), underlying="KOSPI200", futures_symbol="101S03",
                                         start=_START, end=_END)

    assert steps[0].signal_inputs.regime_state is None
