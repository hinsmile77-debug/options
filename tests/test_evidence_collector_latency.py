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
    # 2026-08-23 고도화#1 — 뒤 세 열(검열건수 · 검열% · p50표기)이 추가됐다.
    # 2026-08-26 P2-5 — 그 뒤로 두 열(p95초 · p95/timeout)이 더 붙었다.
    assert lines[0].split(chr(9)) == [
        "창시각", "호출수", "p50초", "p50/timeout", "검열건수", "검열%", "p50표기",
        "p95초", "p95/timeout",
    ]
    assert len(lines) == 6, "머리 1줄 + 창 5줄"
    assert lines[4].split(chr(9))[0][:5] == "14:06"
    assert lines[4].split(chr(9))[3] == "1.01"


def test_a_censored_window_is_written_as_a_floor_not_as_a_median(collector, scan, tmp_path):
    """**08-21에 네 회차가 잘못 읽은 그 값.**

    14:06 창의 p50은 4.05초이고 read timeout은 4.0초다 — 그 값은 중앙값이 아니라 **하한**이다.
    `4.05`로 적으면 「제한시간을 6초로 늘리면 흡수된다」는 추론이 자연스러워 보이는데,
    그 계산의 재료가 애초에 없다는 것이 이 열의 요점이다.
    """
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT, {"14:06:13": 41},
    )
    row = dict(zip(
        out.read_text(encoding="utf-8").splitlines()[0].split(chr(9)),
        out.read_text(encoding="utf-8").splitlines()[4].split(chr(9)),
    ))
    assert row["p50표기"] == ">=4.0"
    assert row["검열건수"] == "41"
    # 검열되지 않은 창은 표기가 **한 글자도 안 바뀐다**(회귀 없음).
    early = dict(zip(
        out.read_text(encoding="utf-8").splitlines()[0].split(chr(9)),
        out.read_text(encoding="utf-8").splitlines()[1].split(chr(9)),
    ))
    assert early["p50표기"] == early["p50초"]


def test_an_unmeasured_censoring_prints_a_dash_not_a_zero(collector, scan, tmp_path):
    """「검열 0건」과 「안 셌다」는 다르다(규약 C) — 후자를 0으로 적으면 깨끗한 날로 읽힌다."""
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT, None,
    )
    row = out.read_text(encoding="utf-8").splitlines()[4].split(chr(9))
    assert row[4] == "-" and row[5] == "-"


def test_no_windows_means_no_file_rather_than_an_empty_one(collector, tmp_path):
    """빈 파일은 「쟀는데 0창이었다」로 읽힌다 — 그날 파서가 눈먼 것과 구별되지 않는다."""
    assert collector.write_latency_windows(tmp_path, date(2026, 8, 14), [], TIMEOUT) is None


# ===== 2026-08-26 (08-26 §1-10 / P2-5) — 지연창 TSV가 p95를 싣는다 =====


def test_the_tsv_carries_p95_next_to_p50(collector, scan, tmp_path):
    """`2026-08-25-fix-p95-reaches-the-intraday-round`의 **②**다(①·③은 `c71574d`로 완료).

    **이 회차가 그 대가를 세 번째로 치렀다** — 08-26에 절벽이 풀린 시각(15:26)을 p95로는
    창 단위로 못 짚었다. p50은 타임아웃 벽에 눌려 회복을 못 보여 준다.
    """
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT,
        None, scan.window_latency_p95(),
    )
    head = out.read_text(encoding="utf-8").splitlines()[0].split(chr(9))
    row = dict(zip(head, out.read_text(encoding="utf-8").splitlines()[4].split(chr(9))))

    assert "p95초" in head and "p95/timeout" in head
    assert row["p95초"] not in ("", "-"), "그 창에는 p95 표본이 있다"


def test_a_window_without_p95_is_a_dash_not_a_zero(collector, scan, tmp_path):
    """⚠ **「p95 없음」과 「p95=0」을 다른 문구로 인쇄한다**(규약 C).

    이 한 칸이 틀리면 이 fix는 헛경보 생성기가 된다 — 원 가설이 같은 주의를 이미 적어 뒀다.
    """
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT, None, {},
    )
    row = dict(zip(
        out.read_text(encoding="utf-8").splitlines()[0].split(chr(9)),
        out.read_text(encoding="utf-8").splitlines()[1].split(chr(9)),
    ))
    assert row["p95초"] == "-"
    assert row["p95/timeout"] == "-"


def test_a_measured_zero_p95_is_printed_as_zero(collector, scan, tmp_path):
    """**쟀는데 0인 창**은 `0.00`이다. 위 테스트와 이 테스트가 함께 규약 C를 지킨다."""
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    first_at = scan.window_latency_p50()[0][0]
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT, None, {first_at: 0.0},
    )
    row = dict(zip(
        out.read_text(encoding="utf-8").splitlines()[0].split(chr(9)),
        out.read_text(encoding="utf-8").splitlines()[1].split(chr(9)),
    ))
    assert row["p95초"] == "0.00"
    assert row["p95/timeout"] == "0.00"


def test_a_censored_p95_is_written_as_a_floor_too(collector, scan, tmp_path):
    """p95도 타임아웃 벽에 눌린다 — p50 열이 쓰는 `>=` 표기 규약을 그대로 따른다."""
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    first_at = scan.window_latency_p50()[0][0]
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT, None, {first_at: 4.05},
    )
    row = dict(zip(
        out.read_text(encoding="utf-8").splitlines()[0].split(chr(9)),
        out.read_text(encoding="utf-8").splitlines()[1].split(chr(9)),
    ))
    assert row["p95초"] == ">=4.0"


def test_omitting_p95_keeps_the_old_columns_byte_identical(collector, scan, tmp_path):
    """**p95를 안 넘긴 호출의 앞 일곱 열은 한 글자도 안 바뀐다** — 회귀 판정선이다."""
    (tmp_path / "docs" / "동작점검" / "auto").mkdir(parents=True)
    out = collector.write_latency_windows(
        tmp_path, date(2026, 8, 14), scan.window_latency_p50(), TIMEOUT, {"14:06:13": 41},
    )
    row = out.read_text(encoding="utf-8").splitlines()[4].split(chr(9))
    assert row[:7] == ["14:06:13", "53", "4.03", "1.01", "41", "77", ">=4.0"]
    assert row[7:] == ["-", "-"]
