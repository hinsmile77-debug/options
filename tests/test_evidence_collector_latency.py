"""증거 수집기 §5-1 — **절벽이 언제 시작됐는가** (2026-08-19 Fix#7).

## 이 테스트에 정답이 있다

08-19 보고서 §4 Fix#7이 검증 방법을 이렇게 적었다: *"08-14 로그로 돌려 `13:51`(경고 최초)·
`14:06` 1.01(위험 최초)이 자동으로 찍히는가. **그 두 숫자는 사람이 이미 손으로 구해 놨으므로
정답이 있다.**"* 픽스처는 그날 로그의 실제 다섯 줄이다.

## 왜 시간대 표로는 부족한가

08-19 13시 행은 「창 최대 1.01 ⛔」 한 줄인데 실제로는 `13:01` 0.84 → `13:06` **1.01** →
`13:11` 0.69로 6분 만에 오르내렸다. 그 시계열을 14:30 회차가 손으로 다시 파싱해야 했다.
**최대는 「얼마나 나빴나」이고 최초는 「언제부터 나빴나」다.**
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
TIMEOUT = 4.0  # 그날 `inquire-price`에 실제로 걸려 있던 read timeout


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_fix7", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scan(collector):
    s = collector.LoopScan()
    for line in collector.iter_day_lines(
        FIXTURE_DIR, date(2026, 8, 14), stem="latency_windows_20260814.log"
    ):
        s.feed(line)
    return s


def test_every_window_keeps_its_own_timestamp(collector, scan):
    """종전에는 `(건수, p50)`만 남겨 **창 시각이 시간대 안에서 사라졌다.**"""
    windows = scan.window_latency_p50()

    assert [at[:5] for at, _n, _p in windows] == ["13:46", "13:51", "13:56", "14:06", "14:11"]
    assert [p for _at, _n, p in windows] == [2.98, 3.53, 3.21, 4.03, 4.06]


def test_the_first_warning_window_is_the_one_the_human_found_by_hand(collector, scan):
    """08-14 경고선 최초 돌파 = `13:51`. 그 앞 창(13:46, 0.75)은 임계 밑이다."""
    at, n, p50, ratio = scan.first_latency_breach(TIMEOUT, collector.P50_TIMEOUT_WARN_RATIO)

    assert at[:5] == "13:51"
    assert (n, p50) == (60, 3.53)
    assert ratio == 0.88


def test_the_first_danger_window_is_1_01_at_14_06(collector, scan):
    """**14:00에 전멸이 시작됐고 14:06이 위험선 최초 돌파다.** 그 사이가 그 하루의 전부였다."""
    at, n, p50, ratio = scan.first_latency_breach(TIMEOUT, 1.0)

    assert at[:5] == "14:06"
    assert (n, p50, ratio) == (53, 4.03, 1.01)


def test_first_not_max_is_what_this_answers(collector, scan):
    """최대는 `14:11`(4.06)인데 최초는 `14:06`이다 — 두 질문이 다르다."""
    windows = scan.window_latency_p50()
    worst = max(windows, key=lambda w: w[2])
    first = scan.first_latency_breach(TIMEOUT, 1.0)

    assert worst[0][:5] == "14:11"
    assert first[0][:5] == "14:06"


def test_a_day_that_never_breaches_reports_none_not_a_zero(collector, scan):
    """돌파가 없는 날은 **없음**이다. 0으로 접으면 「00:00에 돌파」로 읽힌다."""
    assert scan.first_latency_breach(TIMEOUT, 5.0) is None


def test_the_tsv_carries_every_window_because_the_table_only_shows_breaches(collector, scan, tmp_path):
    """표는 돌파 창만 인쇄한다 — 98창을 md에 실으면 그 표가 §5-1을 통째로 밀어낸다."""
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT
    )

    assert out is not None and out.name == "2026-08-14_지연창.tsv"
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(chr(9)) == ["창시각", "호출수", "p50초", "p50/timeout"]
    assert len(lines) == 6, "머리 1줄 + 창 5줄"
    assert lines[4].split(chr(9))[0][:5] == "14:06"
    assert lines[4].split(chr(9))[3] == "1.01"


def test_no_windows_means_no_file_rather_than_an_empty_one(collector, tmp_path):
    """빈 파일은 「쟀는데 0창이었다」로 읽힌다 — 그날 파서가 눈먼 것과 구별되지 않는다."""
    assert collector.write_latency_windows(tmp_path, date(2026, 8, 14), [], TIMEOUT) is None
