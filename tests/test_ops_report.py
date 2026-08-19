"""`mahdi.ops.report` — 지표 dict → 마크다운 렌더러.

2026-08-01(운영점검보고서 2026-07-31 §5-2). 렌더러는 순수 함수라 dict만으로 전부 검증된다.
"""

from __future__ import annotations

from datetime import date

import pytest

from mahdi.ops import db_metrics, log_metrics, report

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
    assert "## 0-1. 가설 검정" in out
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


# ===== 2026-08-05 Fix#2 / #4 / #5 / #6 렌더러 =====


def test_missing_section_flags_the_gap_between_the_log_axis_and_the_db_axis():
    """§2-6 — 로그 축(사이클이 돌았는가)과 DB 축(행이 남았는가)이 다르면 그 차이가 신호다.

    08-05: 로그 기준 결손 1분(10:04)인데 DB 기준 0행은 4분이었다. `rows=0`으로 끝난 사이클
    (14:31, KIS가 53초간 전 레그 타임아웃)은 로그 축이 구조적으로 못 본다.
    """
    metrics = {"cycles": {"missing": {"count": 1, "odd": 0, "even": 1, "list": ["10:04"],
                                      "recovered_by_catchup": 0, "unrecovered_count": 1}}}
    db = {"chain_minute_coverage": {
        "available": True, "span_minutes": 494, "minutes_with_rows": 490,
        "zero_row_minutes": ["10:04", "10:54", "12:57", "14:31"], "zero_row_count": 4,
        "over_design_minutes": [["10:03", 24], ["12:56", 22]], "over_design_count": 2,
    }}
    rendered = "\n".join(report._render_missing(metrics, db))

    assert "로그 1분 vs DB 4분" in rendered
    assert "14:31" in rendered
    assert "설계 상한" in rendered and "10:03(24행)" in rendered


def test_missing_section_is_quiet_when_both_axes_agree():
    """오탐 방지 — 두 축이 같으면 경고를 내지 않는다."""
    metrics = {"cycles": {"missing": {"count": 0, "odd": 0, "even": 0, "list": [],
                                      "recovered_by_catchup": 0, "unrecovered_count": 0}}}
    db = {"chain_minute_coverage": {
        "available": True, "span_minutes": 400, "minutes_with_rows": 400,
        "zero_row_minutes": [], "zero_row_count": 0,
        "over_design_minutes": [], "over_design_count": 0,
    }}
    rendered = "\n".join(report._render_missing(metrics, db))

    assert "두 축이 어긋난다" not in rendered
    assert "설계 상한" not in rendered


def test_log_volume_prints_the_line_composition_identity():
    """§2-4 — `총계 = httpx + 사람 + 트레이스백`을 눈으로 확인할 수 있어야 한다.

    08-05에 이 줄이 없어 트레이스백 16,577줄이 `human_lines`에 섞였고, 그 값으로 가설
    `2026-08-04-p4`가 **거짓 반증**됐다(실측 4,599줄은 예측치 <=6,500을 통과했다).
    """
    metrics = {"log_volume": {
        "total_bytes": 4_444_444, "total_lines": 33737, "httpx_bytes": 2_650_000,
        "httpx_pct": 62.4, "human_lines": 4599, "traceback_lines": 16577,
        "httpx_lines": 12561, "by_level": {"INFO": 1},
    }}
    rendered = "\n".join(report._render_log_volume(metrics))

    assert "httpx **12,561** + 사람 **4,599** + 트레이스백 **16,577** = **33,737**" in rendered
    assert "총계" not in rendered.split("줄 구성")[1].split("\n")[0], "항등식이 맞으면 불일치 문구가 없어야 한다"
    assert "트레이스백이 로그의 **49%**" in rendered


def test_member_availability_separates_structural_unavailability():
    """§2-8 — 종가 단일가의 미가용은 결함이 아니다. 가용률에 녹이지 않고 열을 따로 둔다."""
    db = {"member_availability": {
        "available": True, "minutes": 494,
        "members": [{"member": "orderflow_ofi_vpin", "available_minutes": 410,
                     "available_pct": 83.0, "top_unavailable_reason": "종가 단일가(연속체결 없음)",
                     "structural_minutes": 9, "implemented": True}],
    }}
    rendered = "\n".join(report._render_member_availability(db))

    assert "그중 구조적" in rendered
    assert "구조적 미가용은 결함이 아니다" in rendered


