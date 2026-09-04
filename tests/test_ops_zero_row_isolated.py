"""단발 완전실패 축 — 「몇 분 붙어 있었는가」의 반대쪽 (2026-09-04 §1-7 / 제4부 P2-B).

09-04는 「옵션체인 이번 분 전멸」 ERROR가 세 번(14:34 · 14:51 · 15:06) 났는데 셋 다 다음
분에 회복돼 최장 연속은 **1분**이었다. 그 축만 보면 그날은 「연속 1분짜리 평범한 날」로
접히고, 세 번의 완전실패는 어느 축에도 안 남는다. 09-03은 반대로 **53분 한 덩어리**이고
고립 분은 0개다.

단발 완전실패는 신선도 창(5분)이 덮으므로 그날 판단을 안 끊는다 — **그래서 경보가 안 울린
채로 잦아진다.** 그 빈도가 늘고 있는지 물으려면 전날과 비교할 숫자가 있어야 한다.

⛔ **임계를 만들지 않았다.** 이 축은 세기만 한다. 아래 「판정 무변경」 테스트가 그것을 지킨다 —
최장 연속 축·임계·절벽일 플래그는 한 글자도 안 바뀐다.
"""

from __future__ import annotations

from mahdi.ops import db_metrics, report


# ===== 집계 자체 =====

def test_isolated_minutes_counts_09_04_three_singletons():
    """09-04 실측 — 셋 다 앞뒤가 정상이라 전부 고립이다."""
    assert db_metrics._isolated_minutes(["14:34", "14:51", "15:06"]) == [
        "14:34", "14:51", "15:06"
    ]


def test_isolated_minutes_is_zero_when_the_day_is_one_long_cliff():
    """09-03형 — 53분이 한 덩어리면 고립 분은 **0개**다. 총계로는 안 갈리는 자리가 여기다."""
    cliff = [f"{(14 * 60 + 31 + i) // 60:02d}:{(14 * 60 + 31 + i) % 60:02d}" for i in range(53)]
    assert db_metrics._isolated_minutes(cliff) == []


def test_isolated_minutes_splits_a_mixed_day():
    """섞인 날 — 붙은 것은 빼고 떨어진 것만 남는다. 시(hour) 경계를 넘어도 같다."""
    assert db_metrics._isolated_minutes(
        ["09:30", "10:59", "11:00", "11:01", "13:07"]
    ) == ["09:30", "13:07"]


def test_isolated_and_run_minutes_sum_to_the_total():
    """불변식 — 고립 분 + 연속 구간 소속 분 = 총 0행 분. 어긋나면 새 축이 잘못 센 것이다."""
    for minutes in (
        [],
        ["14:34", "14:51", "15:06"],
        ["09:30", "10:59", "11:00", "11:01", "13:07"],
        ["10:00", "10:01"],
    ):
        isolated = db_metrics._isolated_minutes(minutes)
        in_runs = [m for m in minutes if m not in set(isolated)]
        assert len(isolated) + len(in_runs) == len(minutes)


def test_isolated_minutes_is_empty_not_none_on_a_clean_day():
    """규약 C — 0건인 날은 빈 목록이다. 「없음」과 「안 셌다」는 소비측이 키 유무로 가른다."""
    assert db_metrics._isolated_minutes([]) == []


# ===== 렌더링 =====

def _coverage(minutes: list[str], *, with_key: bool = True) -> dict:
    cov = {
        "available": True,
        "span_minutes": 493,
        "minutes_with_rows": 493 - len(minutes),
        "zero_row_minutes": minutes,
        "zero_row_count": len(minutes),
        "zero_row_longest_run": db_metrics._longest_run(minutes)
        or {"length": 0, "start": None, "end": None},
        "zero_row_run_alert_minutes": db_metrics.ZERO_ROW_RUN_ALERT_MINUTES,
        "over_design_minutes": [],
        "over_design_count": 0,
    }
    if with_key:
        isolated = db_metrics._isolated_minutes(minutes)
        cov["zero_row_isolated_minutes"] = isolated
        cov["zero_row_isolated_count"] = len(isolated)
    return cov


