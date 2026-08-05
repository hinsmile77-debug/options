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

    times, decisions, convictions, risk_labels, reasons, members = fig.data[0].cells.values
    assert list(times) == ["09:05:00", "09:00:00"]
    assert list(decisions) == ["진입 후보", "진입 없음"]
    assert risk_labels[0] == "승인(0.50)"
    assert risk_labels[1] == "-"
    # 2026-08-05(P2-11): 매핑에 없는 사유는 원문 그대로 — 새 사유를 "알 수 없음"으로 덮으면
    # 그 사유를 추적할 수단을 잃는다.
    assert reasons[1] == "defensive_regime_no_new_entries"
    # 진입 후보가 아닌 행의 확신도는 쓰이지 않은 값이라 그 사실을 함께 쓴다.
    assert convictions[0] == "고확신"
    assert convictions[1] == "거래 없음 (미사용)"
    assert list(members) == ["-", "-"]  # member_unavailable 키가 없는 구 버전 행


def test_build_decision_history_table_translates_known_reject_reasons():
    """2026-08-05 P2-11 — 08-05 화면은 시각/결정/확신도가 전부 한글인데 거부사유만 내부 식별자였다.

    20행 중 19행이 같은 문자열이라 "읽을 것이 없는 열"로 보이지만, 실제로는 팔레트 관망과
    앙상블 합의 실패가 전혀 다른 원인이다.
    """
    history = [
        {"timestamp": datetime(2026, 8, 5, 12, 12), "conviction": "STANDARD", "decision": "REJECT",
         "reject_reason": "strategy_palette:wait_only", "risk_gate_state": {}, "exec_mode": "ADVISORY"},
        {"timestamp": datetime(2026, 8, 5, 11, 59), "conviction": "SMALL_TEST", "decision": "REJECT",
         "reject_reason": "conflict_resolution:no_clear_consensus", "risk_gate_state": {},
         "exec_mode": "ADVISORY"},
    ]

    reasons = build_decision_history_table(history).data[0].cells.values[4]

    assert reasons[0] == "팔레트: 관망 지시만 있음"
    assert reasons[1] == "충돌 조정: 명확한 합의 없음"


def test_build_decision_history_table_surfaces_unavailable_ensemble_members():
    """`member_unavailable`은 2026-08-04에 바로 이런 추적을 위해 판단 행에 남기기 시작한 값인데
    COCKPIT에는 한 번도 표시되지 않았다 — 08-05에 11:59 이상행을 파고들 방법이 없었던 이유다."""
    history = [
        {"timestamp": datetime(2026, 8, 5, 12, 12), "conviction": "STANDARD", "decision": "REJECT",
         "reject_reason": "strategy_palette:wait_only",
         "risk_gate_state": {"member_unavailable": {"regime_hmm": "방향성 없는 레짐",
                                                    "orderflow_ofi_vpin": "미시구조 없음"}},
         "exec_mode": "ADVISORY"},
        {"timestamp": datetime(2026, 8, 5, 12, 11), "conviction": "STANDARD", "decision": "REJECT",
         "reject_reason": None, "risk_gate_state": {"member_unavailable": {}}, "exec_mode": "ADVISORY"},
    ]

    members = build_decision_history_table(history).data[0].cells.values[5]

    assert members[0] == "2개 미가용: orderflow_ofi_vpin, regime_hmm"
    assert members[1] == "전원 가용"  # 빈 dict와 키 부재("-")는 다르다


def test_build_decision_history_table_handles_empty_history():
    fig = build_decision_history_table([])
    assert len(fig.data[0].cells.values[0]) == 0
