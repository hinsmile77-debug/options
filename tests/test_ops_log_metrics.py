"""`mahdi.ops.log_metrics` — 운영 지표 로그 파서.

2026-08-01(운영점검보고서 2026-07-31 §5-2). 픽스처(`tests/fixtures/observation_loop_sample.log`)의
숫자는 전부 손으로 셀 수 있게 작게 만들었다 — 파서가 틀리면 여기서 바로 드러난다.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from mahdi.ops import log_metrics

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TARGET = date(2026, 7, 31)


@pytest.fixture
def metrics() -> dict:
    lines = log_metrics.iter_day_lines(FIXTURE_DIR, TARGET, stem="observation_loop_sample.log")
    return log_metrics.parse_day(lines, TARGET)


# ===== 입력 해석(로테이션 + 트레이스백 승계) =====


def test_iter_day_lines_keeps_only_the_target_date():
    lines = list(log_metrics.iter_day_lines(FIXTURE_DIR, TARGET, stem="observation_loop_sample.log"))
    stamped = [line for line in lines if line[:4].isdigit()]
    assert stamped and all(line.startswith("2026-07-31") for line in stamped)
    # 본문에 다른 날짜가 들어간 줄(직전 기동 시각)은 날짜 접두사 기준으로 남아야 한다.
    assert any("직전 정상 기동: 2026-07-30" in line for line in lines)


def test_iter_day_lines_carries_traceback_continuation_into_the_owning_date():
    # 회귀 방지 1: 트레이스백 연속 줄은 타임스탬프가 없다 — 직전 줄의 날짜를 승계하지 않으면
    # ZN/ES 트레이스백이 통째로 누락돼 로그 볼륨 집계가 틀린다.
    lines = list(log_metrics.iter_day_lines(FIXTURE_DIR, TARGET, stem="observation_loop_sample.log"))
    assert "Traceback (most recent call last):" in lines
    assert any(line.startswith("httpx.HTTPStatusError") for line in lines)
    assert any(line.startswith("httpx.RemoteProtocolError") for line in lines)


def test_iter_day_lines_reads_rotated_backups_oldest_first(tmp_path):
    # 회귀 방지 2: 하루치가 `.log.1`과 `.log`에 걸치는 일이 실제로 있었다(07-30).
    (tmp_path / "obs.log.1").write_text(
        "2026-07-31 09:00:00,000 INFO:mahdi.main:이른 줄\n", encoding="utf-8"
    )
    (tmp_path / "obs.log").write_text(
        "2026-07-31 15:00:00,000 INFO:mahdi.main:늦은 줄\n", encoding="utf-8"
    )
    lines = list(log_metrics.iter_day_lines(tmp_path, TARGET, stem="obs.log"))
    assert [line.split(":")[-1] for line in lines] == ["이른 줄", "늦은 줄"]


# ===== 사이클/결손 =====


def test_cycle_start_is_derived_from_end_minus_total_duration(metrics):
    # 로그 타임스탬프는 사이클 **종료** 시각이다 — 시작 시각을 역산해야 분 격자와 맞는다.
    assert metrics["cycles"]["count"] == 4
    assert metrics["cycles"]["first_start"] == "09:00"  # 09:00:30 종료 − 30초
    # 09:05:04 종료 − 64.5초 = 09:04 시작 — 종료 시각을 그대로 쓰면 09:05로 한 칸 밀린다.
    assert metrics["cycles"]["last_start"] == "09:04"
    assert metrics["cycles"]["rows_distribution"] == {10: 2, 30: 2}


def test_overrun_cycle_is_counted_once_not_twice(metrics):
    # 밀린 사이클은 WARNING과 INFO 두 줄을 남긴다 — 둘 다 세면 사이클 수가 부풀려진다.
    assert metrics["overrun"]["count"] == 1
    assert metrics["overrun"]["max_seconds"] == 4.5
    assert metrics["cycles"]["over_60s"] == 1


def test_missing_minutes_and_catchup_recovery(metrics):
    missing = metrics["cycles"]["missing"]
    assert missing["list"] == ["09:02"]
    # 09:05 회수 로그는 결손 목록(09:02)에 없으므로 회수 0 — 실제 회수분만 차감해야 한다.
    assert missing["recovered_by_catchup"] == 0
    assert missing["unrecovered_count"] == 1
    assert metrics["catchups"] == {"count": 1, "minutes": ["09:05"]}


def test_foreign_calls_are_measured_inside_the_cycle_window(metrics):
    # 09:00 사이클(09:00:00~09:00:30)엔 옵션체인 2건만 있고 타폴러(09:00:40 수급)는 창 밖이다.
    by_mod10 = {r["mod10"]: r for r in metrics["cycles"]["by_mod10"]}
    assert by_mod10[0]["foreign_by_group"] == {}
    # 09:01 사이클(09:01:00~09:01:10)도 만기유동성(09:01:15~)보다 앞이라 겹치지 않는다.
    assert by_mod10[1]["foreign_mean"] == 0.0


# ===== REST 수요 =====


def test_rest_demand_and_deficit_threshold(metrics):
    rest = metrics["rest"]
    assert rest["total_calls"] == 13
    assert rest["by_group"]["옵션체인"] == 6
    assert rest["by_group"]["만기유동성"] == 3
    assert rest["by_group"]["투자자수급"] == 1
    assert rest["by_group"]["계좌잔고"] == 1
    assert rest["by_group"]["매크로"] == 2
    assert rest["by_status"] == {"200": 11, "500": 2}
    assert rest["non_200"]["count"] == 2
    # 적자 시작 배율 = 1 / (수요÷용량) — 수요가 절반이면 2배까지 버틴다.
    assert rest["deficit_threshold_multiplier"] == pytest.approx(
        1 / (rest["calls_per_second"] / log_metrics.PACER_CAPACITY_CALLS_PER_SECOND), rel=0.01
    )


def test_pacer_capacity_matches_the_broker_default():
    # 이 모듈은 순수 파서라 브로커 계층을 import하지 않는다 — 두 상수가 갈라지면 수요 비율이
    # 조용히 틀리므로 여기서 못박는다.
    from mahdi.broker.rest_client import DEFAULT_MIN_REQUEST_INTERVAL_SECONDS

    assert log_metrics.PACER_CAPACITY_CALLS_PER_SECOND == 1 / DEFAULT_MIN_REQUEST_INTERVAL_SECONDS


# ===== 백오프/버스트/위상 =====


def test_backoff_events(metrics):
    assert metrics["backoff"]["expand"] == 1
    assert metrics["backoff"]["recover"] == 1
    assert metrics["backoff"]["max_multiplier"] == 1.5


def test_burst_occupancy_is_measured_per_group(metrics):
    bursts = metrics["bursts"]
    assert "만기유동성" in bursts
    assert bursts["만기유동성"]["occupancy_seconds"]["max"] == pytest.approx(11.0)


def test_bursts_exclude_continuously_running_pollers():
    # "쉬는 시간보다 일하는 시간이 길면 그건 버스트가 아니라 연속 가동"이라 표에서 뺀다 —
    # 매 분 도는 옵션체인/투자자수급은 60초 임계 안에서 하루가 통째로 뭉쳐 무의미한 행이 된다.
    continuous = [(float(t), "옵션체인", "200") for t in range(0, 600, 5)]  # 5초 간격 연속 가동
    bursty = [(float(base + i), "만기유동성", "200") for base in (0, 600) for i in range(5)]
    result = log_metrics._burst_metrics(sorted(continuous + bursty))
    assert "만기유동성" in result
    assert "옵션체인" not in result


def test_phase_is_measured_from_burst_starts_not_every_call(metrics):
    # 만기유동성은 09:01:15에 발사를 시작해 09:01:26까지 쏜다 — 위상은 15초(시작)여야 하고
    # 전체 호출 평균(약 20초)이면 안 된다. 2026-08-01 최초 실행에서 실제로 어긋났던 지점.
    assert metrics["poller_phase"]["만기유동성"]["mode_second"] == 15


# ===== 느린 호출/정성 항목/로그 볼륨 =====


def test_slow_calls_attribute_pacer_versus_http(metrics):
    sc = metrics["slow_calls"]
    assert sc["count"] == 2
    assert sc["http_dominant"] == 1  # 9.52초 = 0.31 + 9.21
    assert sc["pacer_dominant"] == 1  # 5.00초 = 4.50 + 0.50
    assert sc["total_seconds"]["max"] == 9.52


def test_exception_counts_are_per_event_not_per_traceback_line(metrics):
    # RemoteProtocolError는 트레이스백 안에 여러 줄로 나타난다 — 부분 문자열로 세면 부풀려진다
    # (2026-08-01 최초 실행에서 8건이 24건으로 부풀었고, 사람 보고서는 반대로 0건이라 적었다).
    assert metrics["qualitative"]["remote_protocol_error"] == 1
    assert metrics["qualitative"]["ws_reconnect"] == 1
    assert metrics["qualitative"]["egw00201"] == 1
    assert metrics["qualitative"]["market_operation_message"] == 1


def test_levels_count_every_line_not_only_unmatched_ones(metrics):
    # 레벨 집계가 continue 뒤에 있으면 httpx/사이클/백오프 줄이 통째로 빠진다
    # (2026-08-01 최초 실행에서 INFO가 13,726건 → 2건으로 집계됐다).
    levels = metrics["log_volume"]["by_level"]
    assert levels["INFO"] == 22
    assert levels["WARNING"] == 6


def test_failure_types_are_grouped_by_kind(metrics):
    assert metrics["failures"]["만기 유동성 폴링 실패"] == 1
    assert metrics["failures"]["ZN(10년 국채선물) 근월물 조회 실패"] == 1


def test_log_volume_separates_httpx_from_human_lines(metrics):
    """2026-08-05 §2-4 — 줄을 **세 갈래**로 가른다: httpx / 사람 로그 / 트레이스백 본문.

    종전에는 `human_lines = total - httpx`였고 트레이스백 본문이 전부 "사람이 읽는 줄"로
    셌다. 08-05에 그 정의가 21,176줄 중 16,577줄(78%)을 사람 로그로 만들었고, 자동 리포트
    §0은 그 값으로 가설 `2026-08-04-p4`를 **반증 판정했다** — 실측 4,599줄은 예측치(<=6,500)를
    통과했으므로 거짓 반증이었다.
    """
    lv = metrics["log_volume"]
    assert lv["httpx_lines"] == 13
    assert lv["traceback_lines"] == 12  # 트레이스백 3건 x 4줄
    assert lv["human_lines"] == 15
    # 항등식 — 리포트 §11이 이 셋을 나란히 찍는 근거다.
    assert lv["httpx_lines"] + lv["human_lines"] + lv["traceback_lines"] == lv["total_lines"]
    assert 0 < lv["httpx_pct"] < 100


# ===== 빈 입력 =====


def test_parse_day_on_empty_input_returns_zeros_not_fabrications():
    m = log_metrics.parse_day([], TARGET)
    assert m["cycles"]["count"] == 0
    assert m["rest"]["total_calls"] == 0
    assert m["rest"]["calls_per_second"] is None  # 지어내지 않는다
    assert m["bursts"] == {}


def test_resolve_target_date_and_previous_business_day():
    assert log_metrics.resolve_target_date("2026-07-31", datetime(2026, 8, 1, 15, 0)) == TARGET
    assert log_metrics.resolve_target_date(None, datetime(2026, 8, 1, 15, 0)) == date(2026, 8, 1)
    # 월요일(08-03)의 직전 영업일은 일/토를 건너뛴 금요일(07-31)이다.
    assert log_metrics.previous_business_day(date(2026, 8, 3)) == TARGET
