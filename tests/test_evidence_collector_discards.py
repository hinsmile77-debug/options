"""증거 수집기 §8-2 — **폐기된 안건이 다음 회차에서 되살아나지 않게 한다** (2026-08-19 Fix#5).

08-19 장중 두 회차가 08-18에 이미 폐기된 진단을 P1으로 되살렸고(보고서 §2-3), 같은 날
장후 보고서의 §4 Fix#1이 2026-08-01 사용자 결정으로 보류 확정된 Slack 토글을 다시 올렸다.
둘 다 `NEXT_TODO.md`에 답이 적혀 있었다 — 경로가 닿지 않았을 뿐이다.

이 파일이 지키는 것은 **그 목록이 매 회차 증거에 실린다**는 것이고, 특히
「못 읽었다」가 「0건」으로 접히지 않는다는 것이다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


def _load():
    spec = importlib.util.spec_from_file_location("collect_evidence_fix5", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collector():
    return _load()


def _write(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "docs" / "dev_memory"
    target.mkdir(parents=True)
    (target / "NEXT_TODO.md").write_text(text, encoding="utf-8", newline=chr(10))
    return tmp_path


SAMPLE = chr(10).join([
    "# NEXT_TODO",
    "",
    "### 보고서 Fix#1을 **폐기한다** - 착수 전 코드 검증에서 진단이 뒤집혔다",
    "본문 한 줄.",
    "",
    "## 살아 있는 안건",
    "- [ ] **이것은 폐기가 아니다**",
    "",
    "## 폐기·종결된 안건 (다시 올리지 말 것)",
    "",
    "- [x] **Slack 알림 - 2026-08-01 결정, 보류 유지.** 매 점검 보고서에서 다시 올리지 말 것.",
    "- [x] **폴러별 전용 페이서 분리 - 2026-08-01 기각.**",
    "",
    "## (이하 아카이브)",
    "- [x] **아카이브 항목은 세지 않는다**",
])


def test_the_do_not_raise_again_list_is_extracted_with_line_numbers(collector, tmp_path):
    """줄 번호가 함께 나와야 한다 — 회차가 원문을 확인하러 갈 자리가 그것이다."""
    items = collector.discarded_items(_write(tmp_path, SAMPLE))
    titles = [t for _n, t in items]

    assert any("Slack" in t for t in titles)
    assert any("페이서" in t for t in titles)
    assert any("폐기한다" in t for t in titles), "절 밖의 개별 폐기 선언도 잡아야 한다"
    assert all(isinstance(n, int) and n > 0 for n, _t in items)


def test_the_section_ends_at_the_next_heading_so_the_archive_is_not_swept_in(collector, tmp_path):
    """아카이브까지 끌어오면 목록이 수십 건이 되고, 그러면 아무도 안 읽는다."""
    titles = [t for _n, t in collector.discarded_items(_write(tmp_path, SAMPLE))]

    assert not any("아카이브 항목" in t for t in titles)
    assert not any("이것은 폐기가 아니다" in t for t in titles)


def test_an_unreadable_file_is_not_the_same_as_an_empty_list(collector, tmp_path):
    """규약 C — 조용히 비면 이 절은 **매일 통과**한다. 그 둘은 조치가 다르다."""
    assert collector.discarded_items(tmp_path) is None

    empty = _write(tmp_path, "# NEXT_TODO" + chr(10) + chr(10) + "본문뿐.")
    assert collector.discarded_items(empty) == []


def test_the_live_repository_list_is_not_empty(collector):
    """저장소 실물 — 절 제목 규약이 깨지면 이 절이 조용히 비므로 여기서 잡는다."""
    items = collector.discarded_items(PROJECT_ROOT)

    assert items, "`## 폐기·종결된 안건` 절을 못 찾았다 — 제목이 바뀌었는지 확인할 것"
    assert any("Slack" in t for _n, t in items), (
        "2026-08-01 사용자 결정(Slack 보류)이 목록에 없다 — "
        "08-19 보고서 §4 Fix#1이 그것을 다시 올린 항목이다"
    )
