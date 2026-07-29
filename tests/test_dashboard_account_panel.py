from datetime import datetime

from mahdi.dashboard.panels.account_panel import build_account_summary_cards


def test_build_account_summary_cards_handles_none_status():
    # 계좌 잔고 폴러가 아직 스냅샷을 못 쌓았을 때 — 손익 0으로 지어내지 않고 "아직 없음"만 표시.
    cards = build_account_summary_cards(None)
    assert len(cards) == 1
    assert cards[0]["value"] == "아직 없음"


def test_build_account_summary_cards_shows_positive_returns_as_ok():
    status = {
        "timestamp": datetime(2026, 7, 29, 9, 0, 0),
        "prsm_dpast": 51_000_000.0,
        "dnca_cash": 50_500_000.0,
        "ord_psbl_cash": 50_500_000.0,
        "mgna_tota": 0.0,
        "evlu_pfls_amt_smtl": 500_000.0,
        "trad_pfls_amt_smtl": 0.0,
        "daily_pnl_pct": 0.02,
        "weekly_pnl_pct": 0.03,
        "drawdown_pct": 0.0,
    }

    cards = build_account_summary_cards(status)
    by_label = {c["label"]: c for c in cards}

    assert by_label["추정예탁자산"]["value"] == "51,000,000원"
    assert by_label["현금(주문가능)"]["value"] == "50,500,000원"
    assert by_label["평가손익"]["value"] == "+500,000원"
    assert by_label["평가손익"]["status"] == "ok"
    assert by_label["일간 수익률"]["value"] == "+2.00%"
    assert by_label["일간 수익률"]["status"] == "ok"
    assert by_label["주간 수익률"]["value"] == "+3.00%"
    assert by_label["최대낙폭"]["value"] == "+0.00%"
    assert by_label["최대낙폭"]["status"] == "neutral"


def test_build_account_summary_cards_shows_negative_returns_as_warning():
    status = {
        "timestamp": datetime(2026, 7, 29, 9, 0, 0),
        "prsm_dpast": 49_000_000.0,
        "dnca_cash": 49_000_000.0,
        "ord_psbl_cash": 49_000_000.0,
        "mgna_tota": 0.0,
        "evlu_pfls_amt_smtl": -1_000_000.0,
        "trad_pfls_amt_smtl": 0.0,
        "daily_pnl_pct": -0.02,
        "weekly_pnl_pct": -0.02,
        "drawdown_pct": -0.02,
    }

    cards = build_account_summary_cards(status)
    by_label = {c["label"]: c for c in cards}

    assert by_label["평가손익"]["value"] == "-1,000,000원"
    assert by_label["평가손익"]["status"] == "warning"
    assert by_label["일간 수익률"]["value"] == "-2.00%"
    assert by_label["일간 수익률"]["status"] == "warning"
    assert by_label["최대낙폭"]["value"] == "-2.00%"
    assert by_label["최대낙폭"]["status"] == "warning"
