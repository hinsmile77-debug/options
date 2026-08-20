"""증거 수집기 — **하루 한 파일** 전환이 옛 날짜를 눈멀게 하지 않는가 (2026-08-20).

2026-08-20에 점검 산출물이 국면별 4파일에서 **하루 한 파일**(`{날짜}_마흐디_일일점검.md`)로
바뀌었다. 장전이 만들고 장중·장후가 append한다(`mahdi-daily-check` 대원칙 B).

이 파일이 지키는 것은 셋이다.

1. **전환일 이후**: §9가 새 이름 하나만 기대한다 — 옛 4종을 계속 기대하면 매 회차
   「없음 ⚠」 4줄이 뜨고, 그 소음에 진짜 누락이 묻힌다.
2. **전환일 이전**: 옛 규약 그대로 판정한다 — 과거 날짜를 재집계할 때 새 이름을 기대하면
   **하루도 빠짐없이 거짓 누락**이 뜬다(`INTRA_1430_SINCE`가 같은 이유로 존재한다).
3. **`latest_report_before()`가 신·구를 함께 찾는다** — 새 이름만 찾으면 §8-1
   「전일 보고서 대조」가 전환일 이후 **영구히** 빈다. 2026-07-16~08-20의 20여 편이
   옛 이름이고, 「전일 보고서」는 달력이 아니라 파일이 진실원천이기 때문이다.

3번이 이 전환의 **가장 조용한 실패 모드**다. 1·2번은 표에 「없음 ⚠」으로 보이지만,
3번은 §8-1이 한 줄 짧아질 뿐이라 아무도 안 본다.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_one_file", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base(tmp_path: Path) -> Path:
    base = tmp_path / "docs" / "동작점검"
    base.mkdir(parents=True)
    return base


def _touch(base: Path, *names: str) -> None:
    for name in names:
        (base / name).write_text("x", encoding="utf-8", newline=chr(10))


# ---------------------------------------------------------------- 전환 경계

def test_the_switchover_date_is_pinned(collector):
    """날짜를 상수로 박아 두지 않으면 「언제부터 새 규약인가」가 사람 기억에 남는다."""
    assert collector.ONE_FILE_SINCE == date(2026, 8, 21)


# ---------------------------------------------------------------- §8-1 전일 보고서

def test_the_previous_report_is_found_under_the_old_name(collector, tmp_path):
    """전환 직후의 「전일」은 **옛 이름**이다. 이것을 못 찾으면 §8-1이 그날부터 영구히 빈다."""
    base = _base(tmp_path)
    _touch(base, "2026-08-20_마흐디_운영점검보고서.md")
    found = collector.latest_report_before(tmp_path, date(2026, 8, 21))
    assert found is not None and found.name == "2026-08-20_마흐디_운영점검보고서.md"


def test_the_previous_report_is_found_under_the_new_name(collector, tmp_path):
    base = _base(tmp_path)
    _touch(base, "2026-08-21_마흐디_일일점검.md")
    found = collector.latest_report_before(tmp_path, date(2026, 8, 24))
    assert found is not None and found.name == "2026-08-21_마흐디_일일점검.md"


def test_on_a_tie_the_new_name_wins(collector, tmp_path):
    """전환일 하루는 두 파일이 공존한다. 그날 「전일 보고서」로 읽어야 할 것은 **종합
    완성본인 새 파일**이고, 경로 문자열 정렬에 답이 끌려가면 안 된다."""
    base = _base(tmp_path)
    _touch(base, "2026-08-20_마흐디_운영점검보고서.md", "2026-08-20_마흐디_일일점검.md")
    found = collector.latest_report_before(tmp_path, date(2026, 8, 21))
    assert found is not None and found.name == "2026-08-20_마흐디_일일점검.md"


def test_the_newest_wins_across_naming_regimes(collector, tmp_path):
    """옛 이름이 더 많다고 옛 이름이 이기면 안 된다 — 이기는 것은 **날짜**다."""
    base = _base(tmp_path)
    _touch(
        base,
        "2026-08-18_마흐디_운영점검보고서.md",
        "2026-08-19_마흐디_운영점검보고서.md",
        "2026-08-20_마흐디_운영점검보고서.md",
        "2026-08-21_마흐디_일일점검.md",
    )
    found = collector.latest_report_before(tmp_path, date(2026, 8, 24))
    assert found is not None and found.name == "2026-08-21_마흐디_일일점검.md"


def test_a_report_dated_today_is_not_the_previous_one(collector, tmp_path):
    """**이전** 날짜만 센다 — 오늘 것을 전일로 읽으면 자기 지시를 자기가 대조한다."""
    base = _base(tmp_path)
    _touch(base, "2026-08-21_마흐디_일일점검.md")
    assert collector.latest_report_before(tmp_path, date(2026, 8, 21)) is None


def test_a_file_whose_prefix_is_not_a_date_is_skipped(collector, tmp_path):
    base = _base(tmp_path)
    _touch(base, "초안_마흐디_일일점검.md")
    assert collector.latest_report_before(tmp_path, date(2026, 8, 24)) is None


def test_both_naming_patterns_are_searched(collector):
    """상수 자체를 못 박는다 — 옛 패턴이 조용히 빠지면 위 테스트들이 다 통과하면서도
    저장소의 20여 편이 안 보인다."""
    assert "*_마흐디_일일점검.md" in collector._REPORT_GLOBS
    assert "*_마흐디_운영점검보고서.md" in collector._REPORT_GLOBS
    assert collector._REPORT_GLOBS[0] == "*_마흐디_일일점검.md"
