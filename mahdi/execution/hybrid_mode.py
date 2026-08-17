"""Execution Engine — 하이브리드 3모드 (v6 §13.1).

모드는 "진입 자유"의 정도만 조절한다 — Hard Stop/Circuit Breaker/Forced Flat은
`strategy_params.yaml`의 `hybrid_mode.always_automatic` 목록에 있으므로 모드와
무관하게 항상 자동이다(v6 §13.1 불변 규칙: "수동 모드는 공격의 자유이지 방어의
자유가 아니다").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_ALWAYS_AUTOMATIC_EXIT_LAYERS = frozenset({"hard_stop", "circuit_breaker", "forced_flat_15_10"})


class HybridMode(str, Enum):
    ADVISORY = "ADVISORY"
    CONFIRM = "CONFIRM"
    FULL_AUTO = "FULL_AUTO"


class GateAction(str, Enum):
    AUTO_SUBMIT = "AUTO_SUBMIT"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    ADVISORY_ONLY = "ADVISORY_ONLY"


@dataclass(frozen=True, slots=True)
class GateDecision:
    action: GateAction
    confirmation_timeout_seconds: int | None = None


def mode_from_params(params: dict | None) -> HybridMode:
    """
    입력: `strategy_params.yaml` 전체 dict(`hybrid_mode.default`를 읽는다).
    계산: 설정 문자열을 `HybridMode`로 옮긴다.
    해석: 2026-08-17 — **이 함수가 없어서 설정값이 아무 데도 안 닿고 있었다.**
         `strategy_params.yaml`에 `hybrid_mode.default: "ADVISORY"`가 v6 §13.1을 따라 처음부터
         있었는데, 그것을 읽어 `HybridMode`로 바꾸는 코드가 저장소 어디에도 없었다. 라이브는
         `main.py`가 문자열 `"ADVISORY"`를 DB에 직접 하드코딩했고, 백테스트는 생성자 기본값
         `FULL_AUTO`를 썼다 — 즉 설정 파일은 **읽히지 않는 선언**이었다.
    실패 조건: 값이 없거나 모르는 문자열이면 ADVISORY로 떨어진다. **가장 보수적인 쪽으로
              떨어지는 것이 요점이다** — 오타 하나가 자동매매를 켜면 안 된다.
    """
    configured = ((params or {}).get("hybrid_mode") or {}).get("default")
    try:
        return HybridMode(str(configured).upper())
    except ValueError:
        return HybridMode.ADVISORY


def effective_mode(configured: HybridMode, *, order_path_wired: bool) -> tuple[HybridMode, str | None]:
    """
    입력: 설정된 모드, 주문 제출 경로가 실제로 배선돼 있는지.
    계산: 배선 전이면 무조건 ADVISORY로 낮추고 그 사유를 함께 돌려준다.
    해석: **설정이 곧 사실이 되지 않게 하는 지점이다.** 주문 경로가 없는데 `exec_mode`에
         "FULL_AUTO"를 기록하면 `signal_decisions`가 «자동매매 중»이라고 말하면서 체결은 0건인
         상태가 된다 — 사후에 그 기록을 읽는 사람은 자동매매가 돌았는데 신호가 없었다고 읽는다.
         설정과 실제를 **둘 다** 남기고, 값이 갈리면 호출측이 경고한다.
    실패 조건: 없음 — 배선 후에는 설정 그대로 통과한다(두 번째 반환값 None).
    """
    if order_path_wired or configured == HybridMode.ADVISORY:
        return configured, None
    return HybridMode.ADVISORY, "order_path_not_wired"


def gate_entry(mode: HybridMode, confirmation_timeout_seconds: int = 60) -> GateDecision:
    """
    입력: 현재 하이브리드 모드, CONFIRM 모드 대시보드 승인 타임아웃(초).
    계산: FULL_AUTO -> AUTO_SUBMIT. CONFIRM -> PENDING_CONFIRMATION(타임아웃 포함 — 타임아웃 시
         자동 취소는 Order Manager 책임). ADVISORY -> ADVISORY_ONLY(주문 없음, 신호만 표시).
    실패 조건: 없음 — 3가지 모드 전부 매핑된다.
    """
    if mode == HybridMode.FULL_AUTO:
        return GateDecision(action=GateAction.AUTO_SUBMIT)
    if mode == HybridMode.CONFIRM:
        return GateDecision(
            action=GateAction.PENDING_CONFIRMATION,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
        )
    return GateDecision(action=GateAction.ADVISORY_ONLY)


def gate_exit(
    mode: HybridMode,
    exit_layer: str,
    confirmation_timeout_seconds: int = 3,
    always_automatic: frozenset[str] = _ALWAYS_AUTOMATIC_EXIT_LAYERS,
) -> GateDecision:
    """
    입력: 현재 모드, 청산을 촉발한 레이어 이름("hard_stop"/"structure_stop"/"flow_stop"/
         "belief_decay_stop"/"time_stop"/"forced_flat_15_10").
    계산: exit_layer가 always_automatic에 있으면 모드 무관 AUTO_SUBMIT(v6 §13.1 불변 규칙).
         그 외 레이어는 gate_entry()와 같은 모드별 규칙을 따르되, CONFIRM 모드는 §13.3
         "Structure Stop: 3초 경고 후 자동"에 맞춰 기본 타임아웃을 3초로 짧게 둔다(진입 확인
         타임아웃보다 훨씬 짧음 — 청산 지연은 진입 지연보다 비용이 크다).
    실패 조건: 없음.
    """
    if exit_layer in always_automatic:
        return GateDecision(action=GateAction.AUTO_SUBMIT)
    if mode == HybridMode.FULL_AUTO:
        return GateDecision(action=GateAction.AUTO_SUBMIT)
    if mode == HybridMode.CONFIRM:
        return GateDecision(
            action=GateAction.PENDING_CONFIRMATION,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
        )
    return GateDecision(action=GateAction.ADVISORY_ONLY)
