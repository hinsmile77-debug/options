from datetime import datetime

from mahdi.risk.market_halt import MarketHaltMonitor, MarketOperationStatus


def _status(code: str) -> MarketOperationStatus:
    return MarketOperationStatus(trht_yn="N", tr_susp_reas_cntt="", mkop_cls_code=code, vi_cls_code="0")


def test_trigger_code_halts_and_records_start_time():
    monitor = MarketHaltMonitor()
    now = datetime(2026, 7, 29, 9, 5, 0)

    transition = monitor.update(_status("174"), now)

    assert transition.changed is True
    assert transition.is_halted is True
    assert monitor.is_halted is True
    assert monitor.current_code == "174"
    assert monitor.halted_since == now


def test_clear_code_resumes_immediately_no_daily_latch():
    # 거래소 CB는 KRX가 해제 이벤트를 보내는 즉시 실시간으로 풀려야 한다 — risk/circuit_breaker.py의
    # CircuitBreaker(래치, reset_daily() 전까지 유지)와 달리 이 모듈은 래치가 없어야 한다.
    monitor = MarketHaltMonitor()
    monitor.update(_status("174"), datetime(2026, 7, 29, 9, 5, 0))

    transition = monitor.update(_status("175"), datetime(2026, 7, 29, 9, 25, 0))

    assert transition.changed is True
    assert transition.is_halted is False
    assert monitor.is_halted is False
    assert monitor.halted_since is None


def test_halt_start_time_preserved_across_different_halt_codes():
    # 174(서킷브레이크 발동) -> 182(서킷브레이크 장중동시마감)처럼 halted 상태에서 다른 halt
    # 코드로 바뀌어도 최초 진입 시각은 그대로 유지돼야 한다.
    monitor = MarketHaltMonitor()
    first = datetime(2026, 7, 29, 9, 5, 0)
    monitor.update(_status("174"), first)

    transition = monitor.update(_status("182"), datetime(2026, 7, 29, 9, 25, 0))

    assert transition.changed is False  # 이미 halted였으므로 상태 자체는 안 바뀜
    assert monitor.is_halted is True
    assert monitor.current_code == "182"
    assert monitor.halted_since == first


def test_unknown_code_leaves_state_unchanged():
    monitor = MarketHaltMonitor()

    transition = monitor.update(_status("112"), datetime(2026, 7, 29, 9, 0, 0))  # 112: 장개시(정상)

    assert transition.changed is False
    assert monitor.is_halted is False


def test_sidecar_code_does_not_affect_halt_state():
    monitor = MarketHaltMonitor()

    transition = monitor.update(_status("387"), datetime(2026, 7, 29, 10, 0, 0))  # 사이드카 매도발동

    assert transition.changed is False
    assert monitor.is_halted is False
