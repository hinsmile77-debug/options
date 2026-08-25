"""증거 수집기 §5-1 — **p95가 장중 회차에 닿는다** (2026-08-25 P1-1·P1-2·P2-1).

## 이 파일이 지키는 것

08-25에 12:30·14:30 두 회차가 「p95(느린 쪽 5%)를 아무도 안 본다」를 신규 P1으로 올렸는데,
`daily_ops_report`는 그것을 전부 인쇄하고 있었다 — 그 파일이 **15:46에 생길 뿐이다.**
`LATENCY_ITEM_RE`는 p50/p95/p99/max 네 값을 다 잡아 놓고 p50만 쓰고 있었다.

1. p95가 p50과 **같은 줄에서 함께** 보관된다 — 새로 재는 것이 아니라 안 쓰던 값이다.
2. 「이틀 연속」 판정은 지표 사이드카의 `p95_by_hour`와 **같은 식**(호출 수 가중 평균)이다 —
   축이 갈리면 장중 판정과 장후 판정이 어긋난다.
3. 「판정 못 함」과 「겹침 없음」은 다른 값이다(규약 C).
4. (P1-2) 하루 검열 건수는 p50 상태와 무관하게 세어져 있다.
5. (P2-1) §5-1-1 「회복실패」의 0은 `0`으로 찍힌다 — `—`는 계측 부재에만 쓴다.

가설: `2026-08-25-fix-p95-reaches-the-intraday-round` — 이 파일이 그 항목의 계약 테스트다.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_p95", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scan(collector):
    """08-14 실로그 픽스처 — p95 값이 이미 그 줄 안에 있었다(4.01/4.05/4.05/4.06/4.09)."""
    s = collector.LoopScan()
    for line in collector.iter_day_lines(
        FIXTURE_DIR, date(2026, 8, 14), stem="latency_windows_20260814.log"
    ):
        s.feed(line)
    return s


def test_p95_is_kept_alongside_p50_window_by_window(collector, scan):
    """같은 줄의 네 값 중 p50만 쓰던 것이 결함이었다 — p95가 창 단위로 남는다."""
    p95_windows = [(at[:5], n, p) for hour in sorted(scan.latency_p95)
                   for at, n, p in scan.latency_p95[hour]]
    assert p95_windows == [
        ("13:46", 61, 4.01), ("13:51", 60, 4.05), ("13:56", 58, 4.05),
        ("14:06", 53, 4.06), ("14:11", 49, 4.09),
    ]


def test_hourly_p95_max_answers_none_for_an_unmeasured_hour(collector, scan):
    """「p95 없음(None)」과 「p95=0」은 다른 사실이다(규약 C) — 없는 시간대는 None이다."""
    assert scan.hourly_latency_p95_max(13) == 4.05
    assert scan.hourly_latency_p95_max(14) == 4.09
    assert scan.hourly_latency_p95_max(10) is None


def test_the_weighted_fold_matches_the_sidecar_formula(collector, scan):
    """이틀 연속 판정 축 = 사이드카 `p95_by_hour`(호출 수 가중 평균)와 같은 식이어야 한다.

    13시 inquire-price: (61x4.01 + 60x4.05 + 58x4.05) / 179 = 4.036. 최대(4.05)도 단순
    평균(4.037)도 아니다 — 축이 갈리면 장중과 장후가 다른 답을 내고 사람은 어느 쪽을 믿을지 모른다.
    """
    grid = scan.hourly_p95_weighted()
    assert grid["inquire-price"][13] == pytest.approx(4.036, abs=0.001)
    assert grid["inquire-price"][14] == pytest.approx(4.074, abs=0.001)
    # 이틀 연속 판정은 체인 엔드포인트만의 축이 아니다 — 08-25 성립 6구간에 inquire-balance가
    # 둘 있었다. 같은 줄의 다른 엔드포인트도 접혀 있어야 한다.
    assert grid["overseas-inquire-price"][13] == pytest.approx(4.03, abs=0.001)


def test_breaches_share_the_daily_report_threshold(collector, scan):
    """임계 복제본이 원천(`log_metrics.REST_LATENCY_P95_WARN_SECONDS`)과 갈라지면 안 된다."""
    from mahdi.ops import log_metrics

    assert collector.P95_WARN_THRESHOLD_SECONDS == log_metrics.REST_LATENCY_P95_WARN_SECONDS

    breaches = collector.p95_breaches(scan.hourly_p95_weighted())
    assert [(ep, hh) for ep, hh, _v in breaches] == [
        ("inquire-price", 13), ("inquire-price", 14),
        ("overseas-inquire-price", 13), ("overseas-inquire-price", 14),
    ]
    assert collector.p95_breaches(scan.hourly_p95_weighted(), threshold=5.0) == []


def test_a_missing_previous_sidecar_is_not_the_same_as_no_overlap(collector, scan):
    """규약 C — 「판정 못 함(None)」과 「겹침 없음([])」이 같은 값이면 월요일마다 거짓 안심이 뜬다."""
    today = collector.p95_breaches(scan.hourly_p95_weighted())
    assert collector.two_day_p95_overlap(today, None) is None
    assert collector.two_day_p95_overlap(today, {"p95_by_hour": {}}) == []


def test_the_overlap_uses_the_previous_days_own_threshold(collector, scan):
    """직전 날의 초과 여부는 **그 사이드카에 적힌 임계**로 판정한다 — 그날 실제로 걸려 있던 값이다."""
    today = collector.p95_breaches(scan.hourly_p95_weighted())
    prev = {
        "p95_warn_threshold": 2.5,
        "p95_by_hour": {
            "13": {"inquire-price": 3.0, "overseas-inquire-price": 1.2},
            "14": {"inquire-price": 2.4},
        },
    }
    overlap = collector.two_day_p95_overlap(today, prev)
    # 13시 inquire-price만 이틀 연속이다 — 13시 overseas(1.2)와 14시(2.4)는 어제 임계 밑이었다.
    assert [(ep, hh) for ep, hh, _v in overlap] == [("inquire-price", 13)]

    # 어제 임계가 3.5였다면(사이드카가 그렇게 말하면) 3.0도 초과가 아니다.
    prev_high = dict(prev, p95_warn_threshold=3.5)
    assert collector.two_day_p95_overlap(today, prev_high) == []


def test_daily_censoring_is_counted_even_when_p50_is_healthy(collector):
    """P1-2 — 08-25의 형태: p50은 종일 안전선 아래(0.52배)였는데 꼬리 186건이 잘려 있었다.

    `window_censored_counts`는 p50 상태와 무관하게 세어야 하고, 본문 인쇄는 `floored`가
    비어도 이 값을 내야 한다(그 인쇄 분기가 이 계산을 쓴다).
    """
    s = collector.LoopScan()
    lines = [
        # p50 0.52초(안전) — 그런데 같은 창에서 타임아웃 검열이 있었다.
        "2026-08-25 14:46:13,101 INFO:mahdi.main:REST 응답시간(300초 창): "
        "inquire-price=60건 0.52/3.50/4.00/4.00초",
        "2026-08-25 14:43:10,000 INFO:mahdi.broker.rest_client:"
        "느린 REST 호출 5.01초 = 페이서대기 1.00초 + HTTP 4.01초 (배율 1.00배, GET inquire-price)",
        "2026-08-25 14:44:10,000 INFO:mahdi.broker.rest_client:"
        "느린 REST 호출 4.30초 = 페이서대기 1.00초 + HTTP 3.30초 (배율 1.00배, GET inquire-price)",
    ]
    for line in lines:
        s.feed(line)

    timeout = 4.0
    # p50은 검열 문턱(0.98배) 근처에도 안 갔다 — floored는 빈다.
    assert all(p50 < timeout * collector.P50_CENSORED_FLOOR_RATIO
               for _at, _n, p50 in s.window_latency_p50())
    # 그런데 검열은 세어져 있다: HTTP 4.01초 1건이 잘렸고, 3.30초는 타임아웃 밑이라 아니다.
    assert s.window_censored_counts(timeout) == {"14:46:13": 1}
    assert sum(1 for _at, http in s.censored_seconds if http >= timeout) == 1


def test_the_revival_failure_cell_prints_zero_as_zero(collector):
    """P2-1 — 「한 번도 실패 안 했다」와 「실패를 세는 눈이 없다」는 같은 글자면 안 된다(규약 C)."""
    assert collector.revival_failure_cell(0, retry_axis_measured=True) == "0"
    assert collector.revival_failure_cell(2, retry_axis_measured=True) == "2"
    assert collector.revival_failure_cell(0, retry_axis_measured=False) == "—(계측없음)"
