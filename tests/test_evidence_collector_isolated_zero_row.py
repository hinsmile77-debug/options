"""증거 수집기 §5-1 — **단발 완전실패**를 연속 절벽과 따로 센다 (2026-09-04 §1-7 / P2-B).

09-04의 ERROR는 세 건이 전부였고 셋 다 「옵션체인 이번 분 전멸」이었다
(14:34:34 · 14:51:32 · 15:06:52). 세 번 다 **다음 분에 회복돼 연속되지 않았고**, 그래서
최장 연속 `rows=0` 구간은 **1분**으로 찍혔다 — 그 축만 보면 09-04는 아무 일 없는 날이다.
09-03은 반대로 53분 한 덩어리라 고립 분이 0개다.

단발 완전실패는 신선도 창(5분)이 덮으므로 그날 판단을 안 끊는다. **그래서 경보가 안 울린
채로 잦아지고**, 09-03의 53분 절벽은 그 끝에 왔다. 이 파일이 지키는 것은 셋이다.

1. 붙어 있는 분과 떨어져 있는 분을 **가른다**(09-04형 3건 vs 09-03형 0건).
2. **0건인 날도 줄이 실린다**(규약 C — 「없었다」와 「안 셌다」는 조치가 다르다).
3. **임계를 만들지 않았다** — 이 줄은 적신호로 올라가지 않는다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_isolated", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cycle(hhmm: str, rows: int) -> str:
    """실제 로그 한 줄의 모양 그대로다 — `분=` 라벨이 적재 분의 정본이다(08-10 사고)."""
    return (
        f"2026-09-04 {hhmm}:20,416 INFO:mahdi.main:옵션체인 사이클 소요 분해: "
        f"REST수집 19.53초 + DB적재 0.11초 + 상태기록 0.03초 + 기타 0.00초 "
        f"(rows={rows}, 밀림=0.0초, 타폴러동시호출추정=0건) 분={hhmm}"
    )


def _scan(collector, minutes_zero, minutes_ok=()):
    scan = collector.LoopScan()
    for hhmm in minutes_ok:
        scan.feed(_cycle(hhmm, 20))
    for hhmm in minutes_zero:
        scan.feed(_cycle(hhmm, 0))
    return scan


def test_counts_the_three_singletons_of_09_04(collector):
    """09-04 실측 — 셋 다 앞뒤가 정상이라 전부 고립이다."""
    scan = _scan(collector, ["14:34", "14:51", "15:06"], ["14:33", "14:35", "14:50", "15:07"])
    count, listed = scan.isolated_zero_row_count()
    assert count == 3
    assert listed == ["14:34", "14:51", "15:06"]


def test_a_single_long_cliff_has_no_singletons(collector):
    """09-03형 — 붙어 있으면 고립이 아니다. 이 자리가 두 날을 가른다."""
    cliff = [f"{(14 * 60 + 31 + i) // 60:02d}:{(14 * 60 + 31 + i) % 60:02d}" for i in range(53)]
    scan = _scan(collector, cliff)
    assert scan.isolated_zero_row_count() == (0, [])
    # 대가 축 — 종전 축은 그대로 53분을 낸다.
    assert scan.longest_zero_row_run()[0] == 53


def test_mixed_day_keeps_only_the_detached_minutes(collector):
    scan = _scan(collector, ["09:30", "10:59", "11:00", "11:01", "13:07"])
    count, listed = scan.isolated_zero_row_count()
    assert (count, listed) == (2, ["09:30", "13:07"])


def test_a_clean_day_counts_zero_not_none(collector):
    """규약 C — 0행 분이 하나도 없는 날은 `(0, [])`다. `None`이 아니다."""
    scan = _scan(collector, [], ["09:30", "09:31"])
    assert scan.isolated_zero_row_count() == (0, [])


def test_the_two_axes_are_complementary(collector):
    """불변식 — 고립 분과 연속 구간은 겹치지 않는다. 09-04는 고립 3 · 최장 1이다."""
    scan = _scan(collector, ["14:34", "14:51", "15:06"])
    assert scan.isolated_zero_row_count()[0] == 3
    assert scan.longest_zero_row_run() == (1, "14:34", "14:34")


def test_longest_run_is_untouched(collector):
    """판정 무변경(B등급) — 종전 축의 값도 임계도 안 건드렸다."""
    assert collector.ZERO_ROW_RUN_ALERT_MINUTES == 20
    scan = _scan(collector, ["10:00", "10:01", "10:02", "13:07"])
    assert scan.longest_zero_row_run() == (3, "10:00", "10:02")
    assert scan.isolated_zero_row_count() == (1, ["13:07"])
