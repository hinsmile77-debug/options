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

from mahdi.fusion.strategy_palette import NON_ENTRY_STRATEGIES

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

# 2026-08-05(COCKPIT 육안 점검 P2-11) — 거부사유 원문 → 한글.
#
# 08-05 화면의 판단 표는 시각/결정/확신도가 전부 한글인데 거부사유만 `strategy_palette:wait_only`,
# `conflict_resolution:no_clear_consensus` 같은 내부 식별자였다. 20행 중 19행이 같은 문자열이라
# **읽을 것이 없는 열**로 보이는데, 실제로는 "팔레트가 관망을 지시함"과 "앙상블이 합의에 못
# 이름"이 전혀 다른 원인이다 — 후자는 신호가 갈렸다는 뜻이고 전자는 신호를 볼 자리가 없었다는 뜻이다.
#
# 매핑에 없는 사유는 **원문을 그대로 보여준다.** 새 사유가 생겼을 때 "알 수 없음" 같은 말로
# 덮으면 그 사유를 추적할 수단을 잃는다(정보를 줄이는 방향으로 실패하지 않는다).
# 원문은 `mahdi/fusion/engine.py`(meta_label / conflict_resolution / strategy_palette:*)와
# `strategy_palette.py`의 `reason` 값들이 단일 소스다.
REJECT_REASON_LABEL_KO: dict[str, str] = {
    "meta_label:no_trade": "메타 라벨: 거래 안 함",
    "conflict_resolution:no_clear_consensus": "충돌 조정: 명확한 합의 없음",
    "strategy_palette:wait_only": "팔레트: 관망 지시만 있음",
    "strategy_palette:defensive_regime_no_new_entries": "팔레트: 방어 레짐 — 신규 진입 금지",
    "strategy_palette:short_gamma_requires_not_met": "팔레트: 숏감마 전제 미충족",
    "strategy_palette:no_strategy_for_this_cell": "팔레트: 해당 셀에 전략 없음",
}


def reject_reason_ko(reason: str | None) -> str:
    """매핑에 있으면 한글로, 없으면 원문 그대로(정보를 줄이지 않는다). 사유가 없으면 "-"."""
    if not reason:
        return "-"
    return REJECT_REASON_LABEL_KO.get(reason, reason)


def _unavailable_members_label(risk_gate_state: dict) -> str:
    """
    입력: 판단 행의 `risk_gate_state`.
    계산: 산출되지 않은 앙상블 멤버 수와 이름을 한 칸으로 요약한다.
    해석: 2026-08-05 P2-11. `member_unavailable`(어느 멤버가 왜 죽었는가)은 2026-08-04에 **바로
         이런 추적을 위해** 판단 행에 남기기 시작한 값인데(그전에는 사람이 signal_layer.py를 읽어
         역산해야 했다) COCKPIT에는 한 번도 표시되지 않았다. 08-05 화면에서 11:59의 단 한 건
         이상행(소규모 테스트 / 합의 없음)을 파고들 방법이 없었던 이유다.
    실패 조건: 키가 없으면(구 버전 행) "-".
    """
    unavailable = risk_gate_state.get("member_unavailable")
    if unavailable is None:
        return "-"
    if not unavailable:
        return "전원 가용"
    return f"{len(unavailable)}개 미가용: {', '.join(sorted(unavailable))}"

_EXIT_STAGE_NOT_WIRED_CARD = {
    "label": "청산 단계 (실행엔진)",
    "value": "미배선",
    "status": "info",
    "help": "ExecutionEngine의 6-레이어 청산 스택은 아직 관측 루프에 연결되지 않았습니다 — "
    "ADVISORY 모드는 진입 신호 평가까지만 수행합니다.",
}


