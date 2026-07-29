from datetime import datetime

import pytest

from mahdi.execution.account_tracker import (
    BalanceSnapshot,
    build_account_state,
    parse_balance_response,
    snapshot_to_row,
)

_TS = datetime(2026, 7, 28, 10, 0)

_RESPONSE = {
    "rt_cd": "0",
    "output1": [
        {"shtn_pdno": "101S03", "sll_buy_dvsn_name": "BUY"},
        {"shtn_pdno": "201S03C325", "sll_buy_dvsn_name": "SLL"},
        {"shtn_pdno": "201S03C330", "sll_buy_dvsn_name": "SLL"},
        {"shtn_pdno": "flattened", "sll_buy_dvsn_name": ""},  # 당일 청산돼 빈칸 -> 어느 쪽도 아님
    ],
    "output2": {
        "prsm_dpast": "50000000",
        "evlu_pfls_amt_smtl": "16125000",
        "trad_pfls_amt_smtl": "0",
        "dnca_cash": "50000000",
        "ord_psbl_cash": "48000000",
        "mgna_tota": "2000000",
    },
}


def test_parse_balance_response_extracts_pnl_fields_and_position_counts():
    snapshot = parse_balance_response(_RESPONSE, _TS)

    assert snapshot.prsm_dpast == 50000000.0
    assert snapshot.evlu_pfls_amt_smtl == 16125000.0
    assert snapshot.dnca_cash == 50000000.0
    assert snapshot.ord_psbl_cash == 48000000.0
    assert snapshot.same_direction_buy_count == 1
    assert snapshot.same_direction_sell_count == 2


def test_parse_balance_response_missing_output2_raises():
    with pytest.raises(ValueError):
        parse_balance_response({"output1": []}, _TS)


def test_parse_balance_response_missing_output1_means_no_positions():
    snapshot = parse_balance_response({"output2": {"prsm_dpast": "1000"}}, _TS)
    assert snapshot.same_direction_buy_count == 0
    assert snapshot.same_direction_sell_count == 0


def test_parse_balance_response_treats_blank_or_none_numeric_fields_as_zero():
    snapshot = parse_balance_response({"output2": {"prsm_dpast": None, "dnca_cash": ""}}, _TS)
    assert snapshot.prsm_dpast == 0.0
    assert snapshot.dnca_cash == 0.0


def test_snapshot_to_row_round_trips_all_fields():
    snapshot = parse_balance_response(_RESPONSE, _TS)
    row = snapshot_to_row(snapshot)
    assert row["timestamp"] == _TS
    assert row["prsm_dpast"] == 50000000.0
    assert row["same_direction_buy_count"] == 1
    assert row["same_direction_sell_count"] == 2


def _snapshot(prsm_dpast: float, buy=0, sell=0) -> BalanceSnapshot:
    return BalanceSnapshot(
        timestamp=_TS, prsm_dpast=prsm_dpast, evlu_pfls_amt_smtl=0.0, trad_pfls_amt_smtl=0.0,
        dnca_cash=0.0, ord_psbl_cash=0.0, mgna_tota=0.0,
        same_direction_buy_count=buy, same_direction_sell_count=sell,
    )


def test_build_account_state_computes_daily_and_weekly_pnl_pct():
    latest = _snapshot(110.0)
    start_of_day = _snapshot(100.0)
    start_of_week = _snapshot(80.0)

    state = build_account_state(
        latest, start_of_day, start_of_week, peak_prsm_dpast=110.0,
        candidate_side="BUY", daily_trades_by_strategy={},
    )

    assert state.daily_pnl_pct == pytest.approx(0.10)
    assert state.weekly_pnl_pct == pytest.approx(0.375)
    assert state.drawdown_pct == pytest.approx(0.0)  # latest == peak


def test_build_account_state_drawdown_negative_when_below_peak():
    latest = _snapshot(90.0)
    state = build_account_state(
        latest, start_of_day=None, start_of_week=None, peak_prsm_dpast=100.0,
        candidate_side="BUY", daily_trades_by_strategy={},
    )
    assert state.drawdown_pct == pytest.approx(-0.10)


def test_build_account_state_missing_baselines_fall_back_to_zero():
    latest = _snapshot(100.0)
    state = build_account_state(
        latest, start_of_day=None, start_of_week=None, peak_prsm_dpast=None,
        candidate_side="BUY", daily_trades_by_strategy={},
    )
    assert state.daily_pnl_pct == 0.0
    assert state.weekly_pnl_pct == 0.0
    assert state.drawdown_pct == 0.0


def test_build_account_state_same_direction_positions_matches_candidate_side():
    latest = _snapshot(100.0, buy=2, sell=5)
    buy_state = build_account_state(
        latest, None, None, None, candidate_side="BUY", daily_trades_by_strategy={}
    )
    sell_state = build_account_state(
        latest, None, None, None, candidate_side="SELL", daily_trades_by_strategy={}
    )
    assert buy_state.same_direction_positions == 2
    assert sell_state.same_direction_positions == 5


def test_build_account_state_passes_through_daily_trades_and_pending_loss():
    latest = _snapshot(100.0)
    state = build_account_state(
        latest, None, None, None, candidate_side="BUY",
        daily_trades_by_strategy={"vrp_harvest": 2}, pending_trade_loss_pct=-0.01,
    )
    assert state.daily_trades_by_strategy == {"vrp_harvest": 2}
    assert state.pending_trade_loss_pct == -0.01
