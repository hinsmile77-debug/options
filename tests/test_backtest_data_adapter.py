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


# --- 2026-08-17 — 백테스트가 재던 레짐은 실제로 한 번도 나온 적이 없었다 -----------------------
#
# 종전 어댑터는 `exit_rules_regime`을 안 채웠고, `BacktestStep`이 그러면 "TREND_STRONG"으로
# 떨어진다. 실측 레짐 이력에서 그 계열은 **0분**이고, 실제 71.7%인 VOL_COMPRESSION은
# `exit_rules`에 행이 없어 타임스톱이 안 걸린다. 즉 손익 분포를 한 번도 일어난 적 없는 레짐의
# 청산 규칙으로 재고 있었다.


def _bar(close=100.0):
    return {"timestamp": datetime(2026, 7, 28, 9, 0), "open": 100.0, "high": 100.5, "low": 99.5, "close": close}


def _patch_adapter(monkeypatch, *, regime_label, chain_rows=()):
    monkeypatch.setattr(db, "market_bars_between", lambda conn, symbol, start, end: [_bar()])
    monkeypatch.setattr(db, "option_chain_as_of", lambda conn, underlying, as_of: list(chain_rows))
    monkeypatch.setattr(db, "investor_flow_as_of", lambda conn, underlying, as_of: None)
    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: regime_label)
    return load_backtest_steps_from_db(
        conn=object(), underlying="KOSPI200", futures_symbol="101S03", start=_START, end=_END
    )


def test_the_exit_rules_key_comes_from_the_observed_regime_not_a_default(monkeypatch):
    steps = _patch_adapter(monkeypatch, regime_label=int(RegimeLabel.VOL_COMPRESSION))
    assert steps[0].exit_rules_regime == "VOL_COMPRESSION"


def test_a_range_regime_maps_to_the_range_exit_rules_row(monkeypatch):
    steps = _patch_adapter(monkeypatch, regime_label=int(RegimeLabel.RANGE_BREAK_PREP))
    assert steps[0].exit_rules_regime == "RANGE_TIGHT"


def test_an_expiry_day_bar_switches_to_the_0dte_parameter_set(monkeypatch):
    """만기 당일은 레짐과 무관하게 별도 파라미터 세트다(§11.4) — 감마 폭발 구간이라."""
    same_day = [{"strike": 100.0, "option_type": "C", "oi": 10.0, "iv": 0.2, "gamma": 0.01,
                 "gex": 0.0, "expiry": date(2026, 7, 28)}]
    steps = _patch_adapter(
        monkeypatch, regime_label=int(RegimeLabel.VOL_COMPRESSION), chain_rows=same_day
    )
    assert steps[0].exit_rules_regime == "EXPIRY_DAY_0DTE"


def test_an_unknown_regime_leaves_the_key_empty_rather_than_guessing(monkeypatch):
    steps = _patch_adapter(monkeypatch, regime_label=None)
    assert steps[0].exit_rules_regime is None


def test_load_backtest_steps_from_db_missing_regime_label_yields_none_regime_state(monkeypatch):
    bars = [{"timestamp": datetime(2026, 7, 28, 9, 0), "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}]
    monkeypatch.setattr(db, "market_bars_between", lambda conn, symbol, start, end: bars)
    monkeypatch.setattr(db, "option_chain_as_of", lambda conn, underlying, as_of: [])
    monkeypatch.setattr(db, "investor_flow_as_of", lambda conn, underlying, as_of: None)
    monkeypatch.setattr(db, "latest_regime_before", lambda conn, before: None)

    steps = load_backtest_steps_from_db(conn=object(), underlying="KOSPI200", futures_symbol="101S03",
                                         start=_START, end=_END)

    assert steps[0].signal_inputs.regime_state is None