def test_section_prints_the_three_singletons_of_09_04():
    rendered = "\n".join(report._render_zero_row_isolated(_coverage(["14:34", "14:51", "15:06"])))
    assert "**3건**" in rendered
    assert "14:34, 14:51, 15:06" in rendered


def test_section_prints_zero_on_a_clean_day():
    """규약 C — 0건인 날도 줄이 실린다. 사라지면 「안 셌다」와 구별이 안 된다."""
    rendered = "\n".join(report._render_zero_row_isolated(_coverage([])))
    assert "**0건**" in rendered
    assert "측정 불가" not in rendered


def test_section_says_unmeasurable_when_the_key_is_absent():
    """구버전 집계(P2-B 이전)는 0건이 아니라 **측정 불가**다."""
    rendered = "\n".join(
        report._render_zero_row_isolated(_coverage(["14:34"], with_key=False))
    )
    assert "측정 불가" in rendered


def test_section_carries_no_threshold():
    """⛔ 임계를 만들지 않았다 — 경고 표식(⚠·⛔)이 이 줄에 붙으면 안 된다."""
    for minutes in ([], ["14:34", "14:51", "15:06"], ["0%d:%02d" % (9, i) for i in range(0, 40, 2)]):
        rendered = "\n".join(report._render_zero_row_isolated(_coverage(minutes)))
        assert "⚠" not in rendered and "⛔" not in rendered
        assert "임계" not in rendered or "임계는 두지 않는다" in rendered


def test_headline_table_carries_the_new_row_next_to_the_run():
    """§1 한눈에 표 — 두 줄이 나란히 있어야 09-03형과 09-04형이 같은 날로 안 보인다."""
    paths = [path for _label, path, _fmt, _dir in report.HEADLINE_DB_METRICS]
    assert "chain_minute_coverage.zero_row_isolated_count" in paths
    assert paths.index("chain_minute_coverage.zero_row_longest_run.length") + 1 == paths.index(
        "chain_minute_coverage.zero_row_isolated_count"
    )


# ===== 판정 무변경 (B등급 필수) =====

def test_longest_run_is_untouched_by_the_new_axis():
    """대가 축 — 종전 축의 정의도 값도 임계도 안 건드렸다. 09-04는 1분, 09-03은 53분 그대로다."""
    assert db_metrics._longest_run(["14:34", "14:51", "15:06"]) == {
        "length": 1, "start": "14:34", "end": "14:34"
    }
    cliff = [f"{(14 * 60 + 31 + i) // 60:02d}:{(14 * 60 + 31 + i) % 60:02d}" for i in range(53)]
    assert db_metrics._longest_run(cliff)["length"] == 53
    assert db_metrics.ZERO_ROW_RUN_ALERT_MINUTES == 20


def test_run_section_output_is_byte_identical_before_and_after():
    """§4의 「최장 연속 0행 구간」 줄은 한 글자도 안 바뀐다 — 새 줄은 그 **뒤에** 붙는다."""
    for minutes in ([], ["14:34", "14:51", "15:06"]):
        with_new = report._render_zero_row_run(_coverage(minutes))
        without = report._render_zero_row_run(_coverage(minutes, with_key=False))
        assert with_new == without


def test_cliff_day_flag_is_unaffected():
    """절벽일 플래그(`_cliff_run_minutes`)의 동작이 안 바뀐다 — 09-04는 안 뜨고 09-03은 뜬다."""
    assert report._cliff_run_minutes({"chain_minute_coverage": _coverage(["14:34", "14:51", "15:06"])}) == 1
    cliff = [f"{(14 * 60 + 31 + i) // 60:02d}:{(14 * 60 + 31 + i) % 60:02d}" for i in range(53)]
    assert report._cliff_run_minutes({"chain_minute_coverage": _coverage(cliff)}) == 53
