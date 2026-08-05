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
    # 2026-08-05(P1-5): 드로우다운은 정의상 0 이하라 "+0.00%"는 있을 수 없는 표기다.
    assert by_label["최대낙폭"]["value"] == "0.00%"
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


# ===== 2026-08-05 P1-5: "기준이 없다"와 "변동이 없다"를 구분한다 =====
#
# 08-05 화면의 일간/주간 +0.00%와 최대낙폭 +0.00%는 계좌를 막 연 상태라 **비교할 과거가 없다**는
# 뜻이었는데, 계산된 0%와 화면에서 구분되지 않았다. `build_account_state()`가 baseline/peak 부재를
# 0.0으로 흡수하는 것은 RiskEngine에는 맞지만(어느 쪽이든 한도 위반이 아니다) 사람에게는 틀리다.


def _status_without_baselines(**overrides) -> dict:
    base = {
        "timestamp": datetime(2026, 8, 5, 12, 12),
        "prsm_dpast": 50_000_000.0,
        "dnca_cash": 50_000_000.0,
        "ord_psbl_cash": 50_000_000.0,
        "mgna_tota": 0.0,
        "evlu_pfls_amt_smtl": 0.0,
        "trad_pfls_amt_smtl": 0.0,
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "drawdown_pct": 0.0,
        "has_daily_baseline": False,
        "has_weekly_baseline": False,
        "has_peak": False,
    }
    return {**base, **overrides}


def test_pct_cards_say_no_baseline_instead_of_zero_percent():
    by_label = {c["label"]: c for c in build_account_summary_cards(_status_without_baselines())}

    for label in ("일간 수익률", "주간 수익률", "최대낙폭"):
        assert by_label[label]["value"] == "기준 없음", label
        assert by_label[label]["help"], f"{label}: 왜 기준이 없는지 설명이 있어야 한다"


def test_pct_cards_show_real_zero_when_the_baseline_exists():
    # 기준이 있는데 정말 0%인 경우 — 위와 화면에서 구분돼야 한다.
    status = _status_without_baselines(
        has_daily_baseline=True, has_weekly_baseline=True, has_peak=True
    )

    by_label = {c["label"]: c for c in build_account_summary_cards(status)}

    assert by_label["일간 수익률"]["value"] == "+0.00%"
    assert by_label["최대낙폭"]["value"] == "0.00%"


def test_pct_cards_default_to_showing_values_when_flags_are_absent():
    # 플래그가 없는 옛 형태의 dict(테스트/호출측 잔존)로도 깨지지 않아야 한다.
    status = _status_without_baselines()
    for key in ("has_daily_baseline", "has_weekly_baseline", "has_peak"):
        del status[key]

    by_label = {c["label"]: c for c in build_account_summary_cards(status)}

    assert by_label["일간 수익률"]["value"] == "+0.00%"
