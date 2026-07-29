from datetime import datetime

from mahdi.dashboard.panels.decision_panel import (
    CONVICTION_LABEL_KO,
    build_decision_history_table,
    build_decision_summary_cards,
)


def test_build_decision_summary_cards_handles_none_latest():
    # 계좌/신호 폴러가 아직 한 번도 안 돌았을 때(합성 폴백 없이 그대로) — 청산 단계 카드는
    # 항상 자리를 확보해둔다(§설계원칙: 향후 배선 시 이 카드 하나만 실데이터로 바뀜).
    cards = build_decision_summary_cards(None)

    labels = [c["label"] for c in cards]
    assert "청산 단계 (실행엔진)" in labels
    exit_card = next(c for c in cards if c["label"] == "청산 단계 (실행엔진)")
    assert exit_card["value"] == "미배선"
    assert exit_card["status"] == "info"


def test_build_decision_summary_cards_shows_enter_with_risk_approval():
    latest = {
        "timestamp": datetime(2026, 7, 29, 9, 5, 0),
        "conviction": "HIGH_CONVICTION",
        "decision": "ENTER",
        "reject_reason": None,
        "risk_gate_state": {
            "allowed_strategies": ["atm_long", "debit_spread"],
            "risk_engine": {"approved": True, "approved_size": 0.75, "reject_reasons": []},
        },
        "exec_mode": "ADVISORY",
    }

    cards = build_decision_summary_cards(latest)
    by_label = {c["label"]: c for c in cards}

    assert by_label["최근 판단"]["value"] == "진입 후보 (09:05:00)"
    assert by_label["최근 판단"]["status"] == "ok"
    assert by_label["확신도"]["value"] == CONVICTION_LABEL_KO["HIGH_CONVICTION"]
    assert by_label["허용 전략"]["value"] == "atm_long"
    assert by_label["RiskEngine 승인"]["value"] == "승인 (사이즈 0.75)"
    assert by_label["RiskEngine 승인"]["status"] == "ok"
    assert by_label["청산 단계 (실행엔진)"]["value"] == "미배선"


def test_build_decision_summary_cards_shows_risk_rejection():
    latest = {
        "timestamp": datetime(2026, 7, 29, 9, 6, 0),
        "conviction": "STANDARD",
        "decision": "ENTER",
        "reject_reason": None,
        "risk_gate_state": {
            "allowed_strategies": ["gamma_scalp"],
            "risk_engine": {"approved": False, "approved_size": 0.0, "reject_reasons": ["daily_loss_limit_exceeded"]},
        },
        "exec_mode": "ADVISORY",
    }

    cards = build_decision_summary_cards(latest)
    by_label = {c["label"]: c for c in cards}

    assert by_label["RiskEngine 승인"]["value"] == "거부: daily_loss_limit_exceeded"
    assert by_label["RiskEngine 승인"]["status"] == "warning"


def test_build_decision_summary_cards_shows_account_tracker_not_ready():
    latest = {
        "timestamp": datetime(2026, 7, 29, 9, 7, 0),
        "conviction": "SMALL_TEST",
        "decision": "ENTER",
        "reject_reason": None,
        "risk_gate_state": {"allowed_strategies": ["vrp_harvest"], "risk_engine": "account_tracker_not_ready"},
        "exec_mode": "ADVISORY",
    }

    cards = build_decision_summary_cards(latest)
    by_label = {c["label"]: c for c in cards}

    assert by_label["RiskEngine 승인"]["value"] == "계좌 추적기 대기중"
    assert by_label["RiskEngine 승인"]["status"] == "warning"


def test_build_decision_summary_cards_shows_reject_without_risk_engine():
    # Signal Fusion 자체가 REJECT를 낸 경우 risk_engine 키가 아예 없다(ENTER 후보가 아니었음).
    latest = {
        "timestamp": datetime(2026, 7, 29, 9, 8, 0),
        "conviction": "NO_TRADE",
        "decision": "REJECT",
        "reject_reason": "no_strategy_for_this_cell",
        "risk_gate_state": {"allowed_strategies": []},
        "exec_mode": "ADVISORY",
    }

    cards = build_decision_summary_cards(latest)
    by_label = {c["label"]: c for c in cards}

    assert by_label["최근 판단"]["value"] == "진입 없음 (09:08:00)"
    assert by_label["최근 판단"]["status"] == "info"
    assert by_label["허용 전략"]["value"] == "-"
    assert by_label["RiskEngine 승인"]["value"] == "게이트 미평가"
    assert by_label["RiskEngine 승인"]["status"] == "neutral"


def test_build_decision_history_table_renders_recent_rows():
    history = [
        {
            "timestamp": datetime(2026, 7, 29, 9, 5, 0),
            "conviction": "HIGH_CONVICTION",
            "decision": "ENTER",
            "reject_reason": None,
            "risk_gate_state": {"risk_engine": {"approved": True, "approved_size": 0.5}},
            "exec_mode": "ADVISORY",
        },
        {
            "timestamp": datetime(2026, 7, 29, 9, 0, 0),
            "conviction": "NO_TRADE",
            "decision": "REJECT",
            "reject_reason": "defensive_regime_no_new_entries",
            "risk_gate_state": {},
            "exec_mode": "ADVISORY",
        },
    ]

    fig = build_decision_history_table(history)

    times, decisions, convictions, risk_labels, reasons = fig.data[0].cells.values
    assert list(times) == ["09:05:00", "09:00:00"]
    assert list(decisions) == ["진입 후보", "진입 없음"]
    assert risk_labels[0] == "승인(0.50)"
    assert risk_labels[1] == "-"
    assert reasons[1] == "defensive_regime_no_new_entries"


def test_build_decision_history_table_handles_empty_history():
    fig = build_decision_history_table([])
    assert len(fig.data[0].cells.values[0]) == 0