def test_spot_divergence_section_refuses_to_set_a_threshold_on_the_basis():
    """§2-3 / Fix#6 — 괴리율에는 임계를 걸지 않는다(선물 베이시스는 실재하는 경제량이다).

    보고서가 처음 적었던 "0.5% 2분 연속" 규칙은 08-05 실측에서 정상 베이시스 구간
    (09:01·09:02·09:22·10:32)에 오경보를 냈을 것이다. 판정은 **지수 정지**로 한다.
    """
    db = {"spot_source_divergence": {
        "available": True, "futures_symbol": "A01609", "minutes": 405,
        "max_pct": 5.002, "median_pct": 0.252,
        "index_frozen_minutes": 27, "index_frozen_max_run": 15,
    }}
    rendered = "\n".join(report._render_spot_divergence(db))

    assert "임계를 걸지 않는다" in rendered
    assert "최장 연속 **15분**" in rendered
    assert "판정은 **지수 정지**로 한다" in rendered


# ===== 2026-08-05 고도화#5 — KIS p95 "이틀 연속" 발동 조건 자동 판정 =====


def _latency(warnings):
    return {"rest_latency": {"endpoints": {}, "p95_by_hour": {}, "p95_warn_threshold": 2.5,
                             "warnings": warnings}}


def test_latency_streak_fires_when_the_same_hour_repeats():
    """08-04 고도화#5가 숫자 보기 전에 적어둔 규칙의 **발동 조건을 자동으로 판정**한다.

    그 전까지 "이틀 연속 같은 시간대"는 어제 리포트와 오늘 리포트를 사람이 손으로 대조해야만
    확인할 수 있었다 — 대조를 안 하면 규칙은 적어둔 채로 영영 발동하지 않는다.
    """
    today = [{"hour": "10", "endpoint": "inquire-price", "p95": 3.76},
             {"hour": "14", "endpoint": "inquire-price", "p95": 3.90}]
    prev = dict(_latency([{"hour": "10", "endpoint": "inquire-price", "p95": 2.9}]), date="2026-08-05")

    rendered = "\n".join(report._render_latency_streak(today, prev, 2.5))

    assert "연속 판정 성립: 1개 구간" in rendered
    assert "10시" in rendered and "14시" not in rendered, "겹치는 시간대만 세야 한다"
    assert "적용 여부는 사람이 결정" in rendered, "자동 발동은 되먹임을 만든다(07-08에 203분을 잃었다)"


def test_latency_streak_is_silent_when_hours_do_not_repeat():
    today = [{"hour": "14", "endpoint": "inquire-price", "p95": 3.9}]
    prev = dict(_latency([{"hour": "10", "endpoint": "inquire-price", "p95": 2.9}]), date="2026-08-05")

    rendered = "\n".join(report._render_latency_streak(today, prev, 2.5))

    assert "해당 없음" in rendered
    assert "연속 판정 성립" not in rendered


def test_latency_streak_declines_to_judge_without_yesterday():
    """전일 계측이 없으면 **판정하지 않는다** — 08-05가 그랬다(§9-1은 08-04 저녁에 생겼다)."""
    rendered = "\n".join(report._render_latency_streak([{"hour": "10", "endpoint": "x", "p95": 3.0}], None, 2.5))

    assert "연속 판정을 못 한다" in rendered


# ===== 2026-08-06 §3-1 / Fix#3 — 「경로 없음」 배너와 리스트 절 색인 =====


def test_dead_metric_paths_get_their_own_banner_above_the_table():
    """표 안의 한 줄이면 08-06처럼 28행 중에 묻힌다 — 가장 위로 올린다."""
    from mahdi.ops import hypotheses, report

    results = hypotheses.evaluate(
        [{
            "id": "h-dead", "가설": "죽은 경로", "상태": "pending", "검증예정일": date(2026, 8, 6),
            "예측": [{"metric": "db.없는절.값", "expect": ">= 0", "역할": hypotheses.ROLE_CLAIM}],
        }],
        date(2026, 8, 6), {"overrun": {"count": 0}}, {"decisions": {"total": 1}},
    )
    rendered = "\n".join(report._render_hypotheses(results))
    assert "경로 없음 1건" in rendered
    assert "db.없는절.값" in rendered
    # 배너가 표보다 위에 있어야 한다.
    assert rendered.index("경로 없음 1건") < rendered.index("| id |")


def test_dig_indexes_a_list_section_by_its_natural_key():
    """`db.tables.underlying_spot_1m.rows` — 08-05 p6이 적었고 영원히 None이던 경로다."""
    from mahdi.ops.report import dig

    metrics = {"tables": [
        {"table": "option_analysis_1m", "rows": 9069},
        {"table": "underlying_spot_1m", "rows": 384},
    ]}
    assert dig(metrics, "tables.underlying_spot_1m.rows") == 384
    assert dig(metrics, "tables.없는테이블.rows") is None


def test_dig_list_indexing_does_not_swallow_scalars():
    from mahdi.ops.report import dig

    assert dig({"a": 1}, "a.b") is None
    assert dig({"a": [1, 2, 3]}, "a.b") is None


