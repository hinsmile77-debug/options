"""§5-2 이탈 적신호가 **「형태 무변화」와 「진짜 미관측」을 가른다** (2026-09-03 제4부 P1-B).

09-03 §1-6 — 자동 §12 적신호가 「63분째 미관측」을 냈는데 실제로는 **축이 죽은 것이 아니라
판단 형태가 안 바뀌어 로그가 조용했던 것**이었다. `member_last_seen`은 「판단 형태 전이」
줄에서만 갱신되고 그 줄은 형태가 바뀔 때만 찍히므로, **판단이 멀쩡히 도는 안정된 시간대일수록
이 적신호가 잘 뜬다.** 오탐이 진짜 정지를 덮는 것이 이 자리의 위험이다.

⛔ **임계는 손대지 않는다.** `MEMBER_DROPOUT_ALERT_MIN = 30`은 그대로다 — 바뀐 것은
「무엇을 재는가」(마지막 관측 시각만 → 그 시각 + 같은 구간의 판단 사이클 생존)다.
⛔ **적신호를 지우지 않는다.** 두 경우 다 적신호로 나가고 **문구만** 갈린다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_p1b", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minutes(start: str, end: str, collector) -> set:
    """`start`~`end`(포함) 매분에 사이클이 찍힌 것으로 둔다."""
    return set(range(collector.hhmm_to_min(start), collector.hhmm_to_min(end) + 1))


# ===== 09-03 §1-6 재현 — 이것이 오탐이었다 =====


def test_a_quiet_axis_with_a_live_cycle_is_named_shape_unchanged(collector):
    """12:30 회차의 「63분째 미관측」 — 사이클은 그 63분 내내 돌고 있었다."""
    line = collector._dropout_flag(
        "options_flow", "11:27:03", collector.hhmm_to_min("12:30"),
        _minutes("11:28", "12:29", collector),
    )

    assert "형태 무변화" in line
    assert "미관측" not in line.split("형태 무변화")[0]
    assert "62분" in line, "그 사이 사이클이 찍힌 분 수를 함께 말해야 근거가 된다"


def test_the_shape_unchanged_wording_still_sends_people_to_the_db(collector):
    """「형태가 같다」는 「그 축이 살아 있다」와 같은 말이 아니다 — 단정하지 않는다."""
    line = collector._dropout_flag(
        "options_flow", "11:27:03", collector.hhmm_to_min("12:30"),
        _minutes("11:28", "12:29", collector),
    )

    assert "member_scores" in line


# ===== 판정 무변경 — 진짜 사건에서는 종전 문구 그대로다 =====


def test_a_dead_cycle_still_raises_the_original_alert(collector):
    """**이 항목은 적신호를 없애는 것이 아니라 둘로 가르는 것이다.**"""
    line = collector._dropout_flag(
        "options_flow", "14:05:11", collector.hhmm_to_min("15:20"), set(),
    )

    assert "75분째 미관측" in line
    assert "축이 도중에 빠졌다" in line
    assert "입력 고갈" in line, "08-14 오후의 귀속 질문은 그대로 남아야 한다"
    assert "형태 무변화" not in line


def test_cycles_outside_the_gap_do_not_count(collector):
    """양 끝 바깥의 사이클은 「그 사이」가 아니다 — 그것으로 오탐을 지우면 안 된다."""
    before = _minutes("09:00", "14:05", collector)
    after = _minutes("15:20", "15:44", collector)
    line = collector._dropout_flag(
        "options_flow", "14:05:11", collector.hhmm_to_min("15:20"), before | after,
    )

    assert "진짜 미관측" in line, "구간 밖 사이클로 사건을 정상으로 읽으면 08-14를 놓친다"


def test_a_single_surviving_cycle_is_enough_to_split(collector):
    """한 분이라도 판단이 돌았으면 「판단 경로 자체가 멈췄다」고 말할 수 없다."""
    line = collector._dropout_flag(
        "options_flow", "14:05:11", collector.hhmm_to_min("15:20"),
        {collector.hhmm_to_min("14:30")},
    )

    assert "형태 무변화" in line
    assert "**1분**" in line


# ===== 임계 무변경 =====


def test_the_threshold_is_untouched(collector):
    """바꾸는 것은 임계가 아니라 「무엇을 재는가」여야 한다."""
    assert collector.MEMBER_DROPOUT_ALERT_MIN == 30
