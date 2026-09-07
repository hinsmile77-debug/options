"""증거 수집기 §5-1-1 — **분자와 분모를 같은 표에** (2026-08-24 Fix#6 B · Fix#4 B).

08-24에 「백오프 확대가 잔고 폴링을 떨어뜨렸는가」를 하루에 여섯 번 대조했고 **결론이 세 번
뒤집혔다**(장중 ① 「지연 상승이 떨어뜨렸다」 → 장중 ② 「7번 중 1번짜리 우연」 → 장후
「확대→실패 2/21 · 실패→확대 2/2」). 두 값이 서로 다른 파일에 있었기 때문이다.

그리고 15:10:50의 「3개 중 0개 회복」은 사람이 하루치 로그를 훑어서 찾았다 — 그 줄은 그날
INFO였고 회복률(하루 96.1%)이 그 전멸을 평균 안에 접어 없앴다.

이 파일이 지키는 것은 셋이다.
1. 세 축이 **같은 시간대 축**으로 세어진다.
2. 회복 실패는 **숫자(회복 < 대상)로** 잡는다 — 문구가 바뀌어도 눈이 멀지 않는다.
3. 세 줄이 하루 0건이면 **「없었다」가 아니라 「그 줄이 없는 버전일 수 있다」**로 인쇄한다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_recovery", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scan(collector, lines):
    scan = collector.LoopScan()
    for line in lines:
        scan.feed(line)
    return scan


def _line(hhmmss, level, msg):
    return f"2026-08-24 {hhmmss},000 {level}:mahdi.main:{msg}"


def test_the_three_axes_land_on_the_same_hour(collector):
    """08-24 12시대의 실제 형태 — 백오프 확대와 잔고 폴링 실패가 같은 칸에서 만난다."""
    from mahdi.main import LOG_BALANCE_POLL_FAILED

    scan = _scan(collector, [
        f"2026-08-24 12:30:10,000 INFO:mahdi.broker.rest_client:"
        f"레이트리밋 백오프 확대: 1.00s -> 1.50s (기준 대비 1.50배)",
        _line("12:34:32", "WARNING",
              LOG_BALANCE_POLL_FAILED % ("ReadTimeout", "없음", "1.50배", "12:33:32")),
        _line("12:35:20", "INFO", "먼슬리 레그 재시도: 3개 중 3개 회복(남은 예산 12.4초) "
                                  "— 판단 주입력(GEX/감마플립)의 두께다"),
    ])
    assert scan.backoff_expansions[12] == 1
    assert scan.balance_poll_failures[12] == 1
    assert scan.priority_retries[12] == 1
    assert scan.priority_retry_failures[12] == 0
    assert scan.priority_retry_total_failures[12] == 0
    assert scan.priority_retry_budget_min[12] == 12.4


def test_a_failed_revival_is_caught_by_the_numbers_not_the_wording(collector):
    """**문구가 바뀌어도 눈이 멀지 않는다** — 08-04(362건이 0건)의 재발을 막는 자리다."""
    scan = _scan(collector, [
        _line("15:10:50", "WARNING", "먼슬리 레그 재시도: 3개 중 0개 회복(남은 예산 0.0초) "
                                     "— 여기 문구가 무엇이든"),
    ])
    assert scan.priority_retries[15] == 1 and scan.priority_retry_failures[15] == 1
    assert scan.priority_retry_total_failures[15] == 1
    assert scan.priority_retry_budget_min[15] == 0.0


def test_the_old_info_wording_is_still_counted(collector):
    """옛 로그(전부 INFO)를 재집계해도 같은 답이어야 한다 — 레벨은 계측의 정체성이 아니다."""
    scan = _scan(collector, [
        _line("15:10:50", "INFO", "먼슬리 레그 재시도: 3개 중 0개 회복(남은 예산 0.0초) "
                                  "— 판단 주입력(GEX/감마플립)의 두께다"),
    ])
    assert scan.priority_retry_failures[15] == 1


def test_the_window_minimum_is_the_worst_moment_not_the_last(collector):
    """창 최소를 재는 이유는 §5-1과 같다 — 평균·마지막 값은 절벽을 눌러 없앤다."""
    scan = _scan(collector, [
        _line("14:31:20", "INFO", "먼슬리 레그 재시도: 2개 중 2개 회복(남은 예산 0.0초) — x"),
        _line("14:45:20", "INFO", "먼슬리 레그 재시도: 2개 중 2개 회복(남은 예산 9.9초) — x"),
    ])
    assert scan.priority_retry_budget_min[14] == 0.0


def test_the_tokens_still_match_the_source_constants(collector):
    """복제한 문구가 원본과 갈라지면 이 절이 **조용히 빈다**(ANCHORS와 같은 계약)."""
    from mahdi import main
    from mahdi.broker import rest_client

    assert collector.BALANCE_POLL_FAILED_TOKEN in main.LOG_BALANCE_POLL_FAILED
    assert collector.BACKOFF_EXPAND_TOKEN in rest_client.LOG_BACKOFF_EXPAND
    assert collector.PRIORITY_RETRY_RE.search(main.LOG_CHAIN_PRIORITY_RETRY % (3, 2, 1.5))
    assert collector.PRIORITY_RETRY_RE.search(
        main.LOG_CHAIN_PRIORITY_RETRY_FAILED % (3, 0, 0.0)
    )
    assert collector.PRIORITY_RETRY_RE.search(
        main.LOG_CHAIN_PRIORITY_RETRY_BUDGET_FLOOR % (3, 3, 2.9)
    )
    assert collector.PRIORITY_RETRY_FAILED_TOKEN in main.LOG_CHAIN_PRIORITY_RETRY_FAILED


# ===== 2026-09-07 Fix C (09-07 §1-9 정정) — **「간신히 실패」와 「전멸」을 가른다** =====
#
# 09-07 장중② 회차가 하루치 로그를 눈으로 훑어 완전 실패를 「두 차례」로 적었다. 실제로는
# 세 차례였고, 그중 하나로 센 13:38은 **6개 중 1개 회복**(부분)이었다. 사람이 "0개 회복"과
# "1개 회복"을 섞어 센 것이다 — 그 나눔을 축이 대신 한다.


def test_a_partial_recovery_is_a_failure_but_not_a_total_one(collector):
    """13:38 「6개 중 1개 회복」 — 실패 칸에는 들어가고 전멸 칸에는 안 들어간다."""
    scan = _scan(collector, [
        _line("13:38:53", "WARNING", "먼슬리 레그 재시도: 6개 중 1개 회복(남은 예산 0.0초) "
                                     "— 되살리기 실패"),
    ])
    assert scan.priority_retry_failures[13] == 1
    assert scan.priority_retry_total_failures[13] == 0


def test_the_09_07_day_splits_into_three_totals_and_nine_partials(collector):
    """09-07 실측 그대로 — 12:30·14:00·14:10만 전멸이다."""
    scan = _scan(collector, [
        _line("11:50:53", "WARNING", "먼슬리 레그 재시도: 6개 중 4개 회복(남은 예산 0.0초) — x"),
        _line("12:30:54", "WARNING", "먼슬리 레그 재시도: 7개 중 0개 회복(남은 예산 0.0초) — x"),
        _line("13:38:53", "WARNING", "먼슬리 레그 재시도: 6개 중 1개 회복(남은 예산 0.0초) — x"),
        _line("14:00:53", "WARNING", "먼슬리 레그 재시도: 1개 중 0개 회복(남은 예산 0.0초) — x"),
        _line("14:10:55", "WARNING", "먼슬리 레그 재시도: 7개 중 0개 회복(남은 예산 0.0초) — x"),
    ])
    assert sum(scan.priority_retry_failures.values()) == 5
    assert sum(scan.priority_retry_total_failures.values()) == 3
    assert dict(scan.priority_retry_total_failures) == {12: 1, 14: 2}


def test_a_cycle_with_nothing_to_retry_is_not_a_total_failure(collector):
    """「0개 중 0개 회복」은 실패가 아니라 **할 일이 없던 분**이다 — 세면 축이 뜻을 잃는다."""
    scan = _scan(collector, [
        _line("10:00:20", "INFO", "먼슬리 레그 재시도: 0개 중 0개 회복(남은 예산 5.0초) — x"),
    ])
    assert scan.priority_retries[10] == 1
    assert scan.priority_retry_failures[10] == 0
    assert scan.priority_retry_total_failures[10] == 0


def test_the_total_failures_are_a_subset_of_the_failures(collector):
    """부분집합 불변식 — 깨지면 새 축이 잘못 센 것이다(09-07 예측치의 대가 항목)."""
    scan = _scan(collector, [
        _line("12:30:54", "WARNING", "먼슬리 레그 재시도: 7개 중 0개 회복(남은 예산 0.0초) — x"),
        _line("12:35:20", "INFO", "먼슬리 레그 재시도: 3개 중 3개 회복(남은 예산 9.9초) — x"),
        _line("13:38:53", "WARNING", "먼슬리 레그 재시도: 6개 중 1개 회복(남은 예산 0.0초) — x"),
    ])
    for hour in set(scan.priority_retries):
        assert scan.priority_retry_total_failures[hour] <= scan.priority_retry_failures[hour]