# ==========================================================================================
# 2026-08-07 고도화#5 — 멤버 부호 일치율의 3영업일 추이
# ==========================================================================================


def _scores_db(pairs):
    return {"member_score_quality": {"available": True, "members": [], "pairs": pairs}}


def test_member_score_pairs_show_the_previous_business_days():
    """하루치 변화로는 「갈렸다」와 「부호가 뒤집혀 있다」를 구분할 수 없다.

    08-07 실측 `flow_position ↔ options_flow` 22.3%(197분 중 44분만 부호 일치)가 이 열을
    만든 이유다 — 무작위라면 50% 근처여야 한다.
    """
    today = _scores_db([
        {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 197,
         "same_sign_minutes": 44, "same_sign_pct": 22.3},
    ])
    history = [
        {"date": "2026-08-06", "db": _scores_db([
            {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 380,
             "same_sign_minutes": 95, "same_sign_pct": 25.0},
        ])},
        {"date": "2026-08-05", "db": _scores_db([
            {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 300,
             "same_sign_minutes": 72, "same_sign_pct": 24.0},
        ])},
    ]

    text = "\n".join(report._render_member_scores(today, history))

    assert "2026-08-06" in text and "2026-08-05" in text
    assert "22.3%" in text and "25.0%" in text and "24.0%" in text
    # 임계를 걸지 않는다는 것 자체가 이 절의 결정이다 — 문구가 사라지면 다음 사람이 임계를 만든다.
    assert "여전히 임계를 걸지 않는다" in text


def test_member_score_pairs_leave_a_gap_instead_of_dropping_the_column():
    """그날 그 쌍이 없었으면 자리를 비운다 — 열이 사라지면 「안 쟀다」와 「0%였다」가 같아진다."""
    today = _scores_db([
        {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 10,
         "same_sign_minutes": 5, "same_sign_pct": 50.0},
    ])
    history = [{"date": "2026-08-06", "db": _scores_db([])}]

    rows = [
        line for line in report._render_member_scores(today, history)
        if line.startswith("| flow_position ↔")
    ]

    assert len(rows) == 1
    # 마지막 두 칸이 「전일 대비」와 그날의 일치율 — 둘 다 비어야 한다(지어내지 않는다).
    assert rows[0].rstrip().endswith("| — | — |")


def test_member_scores_without_history_render_the_same_columns_as_before():
    """이력이 없으면(첫날) 열이 안 늘어난다 — 기존 리포트 형태가 그대로여야 한다."""
    today = _scores_db([
        {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 10,
         "same_sign_minutes": 5, "same_sign_pct": 50.0},
    ])

    header = [line for line in report._render_member_scores(today, None) if "멤버 쌍" in line][0]

    # 앞뒤 파이프 + 5열(멤버 쌍 / 분 / 일치 / 일치율 / 전일 대비) — 이력 열은 안 늘어난다.
    assert header.count("|") == 6


# ==========================================================================================
# 2026-08-07 고도화#C — REJECT 대조군(시간대 매칭)
# ==========================================================================================


def _control_rows():
    """(decision, hour, total, n_5, h_5, n_15, h_15, n_30, h_30)."""
    from datetime import datetime as _dt

    h9, h10, h15 = _dt(2026, 8, 7, 9), _dt(2026, 8, 7, 10), _dt(2026, 8, 7, 15)
    return [
        ("ENTER", h9, 40, 40, 20, 40, 24, 40, 18),
        ("ENTER", h10, 30, 30, 15, 30, 15, 30, 15),
        ("REJECT", h9, 20, 20, 12, 20, 14, 20, 14),
        # 15시는 ENTER가 없다 — 시간대 매칭에서 빠져야 한다(진입 컷오프 이후라 구조적이다).
        ("REJECT", h15, 45, 45, 45, 45, 45, 45, 45),
    ]


def test_control_group_matches_hours_before_comparing():
    """08-07 실측에서 두 그룹의 시간대 분포가 심하게 달랐다 — 그대로 비교하면 시간대를 잰다."""
    from mahdi.ops import decision_outcomes

    folded = decision_outcomes._fold_control_group(_control_rows())

    assert folded["shared_hours"] == ["09"]
    # 15시 REJECT 45건(전부 적중)이 매칭에서 빠져야 한다 — 안 빼면 REJECT가 100%에 가까워진다.
    assert folded["horizons"]["5m"]["hit_pct"] == pytest.approx(87.7, abs=0.1)   # 원시(교란됨)
    assert folded["time_matched"]["reject"]["5m"]["hit_pct"] == 60.0             # 12/20
    assert folded["time_matched"]["enter"]["5m"]["hit_pct"] == 50.0              # 20/40


