"""기동 표식은 콘솔이 남긴 `^C`를 벗고 읽는다 (2026-09-21 / 09-21 §1-1 · 제4부 P1-1).

## 08-23이 고친 자리에서 09-21에 다시 걸렸다 — 이번엔 두 글자 때문에

08-23이 크래시 판정을 mtime에서 **표식**으로 옮겼다. 그런데 09-21에 그 표식을 **못 찾았다.**
문구가 바뀐 것도 옛 로그도 아니었다 — 줄 앞의 리터럴 `^C`(0x5e 0x43) 두 글자다:

    127: ^C[2026-09-21  7:30:46.71] ===== 관측 루프 기동 =====
    126:   [2026-09-20 10:43:21.38] ===== 관측 루프 기동 =====   ← 108~126행은 전부 정상

bat 창이 Ctrl+C로 끊긴 흔적이 다음 append 앞에 남은 것이다. **두 파서가 똑같이 못 맞췄다** —
`collect_evidence.py::_CRASH_START_MARKER_RE` 와 `mahdi/ops/crash_metrics.py::_START_MARKER_RE`
가 둘 다 `r"^\\["` 로 시작한다. 그래서 이 파일은 두 파서를 **나란히** 검사한다.

두 파일 모두 **트레이스백 줄에는 이미** `.lstrip("^C")` 를 쓰고 있었다
(*"08-19 로그의 세 트레이스백이 전부 `^C`로 시작한다"*). 표식 줄에만 안 썼다 — 그 비대칭이
이 사고의 전부다.

## 왜 오탐 하나가 P1인가

정상 기동한 날에 *"이 줄이 있는 동안 아래 판정은 믿을 수 없다"* 가 뜬다. 그 문구는 **진짜
크래시가 난 날과 글자 하나 다르지 않다.** 오탐이 반복되면 진짜를 지나친다 — 08-20·08-21에
여덟 번 반복된 뒤 08-23이 고쳤던 것이 바로 그 형태다.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from mahdi.ops import crash_metrics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"
CRASH_LOG = PROJECT_ROOT / "logs" / "observation_loop_crash.log"

# 09-21 실측 127행을 그대로 옮긴 것이다(추정이 아니다).
_CARET_MARKER = "^C[2026-09-21  7:30:46.71] ===== 관측 루프 기동 ====="
_PLAIN_MARKER = "[2026-09-20 10:43:21.38] ===== 관측 루프 기동 ====="
_TRACEBACK = [
    "^CTraceback (most recent call last):",
    '  File "C:\\mahdi\\main.py", line 1146, in run_observation_loop',
    "psycopg.OperationalError: connection failed",
]


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_caret_c", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- 주장: 표식을 찾는다

def test_the_collector_finds_a_marker_that_the_console_prefixed_with_caret_c(collector):
    """**09-21 그 자체.** 고치기 전 이 입력은 `None`(= 옛 방식으로 물러서라)을 냈다."""
    segment = collector.crash_since_last_start([_PLAIN_MARKER, _CARET_MARKER], date(2026, 9, 21))
    assert segment is not None, "`^C` 접두 때문에 표식을 못 찾으면 mtime 오탐이 다시 난다"
    assert segment["at"] == "07:30:46"
    assert segment["count"] == 0 and segment["traceback"] == []


def test_crash_metrics_finds_the_same_marker(collector):
    """**같은 버그가 두 곳에 있었다.** 수집기만 고치면 사이드카는 계속 틀린 값을 낸다.

    09-21 실측이 그것을 말해 준다 — `starts` 09-15~09-18 **1·1·1·1 → 09-21 0**,
    `marker_present` **True → False**. 오늘 하루만 뒤집혔다.
    """
    result = crash_metrics.parse([_PLAIN_MARKER, _CARET_MARKER], date(2026, 9, 21))
    assert result["starts"] == 1
    assert result["marker_present"] is True
    assert result["crashes"] == 0


def test_repeated_caret_c_is_stripped_too(collector):
    """실측 57행이 `^C^C^C^C^CTraceback`이다 — 하나만 벗기면 그 형태에서 또 깨진다."""
    line = "^C^C^C[2026-09-21  7:30:46.71] ===== 관측 루프 기동 ====="
    assert collector.crash_since_last_start([line], date(2026, 9, 21))["at"] == "07:30:46"
    assert crash_metrics.parse([line], date(2026, 9, 21))["starts"] == 1


# ------------------------------------------- 대가: 판정 무변경 · 본문을 잘못 먹지 않는다

def test_a_real_traceback_after_a_caret_c_marker_is_still_caught(collector):
    """**경보를 끈 것이 아니라 고친 것**이어야 한다 — 진짜 크래시는 여전히 떠야 한다."""
    segment = collector.crash_since_last_start([_CARET_MARKER, *_TRACEBACK], date(2026, 9, 21))
    assert segment["count"] == 1
    assert "psycopg.OperationalError" in segment["traceback"][-1]

    result = crash_metrics.parse([_CARET_MARKER, *_TRACEBACK], date(2026, 9, 21))
    assert result["starts"] == 1 and result["crashes"] == 1
    assert result["events"][0]["cause"] == "psycopg.OperationalError"


def test_body_lines_that_merely_start_with_caret_or_c_are_not_mistaken_for_markers(collector):
    """벗긴 뒤에도 `[YYYY-MM-DD`가 와야 한다 — 그래서 본문을 표식으로 잘못 먹지 않는다.

    이것이 이 fix의 **대가 축**이다. 넓게 벗기면 구간 경계가 엉뚱한 데서 갈리고,
    그러면 `unattributed`(날짜 모르는 트레이스백 수)가 흔들린다.
    """
    body = [
        "CancelledError: task was cancelled",
        "^ 여기서 났다",
        "Connection closed by peer",
    ]
    segment = collector.crash_since_last_start([_CARET_MARKER, *body], date(2026, 9, 21))
    assert segment["at"] == "07:30:46", "본문 줄이 표식으로 오인되면 구간이 잘못 갈린다"
    assert len(segment["traceback"]) == 3, "본문 세 줄이 전부 이 구간에 남아야 한다"


def test_plain_markers_are_read_exactly_as_before(collector):
    """**판정 무변경.** `^C`가 없는 날의 판정은 한 글자도 안 바뀌어야 한다."""
    lines = ["[2026-09-18  7:30:50.54] ===== 관측 루프 기동 =====", *_TRACEBACK]
    segment = collector.crash_since_last_start(lines, date(2026, 9, 18))
    assert segment["at"] == "07:30:50" and segment["count"] == 1
    assert crash_metrics.parse(lines, date(2026, 9, 18))["starts"] == 1


# ------------------------------------------------- 실물 로그로 확인한다 (있을 때만)

@pytest.mark.skipif(not CRASH_LOG.exists(), reason="운영 PC의 크래시 로그가 없는 환경")
def test_the_real_log_parses_without_the_fallback_warning(collector):
    """**실물 재생.** 예측치(`2026-09-21-p1-1-...`)가 건 주장을 그대로 건다.

    ⚠ 이 로그는 운영 PC에만 있고 계속 자란다 — 그래서 값을 못 박지 않고 **성질만** 본다:
    표식이 있는 날은 `None`이 아니고, 날짜를 모르는 트레이스백 수가 **날마다 같다**
    (구간 경계가 표식으로만 갈린다는 뜻이고, 그것이 이 fix의 대가 축이다).
    """
    lines = CRASH_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    marked_days = sorted(
        {
            m.group(1)
            for m in (collector._CRASH_START_MARKER_RE.match(ln.lstrip("^C")) for ln in lines)
            if m
        }
    )
    assert marked_days, "표식이 하나도 없다 — bat 문구가 바뀌었는지 먼저 볼 것"

    unattributed = {
        crash_metrics.parse(lines, date.fromisoformat(d))["unattributed"] for d in marked_days
    }
    assert len(unattributed) == 1, f"날짜마다 다르면 구간 경계가 흔들린 것이다: {unattributed}"

    for day in marked_days[-5:]:
        parsed = date.fromisoformat(day)
        assert collector.crash_since_last_start(lines, parsed) is not None, (
            f"{day}에 표식이 있는데 수집기가 못 찾았다 — 옛 방식(mtime)으로 물러선다"
        )
        result = crash_metrics.parse(lines, parsed)
        assert result["starts"] >= 1 and result["marker_present"] is True
