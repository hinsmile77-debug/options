"""워치독 `no_ingest` 경보의 **회복 이력 참고 문구** (2026-09-03 제4부 P2-3).

절벽 표본이 넷이 됐고 **회복 시각이 관측된 셋(08-26 · 09-01 · 09-03)이 전부 15:25~15:26**,
즉 정규장 마감 +5~6분에 사람 손 없이 풀렸다. 그 사실이 `cliff_episodes.md`와 사람 머릿속에만
있고 경보에는 없었다 — 09-03에 워치독은 DEGRADED를 45회 내면서 한 번도 그 말을 안 했다.

**이 파일이 지키는 것은 둘이다.**
1. 참고 문구가 **억제된다** — 45분 사건에 45줄이 아니라 2줄이다.
2. **판정 무변경** — 문구를 줄 끝에만 붙였으므로 `watchdog_metrics`의 집계가 안 바뀐다.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from mahdi.ops import watchdog_metrics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "watchdog_observation_loop.py"


@pytest.fixture(scope="module")
def loop():
    spec = importlib.util.spec_from_file_location("watchdog_loop_p2_3", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===== 억제 — 45분 사건에 45줄이 되면 억제가 안 듣는 것이다 =====


def test_the_hint_rides_the_first_minute_of_an_episode(loop):
    assert loop._recovery_hint_due({"minutes": 1}) is True


def test_the_hint_is_silent_in_between(loop):
    for minute in (2, 3, 17, 29, 31, 44):
        assert loop._recovery_hint_due({"minutes": minute}) is False, minute


def test_the_hint_returns_every_thirty_minutes(loop):
    """사람이 사건 도중에 합류해도 30분 안에 한 번은 근거를 본다."""
    assert loop._recovery_hint_due({"minutes": 30}) is True
    assert loop._recovery_hint_due({"minutes": 60}) is True


def test_a_forty_five_minute_episode_costs_two_lines(loop):
    """09-03 재현 — 45분 DEGRADED(14:40~15:24)에 참고 문구는 **2줄**이다."""
    lines = sum(1 for m in range(1, 46) if loop._recovery_hint_due({"minutes": m}))

    assert lines == 2, "매 줄에 붙으면 45줄이다 — 그러면 억제가 없는 것과 같다"


def test_an_unreadable_state_says_nothing(loop):
    """상태를 못 읽었으면 **안 붙인다.** 모르는 채 매분 붙이면 억제가 통째로 풀린다."""
    for broken in (None, {}, {"minutes": None}, {"minutes": "3"}, {"minutes": 0}):
        assert loop._recovery_hint_due(broken) is False, broken


# ===== 문구 자체 — 표본 수가 박혀 있어야 낡은 것이 보인다 =====


def test_the_hint_names_how_many_samples_it_stands_on(loop):
    """「대체로」라고 쓰면 다섯 번째 표본이 나와도 이 문구는 영원히 안 늙는다."""
    hint = loop._NO_INGEST_RECOVERY_HINT

    assert "3건" in hint
    assert "15:25~15:26" in hint
    for sample in ("08-26", "09-01", "09-03"):
        assert sample in hint


def test_the_hint_does_not_promise_an_automatic_fix(loop):
    """08-26이 증명한 것은 **자동 재기동을 켜지 않은 것이 옳았다**는 것이다."""
    assert "사람" in loop._NO_INGEST_RECOVERY_HINT


# ===== 판정 무변경 — 파서는 이 문구를 못 본 척해야 한다 =====


def _degraded_line(suffix: str) -> str:
    return (
        "[2026-09-03 14:40:02] DEGRADED — 관측 루프 적재 정지(no_ingest) — "
        "최근 10분 적재 0건" + suffix
    )


def test_the_parser_counts_the_same_with_and_without_the_hint(loop):
    """**규약 — 문구는 줄 끝에만 붙인다.** 앞머리를 건드리면 집계가 눈이 먼다."""
    plain = [_degraded_line("")]
    hinted = [_degraded_line(f" · 연속 1분째(14:40부터) · {loop._NO_INGEST_RECOVERY_HINT}")]

    target = date(2026, 9, 3)
    before = watchdog_metrics.parse(plain, target)
    after = watchdog_metrics.parse(hinted, target)

    assert before["degraded_checks"] == after["degraded_checks"] == 1
    assert before["restarts"] == after["restarts"] == 0
    assert before["recovered_episodes"] == after["recovered_episodes"] == 0


def test_the_recovered_line_is_untouched():
    """종료 줄에는 아무것도 안 붙였다 — 사건 수를 세는 것이 그 줄이다."""
    target = date(2026, 9, 3)
    metrics = watchdog_metrics.parse(
        [
            _degraded_line(""),
            "[2026-09-03 15:25:01] RECOVERED — 적재 정지 45분 지속 후 회복(14:40~15:24)",
        ],
        target,
    )

    assert metrics["recovered_episodes"] == 1