def test_control_group_delta_is_none_when_a_side_has_no_sample():
    """한쪽 표본이 없으면 차이를 0으로 만들지 않는다 — 없는 것과 같은 것은 다르다."""
    assert report._pt_delta({"hit_pct": 50.0}, {"hit_pct": None}) is None
    assert report._pt_delta(None, {"hit_pct": 50.0}) is None
    assert report._pt_delta({"hit_pct": 49.5}, {"hit_pct": 60.8}) == -11.3


def test_control_group_renders_the_direction_of_the_gap():
    """차이가 음수면 「우리가 거른 판단이 더 잘 맞혔다」 — 그 독법이 표에 붙어 있어야 한다."""
    from mahdi.ops import decision_outcomes

    control = decision_outcomes._fold_control_group(_control_rows())
    text = "\n".join(report._render_outcome_control(control))

    assert "시간대를 맞춘 비교다" in text
    assert "거른 판단이 더 잘 맞혔다" in text
    assert "하루치로 결론 내지 않는다" in text


def test_member_sign_agreement_shows_the_day_over_day_swing():
    """2026-08-07 고도화#B — 판정 축은 고정 임계가 아니라 **전일 대비 변화폭**이다.

    `flow_position ↔ options_flow`가 08-06의 74.6% → 08-07의 13.1%로 하루 만에 뒤집혔다.
    그 전날 낮에 제기한 「부호 규약이 뒤집혀 있을 가능성」은 이 값으로 기각된다 —
    규약 버그라면 매일 낮아야 한다.
    """
    today = _scores_db([
        {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 404,
         "same_sign_minutes": 53, "same_sign_pct": 13.1},
    ])
    history = [
        {"date": "2026-08-06", "db": _scores_db([
            {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 380,
             "same_sign_minutes": 283, "same_sign_pct": 74.6},
        ])},
    ]

    text = "\n".join(report._render_member_scores(today, history))

    assert "-61.5pt" in text
    assert "흔들린다는 것이 앙상블이 살아 있다는 증거다" in text
    assert "여전히 임계를 걸지 않는다" in text


def test_member_sign_agreement_delta_is_blank_without_yesterday():
    """전일 값이 없으면 변화폭을 0(변화 없음)으로 지어내지 않는다."""
    today = _scores_db([
        {"a": "flow_position", "b": "options_flow", "both_nonzero_minutes": 10,
         "same_sign_minutes": 5, "same_sign_pct": 50.0},
    ])
    row = next(
        line for line in report._render_member_scores(today, None)
        if line.startswith("| flow_position ↔")
    )
    assert row.split("|")[5].strip() == "—"


# ===== 최장 연속 0행 구간 (2026-08-14 §2-1 / Fix#3) =====


def _coverage(zero_minutes, run):
    return {
        "available": True, "span_minutes": 493, "minutes_with_rows": 407,
        "zero_row_minutes": zero_minutes, "zero_row_count": len(zero_minutes),
        "zero_row_longest_run": run,
        "zero_row_run_alert_minutes": db_metrics.ZERO_ROW_RUN_ALERT_MINUTES,
        "over_design_count": 0, "over_design_minutes": [],
    }


def test_a_long_zero_row_run_is_shouted_not_just_counted():
    """08-14 재현 — 「0행 86분」만으로는 흩어진 날과 무너진 날이 구별되지 않는다."""
    text = "\n".join(report._render_zero_row_run(
        _coverage(["14:33"], {"length": 51, "start": "14:33", "end": "15:23"})
    ))

    assert "51분" in text and "14:33~15:23" in text
    assert "⛔" in text
    assert "판단이 감마 지형 없이 간 시간" in text


def test_a_short_zero_row_run_is_reported_without_an_alarm():
    """평시 딸꾹질까지 붉게 하면 그 표시는 곧 무시된다."""
    text = "\n".join(report._render_zero_row_run(
        _coverage(["13:30", "13:31"], {"length": 2, "start": "13:30", "end": "13:31"})
    ))

    assert "2분" in text
    assert "⛔" not in text


def test_a_legacy_coverage_says_unmeasurable_not_zero():
    """규약 C — 키가 없는 날을 「연속 0분」으로 인쇄하면 그 0이 증거처럼 읽힌다."""
    coverage = _coverage(["13:30"], None)
    coverage.pop("zero_row_longest_run")
    assert "측정 불가" in "\n".join(report._render_zero_row_run(coverage))


# ===== p50 × read timeout 교차 (2026-08-14 §2-2 / Fix#3) =====


def _latency_metrics(p50_by_hour):
    return {
        "endpoints": {}, "p95_by_hour": {}, "p50_by_hour": p50_by_hour,
        "p95_warn_threshold": log_metrics.REST_LATENCY_P95_WARN_SECONDS,
        "p50_timeout_ratio_warn": log_metrics.REST_LATENCY_P50_TIMEOUT_RATIO_WARN,
        "global_read_timeout_seconds": log_metrics.GLOBAL_HTTP_READ_TIMEOUT_SECONDS,
        "warnings": [],
    }


