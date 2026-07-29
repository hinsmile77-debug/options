"""마흐디 판단 현황 패널 (2026-07-29 신규) — Signal Fusion + Risk Engine이 지금 어떤 진입
판단을 내리고 있는지를 COCKPIT 최상단에서 "3초 룰"(스크롤 없이 한눈에)로 보여준다.

ADVISORY 전용(실주문 없음) — `mahdi.main.poll_signal_fusion_cycle()`이 남긴 `signal_decisions`
행을 그대로 읽어 보여줄 뿐, 이 패널이 직접 계산하는 값은 없다. ExecutionEngine(6-레이어 청산
스택·하이브리드 모드)은 아직 `main.py` 라이브 루프에 배선되지 않았으므로(`mahdi/execution/
engine.py` docstring 참고), "청산 단계" 카드는 실데이터 없이 "미배선" 상태를 고정 표시한다 —
나중에 배선되면 이 카드 한 칸만 실데이터로 바꾸면 되도록 레이아웃을 미리 확보해둔다.
"""

from __future__ import annotations

import plotly.graph_objects as go

CONVICTION_LABEL_KO: dict[str, str] = {
    "NO_TRADE": "거래 없음",
    "SMALL_TEST": "소규모 테스트",
    "STANDARD": "표준",
    "HIGH_CONVICTION": "고확신",
}

# 2026-07-29(사용자 피드백) — DB의 "ENTER"/"REJECT"는 poll_signal_fusion_cycle()이 60초마다
# 상태 없이 독립적으로 재평가한 결과일 뿐, 실제 주문 체결/포지션 보유를 뜻하지 않는다(ADVISORY
# 전용, ExecutionEngine 미배선). 원문 그대로 노출하면 "매분 새로 진입했다"로 오독하기 쉬워
# "이번 평가에서 후보로 뽑혔는지" 뉘앙스가 드러나는 라벨로 바꾼다. "보류/유보"는 "다음에 다시
# 시도한다"는 시간적 이연 뉘앙스를 주는데, 실제로는 그런 이연 없이 매 사이클이 완전히 새 평가라
# 오해를 하나 더 만든다 — 그래서 "없음"(이번 사이클엔 후보가 없었다) 쪽을 선택.
DECISION_LABEL_KO: dict[str, str] = {
    "ENTER": "진입 후보",
    "REJECT": "진입 없음",
}

_EXIT_STAGE_NOT_WIRED_CARD = {
    "label": "청산 단계 (실행엔진)",
    "value": "미배선",
    "status": "info",
    "help": "ExecutionEngine의 6-레이어 청산 스택은 아직 관측 루프에 연결되지 않았습니다 — "
    "ADVISORY 모드는 진입 신호 평가까지만 수행합니다.",
}


def _risk_engine_card(risk_gate_state: dict) -> dict:
    risk_engine = risk_gate_state.get("risk_engine")
    if risk_engine is None:
        return {
            "label": "RiskEngine 승인",
            "value": "게이트 미평가",
            "status": "neutral",
            "help": "진입 후보(ENTER)가 아니어서 RiskEngine을 호출하지 않았습니다.",
        }
    if risk_engine == "account_tracker_not_ready":
        return {
            "label": "RiskEngine 승인",
            "value": "계좌 추적기 대기중",
            "status": "warning",
            "help": "계좌 잔고 폴러가 아직 스냅샷을 한 건도 못 쌓아 RiskEngine을 호출하지 못했습니다.",
        }
    approved = risk_engine.get("approved")
    if approved:
        return {
            "label": "RiskEngine 승인",
            "value": f"승인 (사이즈 {risk_engine.get('approved_size', 0.0):.2f})",
            "status": "ok",
            "help": None,
        }
    reasons = risk_engine.get("reject_reasons") or []
    return {
        "label": "RiskEngine 승인",
        "value": f"거부: {reasons[0] if reasons else '사유 미상'}",
        "status": "warning",
        "help": None,
    }


def build_decision_summary_cards(latest: dict | None) -> list[dict]:
    """
    입력: `data_source.get_latest_decision_context()["latest"]` — 없으면(아직 폴링 전) None.
    계산: "3초 룰" 카드 5장 — 최근 결정(시각 포함)/확신도/허용 전략/RiskEngine 승인/청산 단계.
         카드는 label/value/status("ok"|"warning"|"info"|"neutral")/help로 구성된 dict라,
         향후 지표를 추가할 땐 이 리스트에 dict 하나만 더 붙이면 된다(레이아웃 코드 불변 —
         `get_health_summary()`가 이미 쓰는 패턴과 동일).
    """
    if latest is None:
        return [
            {"label": "최근 판단", "value": "아직 없음", "status": "neutral", "help": None},
            _EXIT_STAGE_NOT_WIRED_CARD,
        ]

    risk_gate_state = latest.get("risk_gate_state") or {}
    decision = latest["decision"]
    conviction_label = CONVICTION_LABEL_KO.get(latest["conviction"], latest["conviction"])
    allowed_strategies = risk_gate_state.get("allowed_strategies") or []
    time_label = latest["timestamp"].strftime("%H:%M:%S")

    return [
        {
            "label": "최근 판단",
            "value": f"{DECISION_LABEL_KO.get(decision, decision)} ({time_label})",
            "status": "ok" if decision == "ENTER" else "info",
            "help": latest.get("reject_reason"),
        },
        {"label": "확신도", "value": conviction_label, "status": "neutral", "help": None},
        {
            "label": "허용 전략",
            "value": allowed_strategies[0] if allowed_strategies else "-",
            "status": "neutral",
            "help": None,
        },
        _risk_engine_card(risk_gate_state),
        _EXIT_STAGE_NOT_WIRED_CARD,
    ]


def build_decision_history_table(history: list[dict]) -> go.Figure:
    """
    입력: `data_source.get_latest_decision_context()["history"]`(최신순 N건).
    계산: 최근 판단 이력을 `macro_panel.build_macro_snapshot_table`과 동일한 `go.Table` 스타일로
         타임라인 표시한다 — 시각/결정/확신도/RiskEngine 승인/거부사유.
    """
    times, decisions, convictions, risk_labels, reasons = [], [], [], [], []
    for row in history:
        risk_engine = (row.get("risk_gate_state") or {}).get("risk_engine")
        if risk_engine is None:
            risk_label = "-"
        elif risk_engine == "account_tracker_not_ready":
            risk_label = "계좌 추적기 대기중"
        elif risk_engine.get("approved"):
            risk_label = f"승인({risk_engine.get('approved_size', 0.0):.2f})"
        else:
            risk_label = "거부"
        times.append(row["timestamp"].strftime("%H:%M:%S"))
        decisions.append(DECISION_LABEL_KO.get(row["decision"], row["decision"]))
        convictions.append(CONVICTION_LABEL_KO.get(row["conviction"], row["conviction"]))
        risk_labels.append(risk_label)
        reasons.append(row.get("reject_reason") or "-")

    fig = go.Figure(
        go.Table(
            header=dict(values=["시각", "결정", "확신도", "RiskEngine", "거부사유"], align="center"),
            cells=dict(values=[times, decisions, convictions, risk_labels, reasons], align="center"),
        )
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=min(400, 60 + 28 * max(len(history), 1)))
    return fig
