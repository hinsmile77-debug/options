"""`mahdi.ops.report` — 지표 dict → 마크다운 렌더러.

2026-08-01(운영점검보고서 2026-07-31 §5-2). 렌더러는 순수 함수라 dict만으로 전부 검증된다.
"""

from __future__ import annotations

from mahdi.ops import report

_TODAY = {
    "date": "2026-08-03",
    "rest": {
        "total_calls": 13000, "calls_per_second": 0.45, "capacity_pct": 45.0,
        "deficit_threshold_multiplier": 2.22, "span_seconds": 29400,
        "by_group": {"옵션체인": 9000}, "by_status": {"200": 12950, "500": 50},
        "non_200": {"count": 50, "pct": 0.38, "by_group": {"옵션체인": 50}},
    },
    "cycles": {
        "count": 480, "rest_seconds": {"mean": 25.0},
        "missing": {"count": 12, "odd": 11, "even": 1, "list": ["09:03"],
                    "recovered_by_catchup": 10, "unrecovered_count": 2},
        "by_hour": [{"hour": 9, "cycles": 60, "rest_mean": 22.0, "rest_max": 40.0,
                     "over_60s": 0, "slip_max": 0.0, "foreign_sum": 30}],
        "by_mod10": [{"mod10": 2, "cycles": 49, "rest_mean": 30.0, "foreign_mean": 0.5,
                      "foreign_by_group": {"투자자수급": 0.5}, "over_60s": 1}],
    },
    "overrun": {"count": 12, "max_seconds": 30.0},
    "catchups": {"count": 10, "minutes": ["09:03"]},
    "backoff": {"expand": 20, "recover": 80, "max_multiplier": 1.8,
                "mean_multiplier": 1.1, "mean_multiplier_by_hour": {"9": 1.05}},
    "bursts": {"만기유동성": {"burst_count": 49, "calls_per_burst_median": 11,
                              "occupancy_seconds": {"median": 13.0, "max": 20.0},
                              "start_positions_mod10": {"1:15": 49}}},
    "stalls": [],
    "slow_calls": {"count": 0},
    "poller_phase": {"만기유동성": {"mode_second": 15, "minutes_mod10": {1: 16, 3: 16, 5: 17}}},
    "log_volume": {"total_bytes": 3000000, "total_lines": 15000, "httpx_bytes": 2700000,
                   "httpx_pct": 90.0, "human_lines": 2800, "by_level": {"INFO": 14000}},
    "qualitative": {"egw00201": 40},
    "failures": {"옵션 체인 폴링 실패": 30},
}

_YESTERDAY = {**_TODAY, "date": "2026-07-31", "overrun": {"count": 46, "max_seconds": 96.9}}


def test_render_without_previous_omits_delta_column_instead_of_inventing_one():
    out = report.render(_TODAY)
    assert "| 지표 | 오늘 |" in out
    assert "전일 지표 사이드카가 없어 델타를 생략했다" in out
    assert "2026-07-31" not in out


def test_render_with_previous_adds_delta_and_marks_improvement_direction():
    out = report.render(_TODAY, previous=_YESTERDAY)
    assert "전일(2026-07-31)" in out
    # 밀림 46 → 12건은 감소가 개선이므로 ✅가 붙어야 한다.
    overrun_row = next(line for line in out.splitlines() if line.startswith("| 60초 초과(밀림)"))
    assert "▼34" in overrun_row and "✅" in overrun_row


def test_render_marks_regression_when_a_down_metric_goes_up():
    worse = {**_TODAY, "overrun": {"count": 60, "max_seconds": 30.0}}
    out = report.render(worse, previous=_YESTERDAY)
    overrun_row = next(line for line in out.splitlines() if line.startswith("| 60초 초과(밀림)"))
    assert "▲14" in overrun_row and "⚠" in overrun_row


def test_render_survives_a_broken_section_and_still_emits_the_rest():
    # 절 단위 실패 격리 — 부분 결과라도 있는 편이 아무것도 없는 것보다 낫다.
    broken = {**_TODAY, "cycles": {"by_hour": "이건 리스트가 아니다"}}
    out = report.render(broken)
    assert "렌더링 실패" in out
    assert "## 5. REST 수요/응답" in out  # 나머지 절은 그대로 나온다


def test_render_includes_db_and_hypothesis_sections_only_when_given():
    assert "## 12. DB 적재" not in report.render(_TODAY)
    db = {
        "tables": [{"table": "option_analysis_1m", "rows": 9000, "minutes": 480, "note": ""}],
        "book_coverage": [{"series": "regular", "expiry": "2026-08-13", "minutes": 480,
                           "coverage_pct": 97.2}],
        "signal_decisions": [{"decision": "REJECT", "conviction": "STANDARD",
                              "reject_reason": "strategy_palette:wait_only", "count": 400}],
        "risk_gate_distinct": 4,
        "regime": [{"regime": "RANGE_BALANCED", "today": 400, "total": 7800, "days": 20}],
        "feature_store": {"today": 400, "total": 6200, "hmm_threshold": 8000,
                          "hmm_progress_pct": 77.5, "non_neutral_pct": {"rv_ratio": 0.0}},
        "macro": {"vix_front": {"non_null": 98, "distinct": 17}},
        "market_halt": {"updated_at": "15:43:00", "last_message_at": "09:00:05"},
        "remaining_processes": 0,
        "rate_limiter": {"rows": 480, "overrun_rows": 12, "max_multiplier": 1.8},
    }
    out = report.render(_TODAY, db_metrics=db)
    assert "## 12. DB 적재" in out
    assert "| regular | 2026-08-13 |" in out
    # 절대 커버리지 줄은 monthly_coverage가 있을 때만 나온다 — 없으면 지어내지 않는다.
    assert "먼슬리 절대 커버리지" not in out

    out = report.render(
        _TODAY,
        db_metrics={**db, "monthly_coverage": {"expiry": "2026-08-13", "minutes": 470,
                                               "elapsed_minutes": 480, "coverage_pct": 97.9}},
    )
    assert "먼슬리 절대 커버리지: 97.9%" in out

    out = report.render(_TODAY, hypotheses=[{
        "id": "h1", "가설": "버스트 분할", "metric": "overrun.count",
        "actual": 12, "expect": "<= 20", "verdict": "확인",
    }])
    assert "## 0. 가설 검정" in out
    assert "자동으로 바뀌지 않는다" in out  # 사람이 확정한다는 규약이 표에 붙어야 한다


def test_dig_returns_none_for_missing_paths_instead_of_raising():
    assert report.dig(_TODAY, "rest.total_calls") == 13000
    assert report.dig(_TODAY, "rest.없는키") is None
    assert report.dig(_TODAY, "없는절.a.b") is None