def test_the_p50_timeout_crossing_is_shouted():
    """08-14 14시 재현 — 중앙값 호출이 타임아웃을 넘으면 수집은 느려지지 않고 **비어 버린다**."""
    lat = _latency_metrics({"13": {"inquire-price": 3.08}, "14": {"inquire-price": 4.05}})

    text = "\n".join(report._render_p50_timeout_cross({}, lat))

    assert "1.01" in text          # 4.05 / 4.00
    assert "0.77" in text          # 13시 — 절벽 24분 전에 이미 경고선을 넘었다
    assert "⛔" in text
    assert "최장 연속 0행 구간" in text


def test_the_window_max_wins_over_the_hourly_average():
    """**평균은 절벽을 눌러 없앤다.** 08-14 13시가 정확히 그 형태였다.

    가중평균 2.18초(비율 0.55)만 보면 조용한 시간대인데, 그 안의 창 최대는 3.53초(0.88)로
    이미 경고선을 넘었고 그 20~60분 뒤 84분 전멸이 시작됐다. 판정은 최대로 해야 한다.
    """
    lat = _latency_metrics({"13": {"inquire-price": 2.18}})
    lat["p50_max_by_hour"] = {"13": {"inquire-price": 3.53}}

    text = "\n".join(report._render_p50_timeout_cross({}, lat))

    assert "0.88" in text and "⚠" in text
    assert "2.18" in text and "3.53" in text   # 두 값을 **함께** 보여 준다


def test_the_crossing_uses_the_lever_value_when_the_lever_is_on():
    """레버가 켜진 날 전역값(4.0)으로 비교하면 이 표가 통째로 거짓말을 한다."""
    lat = _latency_metrics({"14": {"inquire-price": 4.05}})
    metrics = {"levers": {"levers": [
        {"key": "OPTION_CHAIN_READ_TIMEOUT_SECONDS", "value": 6.0, "default": None, "on": True},
    ]}}

    text = "\n".join(report._render_p50_timeout_cross(metrics, lat))

    assert "6.00" in text
    assert "⛔" not in text        # 4.05 / 6.0 = 0.68 — 절벽이 아니다
    assert "레버" in text


def test_a_calm_day_says_so_explicitly():
    lat = _latency_metrics({"10": {"inquire-price": 0.03}})
    assert "✅" in "\n".join(report._render_p50_timeout_cross({}, lat))


def test_the_hourly_table_carries_yesterdays_same_hour():
    """08-14 장중 §3 — 단독으로 보면 「12시 47.2초」는 어제의 29.2초와 구별되지 않는다."""
    metrics = {"cycles": {"by_hour": [
        {"hour": 12, "cycles": 60, "rest_mean": 47.2, "rest_max": 52.3,
         "over_60s": 0, "slip_max": 0.0, "foreign_sum": 6},
    ]}}
    previous = {"cycles": {"by_hour": [
        {"hour": 12, "cycles": 60, "rest_mean": 29.2, "rest_max": 33.0,
         "over_60s": 0, "slip_max": 0.0, "foreign_sum": 4},
    ]}}

    text = "\n".join(report._render_by_hour(metrics, previous))

    assert "29.2 (+18.0)" in text
    assert "같은 시간대" in text


def test_the_hourly_table_does_not_invent_a_delta_without_yesterday():
    metrics = {"cycles": {"by_hour": [
        {"hour": 12, "cycles": 60, "rest_mean": 47.2, "rest_max": 52.3,
         "over_60s": 0, "slip_max": 0.0, "foreign_sum": 6},
    ]}}
    row = next(line for line in report._render_by_hour(metrics, None) if line.startswith("| 12시"))
    assert row.split("|")[4].strip() == "—"


# ===== §1 판단 입력 3행 (2026-08-14 §1 / 고도화 1) =====


def _judgement_input_db(**overrides):
    db = {
        "monthly_coverage": {"coverage_pct": 82.6},
        "signal_reach": {"gex_input_missing_minutes": 78},
        "chain_minute_coverage": {"zero_row_longest_run": {"length": 51}},
    }
    db.update(overrides)
    return db


def test_headline_puts_judgement_inputs_beside_the_green_infrastructure():
    """08-14 재현 — 인프라가 전부 초록인 채로 판단 입력의 5분의 1이 사라질 수 있다."""
    text = "\n".join(report._render_headline(_TODAY, None, _judgement_input_db()))

    assert "82.6%" in text and "78분" in text and "51분" in text
    assert "판단이 볼 것을 봤는가" in text


