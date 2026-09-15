"""§7 정리 목록이 **`발동일`을 본다** — 끝난 정리를 매일 다시 부르지 않는다 (2026-09-15 장후).

08-26 P2-1이 레버 경고를 「**지남 AND 꺼짐**」으로 좁혀 다섯 회차 연속 오탐을 멎게 했다.
그런데 그때 남은 「지남 AND 켜짐」 가지가 `발동일`을 안 봐서, **정리가 다 끝난 레버도 매일
다시 호명되고 경과일수만 1씩 늘었다**. 09-15 실측으로 세 레버가 각각 23·13·15일째였고
셋 다 켠 날짜는 이미 적혀 있었다 — 09-15 리포트 §1-5의 「만성 5거래일째」가 이것이다.

⚠ 이 파일의 절반은 **안 바뀌는 것**을 고정한다. 정리 목록은 등급을 내리는 자리이고
(`flags`가 아니다), 그 아래의 `⚠`와 §12 적신호는 한 글자도 움직이면 안 된다 —
그것이 `2026-08-26-p2-1-lever-warning-needs-both-conditions`가 적어 둔 대가 축이다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"

# 09-15 증거 §7이 셋 다 「날짜 정리 필요」로 부른 레버들. 각 레버가 실제로 켜진 날짜를
# 어느 파일이 이미 적어 두고 있는지 함께 둔다 — 이 표가 곧 「정리는 끝났다」의 근거다.
ALREADY_CLEANED = (
    ("use_effective_member_count", "2026-08-23"),
    ("SIGNAL_FUSION_PHASE_OFFSET_SECONDS", "2026-09-02"),
    ("OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS", "2026-08-31"),
)


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_cleanup", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_lever_whose_start_date_is_recorded_has_nothing_left_to_clean(collector):
    """켜져 있고 날짜가 지났어도 **`발동일`이 적혀 있으면** 정리할 것이 없다."""
    assert collector.lever_cleanup_is_pending(True, -23, "2026-08-23") is False


def test_a_lever_without_a_start_date_stays_on_the_cleanup_list(collector):
    """⚠ **대가 축** — `발동일`이 없으면 그대로 남는다.

    여기까지 조용해지면 「언제 켰는지 아무도 모르는 레버」가 영원히 묻힌다.
    이 fix가 없애려는 것은 경고가 아니라 **끝난 일의 반복 호명**이다.
    """
    assert collector.lever_cleanup_is_pending(True, -23, None) is True
    assert collector.lever_cleanup_is_pending(True, -1, "") is True


@pytest.mark.parametrize("on", [False, None])
def test_an_off_or_unreadable_lever_is_never_touched_by_this_rule(collector, on):
    """⚠ **판정 무변경** — 꺼진 레버·못 읽은 레버는 이 함수가 손대지 않는다.

    그 둘은 §7에서 `⚠`를 받고 §12 적신호(`flags`)로도 올라간다. 정리 목록의 등급 조정이
    그 경로를 건드리면 08-26 P2-1이 *"이 fix가 진짜 미발동을 덮는다"*고 경고한 자리가 된다.
    """
    assert collector.lever_cleanup_is_pending(on, -23, None) is False
    assert collector.lever_cleanup_is_pending(on, -23, "2026-08-23") is False


def test_a_deadline_that_has_not_passed_is_not_cleanup(collector):
    """아직 안 지난 기한은 정리 대상이 아니다 — 오늘(D0)도 포함해서."""
    assert collector.lever_cleanup_is_pending(True, 0, None) is False
    assert collector.lever_cleanup_is_pending(True, 3, None) is False


def test_the_three_chronic_levers_are_actually_recorded_in_the_live_repo(collector):
    """**실물 확인** — 09-15에 23·13·15일째로 불린 셋은 `발동일`이 이미 적혀 있다.

    이 테스트가 빨개지면 위 `lever_cleanup_is_pending`이 조용해진 근거가 사라진 것이다 —
    그때는 정리 목록이 다시 그 레버를 부르는 것이 맞다.
    """
    schedule = collector.lever_schedule(PROJECT_ROOT)
    for key, started in ALREADY_CLEANED:
        info = schedule.get(key, {})
        assert info.get("발동일") == started, f"{key}: 발동일이 {info.get('발동일')}이다"
        assert collector.lever_cleanup_is_pending(True, -99, info.get("발동일")) is False
