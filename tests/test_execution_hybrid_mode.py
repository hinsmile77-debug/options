import pytest

from mahdi.execution.hybrid_mode import (
    GateAction,
    HybridMode,
    effective_mode,
    gate_entry,
    gate_exit,
    mode_from_params,
)


# --- 2026-08-17 — 설정 파일이 읽히지 않는 선언이었다 -------------------------------------------
#
# `strategy_params.yaml`은 v6 §13.1을 따라 `hybrid_mode.default`를 처음부터 갖고 있었는데,
# 그 값을 `HybridMode`로 옮기는 코드가 저장소 어디에도 없었다 — 라이브는 문자열을 하드코딩했고
# 백테스트는 생성자 기본값 FULL_AUTO를 썼다.


def test_the_configured_mode_is_actually_read_now():
    assert mode_from_params({"hybrid_mode": {"default": "CONFIRM"}}) == HybridMode.CONFIRM
    assert mode_from_params({"hybrid_mode": {"default": "full_auto"}}) == HybridMode.FULL_AUTO


@pytest.mark.parametrize("params", [None, {}, {"hybrid_mode": {}}, {"hybrid_mode": {"default": "오타"}}])
def test_an_unreadable_setting_falls_to_the_most_conservative_mode(params):
    """오타 하나가 자동매매를 켜면 안 된다 — 모르면 ADVISORY다."""
    assert mode_from_params(params) == HybridMode.ADVISORY


def test_the_mode_is_clamped_to_advisory_while_the_order_path_is_unwired():
    """설정이 곧 사실이 되지 않게 하는 지점 — 주문 경로가 없는데 «자동매매 중»이라 기록하면
    체결 0건이 신호 부재로 읽힌다."""
    mode, reason = effective_mode(HybridMode.FULL_AUTO, order_path_wired=False)
    assert (mode, reason) == (HybridMode.ADVISORY, "order_path_not_wired")

    mode, reason = effective_mode(HybridMode.CONFIRM, order_path_wired=False)
    assert (mode, reason) == (HybridMode.ADVISORY, "order_path_not_wired")


def test_advisory_is_never_reported_as_clamped_because_nothing_was_lowered():
    assert effective_mode(HybridMode.ADVISORY, order_path_wired=False) == (HybridMode.ADVISORY, None)


def test_once_the_order_path_is_wired_the_setting_passes_through():
    assert effective_mode(HybridMode.FULL_AUTO, order_path_wired=True) == (HybridMode.FULL_AUTO, None)


def test_full_auto_entry_is_auto_submit():
    decision = gate_entry(HybridMode.FULL_AUTO)
    assert decision.action == GateAction.AUTO_SUBMIT
    assert decision.confirmation_timeout_seconds is None


def test_confirm_entry_is_pending_confirmation_with_timeout():
    decision = gate_entry(HybridMode.CONFIRM, confirmation_timeout_seconds=60)
    assert decision.action == GateAction.PENDING_CONFIRMATION
    assert decision.confirmation_timeout_seconds == 60


def test_advisory_entry_is_advisory_only():
    decision = gate_entry(HybridMode.ADVISORY)
    assert decision.action == GateAction.ADVISORY_ONLY


def test_hard_stop_exit_is_always_auto_regardless_of_mode():
    for mode in (HybridMode.ADVISORY, HybridMode.CONFIRM, HybridMode.FULL_AUTO):
        decision = gate_exit(mode, "hard_stop")
        assert decision.action == GateAction.AUTO_SUBMIT


def test_forced_flat_exit_is_always_auto_regardless_of_mode():
    for mode in (HybridMode.ADVISORY, HybridMode.CONFIRM, HybridMode.FULL_AUTO):
        decision = gate_exit(mode, "forced_flat_15_10")
        assert decision.action == GateAction.AUTO_SUBMIT


def test_circuit_breaker_exit_is_always_auto_regardless_of_mode():
    for mode in (HybridMode.ADVISORY, HybridMode.CONFIRM, HybridMode.FULL_AUTO):
        decision = gate_exit(mode, "circuit_breaker")
        assert decision.action == GateAction.AUTO_SUBMIT


def test_structure_stop_exit_follows_mode_when_not_always_automatic():
    assert gate_exit(HybridMode.FULL_AUTO, "structure_stop").action == GateAction.AUTO_SUBMIT
    confirm_decision = gate_exit(HybridMode.CONFIRM, "structure_stop")
    assert confirm_decision.action == GateAction.PENDING_CONFIRMATION
    assert confirm_decision.confirmation_timeout_seconds == 3
    assert gate_exit(HybridMode.ADVISORY, "structure_stop").action == GateAction.ADVISORY_ONLY