def test_headline_judgement_inputs_carry_the_previous_day_delta():
    previous = {
        "date": "2026-08-13",
        **_TODAY,
        "db": _judgement_input_db(
            monthly_coverage={"coverage_pct": 96.4},
            signal_reach={"gex_input_missing_minutes": 4},
        ),
    }

    text = "\n".join(report._render_headline(_TODAY, previous, _judgement_input_db()))

    assert "96.4%" in text            # 전일 열
    assert "▼13.8" in text or "▼13.80" in text


def test_headline_omits_a_judgement_row_it_cannot_measure():
    """규약 C — 「82.6%」 자리에 「—」를 그리면 그 대시가 「나쁘지 않았다」로 읽힌다."""
    db = _judgement_input_db()
    db.pop("signal_reach")

    text = "\n".join(report._render_headline(_TODAY, None, db))

    assert "82.6%" in text
    assert "`db.signal_reach.gex_input_missing_minutes`" in text
    assert "재지 않았다" in text


def test_headline_says_nothing_was_measured_when_all_three_are_absent():
    text = "\n".join(report._render_headline(_TODAY, None, {"tables": []}))
    assert "판단 입력 3행이 이 집계에 없다" in text


# ===== 위상 이동 (2026-08-14 장중 §3 / 고도화 2) =====


def _hour_row(hour, phase, rest_mean=30.0):
    return {"hour": hour, "cycles": 60, "rest_mean": rest_mean, "rest_max": rest_mean + 5,
            "over_60s": 0, "slip_max": 0.0, "foreign_sum": 0, "end_second_median": phase}


def test_phase_drift_is_shown_against_the_first_hour():
    """08-14 재현 — :19에서 :50까지 31초 밀렸고, 그 곡선 위에 예산초과·컷·전멸이 다 올라앉았다."""
    metrics = {"cycles": {"by_hour": [
        _hour_row(7, 19.0), _hour_row(9, 25.0), _hour_row(12, 50.0),
    ]}}

    text = "\n".join(report._render_by_hour(metrics, None))

    assert ":19.0 (+0초)" in text
    assert ":50.0 (+31초)" in text
    assert "사이클 종료 위상" in text
    assert "공통 원인인 이 한 줄은 안 쟀다" in text


def test_phase_column_is_a_dash_not_a_zero_on_legacy_metrics():
    """규약 C — 「:00에 끝났다」와 「그 집계에는 이 값이 없다」는 다르다."""
    metrics = {"cycles": {"by_hour": [{"hour": 9, "cycles": 60, "rest_mean": 30.0,
                                       "rest_max": 35.0, "over_60s": 0, "slip_max": 0.0,
                                       "foreign_sum": 0}]}}

    row = next(line for line in report._render_by_hour(metrics, None) if line.startswith("| 09시"))

    assert row.split("|")[7].strip() == "—"


# ===== 2026-08-16 (Block B) — §16-1 보유 포지션 =====


def _positions_db(**overrides) -> dict:
    base = {
        "available": True, "rows": 4, "snapshot_minutes": 2, "symbols": 2,
        "max_concurrent_symbols": 2, "side_distribution": {"BUY": 1, "SELL": 1},
        "unknown_side_count_max": 0,
    }
    base.update(overrides)
    return {"positions": base}


def test_empty_position_table_is_reported_as_two_possibilities_not_one():
    """규약 C — 「행 0」은 「체결이 없었다」와 「적재가 죽었다」 둘이다.

    개시 전에는 전자가 정상이므로, 절이 그 둘을 가르는 방법(`execution_logs`)까지 적어야 한다.
    """
    rendered = "\n".join(report._render_positions(_positions_db(rows=0)))

    assert "두 가지다" in rendered
    assert "execution_logs" in rendered


def test_position_section_prints_concurrency_next_to_the_same_direction_limit():
    """동시 보유 종목 수는 **동일방향 한도(3)와 나란히** 읽어야 의미가 있다."""
    rendered = "\n".join(report._render_positions(_positions_db()))

    assert "동시 보유 최대 **2개**" in rendered
    assert "동일방향 한도 3" in rendered
    assert "`BUY` 1" in rendered and "`SELL` 1" in rendered


def test_unknown_side_raises_a_warning_that_points_at_the_raw_log_line():
    """**이 경고가 §16-1의 존재 이유다.**

    방향 판정에 실패한 날은 동일방향 한도 판정이 부풀려져 있고, 실측값은 로그에만 있다 —
    절이 그 로그 줄을 가리켜야 다음 사람이 R8 범위표를 쓸 수 있다.
    """
    rendered = "\n".join(
        report._render_positions(
            _positions_db(side_distribution={"UNKNOWN": 1}, unknown_side_count_max=1)
        )
    )

    assert "방향 판정 실패 최대 1건" in rendered
    assert "밑돈다" in rendered
    assert "잔고 방향 판정 실패" in rendered  # 로그에서 grep할 문구 그대로
    assert "KIS_RAW_FIELD_RANGES.md" in rendered


