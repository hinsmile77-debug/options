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


# ===== 2026-08-03 §5-1: 신호 도달률 =====


def _reach(**overrides) -> dict:
    base = {
        "available": True, "decisions": 494, "member_count_max": 3,
        "gamma_flip_count": 450, "gamma_flip_pct": 91.1,
        "chain_leg_median": 30.0, "chain_leg_max": 30,
        "chain_age_seconds_median": 60.0, "chain_age_seconds_max": 120.0,
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_signal_reach_section_reports_values_and_no_warning_when_healthy():
    out = report.render(_TODAY, db_metrics={"signal_reach": _reach()})
    assert "## 14. 신호 도달률" in out
    assert "91.1%" in out
    assert "경고 없음" in out


def test_signal_reach_section_surfaces_warnings():
    # 08-03 실측 재현: 감마플립 0%, 앙상블 최대 2멤버, 체인 최고령 4주.
    out = report.render(
        _TODAY,
        db_metrics={"signal_reach": _reach(
            member_count_max=2, gamma_flip_count=0, gamma_flip_pct=0.0,
            chain_leg_max=246, chain_age_seconds_max=2_400_000.0,
            warnings=["앙상블 최대 가용 멤버 2개 — options_flow가 한 번도 활성화되지 않았다"],
        )},
    )
    assert "⚠ 앙상블 최대 가용 멤버 2개" in out
    assert "경고 없음" not in out


def test_signal_reach_section_says_so_when_migration_not_applied():
    # 0%로 표시하면 "오늘 신호가 죽었다"는 거짓 신호가 된다.
    out = report.render(_TODAY, db_metrics={"signal_reach": {"available": False}})
    section = out.split("## 14. 신호 도달률", 1)[1].split("\n## ", 1)[0]
    assert "마이그레이션 022" in section
    assert "감마플립 산출률" not in section


def test_signal_reach_section_quotes_thresholds_from_the_single_source():
    # 배지와 리포트가 다른 임계를 쓰면 어느 쪽을 믿을지 알 수 없다(README 규약).
    from mahdi.ops.db_metrics import SIGNAL_REACH_WARNINGS

    out = report.render(_TODAY, db_metrics={"signal_reach": _reach()})
    assert f"< {SIGNAL_REACH_WARNINGS['gamma_flip_pct_min']}%" in out


def test_signal_reach_renders_missing_chain_columns_as_dashes():
    # 마이그레이션 022 이전 판단 행은 체인 컬럼이 NULL이다 — "None"이 그대로 찍히면 안 된다.
    out = report.render(
        _TODAY,
        db_metrics={"signal_reach": _reach(
            chain_leg_median=None, chain_leg_max=None,
            chain_age_seconds_median=None, chain_age_seconds_max=None,
        )},
    )
    section = out.split("## 14. 신호 도달률", 1)[1].split("\n## ", 1)[0]
    assert "None" not in section


# ===== 2026-08-03 §5-5: 북별 감마 지형 =====


def test_book_gamma_map_separates_expiries_and_marks_expiry_day():
    out = report.render(
        _TODAY,
        db_metrics={"book_gamma_map": [
            {"expiry": "2026-08-03", "legs": 10, "gex": 2_874_115_490, "gamma_flip": None,
             "pin_strike": 1050.0, "pin_concentration_pct": 54.3, "expiry_today": True},
            {"expiry": "2026-08-13", "legs": 10, "gex": -4_761_470_801, "gamma_flip": 987.5,
             "pin_strike": 1050.0, "pin_concentration_pct": 83.6, "expiry_today": False},
        ]},
    )
    section = out.split("## 15. 북별 감마 지형", 1)[1].split("\n## ", 1)[0]
    assert "2026-08-03" in section and "2026-08-13" in section
    assert "**만기 당일**" in section
    # 만기 당일 북의 감마플립이 비는 것은 정상이라는 설명이 함께 나와야 한다.
    assert "감마플립이 정의되지 않는다" in section


def test_book_gamma_map_section_survives_empty_input():
    out = report.render(_TODAY, db_metrics={"book_gamma_map": []})
    assert "## 15. 북별 감마 지형" in out
    assert "데이터 없음" in out.split("## 15. 북별 감마 지형", 1)[1]


# ===== 2026-08-04 고도화 6종의 렌더링 =====

_WIDE_BOOK_NO_FLIP = {
    "expiry": "2026-08-13", "strikes": 25, "strike_min": 952.5, "strike_max": 1012.5,
    "search_pct": 4.75, "net_call_put_oi": -6292, "call_heavy_strikes": 4,
    "put_heavy_strikes": 20, "wide_gamma_flip": None, "flip_possible": False,
}


def test_wide_oi_landscape_calls_out_when_flip_becomes_possible():
    """고도화#4 — 이 표의 존재 이유는 하루치 값이 아니라 **바뀌는 날**이다.

    「GEX 광폭 체인」은 2026-08-04에 폐기됐고(딜러 포지션이 전 구간 한 방향), 그 폐기의
    재개 조건이 바로 이 전환이다. 사람이 매일 두 리포트를 나란히 놓지 않아도 뜨게 한다.
    """
    today = {"wide_oi_landscape": [{**_WIDE_BOOK_NO_FLIP, "wide_gamma_flip": 985.25,
                                    "flip_possible": True}]}
    previous = {"db": {"wide_oi_landscape": [_WIDE_BOOK_NO_FLIP]}}

    out = "\n".join(report._render_book_gamma_map(today, previous))

    assert "🔔" in out
    assert "2026-08-13 불가→**가능**" in out


def test_wide_oi_landscape_is_quiet_when_nothing_changed():
    today = {"wide_oi_landscape": [_WIDE_BOOK_NO_FLIP]}
    previous = {"db": {"wide_oi_landscape": [_WIDE_BOOK_NO_FLIP]}}

    out = "\n".join(report._render_book_gamma_map(today, previous))

    assert "🔔" not in out
    assert "없음" in out  # 광폭 감마플립 열


def test_wide_oi_landscape_does_not_crash_without_a_previous_day():
    out = "\n".join(report._render_book_gamma_map({"wide_oi_landscape": [_WIDE_BOOK_NO_FLIP]}, None))
    assert "🔔" not in out


def test_rest_latency_section_says_not_measured_yet_instead_of_zero():
    """고도화#5 — 계측 도입 이전 로그를 "p95 0초"로 표시하면 08-04 §2-1과 같은 거짓 개선이 된다."""
    out = "\n".join(report._render_rest_latency({}))
    assert "계측 전" in out
    assert "0.00" not in out


def test_rest_latency_section_renders_the_pre_committed_rule_when_p95_is_high():
    metrics = {
        "rest_latency": {
            "endpoints": {"inquire-price": {"calls": 9000, "p50": 1.1, "p95": 2.8,
                                            "p99": 4.5, "max": 6.2}},
            "p95_by_hour": {"13": {"inquire-price": 2.8}},
            "p95_warn_threshold": 2.5,
            "warnings": [{"hour": "13", "endpoint": "inquire-price", "p95": 2.8}],
        }
    }
    out = "\n".join(report._render_rest_latency(metrics))

    assert "임계(2.5초)를 넘은 구간 **1개**" in out
    assert "이틀 연속 같은 시간대" in out  # 사전 대응 규칙이 리포트에 인용된다
    assert "발동은 사람이 한다" in out  # 자동 적응 금지가 함께 남는다


def test_member_availability_names_the_dead_member_and_the_reason():
    """고도화#2 — `available_member_count` 숫자 하나로는 어느 멤버가 왜 죽었는지 모른다."""
    db = {
        "member_availability": {
            "available": True, "minutes": 494,
            "members": [
                {"member": "flow_position", "available_minutes": 494, "available_pct": 100.0,
                 "top_unavailable_reason": None, "implemented": True},
                {"member": "orderflow_ofi_vpin", "available_minutes": 0, "available_pct": 0.0,
                 "top_unavailable_reason": "ofi/queue_imbalance 없음", "implemented": True},
                {"member": "lstm_temporal", "available_minutes": 0, "available_pct": 0.0,
                 "top_unavailable_reason": "미학습(Phase 3)", "implemented": False},
            ],
        }
    }
    out = "\n".join(report._render_member_availability(db))

    assert "orderflow_ofi_vpin" in out and "ofi/queue_imbalance 없음" in out
    assert "*(미구현)*" in out  # 학습 전 멤버는 살아있는 결함과 구분돼야 한다


def test_strike_window_section_warns_against_reading_window_match_as_pass_fail():
    """고도화#3 — 창 정합률은 구조적으로 100% 밑이다(재롤링 1틱 지연 + 히스테리시스).

    이 주석이 빠지면 Fix#6(히스테리시스)이 다음 점검에서 **회귀로 오독**된다.
    """
    db = {
        "strike_window_quality": {
            "available": True, "expiry": "2026-08-13", "minutes": 488,
            "atm_covered_pct": 96.1, "window_covered_pct": 35.5,
            "atm_offset_strikes_median": 1.0, "atm_offset_strikes_max": 7.0,
            "design_strikes": 5, "snapshot_strikes_median": 6.0, "snapshot_strikes_max": 11,
            "width_jitter": 1.2, "snapshot_window_minutes": 5,
        }
    }
    metrics = {"atm_rolls": {"count": 194, "round_trips": 70, "round_trip_pct": 36.1}}
    out = "\n".join(report._render_strike_window(db, metrics))

    assert "합격/불합격으로 읽지 말 것" in out
    assert "일부러" in out  # 히스테리시스가 의도적으로 창을 늦게 옮긴다는 사실
    assert "ATM 롤링 **194회**" in out and "**36.1%**" in out


def test_strike_window_section_says_not_measured_yet_when_the_book_is_unknown():
    out = "\n".join(report._render_strike_window(
        {"strike_window_quality": {"available": False, "reason": "만기유동성 미적재"}}, {}
    ))
    assert "계측 전" in out and "만기유동성 미적재" in out