def _allowed_strategy_card(allowed_strategies: list[str]) -> dict:
    """
    입력: `risk_gate_state["allowed_strategies"]`(v6 §11.4 팔레트 원문).
    계산: 첫 전략을 표시하되, 그것이 관망 계열(`NON_ENTRY_STRATEGIES`)이면 "(관망)"을 덧붙이고
         help로 이유를 설명한다.
    해석: 2026-07-30 운영점검 §2-2 — `wait_and_see`는 정당한 팔레트 값이지만 진입 전략이 아니다.
         카드에 전략명만 덩그러니 뜨면 "이 전략으로 진입 중"으로 오독되기 쉬워 구분해 표시한다.
    """
    if not allowed_strategies:
        return {"label": "허용 전략", "value": "-", "status": "neutral", "help": None}
    top = allowed_strategies[0]
    if top in NON_ENTRY_STRATEGIES:
        return {
            "label": "허용 전략",
            "value": f"{top} (관망)",
            "status": "info",
            "help": "팔레트가 관망을 지시한 상태 — 진입 전략이 아니라 '기다리라'는 결론이다.",
        }
    return {"label": "허용 전략", "value": top, "status": "neutral", "help": None}


# 2026-08-17 §11.5 — 선택기가 후보를 못 만든 사유의 한글 라벨. 매핑에 없는 값은 **원문 그대로**
# 보여준다(정보를 줄이는 방향으로 실패하지 않는다 — `reject_reason_ko`와 같은 규칙).
SELECTION_REASON_LABEL_KO: dict[str, str] = {
    "no_entry_strategy": "진입 전략 없음(팔레트 관망/방어 레짐)",
    "no_chain_snapshot": "체인 스냅샷 없음",
    "no_eligible_book": "쓸 만기 없음(만기 당일 북뿐)",
    "no_liquid_instrument": "유동성 하한 미달",
    "no_selection_rule": "선택 규칙 미정의 전략",
    "spot_unavailable": "기준가(선물 체결가) 없음",
    "no_strike_match": "규칙에 맞는 행사가 없음",
}


def _instrument_label(leg: dict) -> str:
    """계산: 단축코드가 있으면 그것을, 없으면 «행사가 콜/풋»으로 사람이 읽을 라벨을 만든다."""
    kind = "콜" if str(leg.get("option_type", "")).upper() == "C" else "풋"
    strike = leg.get("strike")
    base = f"{strike:g} {kind}" if strike is not None else kind
    symbol = leg.get("symbol")
    return f"{symbol} ({base})" if symbol else base