def test_position_section_flags_when_the_two_axes_disagree():
    """포지션 스냅샷에는 UNKNOWN이 있는데 잔고 스냅샷의 카운트는 0이면 두 축이 갈린 것이다 —
    같은 사이클을 보고 있지 않다는 뜻이라 조용히 넘기면 안 된다."""
    rendered = "\n".join(
        report._render_positions(
            _positions_db(side_distribution={"UNKNOWN": 1}, unknown_side_count_max=0)
        )
    )

    assert "두 축이 갈렸다" in rendered


def test_position_section_says_so_when_every_side_was_understood():
    """0건일 때도 **무엇이 확인됐는지** 적는다 — 침묵은 「안 쟀다」와 구별되지 않는다."""
    rendered = "\n".join(report._render_positions(_positions_db()))

    assert "방향 판정 실패 0건" in rendered
    assert "position_snapshots.raw" in rendered


def test_position_section_sets_no_threshold_because_the_normal_range_is_unknown():
    """첫 포지션이 08-18에 생긴다 — 정상 분포를 모르는 채 임계를 정하면 그 임계가 곧 결론이 된다."""
    import inspect

    source = inspect.getsource(report._render_positions)
    assert "임계를 걸지 않는다" in source


# ===== 2026-08-19 (08-18 보고서 §3-3 / Fix#3) — §1 델타 기준일 배너 =====
#
# 08-18의 §1은 「REST수집 평균 ▲18.7 ⚠」·「느린 REST 호출 ▲2,840 ⚠」·「사람이 읽는 로그
# ▲5,770 ⚠」·「비200 응답 ▲35 ⚠」 넷을 붉게 인쇄했다. **넷 다 거짓이었다** — 전일 08-17이
# 광복절 대체휴일이라 거래일과 휴장일을 뺀 값이었다(규약 G). 배너는 그 표를 읽기 **전에** 뜬다.


def _banner(**baseline):
    base = {
        "date": "2026-08-14", "baseline_is_trading_day": 1, "skipped_non_trading_days": 0,
        "target_is_trading_day": 1, "target_holiday_name": None,
        "calendar_coverage_gap_days": 0, "sidecar_found": 1,
    }
    base.update(baseline)
    return "\n".join(report._delta_baseline_banner({"delta_baseline": base}))


def test_a_normal_day_prints_no_banner_at_all():
    """정상일에 배너가 뜨면 매일 뜨고, 매일 뜨는 경고는 곧 안 읽힌다."""
    assert _banner() == ""


def test_the_baseline_that_skipped_a_holiday_says_so():
    text = _banner(date="2026-08-14", skipped_non_trading_days=3)
    assert "직전 거래일 2026-08-14 기준" in text
    assert "비거래일 3일" in text


def test_a_non_trading_day_labels_its_own_output():
    """휴장일 자신의 산출물이 다음날 기준선으로 쓰이는 것을 막는 것이 이 줄의 목적이다."""
    text = _banner(target_is_trading_day=0, target_holiday_name="광복절 대체공휴일")
    assert "비거래일" in text and "광복절 대체공휴일" in text


def test_an_unknown_baseline_is_printed_as_unknown_not_as_zero():
    text = _banner(date=None, baseline_is_trading_day=None, skipped_non_trading_days=None)
    assert "직전 거래일을 못 찾았다" in text
    assert "0으로 접지 않았다" in text


def test_an_expired_calendar_admits_the_verdict_is_weekend_only():
    assert "달력이 오늘을 못 덮는다" in _banner(calendar_coverage_gap_days=12)
    assert "달력이 오늘을 못 덮는다" in _banner(calendar_coverage_gap_days=None)


def test_an_old_sidecar_without_the_section_is_silent():
    """구버전 사이드카에는 이 절이 없다 — 그때 지어낸 배너를 그리면 그것이 새 거짓이다."""
    assert report._delta_baseline_banner({}) == []


# ===== 2026-08-19 (08-18 보고서 §2-4 / Fix#5) — 컷을 독립 절로 올린다 =====
#
# 08-18에 컷 라벨 3건이 켜졌는데 그 시각의 `p50 ÷ timeout`은 **0.74로 경고선(0.80) 아래**였다.
# 지연 표만 읽은 회차는 그 세 건을 못 봤다 — 두 축은 같은 날 반대 방향으로 갈 수 있다.


def _cuts(**over):
    base = {
        "budget_exceeded": {"count": 177, "skipped_legs_total": 845,
                            "priority_cut_minutes": 3, "priority_before_others_minutes": 3},
        "timeout_abort": {"count": 24, "skipped_legs_total": 255,
                          "priority_cut_minutes": 6, "priority_before_others_minutes": 0},
        "failure_budget_abort": {"count": 2, "skipped_legs_total": 18,
                                 "priority_cut_minutes": 0, "priority_before_others_minutes": 0},
    }
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v} if v is not None else None
    return {k: v for k, v in base.items() if v is not None}


