"""증거 수집기 §8-1 — **이름이 바뀐 것을 「없어진 것」으로 세지 않는다** (2026-08-24 Fix#2).

08-24 장전 §8-1이 「전일 보고서가 언급했는데 yaml에 없는 id **13개**」를 적신호로 올렸다.
그중 7건은 08-23 세션이 등재하며 슬러그 앞의 `fixN`을 바꾼 것이었다:

    보고서: 2026-08-21-fix1-monthly-legs-are-reserved-first
    yaml  : 2026-08-21-fix5-monthly-legs-are-reserved-first

종전 대조는 「완전일치 또는 `id + '-'` 접두」였다 — **꼬리가 같고 머리가 다른** 이 형태를
구조적으로 못 잡는다. 그런 오보가 몇 번 나면 이 절 전체가 무시된다.

**그러나 슬러그 일치로 판정하지는 않는다.** 서로 다른 두 가설이 같은 슬러그를 가질 수 있고,
그때 잘못 이어 붙이면 08-19 Fix#5가 경계한 「거짓 안심」의 반대편 실수가 된다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_renamed", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 08-24 실측 13건 중 슬러그가 살아 있는 7건 — 고정 입력으로 쓴다.
_REPORT_IDS = [
    "2026-08-20-fix2-monthly-legs-are-reserved-first",
    "2026-08-21-fix1-monthly-legs-are-reserved-first",
    "2026-08-21-fix2-expiry-burst-leaves-a-success-line",
    "2026-08-21-fix4-member-exit-is-an-event",
    "2026-08-21-fix5-degraded-episode-gets-a-closing-line",
    "2026-08-21-fix6-warmup-end-is-logged",
    "2026-08-20-fix4-member-exit-is-an-event",
]
_YAML_IDS = {
    "2026-08-21-fix5-monthly-legs-are-reserved-first",
    "2026-08-21-fix6-expiry-burst-leaves-a-success-line",
    "2026-08-21-fix7-member-exit-is-an-event",
    "2026-08-21-fix8-degraded-episode-gets-a-closing-line",
    "2026-08-21-fix9-warmup-end-is-logged",
}


def test_the_slug_survives_a_renumbered_fix(collector):
    assert collector.hypothesis_slug("2026-08-21-fix1-monthly-legs-are-reserved-first") == (
        collector.hypothesis_slug("2026-08-21-fix5-monthly-legs-are-reserved-first")
    )


def test_the_slug_keeps_ids_with_different_tails_apart(collector):
    """개명을 따라가되 **다른 것을 같다고 하지 않는다** — 이 절의 회귀 위험이 그쪽이다."""
    assert collector.hypothesis_slug("2026-08-21-fix3-censored-p50-is-printed-as-a-floor") != (
        collector.hypothesis_slug("2026-08-21-fix3-read-timeout-lever-fills-the-chain")
    )


def test_the_2026_08_24_thirteen_are_recognised_as_renames(collector):
    """오늘 실측 7건을 고정 입력으로 쓴다 — 전부 개명 후보로 잡혀야 한다."""
    renamed = collector.rename_candidates(_REPORT_IDS, _YAML_IDS)
    assert set(renamed) == set(_REPORT_IDS)
    assert renamed["2026-08-21-fix1-monthly-legs-are-reserved-first"] == [
        "2026-08-21-fix5-monthly-legs-are-reserved-first"
    ]


def test_a_genuinely_absent_id_is_not_rescued_by_the_slug(collector):
    """**대가 실측.** 진짜 부재를 조용히 통과시키면 이 fix는 경보를 끈 것이 된다."""
    assert collector.rename_candidates(["2026-08-18-p5"], _YAML_IDS) == {}


def test_an_id_inside_the_discard_block_is_found_with_its_line(collector, tmp_path):
    """「미등재」로 올리기 **전에** 폐기 목록을 본다 — 적신호는 사람이 읽기 전에 눈에 띈다."""
    target = tmp_path / "docs" / "dev_memory"
    target.mkdir(parents=True)
    (target / "NEXT_TODO.md").write_text(
        "## 폐기·종결된 안건\n\n"
        "- [x] **`2026-08-18-p5` — 2026-08-19 폐기.** 진단이 뒤집혔다\n",
        encoding="utf-8", newline=chr(10),
    )
    hits = collector.discarded_hypothesis_ids(
        tmp_path, ["2026-08-18-p5", "2026-08-21-fix9-warmup-end-is-logged"]
    )
    assert set(hits) == {"2026-08-18-p5"} and hits["2026-08-18-p5"][0] == 3


def test_an_id_outside_the_discard_block_is_not_swallowed(collector, tmp_path):
    """폐기 블록 **밖**의 언급은 폐기가 아니다 — 낱말 겹침으로 맞추지 않는 것과 같은 이유다."""
    target = tmp_path / "docs" / "dev_memory"
    target.mkdir(parents=True)
    (target / "NEXT_TODO.md").write_text(
        "## 오늘 실린 것\n\n- [x] `2026-08-18-p5` 구현 완료\n",
        encoding="utf-8", newline=chr(10),
    )
    assert collector.discarded_hypothesis_ids(tmp_path, ["2026-08-18-p5"]) == {}


def test_an_unreadable_next_todo_is_none_not_an_empty_dict(collector, tmp_path):
    """규약 C — 「대조하지 못했다」와 「걸린 것이 없다」는 조치가 다르다."""
    assert collector.discarded_hypothesis_ids(tmp_path, ["x"]) is None