def _selected_instrument_card(selected: dict | None) -> dict:
    """
    입력: 판단 행의 `selected_instruments`(마이그레이션 031). 배선 이전 행은 None이다.
    계산: 고른 종목을 한 칸으로 요약하고, 못 골랐으면 **그 사유**를 보여준다.
    해석: 2026-08-17 — 이 카드가 없을 때 화면은 `허용 전략`까지만 말하고 «그래서 무엇을 살
         것인가»에 답하지 않았다. 사람이 수동 주문하는 단계(§11.5 승격 경로 ②)에서 갈림을
         재려면 화면에 이 값이 있어야 한다.
         **None과 빈 후보를 다르게 표시한다** — 전자는 선택기가 안 돈 행(배선 이전)이고
         후자는 돌았는데 고를 것이 없던 분이다.
    실패 조건: 없음.
    """
    if selected is None:
        return {
            "label": "선택 종목",
            "value": "미기록",
            "status": "neutral",
            "help": "이 판단 행은 종목 선택기 배선(2026-08-17) 이전에 기록됐습니다.",
        }
    candidates = selected.get("candidates") or []
    if not candidates:
        reason = selected.get("reason")
        return {
            "label": "선택 종목",
            "value": "없음",
            "status": "info",
            "help": SELECTION_REASON_LABEL_KO.get(reason, reason) if reason else None,
        }
    first = candidates[0]
    legs = first.get("legs") or []
    label = " / ".join(_instrument_label(leg) for leg in legs) if legs else "-"
    unresolved = sum(1 for leg in legs if not leg.get("symbol"))
    return {
        "label": "선택 종목",
        "value": f"{first.get('strategy', '-')}: {label}",
        "status": "warning" if unresolved else "ok",
        "help": (
            f"{unresolved}개 다리의 단축코드를 종목 마스터에서 찾지 못했습니다 — 선택 자체는 "
            "유효하고, 코드 조회만 실패한 상태입니다."
            if unresolved
            else f"만기 {selected.get('book_expiry') or '-'} · 규칙 "
            + ", ".join(str(leg.get("rule")) for leg in legs)
        ),
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

    # 2026-08-05(P2-11) — 확신도는 진입 후보가 아닌 사이클에서는 계산만 되고 **쓰이지 않는다.**
    # 08-05 화면은 "진입 없음"과 "표준"을 나란히 놓아 "표준 확신도인데도 진입을 안 했다"로
    # 읽혔지만, 실제로는 팔레트에 진입 전략이 없어 확신도가 소비될 자리가 없었다.
    is_entry = decision == "ENTER"
    conviction_card = {
        "label": "확신도",
        "value": conviction_label if is_entry else f"{conviction_label} (미사용)",
        "status": "neutral",
        "help": None if is_entry else "진입 후보가 아니어서 이 확신도는 이번 사이클에 쓰이지 않았습니다.",
    }

    return [
        {
            "label": "최근 판단",
            "value": f"{DECISION_LABEL_KO.get(decision, decision)} ({time_label})",
            "status": "ok" if is_entry else "info",
            "help": reject_reason_ko(latest.get("reject_reason")) if latest.get("reject_reason") else None,
        },
        conviction_card,
        _allowed_strategy_card(allowed_strategies),
        # 2026-08-17 §11.5 — 「전략 이름」 바로 옆에 「그래서 어느 종목인가」를 둔다.
        _selected_instrument_card(latest.get("selected_instruments")),
        _risk_engine_card(risk_gate_state),
        _EXIT_STAGE_NOT_WIRED_CARD,
    ]


def build_decision_history_table(history: list[dict]) -> go.Figure:
    """
    입력: `data_source.get_latest_decision_context()["history"]`(최신순 N건).
    계산: 최근 판단 이력을 `macro_panel.build_macro_snapshot_table`과 동일한 `go.Table` 스타일로
         타임라인 표시한다 — 시각/결정/확신도/RiskEngine 승인/거부사유.
    """
    times, decisions, convictions, risk_labels, reasons, members = [], [], [], [], [], []
    for row in history:
        risk_gate_state = row.get("risk_gate_state") or {}
        risk_engine = risk_gate_state.get("risk_engine")
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
        # 2026-08-05(P2-11): 확신도는 진입 후보가 아닌 행에서는 **쓰이지 않은 값**이다 — 그런데
        # 08-05 화면은 "진입 없음 / 표준"을 나란히 놓아 "표준 확신도로 진입을 안 했다"로 읽혔다.
        # 실제로는 팔레트에 진입 전략이 없어 확신도가 소비된 적이 없다.
        conviction = CONVICTION_LABEL_KO.get(row["conviction"], row["conviction"])
        convictions.append(conviction if row["decision"] == "ENTER" else f"{conviction} (미사용)")
        risk_labels.append(risk_label)
        reasons.append(reject_reason_ko(row.get("reject_reason")))
        members.append(_unavailable_members_label(risk_gate_state))

    fig = go.Figure(
        go.Table(
            header=dict(
                values=["시각", "결정", "확신도", "RiskEngine", "거부사유", "앙상블 미가용 멤버"],
                align="center",
            ),
            cells=dict(
                values=[times, decisions, convictions, risk_labels, reasons, members], align="center"
            ),
        )
    )
    # 2026-08-05(P2-11): 상한이 400이라 기본 20행(= 60 + 28*20 = 620)이 들어가지 않아 **마지막
    # 행이 잘려 보였다**(08-05 화면의 11:55 행). 호출측이 이미 `limit`으로 행 수를 묶으므로
    # 상한은 그 기본값이 온전히 들어가는 크기면 된다.
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=min(640, 60 + 28 * max(len(history), 1)))
    return fig
