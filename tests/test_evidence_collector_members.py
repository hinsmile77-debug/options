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


def test_the_live_log_wording_matches_what_the_parser_expects(collector):
    """복제본 계약 — `fusion/engine.py`의 포맷 문자열을 실제로 채워 보고 파서에 먹인다.

    이 파일은 stdlib 전용이라 정규식이 **복제본**이다. 원본이 움직이면 여기서 깨져야 한다.
    """
    from mahdi.fusion.engine import MEMBER_FIELDS

    source = (PROJECT_ROOT / "mahdi" / "fusion" / "engine.py").read_text(encoding="utf-8")
    assert '"판단 형태 전이: 가용멤버 %s(%d/%d, 비영 %d) · %s · 사유 %s · 전략 %s%s"' in source, (
        "로그 문구가 바뀌었다 — `collect_evidence.MEMBER_RE`를 같이 옮겼는지 확인할 것"
    )
    rendered = "판단 형태 전이: 가용멤버 %s(%d/%d, 비영 %d) · %s · 사유 %s · 전략 %s%s" % (
        ["regime_hmm", "options_flow"], 2, len(MEMBER_FIELDS), 1, "SMALL_TEST", "없음", "없음", "",
    )
    m = collector.MEMBER_RE.search(rendered)
    assert m is not None and m.group(4) == "1" and m.group(5) == "SMALL_TEST"