def test_the_three_causes_are_never_merged_into_one_number():
    """원인이 다르면 조치가 다르다 — 합치면 「무엇이 이 분을 얇게 만들었는가」에 못 답한다."""
    out = "\n".join(report._render_chain_cuts(_cuts()))
    assert "예산 초과(벽시계)" in out and "연속 타임아웃" in out and "실패 예산 소진" in out
    assert "우리가 느렸다" in out and "KIS가 read timeout 천장에 닿았다" in out


def test_the_budget_path_label_is_explicitly_not_a_violation():
    """08-18의 오진을 표 옆에서 막는다 — 그 열은 데드라인 경로에서 **구조적으로** 켜진다."""
    out = "\n".join(report._render_chain_cuts(_cuts()))
    assert "위반으로 읽지 말 것" in out
    assert "먼슬리 두께" in out


def test_the_scope_path_is_still_a_real_ordering_invariant():
    """`timeout_abort`는 1단계 접기를 거치므로 0이어야 한다 — 0이 아니면 Fix#4가 깨진 것이다."""
    ok = "\n".join(report._render_chain_cuts(_cuts()))
    assert "✅ `timeout_abort` 경로 **0분**" in ok

    broken = "\n".join(report._render_chain_cuts(
        _cuts(timeout_abort={"priority_before_others_minutes": 4})
    ))
    assert "🚨" in broken and "선제 강등" in broken


def test_an_unlabelled_log_says_it_did_not_measure():
    """규약 C — 라벨이 없는 날의 `None`을 「순서를 지켰다」로 인쇄하면 안 된다."""
    out = "\n".join(report._render_chain_cuts(
        _cuts(timeout_abort={"priority_before_others_minutes": None})
    ))
    assert "못 쟀다" in out


def test_a_log_without_the_cut_sections_is_honest_about_it():
    out = "\n".join(report._render_chain_cuts({}))
    assert "안 셌다" in out


# ===== 2026-08-19 — 검열 위상은 **배수로 인쇄하고 판정하지 않는다** =====
#
# 08-19에 점유율 22.4%(균등선 13.3%의 **1.68배**)가 「균등선 근처다 — 특정 분에 몰린 것이
# 아니다」로 인쇄됐다. 1.68배를 「근처」라고 부를 수는 없다 — 임계(2배) 하나로 연속량을 이분한
# 결과이고, 경계 근처의 날마다 그 문장이 틀린다.


def _censored(**over):
    base = {
        "count": 747, "by_endpoint": {"inquire-price": 712},
        "phase_minutes": [0, 1, 2, 3, 30, 31, 32, 33],
        "phase_count": 167, "phase_concentration": 0.224,
        "phase_baseline": 0.133, "phase_ratio": 1.68, "samples": [],
    }
    base.update(over)
    return "\n".join(report._render_censored_calls({"count": 4058, "censored": base}))


def test_the_phase_share_is_printed_as_a_multiple():
    out = _censored()
    assert "22.4%" in out and "13.3%" in out and "1.68배" in out


def test_no_verdict_word_is_asserted_either_way():
    """**이 절은 판정하지 않는다.** 두 문장 다 하루치로는 말할 수 없는 주장이다."""
    for ratio, share in ((1.68, 0.224), (2.54, 0.338), (0.9, 0.12)):
        out = _censored(phase_ratio=ratio, phase_concentration=share)
        assert "균등선 근처다" not in out
        assert "특정 분에 몰린 것이 아니다" not in out
        assert "**위상 문제다**" not in out


def test_both_days_are_quoted_so_one_day_cannot_decide():
    out = _censored()
    assert "2.54배" in out and "1.68배" in out


def test_the_day_two_non_replication_is_stated_next_to_the_number():
    """08-18의 「전부 :01」은 표본이 넷이었다 — 그 사실이 같은 자리에 있어야 한다."""
    out = _censored()
    assert "재현되지 않았다" in out and "24건" in out


def test_an_old_sidecar_without_the_ratio_still_renders():
    """구버전 사이드카에는 `phase_ratio`가 없다 — 배수만 빠지고 나머지는 그대로 나와야 한다.

    설명 인용부의 「08-18 2.54배 → 08-19 1.68배」는 **고정 문장**이라 언제나 남는다.
    빠져야 하는 것은 **그날 값이 들어가는 불릿 줄**뿐이므로 그 줄만 본다.
    """
    bullet = next(
        line for line in _censored(phase_ratio=None).splitlines()
        if line.startswith("- 정각·30분 창")
    )
    assert "22.4%" in bullet and "13.3%" in bullet
    assert "배**" not in bullet and "→" not in bullet
