"""증거 수집기 §8-3 — **아직 안 정한 갈림길을 눈앞에 둔다** (2026-08-24 고도화#4).

08-24 장중 두 회차가 「사고 싶다 336번 중 실행까지 간 것은 23번」을 **신규 P1**으로 올렸다.
그 답은 `NEXT_TODO.md`의 「⚠ 남은 결정 하나 — 사람이 골라야 한다」에 (a)/(b)/(c) 체크박스와
함께 **엿새째 열려 있었다.**

§8-2(폐기 목록)의 **거울상**이다 — 08-19는 닫힌 것을 되살렸고 08-24는 열린 것을 새것으로
착각했다. 수집기는 전자만 막고 있었다.

이 파일이 지키는 것은 셋이다.
1. 열린 갈림길을 **선택지와 함께** 뽑는다(파일:행).
2. **닫힌 결정을 담지 않는다** — 소음이 되면 진짜 갈림길이 그 안에 묻힌다.
3. 못 읽으면 **「0건」이 아니라 「못 읽었다」**다(규약 C, §8-2와 같은 자리).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_pending", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "docs" / "dev_memory"
    target.mkdir(parents=True)
    (target / "NEXT_TODO.md").write_text(text, encoding="utf-8", newline=chr(10))
    return tmp_path


_REAL_0818 = """## [MW0601] 2026-08-18 — 첫 실가동 판정 완료

### ⚠ 남은 결정 하나 — 사람이 골라야 한다 (하루치로 정하지 말 것)

`small_strangle_buy`가 요구하는 |델타| 0.20~0.30이 **구독 창(ATM±N) 밖**이라 216건이 실패했다.

- [ ] (a) 구독 창을 넓힌다 — REST 예산이 이미 빡빡하다. **비용이 실재한다.**
- [ ] (b) 그 전략의 밴드를 관측 가능 범위로 본다.
- [ ] (c) 현재 데이터로 계측 불가임을 기록하고 둔다.

### 이번에 실린 것

- [ ] 이건 결정이 아니라 할 일이다
"""


def test_the_open_decision_is_pulled_with_its_choices(collector, tmp_path):
    """**이 고도화의 전부.** 08-18부터 열려 있던 그 블록이 선택지 셋과 함께 나온다."""
    [(line_no, title, choices)] = collector.pending_decisions(_repo(tmp_path, _REAL_0818))
    assert "골라야" in title and line_no == 3
    assert [c for _n, c in choices][0].startswith("(a) 구독 창을 넓힌다")
    assert len(choices) == 3


def test_a_todo_list_is_not_a_decision(collector, tmp_path):
    """선택지가 하나면 갈림길이 아니라 할 일이다 — 그것은 §8-1이 이미 보여 준다."""
    text = _REAL_0818.replace("- [ ] (b) 그 전략의 밴드를 관측 가능 범위로 본다.\n", "").replace(
        "- [ ] (c) 현재 데이터로 계측 불가임을 기록하고 둔다.\n", ""
    )
    assert collector.pending_decisions(_repo(tmp_path, text)) == []


def test_a_decision_already_made_is_not_listed(collector, tmp_path):
    """`### Fix#6 — EGW00201 1건: 고치지 않는다(결정)` — 실측으로 걸러낸 오탐이다.

    제목에 `결정`이 있다는 것만으로 담으면 **이미 정한 것**이 매일 실린다.
    """
    text = """## 옛 절

### Fix#6 — EGW00201 1건: **고치지 않는다**(결정)

- [ ] 08-07 15:02:35 USDCNH 단발이고, 그 1건이 백오프를 1.00 → 1.50배로 올렸다.
- [ ] 폴러 위상을 다시 만지는 것은 07-08에 203분을 잃은 그 구조다.
"""
    assert collector.pending_decisions(_repo(tmp_path, text)) == []


def test_sub_items_under_a_finished_box_do_not_revive_the_section(collector, tmp_path):
    """07-31 절이 그 형태였다 — 최상위는 전부 `- [x]`이고 하위 확인 항목만 미체크다."""
    text = """## 2026-07-31 사용자 결정 필요

- [x] **(2026-08-01 결정) Slack 알림 — 보류 유지.**
  - [ ] **다음 거래일 검증**: 지표가 실제로 나오는지
  - [ ] ZN 이중실패 알림의 벽시계 의미
"""
    assert collector.pending_decisions(_repo(tmp_path, text)) == []


def test_a_discard_heading_is_not_an_open_decision(collector, tmp_path):
    """폐기 선언은 §8-2가 인쇄한다 — 두 절이 같은 항목을 반대 뜻으로 실으면 안 된다."""
    text = """## 절

### ⚠ 이 결정을 폐기한다 — 골라야 할 것이 없어졌다

- [ ] (a) 하나
- [ ] (b) 둘
"""
    assert collector.pending_decisions(_repo(tmp_path, text)) == []


def test_an_unreadable_file_is_none_not_an_empty_list(collector, tmp_path):
    """규약 C — 여기서 조용히 비면 이 절은 **매일 통과**한다(§8-2와 같은 함정)."""
    assert collector.pending_decisions(tmp_path) is None


def test_the_repository_file_still_yields_the_delta_band_decision(collector):
    """**리포지터리 실물**로 고정한다 — 이 목록이 비면 08-24 사고가 그대로 재현된다."""
    found = collector.pending_decisions(PROJECT_ROOT)
    assert found is not None
    assert any("골라야" in title for _n, title, _c in found)
