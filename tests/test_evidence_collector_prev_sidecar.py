"""증거 수집기 §9 — **「전일」은 달력의 어제가 아니다** (2026-08-24 Fix#1).

08-24(월) §9가 `2026-08-23_지표.json`(토요일)을 기대해 「없음 ⚠」을 냈다. 그 파일은 앞으로도
영영 안 생긴다 — 토요일에는 장이 안 선다. **월요일마다 뜨는 헛경보**이고, 그 소음에 진짜
누락이 묻힌다.

이 파일이 지키는 것은 둘이다.

1. **금요일 자료만 있는 월요일** — 직전 거래일을 찾아내고, 몇 일을 건너뛰었는지 함께 인쇄한다.
2. **5일 내내 없는 날** — 조용히 통과하지 않는다. 이 fix는 경보를 끄는 것이 아니라 옮기는
   것이고, 그 경계가 `PREV_SIDECAR_MAX_BACKTRACK_DAYS`다.
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
    spec = importlib.util.spec_from_file_location("collect_evidence_prev_sidecar", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _auto(tmp_path: Path, *days: str) -> Path:
    auto = tmp_path / "auto"
    auto.mkdir(parents=True, exist_ok=True)
    for d in days:
        (auto / f"{d}_지표.json").write_text("{}", encoding="utf-8", newline=chr(10))
    return auto


def test_monday_finds_fridays_sidecar_not_saturdays(collector, tmp_path):
    """08-24(월)의 실제 형태 — 금요일(08-21) 것을 찾고 비거래일 2일을 건너뛴다."""
    auto = _auto(tmp_path, "2026-08-21")
    found, back = collector.previous_metric_sidecar(auto, date(2026, 8, 24))
    assert found == date(2026, 8, 21) and back == 3  # 건너뛴 비거래일 = back - 1 = 2


def test_yesterday_wins_when_it_exists(collector, tmp_path):
    """평일에는 종전과 같은 답이어야 한다 — **가장 가까운 날**이 규칙이다."""
    auto = _auto(tmp_path, "2026-08-20", "2026-08-21")
    assert collector.previous_metric_sidecar(auto, date(2026, 8, 24))[0] == date(2026, 8, 21)


def test_a_five_day_silence_is_a_real_absence_not_a_holiday(collector, tmp_path):
    """**대가 실측.** 무한히 거슬러 오르면 진짜 부재가 조용히 통과한다."""
    auto = _auto(tmp_path, "2026-08-01")
    found, back = collector.previous_metric_sidecar(auto, date(2026, 8, 24))
    assert found is None and back == collector.PREV_SIDECAR_MAX_BACKTRACK_DAYS


def test_the_backtrack_window_covers_the_longest_korean_holiday(collector):
    """추석·설 연휴(4~5일)를 덮으면서 그보다 긴 침묵은 사건으로 남기는 자리가 5다."""
    assert collector.PREV_SIDECAR_MAX_BACKTRACK_DAYS >= 5
