from mahdi.execution.hybrid_mode import GateAction, HybridMode, gate_entry, gate_exit


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
