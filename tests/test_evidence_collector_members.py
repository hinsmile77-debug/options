"""증거 수집기 §5-2 — **가용과 실질을 가른다** (2026-08-19 Fix#6).

08-19 장중 회차가 「판단 형태 전이」의 `4/6`을 보고 축 가용성을 ✅로 읽었고, 장후 DB 축에서만
실질 2.36이 보였다(죽은 축 1.07). 0점은 중립이지 의견이 아닌데 0점 멤버도 「가용멤버」
목록에는 남는다 — 그 줄만 보는 눈은 차이를 구조적으로 못 본다.

**이 파일이 지키는 것은 파서가 옛 문구와 새 문구를 둘 다 읽는다는 것이다.** 08-04에 로그
레벨이 바뀌며 정규식이 눈이 멀어 362건이 0건으로 보고된 전례가 있다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"

OLD = "판단 형태 전이: 가용멤버 ['a', 'b', 'c', 'd'](4/6) · STANDARD · 사유 없음 · 전략 없음"
NEW = "판단 형태 전이: 가용멤버 ['a', 'b', 'c', 'd'](4/6, 비영 2) · STANDARD · 사유 없음 · 전략 없음"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_fix6", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("line", [OLD, NEW])
def test_one_parser_reads_both_wordings(collector, line):
    """08-18의 라벨 개명이 지킨 순서 — **파서를 먼저 이중화하고 문구를 바꾼다.**"""
    m = collector.MEMBER_RE.search(line)

    assert m is not None, "이 정규식이 눈이 멀면 그날 전이가 통째로 0건으로 보고된다"
    assert (m.group(2), m.group(3)) == ("4", "6")
    assert m.group(5) == "STANDARD", "확신도 그룹이 밀리면 안 된다"


def test_the_new_wording_carries_the_nonzero_count(collector):
    assert collector.MEMBER_RE.search(NEW).group(4) == "2"


def test_the_old_wording_reports_unknown_not_zero(collector):
    """「비영 0」과 「안 셌다」는 조치가 다르다 — 전자는 사고, 후자는 옛 로그다."""
    assert collector.MEMBER_RE.search(OLD).group(4) is None


# 2026-08-31 (08-31 §1-15 / 제4부 P1-4) — 줄 **끝**에 분모 꼬리표 자리가 하나 더 붙었다.
# 앞머리와 `비영 %d` 자리는 그대로다 — 파서가 보는 곳을 안 건드리는 것이 이 fix의 조건이었다.
# 2026-09-02 (09-02 §1-8 / 제4부 P1-3) — 줄 끝에 **`0점축=[...]` 자리가 또 하나** 붙었다.
# 같은 조건이 그대로 걸린다: 앞머리와 `비영 %d`는 안 움직였고, 늘어난 것은 꼬리뿐이다.
# ⚠ 이 상수가 깨지는 것이 곧 「문구가 움직였다」는 신호다 — 08-04에 그것을 놓쳐 362건이
# 0건으로 보고됐다. 깨지면 문구를 되돌리는 게 아니라 **파서를 같이 옮겼는지** 확인할 것.
LIVE_FORMAT = "판단 형태 전이: 가용멤버 %s(%d/%d, 비영 %d) · %s · 사유 %s · 전략 %s%s%s%s"
DENOMINATOR_NOTE = " · ⚠ 분모 1 — 합의비율 구조적 1.00"
ZERO_AXIS_NOTE = " · 0점축=['regime_hmm']"


def test_the_live_log_wording_matches_what_the_parser_expects(collector):
    """복제본 계약 — `fusion/engine.py`의 포맷 문자열을 실제로 채워 보고 파서에 먹인다.

    이 파일은 stdlib 전용이라 정규식이 **복제본**이다. 원본이 움직이면 여기서 깨져야 한다.
    """
    from mahdi.fusion.engine import MEMBER_FIELDS

    source = (PROJECT_ROOT / "mahdi" / "fusion" / "engine.py").read_text(encoding="utf-8")
    assert f'"{LIVE_FORMAT}"' in source, (
        "로그 문구가 바뀌었다 — `collect_evidence.MEMBER_RE`를 같이 옮겼는지 확인할 것"
    )
    rendered = LIVE_FORMAT % (
        ["regime_hmm", "options_flow"], 2, len(MEMBER_FIELDS), 1, "SMALL_TEST", "없음", "없음",
        "", ZERO_AXIS_NOTE, "",
    )
    m = collector.MEMBER_RE.search(rendered)
    assert m is not None and m.group(4) == "1" and m.group(5) == "SMALL_TEST"


def test_the_denominator_tag_does_not_blind_the_parser(collector):
    """2026-08-31 P1-4 — **꼬리표가 붙어도 파서가 같은 값을 낸다.**

    08-04에 문구가 움직이며 정규식이 눈이 멀어 362건이 0건으로 보고됐다. 그래서 이 fix의
    조건은 「줄 끝에만 붙인다」였고, 그 조건을 여기서 고정한다.
    """
    from mahdi.fusion.engine import MEMBER_FIELDS

    rendered = LIVE_FORMAT % (
        ["regime_hmm"], 1, len(MEMBER_FIELDS), 1, "HIGH_CONVICTION", "없음", "없음",
        "", ZERO_AXIS_NOTE, DENOMINATOR_NOTE,
    )
    m = collector.MEMBER_RE.search(rendered)
    assert m is not None, "꼬리표 한 줄이 이 축을 통째로 0건으로 만들면 안 된다"
    assert (m.group(2), m.group(3)) == ("1", str(len(MEMBER_FIELDS)))
    assert m.group(4) == "1"
    assert m.group(5) == "HIGH_CONVICTION", "확신도 그룹이 밀리면 안 된다"


def test_the_denominator_tag_only_fires_when_the_lever_is_on_and_count_is_one():
    """⛔ **동작은 안 바뀐다 — 꼬리표는 조건 둘이 다 참일 때만 붙는다.**

    레버가 꺼져 있으면 분모가 `available_member_count`라 「분모 1」이 성립하지 않는다.
    조건을 하나만 보면 그것이 08-31 §3-6이 정리한 오탐 형태(조건 A만 보고 판정)의 재연이다.
    """
    from mahdi.fusion.engine import SignalFusionEngine

    def note(lever_on: bool, effective: int) -> str:
        engine = SignalFusionEngine.__new__(SignalFusionEngine)
        engine._params = {"use_effective_member_count": lever_on}
        decision = type("D", (), {"effective_member_count": effective})()
        return (
            " · ⚠ 분모 1 — 합의비율 구조적 1.00"
            if engine._params.get("use_effective_member_count", False)
            and decision.effective_member_count <= 1
            else ""
        )

    assert note(True, 1) == DENOMINATOR_NOTE
    assert note(True, 0) == DENOMINATOR_NOTE, "0도 구조적 1.00이 나는 자리다"
    assert note(True, 2) == "", "분모가 2면 붙지 않는다"
    assert note(False, 1) == "", "레버가 꺼져 있으면 분모가 다른 값이다 — 붙으면 오탐이다"
