"""증거 수집기 §6 — 크래시 판정은 **표식**을 본다 (2026-08-23 / 08-21 §1-3 · §4 Fix#6).

## 이틀 연속 여덟 번 헛것을 가리켰다

종전 판정은 `observation_loop_crash.log`의 **mtime이 오늘인가** 하나였다. 그런데 08-19부터
`start_mahdi_premarket.bat`이 **기동할 때마다** 이 파일에 표식 한 줄을 append한다 —
즉 **정상 기동만으로 mtime이 오늘이 된다.** 08-20·08-21 두 날 네 회차씩 여덟 번 그 적신호가
떴고 여덟 번 다 크래시는 0건이었다.

`collect_evidence`의 mtime 오탐과 지표 §11-1-1의 「사유 없이 죽었다」 오탐은 **같은 병**이다:
판정이 표식을 안 보고 숫자 하나만 본다.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"

_MARKER = "[2026-08-21 12:19:07.78] ===== 관측 루프 기동 ====="
_EARLIER_MARKER = "[2026-08-21  7:30:38.33] ===== 관측 루프 기동 ====="
_YESTERDAY_MARKER = "[2026-08-20  7:30:24.50] ===== 관측 루프 기동 ====="
_TRACEBACK = [
    "^CTraceback (most recent call last):",
    '  File "C:\\mahdi\\main.py", line 1146, in run_observation_loop',
    "    quote = rest_client.get_quote(futures_symbol)",
    "httpx.HTTPStatusError: Server error '500 Internal Server Error'",
]


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_fix6", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_clean_restart_is_no_longer_reported_as_a_crash(collector):
    """**08-21의 오탐 그 자체.** 그날 파일 끝은 12:19:07 표식이고 그 뒤에 아무것도 없다."""
    lines = [_YESTERDAY_MARKER, *_TRACEBACK, _EARLIER_MARKER, _MARKER]
    result = collector.crash_since_last_start(lines, date(2026, 8, 21))
    assert result is not None
    assert result["at"] == "12:19:07"
    assert result["count"] == 0 and result["traceback"] == []


def test_a_traceback_after_the_last_start_is_still_caught(collector):
    """**경보를 끈 것이 아니라 고친 것**이어야 한다 — 진짜 크래시는 여전히 떠야 한다."""
    result = collector.crash_since_last_start([_MARKER, *_TRACEBACK], date(2026, 8, 21))
    assert result["count"] == 1
    assert "httpx.HTTPStatusError" in result["traceback"][-1]


def test_only_the_last_start_of_the_day_counts(collector):
    """08-21은 기동이 둘이었다(07:30 · 12:19). **12:19 이전의 죽음은 이미 지나간 일이다.**"""
    lines = [_EARLIER_MARKER, *_TRACEBACK, _MARKER]
    result = collector.crash_since_last_start(lines, date(2026, 8, 21))
    assert result["at"] == "12:19:07" and result["count"] == 0


def test_a_past_day_does_not_swallow_the_days_that_follow_it(collector):
    """과거 날짜를 조회할 때 **다음 표식에서 끊는다** — 안 끊으면 그날 크래시가 부풀려진다.

    도입 직후 08-20을 조회해 실제로 이 형태를 확인했다(08-21의 본문이 딸려 들어왔다).
    """
    lines = [_YESTERDAY_MARKER, _EARLIER_MARKER, *_TRACEBACK]
    result = collector.crash_since_last_start(lines, date(2026, 8, 20))
    assert result["at"] == "07:30:24" and result["count"] == 0


def test_no_marker_for_that_day_falls_back_instead_of_lying(collector):
    """표식이 없으면 **None** — 호출측이 옛 방식(mtime)으로 물러서고 그 사실을 인쇄한다.

    「크래시가 없었다」와 「셀 수 없었다」는 다르다(규약 C). 여기서 0을 내면 후자가 전자로 둔갑한다.
    """
    assert collector.crash_since_last_start(_TRACEBACK, date(2026, 8, 21)) is None
    assert collector.crash_since_last_start([], date(2026, 8, 21)) is None


def test_the_marker_regex_matches_what_the_batch_file_actually_writes(collector):
    """**계약 테스트.** 문구가 갈리면 이 파서는 조용히 눈이 먼다 — 08-04에 그렇게 362건을 잃었다.

    `%time%`은 한 자리 시각에 앞 공백이 붙고(` 7:30:00.78`) 소수점 이하가 따라온다.
    """
    batch = (PROJECT_ROOT / "scripts" / "start_mahdi_premarket.bat").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "===== 관측 루프 기동 =====" in batch, "bat이 남기는 표식 문구가 바뀌었다"
    for stamp in ("[2026-08-21  7:30:38.33]", "[2026-08-21 12:19:07.78]"):
        assert collector._CRASH_START_MARKER_RE.match(f"{stamp} ===== 관측 루프 기동 ===== ")
