"""§12 적신호가 `p95/timeout >= 1.0` 창의 **최장 연속**도 센다 (2026-09-02 제4부 P1-4).

09-02 §1-9 — `p95/timeout`이 13:30부터 사실상 상시 위험선 위였는데(하루 14/98창) 적신호는
「14개」와 「최악 창」만 적었다. **흩어진 14창과 내리 이어진 14창은 다른 사건인데 같은 글자로
보였다** — 「13:30 이후로 내리 이어졌다」는 사실은 사람이 `_지연창.tsv`를 손으로 훑어야 나왔다.

⛔ **임계는 손대지 않는다.** 위험선 1.0배는 08-27 P2-E가 정한 그대로다. 이 항목이 바꾼 것은
「무엇을 재는가」(개수 → 개수 + 최장 연속)이지 「얼마부터 위험한가」가 아니다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_p14", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consecutive_windows_are_counted_as_one_run(collector):
    """13:30부터 내리 이어진 네 창은 「4개」가 아니라 「최장 연속 4창」이다."""
    windows = {
        "13:20:06": 2.0, "13:25:06": 2.0,
        "13:30:06": 4.0, "13:35:06": 4.1, "13:40:06": 4.0, "13:45:06": 4.2,
    }
    longest, run_from, run_to = collector._longest_over_run(windows, 4.0)
    assert longest == 4
    assert run_from == "13:30:06"
    assert run_to == "13:45:06"


def test_scattered_windows_do_not_form_a_run(collector):
    """같은 4창이라도 흩어져 있으면 최장 연속은 1이다 — 이것이 이 항목의 요점이다."""
    windows = {
        "10:00:06": 4.0, "10:05:06": 1.0,
        "10:10:06": 4.0, "10:15:06": 1.0,
        "10:20:06": 4.0, "10:25:06": 1.0,
        "10:30:06": 4.0,
    }
    longest, _run_from, _run_to = collector._longest_over_run(windows, 4.0)
    assert longest == 1, "흩어진 것을 뭉친 것으로 세면 이 계측이 거짓말을 한다"


def test_longest_run_wins_over_an_earlier_shorter_one(collector):
    windows = {
        "09:00:06": 4.0, "09:05:06": 4.0, "09:10:06": 1.0,
        "14:00:06": 4.0, "14:05:06": 4.0, "14:10:06": 4.0,
    }
    longest, run_from, run_to = collector._longest_over_run(windows, 4.0)
    assert (longest, run_from, run_to) == (3, "14:00:06", "14:10:06")


def test_windows_are_ordered_by_time_not_insertion(collector):
    """사이드카가 창을 순서 없이 넘겨도 연속 판정은 시각 순이어야 한다."""
    windows = {"13:40:06": 4.0, "13:30:06": 4.0, "13:35:06": 4.0}
    longest, run_from, run_to = collector._longest_over_run(windows, 4.0)
    assert (longest, run_from, run_to) == (3, "13:30:06", "13:40:06")


def test_no_breach_yields_an_empty_run(collector):
    """넘은 창이 없으면 (0, "", "") — 호출측은 이 경우 줄 자체를 안 낸다."""
    assert collector._longest_over_run({"10:00:06": 1.0}, 4.0) == (0, "", "")


def test_unmeasured_windows_never_enter_the_run(collector):
    """규약 C — `window_latency_p95()`가 이미 표본 있는 창만 준다. 「안 쟀다」를 「안 넘었다」로
    세지 않는 것이 이 함수가 표본 없는 창을 **아예 안 받는** 이유다."""
    measured_only = {"13:30:06": 4.0, "13:40:06": 4.0}
    longest, run_from, run_to = collector._longest_over_run(measured_only, 4.0)
    # 13:35 창은 p95 표본이 없어 애초에 dict에 없다 — 연속을 끊지도 잇지도 않는다.
    assert (longest, run_from, run_to) == (2, "13:30:06", "13:40:06")
