import logging

from mahdi.logutil import TracebackBudget, WarningThrottle


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _make_throttle(monkeypatch, window_seconds: float = 60.0):
    logger = logging.getLogger("test.logutil")
    throttle = WarningThrottle(logger, window_seconds=window_seconds)
    clock = _FakeClock()
    monkeypatch.setattr("mahdi.logutil.time.monotonic", clock)
    return throttle, clock, logger


def test_first_occurrence_always_logs(monkeypatch, caplog):
    throttle, clock, logger = _make_throttle(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="test.logutil"):
        throttle.warning("cat_a", "문제 발생: %s", "strike=1340")

    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == "문제 발생: strike=1340"


def test_repeats_within_window_are_suppressed(monkeypatch, caplog):
    # 2026-07-19(§5-5): 얇은 옵션 종목 레그 실패처럼 60초 사이클 안에서 여러 번(레그마다) 반복
    # 발생하는 경고는 window 안에서는 최초 1건만 실제로 로깅돼야 한다.
    throttle, clock, logger = _make_throttle(monkeypatch, window_seconds=60.0)
    with caplog.at_level(logging.WARNING, logger="test.logutil"):
        throttle.warning("cat_a", "실패: strike=%s", 1340.0)
        clock.now = 10.0
        throttle.warning("cat_a", "실패: strike=%s", 1350.0)
        clock.now = 30.0
        throttle.warning("cat_a", "실패: strike=%s", 1360.0)

    assert len(caplog.records) == 1  # 첫 건만 실제 로깅, 나머지 2건은 억제


def test_logs_again_after_window_with_suppressed_count_appended(monkeypatch, caplog):
    throttle, clock, logger = _make_throttle(monkeypatch, window_seconds=60.0)
    with caplog.at_level(logging.WARNING, logger="test.logutil"):
        throttle.warning("cat_a", "실패: strike=%s", 1340.0)
        clock.now = 10.0
        throttle.warning("cat_a", "실패: strike=%s", 1350.0)  # 억제(1건)
        clock.now = 20.0
        throttle.warning("cat_a", "실패: strike=%s", 1360.0)  # 억제(2건)
        clock.now = 61.0  # window(60초) 경과 → 다시 로깅
        throttle.warning("cat_a", "실패: strike=%s", 1370.0)

    assert len(caplog.records) == 2
    second = caplog.records[1].getMessage()
    assert "실패: strike=1370.0" in second
    assert "최근 60초간 2건 추가 억제됨" in second


def test_different_categories_are_independent(monkeypatch, caplog):
    # 서로 다른 category는 서로의 억제 상태에 영향을 주면 안 된다.
    throttle, clock, logger = _make_throttle(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="test.logutil"):
        throttle.warning("cat_a", "A 문제")
        throttle.warning("cat_b", "B 문제")
        clock.now = 1.0
        throttle.warning("cat_a", "A 문제 반복")  # 억제
        throttle.warning("cat_b", "B 문제 반복")  # 억제

    assert len(caplog.records) == 2
    assert caplog.records[0].getMessage() == "A 문제"
    assert caplog.records[1].getMessage() == "B 문제"


def test_exc_info_is_forwarded():
    logger = logging.getLogger("test.logutil.excinfo")
    throttle = WarningThrottle(logger, window_seconds=60.0)
    try:
        raise ValueError("boom")
    except ValueError:
        # exc_info=True를 넘겼을 때 예외 없이 그대로 동작하는지만 확인(로그 포맷터가 실제로
        # traceback을 렌더링하는 건 표준 logging 책임이라 여기선 호출이 깨지지 않는지가 핵심).
        throttle.warning("cat_exc", "예외 발생", exc_info=True)


# ===== TracebackBudget (2026-08-05 §2-4 / Fix#4) =====


class _Boom(RuntimeError):
    pass


def test_traceback_budget_allows_a_few_samples_then_stops():
    """예외 유형당 표본 N건까지만 트레이스백을 허용한다.

    08-05: ReadTimeout 210건 x 78.9줄 = 16,577줄이 로그의 49%를 차지했고, 그 값이
    `human_lines`에 섞여 가설 `2026-08-04-p4`를 **거짓 반증**시켰다. 210번째 트레이스백이
    3번째보다 알려주는 것은 없다.
    """
    budget = TracebackBudget(samples_per_type=3)
    decisions = [budget.take(_Boom("x")) for _ in range(5)]

    assert [keep for keep, _ in decisions] == [True, True, True, False, False]
    assert [nth for _, nth in decisions] == [1, 2, 3, 4, 5], "누적 건수는 표본을 다 쓴 뒤에도 센다"


def test_traceback_budget_counts_each_exception_type_separately():
    """유형별로 따로 센다 — 흔한 예외가 드문 예외의 표본을 잡아먹으면 안 된다.

    이것이 없으면 ReadTimeout 210건이 상한을 다 써버려, 그날 처음 나타난 새 예외의
    트레이스백이 **한 번도 안 남는다.** 원인 추적이 필요한 쪽은 정확히 그 새 예외다.
    """
    budget = TracebackBudget(samples_per_type=1)

    assert budget.take(_Boom("a"))[0] is True
    assert budget.take(_Boom("b"))[0] is False
    assert budget.take(ValueError("c"))[0] is True, "다른 유형은 자기 표본을 그대로 갖는다"


def test_traceback_budget_type_name_matches_the_traceback_tail():
    """유형 표기가 트레이스백 마지막 줄과 같아야 집계가 유형을 잃지 않는다.

    `log_metrics._EXCEPTION_PREFIXES`는 `httpx.ReadTimeout` 표기로 세므로, 트레이스백을
    생략한 요약 줄도 같은 문자열을 실어야 한다(08-04 §2-1 재발 방지).
    """
    import httpx

    assert TracebackBudget.type_name(httpx.ReadTimeout("t")) == "httpx.ReadTimeout"
    assert TracebackBudget.type_name(ValueError("v")) == "builtins.ValueError"
