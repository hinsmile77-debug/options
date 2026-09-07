"""먼슬리 되살리기 — **「간신히 실패」와 「전멸」을 가른다** (2026-09-07 Fix C).

09-07 장중②(14:30) 회차가 하루치 로그를 눈으로 훑어 완전 실패를 「13:38·14:10 두 차례」로
적었다. 장후 회차가 원본을 다시 세어 정정했다 — 13:38은 **6개 중 1개 회복**(부분)이었고,
완전 미회복은 **12:30·14:00·14:10 세 차례**였다.

틀린 것은 사람이 아니라 **축이다.** `failed_cycles`는 「회복 < 대상」을 전부 한 칸에 담아,
사람이 그 칸을 열어 손으로 나누게 만든다. 그 나눔이 자동이면 정정 자체가 없었다.

이 파일이 지키는 것은 넷이다.
1. 전멸(`0개 회복`)만 새 축에 담긴다 — 부분 회복은 안 담긴다.
2. **판정 무변경** — `failed_cycles`·`failed_minutes`·`recovery_pct`는 한 글자도 안 바뀐다.
3. **부분집합 불변식** — `total_failure_cycles <= failed_cycles`가 항상 성립한다.
4. 규약 C — 0건인 날도 키가 실린다.
"""

from __future__ import annotations

from datetime import date

from mahdi.ops import log_metrics

DAY = date(2026, 9, 7)


def _line(hhmmss: str, level: str, attempted: int, recovered: int, left: float) -> str:
    return (
        f"2026-09-07 {hhmmss},000 {level}:mahdi.main:"
        f"먼슬리 레그 재시도: {attempted}개 중 {recovered}개 회복(남은 예산 {left:.1f}초) "
        "— 판단 주입력(GEX/감마플립)의 두께다"
    )


# 09-07 실측 그대로 — 실패 12건 중 전멸 3건(12:30·14:00·14:10), 부분 9건.
# 그중 13:38(6개 중 1개)이 그날 사람이 전멸로 잘못 센 줄이다.
_09_07 = [
    _line("11:50:53", "WARNING", 6, 4, 0.0),
    _line("12:30:54", "WARNING", 7, 0, 0.0),
    _line("12:55:53", "WARNING", 3, 2, 0.0),
    _line("13:11:54", "WARNING", 5, 3, 0.0),
    _line("13:34:53", "WARNING", 4, 2, 0.0),
    _line("13:38:53", "WARNING", 6, 1, 0.0),
    _line("13:50:54", "WARNING", 2, 1, 0.0),
    _line("14:00:53", "WARNING", 1, 0, 0.0),
    _line("14:10:55", "WARNING", 7, 0, 0.0),
    _line("15:00:53", "WARNING", 3, 2, 0.0),
    _line("15:05:53", "WARNING", 4, 3, 0.0),
    _line("15:19:53", "WARNING", 2, 1, 0.0),
    # 전량 회복 — 실패도 전멸도 아니다.
    _line("09:31:20", "INFO", 3, 3, 12.9),
]


def test_the_09_07_day_splits_into_three_total_failures_and_nine_partials():
    """이 fix의 전부 — 사람이 손으로 세다 틀린 그 나눔을 축이 대신 한다."""
    pr = log_metrics.parse_day(_09_07, DAY)["priority_retry"]

    assert pr["failed_cycles"] == 12
    assert pr["total_failure_cycles"] == 3
    assert pr["total_failure_minutes"] == ["12:30", "14:00", "14:10"]
    # 부분 회복 9건 — 이 수는 뺄셈으로만 나온다. 그래서 두 축이 **둘 다** 있어야 한다.
    assert pr["failed_cycles"] - pr["total_failure_cycles"] == 9


def test_the_line_the_human_miscounted_is_a_partial_not_a_total_failure():
    """13:38 「6개 중 1개 회복」 — 0이 아니다. 이 한 줄이 09-07 정정의 원인이었다."""
    pr = log_metrics.parse_day([_line("13:38:53", "WARNING", 6, 1, 0.0)], DAY)["priority_retry"]

    assert pr["failed_cycles"] == 1          # 실패이긴 하다(회복 < 대상)
    assert pr["total_failure_cycles"] == 0   # 그러나 전멸은 아니다
    assert pr["total_failure_minutes"] == []


def test_the_old_axes_do_not_move():
    """**판정 무변경** — 새 축을 더해도 종전 세 값은 09-07 사이드카와 같아야 한다.

    B등급 항목의 조건이다. 종전 축이 한 칸이라도 움직이면 그 순간 이 fix는 「세는 것만
    바꾼다」가 아니라 「판정을 바꾼다」가 된다.
    """
    pr = log_metrics.parse_day(_09_07, DAY)["priority_retry"]

    assert pr["cycles"] == 13
    assert pr["attempted"] == 53
    assert pr["recovered"] == 22
    assert pr["failed_minutes"] == [
        "11:50", "12:30", "12:55", "13:11", "13:34", "13:38",
        "13:50", "14:00", "14:10", "15:00", "15:05", "15:19",
    ]
    # 회복률은 **레그** 기준이고 위 두 축은 **사이클** 기준이다 — 나란히 두되 나누지 않는다.
    assert pr["recovery_pct"] == 41.5


def test_the_total_failures_are_always_a_subset_of_the_failures():
    """부분집합 불변식 — 깨지면 새 축이 잘못 센 것이다(09-07 예측치의 대가 항목)."""
    for lines in ([], _09_07, [_line("10:00:00", "INFO", 4, 4, 9.0)]):
        pr = log_metrics.parse_day(lines, DAY)["priority_retry"]
        assert pr["total_failure_cycles"] <= pr["failed_cycles"]
        assert set(pr["total_failure_minutes"]) <= set(pr["failed_minutes"])


def test_a_cycle_with_nothing_to_retry_is_not_a_total_failure():
    """「0개 중 0개 회복」은 실패가 아니라 **할 일이 없던 분**이다.

    이 구분이 없으면 되살릴 레그가 없던 조용한 분이 매번 전멸로 세어지고, 그 순간 이 축은
    「KIS가 얼마나 나빴는가」가 아니라 「재시도가 몇 번 돌았는가」를 재게 된다.
    """
    pr = log_metrics.parse_day([_line("10:00:00", "INFO", 0, 0, 5.0)], DAY)["priority_retry"]

    assert pr["cycles"] == 1
    assert pr["failed_cycles"] == 0
    assert pr["total_failure_cycles"] == 0


def test_the_key_is_present_on_a_day_with_no_retry_line_at_all():
    """규약 C — 0과 「안 셌다」를 가른다. 키가 없으면 옛 버전의 날과 구분이 안 된다."""
    pr = log_metrics.parse_day([], DAY)["priority_retry"]

    assert pr["total_failure_cycles"] == 0
    assert pr["total_failure_minutes"] == []


def test_the_headline_table_carries_the_new_axis():
    """§1 한눈에 표에 실려야 전일 대비 델타가 자동으로 보인다 — 리포트 Fix C의 절반이다."""
    from mahdi.ops import report

    paths = [path for _, path, _, _ in report.HEADLINE_METRICS]
    assert "priority_retry.total_failure_cycles" in paths
    # 종전 줄도 그대로 있어야 한다(살린 레그와 못 살린 분은 반대쪽 끝이다).
    assert "priority_retry.recovered" in paths
