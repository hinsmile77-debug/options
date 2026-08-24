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


def test_resolve_target_date():
    assert log_metrics.resolve_target_date("2026-07-31", datetime(2026, 8, 1, 15, 0)) == TARGET
    assert log_metrics.resolve_target_date(None, datetime(2026, 8, 1, 15, 0)) == date(2026, 8, 1)


def test_the_weekend_only_baseline_helper_is_gone():
    """2026-08-19 Fix#3 — `previous_business_day()`는 **지웠다**(주말만 보는 함수였다).

    기준일 계산의 유일한 답은 `market_calendar.previous_trading_day()`다. 이 단언이 있는
    이유는 «편해서» 다시 만들어지는 것을 막기 위함이다 — 같은 질문에 답이 둘 있으면
    하나는 반드시 틀린 채로 쓰이고, 08-18에 그 틀린 쪽이 하루를 오독하게 만들었다.
    """
    assert not hasattr(log_metrics, "previous_business_day")


# ===== 2026-08-06 §3-2·§3-3 / Fix#4 — 억제된 예외와 프로세스 재기동 =====
#
# 08-06 실측: `qualitative.read_timeout` 126건으로 보고됐지만 실제는 205건이었다(39% 소실).


def _parse(lines: list[str]) -> dict:
    from mahdi.ops import log_metrics

    return log_metrics.parse_day(lines, date(2026, 8, 6))


def test_throttle_suppressed_exceptions_are_counted_into_their_type():
    """`WarningThrottle`이 삼킨 줄은 이 숫자에만 흔적이 남는다 — 그것을 읽는다."""
    parsed = _parse([
        "2026-08-06 10:00:11,455 WARNING:mahdi.main:옵션 체인 폴링 실패: B01608992 — "
        "The read operation timed out (트레이스백 생략 — httpx.ReadTimeout, 오늘 15번째)",
        "2026-08-06 10:01:11,455 WARNING:mahdi.main:옵션 체인 폴링 실패: C01608992 — "
        "The read operation timed out (트레이스백 생략 — httpx.ReadTimeout, 오늘 22번째) "
        "(최근 60초간 6건 추가 억제됨)",
    ])
    # 줄 2건 + 억제 6건 = 8건
    assert parsed["qualitative"]["read_timeout"] == 8
    assert parsed["qualitative_suppressed"]["read_timeout"] == 6


def test_a_line_without_a_suppression_summary_adds_nothing_extra():
    parsed = _parse([
        "2026-08-06 10:00:11,455 WARNING:mahdi.main:옵션 체인 폴링 실패: B01608992 — "
        "The read operation timed out (트레이스백 생략 — httpx.ReadTimeout, 오늘 15번째)",
    ])
    assert parsed["qualitative"]["read_timeout"] == 1
    assert parsed["qualitative_suppressed"].get("read_timeout", 0) == 0


def test_suppression_is_attributed_per_exception_type():
    parsed = _parse([
        "2026-08-06 10:00:11,455 WARNING:mahdi.main:투자자 수급 폴링 실패: OP01 — x "
        "(트레이스백 생략 — httpx.ConnectError, 오늘 2번째) (최근 60초간 3건 추가 억제됨)",
        "2026-08-06 10:02:11,455 WARNING:mahdi.main:옵션 체인 폴링 실패: B0 — y "
        "(트레이스백 생략 — httpx.ReadTimeout, 오늘 9번째) (최근 60초간 4건 추가 억제됨)",
    ])
    assert parsed["qualitative"]["connect_error"] == 4
    assert parsed["qualitative"]["read_timeout"] == 5


def test_process_starts_are_counted():
    """08-06에 프로세스가 세 번 떴고, 그 사실을 아는 지표가 하나도 없었다."""
    parsed = _parse([
        "2026-08-06 07:31:04,000 INFO:mahdi.main:직전 정상 기동: 2026-08-05 07:30:00 (24.0시간 전)",
        "2026-08-06 08:23:46,681 INFO:mahdi.main:직전 정상 기동: 2026-08-06 07:31:04 (0.9시간 전)",
        "2026-08-06 10:23:25,471 INFO:mahdi.main:직전 정상 기동: 2026-08-06 08:23:46 (2.0시간 전)",
    ])
    assert len(parsed["process_starts"]) == 3
    assert parsed["process_starts"][0] == 7 * 3600 + 31 * 60 + 4


def test_first_ever_start_is_also_counted():
    """마커 파일이 없는 최초 실행도 프로세스 기동이다 — 같은 문구로 시작한다."""
    parsed = _parse([
        "2026-08-06 07:31:04,000 INFO:mahdi.main:직전 정상 기동 기록 없음(최초 실행 또는 마커 파일 삭제됨)",
    ])
    assert len(parsed["process_starts"]) == 1


# ===== 2026-08-06 §3-4 / Fix#5 — 실패의 원인 축 =====


def test_failures_are_split_by_cause():
    """08-06 실측 재현 — 만기유동성 실패 7건이 전부 EGW00201이고 ReadTimeout은 0건이었다."""
    parsed = _parse([
        '2026-08-06 07:31:31,023 WARNING:mahdi.main:만기 유동성 폴링 실패: B01608A21 — '
        '{"rt_cd":"1","msg1":"초당 거래건수를 초과하였습니다.","msg_cd":"EGW00201"}',
        '2026-08-06 09:05:39,047 WARNING:mahdi.main:만기 유동성 폴링 실패: B09F9WA10 — '
        '{"rt_cd":"1","msg1":"초당 거래건수를 초과하였습니다.","msg_cd":"EGW00201"}',
    ])
    assert parsed["failures"]["만기 유동성 폴링 실패"] == 2
    assert parsed["failures_by_cause"]["만기 유동성 폴링 실패"] == {"egw00201": 2}


def test_read_timeout_failures_are_attributed_to_read_timeout():
    parsed = _parse([
        "2026-08-06 10:35:34,074 WARNING:mahdi.main:만기 유동성 만기확인 조회 실패: B09F9W985 — "
        "The read operation timed out (트레이스백 생략 — httpx.ReadTimeout, 오늘 20번째)",
    ])
    assert parsed["failures_by_cause"]["만기 유동성 만기확인 조회 실패"] == {"read_timeout": 1}


def test_non_ratelimit_kis_errors_are_their_own_cause():
    """CBOT 미신청 같은 계정 권한 문제는 레이트리밋과 조치가 전혀 다르다."""
    parsed = _parse([
        '2026-08-06 15:37:00,000 WARNING:mahdi.main:ZN(10년 국채선물) 근월물 조회 실패: ZNU26 — '
        '{"rt_cd":"1","msg_cd":"EGW00552","msg1":"CBOT SUB거래소 신청 계좌가 아닙니다."}',
    ])
    assert parsed["failures_by_cause"]["ZN(10년 국채선물) 근월물 조회 실패"] == {"kis_error": 1}


def test_cause_totals_match_the_failure_totals():
    """총계와 원인별 합이 갈리면 둘 중 하나가 거짓말이다."""
    parsed = _parse([
        '2026-08-06 07:31:31,023 WARNING:mahdi.main:만기 유동성 폴링 실패: B0 — {"rt_cd":"1","msg_cd":"EGW00201"}',
        "2026-08-06 10:35:34,074 WARNING:mahdi.main:만기 유동성 폴링 실패: B1 — x "
        "(트레이스백 생략 — httpx.ReadTimeout, 오늘 2번째)",
        "2026-08-06 11:35:34,074 WARNING:mahdi.main:만기 유동성 폴링 실패: B2 — 알 수 없는 무언가",
    ])
    for kind, total in parsed["failures"].items():
        assert sum(parsed["failures_by_cause"][kind].values()) == total


def test_a_failure_without_any_clue_falls_back_to_other():
    """트레이스백이 살아 있는 예외는 여기로 떨어진다 — 알고 남긴 한계다."""
    parsed = _parse([
        "2026-08-06 11:35:34,074 WARNING:mahdi.main:투자자 수급 폴링 실패: OP01",
    ])
    assert parsed["failures_by_cause"]["투자자 수급 폴링 실패"] == {"other": 1}


def test_egw00201_wins_over_the_generic_kis_error_classification():
    """EGW00201은 rt_cd 응답의 한 종류다 — 더 구체적인 쪽이 이겨야 조치가 갈린다."""
    line = '2026-08-06 07:31:31,023 WARNING:mahdi.main:x 실패: B0 — {"rt_cd":"1","msg_cd":"EGW00201"}'
    assert log_metrics.classify_failure_cause(line) == log_metrics.FAILURE_CAUSE_EGW00201


# ===== 2026-08-06 §3-5 / Fix#6 — 「미가동」과 「인프라 결손」을 가른다 =====


def _cycle_line(hhmm: str, rows: int = 20) -> str:
    # 초 자리를 20으로 두는 이유: 사이클의 **시작 시각**은 이 줄의 시각에서 소요(19.17초)를 뺀
    # 값이라, 초가 그보다 작으면 앞 분으로 넘어간다(파서의 정의를 그대로 따라간다).
    return (
        f"2026-08-06 {hhmm}:20,000 INFO:mahdi.main:옵션체인 사이클 소요 분해: "
        f"REST수집 19.03초 + DB적재 0.11초 + 상태기록 0.03초 + 기타 0.00초 "
        f"(rows={rows}, 밀림=0.0초, 타폴러동시호출추정=0건)"
    )


def test_the_2026_08_06_outage_is_attributed_to_downtime_not_infrastructure():
    """실제 사건 재현 — 10:03 마지막 사이클, 10:23:25 재기동, 10:24 첫 사이클.

    그날 리포트는 `결손 21분 ▲20 ⚠`을 냈고 그 숫자를 인프라 악화로 읽으면 틀린다.
    """
    parsed = _parse([
        _cycle_line("10:01"), _cycle_line("10:02"), _cycle_line("10:03"),
        "2026-08-06 10:23:25,471 INFO:mahdi.main:직전 정상 기동: 2026-08-06 08:23:46 (2.0시간 전)",
        _cycle_line("10:24"), _cycle_line("10:25"),
    ])
    missing = parsed["cycles"]["missing"]
    assert missing["count"] == 20  # 10:04 ~ 10:23
    assert missing["downtime_count"] == 20
    assert missing["infra_count"] == 0


def test_a_missing_minute_while_the_loop_was_running_stays_infrastructure():
    """루프가 도는 중에 놓친 분은 진짜 인프라 결손이다 — 08-06의 13:19가 그 한 건이었다."""
    parsed = _parse([_cycle_line("13:17"), _cycle_line("13:18"), _cycle_line("13:20")])
    missing = parsed["cycles"]["missing"]
    assert missing["count"] == 1
    assert missing["downtime_count"] == 0
    assert missing["infra_count"] == 1


def test_downtime_and_infrastructure_gaps_coexist():
    parsed = _parse([
        _cycle_line("10:01"), _cycle_line("10:03"),   # 10:02 = 루프가 돌던 중의 결손
        "2026-08-06 10:05:30,000 INFO:mahdi.main:직전 정상 기동: 2026-08-06 08:23:46 (2.0시간 전)",
        _cycle_line("10:06"),                          # 10:04~10:05 = 재기동 사이 공백
    ])
    missing = parsed["cycles"]["missing"]
    assert missing["infra_list"] == ["10:02"]
    assert missing["downtime_list"] == ["10:04", "10:05"]
    assert missing["downtime_count"] + missing["infra_count"] == missing["count"]


def test_the_first_start_of_the_day_creates_no_downtime():
    """그날 첫 기동 앞은 애초에 관측 대상이 아니다 — 장전 07:30 이전이다."""
    parsed = _parse([
        "2026-08-06 07:31:04,000 INFO:mahdi.main:직전 정상 기동: 2026-08-05 07:30:00 (24.0시간 전)",
        _cycle_line("07:32"), _cycle_line("07:34"),
    ])
    assert parsed["cycles"]["missing"]["downtime_count"] == 0
    assert parsed["cycles"]["missing"]["infra_count"] == 1  # 07:33


def test_a_log_without_start_markers_behaves_exactly_as_before():
    """구버전 로그에서는 종전대로 전부 인프라 결손이다 — 지어내지 않는다."""
    parsed = _parse([_cycle_line("13:17"), _cycle_line("13:20")])
    missing = parsed["cycles"]["missing"]
    assert missing["downtime_count"] == 0
    assert missing["infra_count"] == 2


def test_recovered_minutes_are_not_counted_as_infrastructure_gaps():
    """회수된 분은 이미 메워졌다 — 세 축의 합이 총계를 넘으면 안 된다."""
    parsed = _parse([
        _cycle_line("13:17"), _cycle_line("13:20"),
        "2026-08-06 13:20:30,000 INFO:mahdi.main:옵션체인 결손 회수: 13:18 분을 먼슬리 10레그로 채움(밀린 사이클 13:19)",
    ])
    missing = parsed["cycles"]["missing"]
    assert missing["recovered_by_catchup"] == 1
    assert missing["infra_count"] == 1  # 13:19만 남는다
    assert missing["downtime_count"] + missing["infra_count"] + missing["recovered_by_catchup"] == missing["count"]


def test_kis_error_bodies_without_quoted_rt_cd_are_still_kis_errors():
    """해외선물 오류 응답은 `rt_cd`에 따옴표가 없다 — 그것 때문에 하루 9건이 `other`로 샜다."""
    parsed = _parse([
        '2026-08-06 15:37:00,000 WARNING:mahdi.main:ES(E-mini S&P500) 근월물 조회 실패: ESU26 — '
        '{rt_cd:"1","msg1":"CME SUB거래소 신청 계좌가 아닙니다.","msg_cd":"EGW00552"}',
    ])
    assert parsed["failures_by_cause"]["ES(E-mini S&P500) 근월물 조회 실패"] == {"kis_error": 1}


# ===== 2026-08-06 고도화#1 — 먼슬리 레그 재시도 집계 =====


def test_priority_retry_counts_attempts_and_recoveries():
    """회복 0건("KIS가 계속 느렸다")과 시도 0건("예산이 없었다")은 원인이 다르다."""
    parsed = _parse([
        "2026-08-06 10:01:20,000 INFO:mahdi.main:먼슬리 레그 재시도: 3개 중 2개 회복(남은 예산 12.4초) "
        "— 판단 주입력(GEX/감마플립)의 두께다",
        "2026-08-06 10:02:20,000 INFO:mahdi.main:먼슬리 레그 재시도: 1개 중 0개 회복(남은 예산 3.1초) "
        "— 판단 주입력(GEX/감마플립)의 두께다",
    ])
    pr = parsed["priority_retry"]
    assert pr == {
        "cycles": 2, "attempted": 4, "recovered": 2, "recovery_pct": 50.0,
        # 2026-08-24 Fix#4 — **두 줄 다 회복 < 대상**이다(3개 중 2개 · 1개 중 0개).
        # 회복률 50%는 그중 전멸 한 건을 평균에 접어 없앤다 — 그것이 이 칸을 만든 이유다.
        "failed_cycles": 2, "failed_minutes": ["10:01", "10:02"],
    }


def test_priority_retry_is_zero_when_the_line_never_appears():
    parsed = _parse([_cycle_line("10:01")])
    assert parsed["priority_retry"]["cycles"] == 0
    assert parsed["priority_retry"]["recovery_pct"] is None


def test_priority_retry_log_format_matches_the_source_string():
    """포맷 원본이 바뀌면 이 파서는 조용히 0을 낸다 — 계약을 여기서 못박는다."""
    from mahdi.main import LOG_CHAIN_PRIORITY_RETRY

    rendered = LOG_CHAIN_PRIORITY_RETRY % (5, 4, 8.25)
    parsed = _parse([f"2026-08-06 10:01:20,000 INFO:mahdi.main:{rendered}"])
    assert parsed["priority_retry"] == {
        "cycles": 1, "attempted": 5, "recovered": 4, "recovery_pct": 80.0,
        "failed_cycles": 1, "failed_minutes": ["10:01"],
    }


# ===== 2026-08-24 (08-24 §1-9 / Fix#4) — **파서를 먼저 넓히고 나서 문구를 바꾼다** =====
#
# 같은 날 `mahdi.main`이 회복 실패를 **WARNING**으로 올린다. 레벨이 `INFO`로 고정돼 있으면
# 이 파서는 **정확히 그 사건에서만** 눈이 먼다 — 08-04에 반대 방향으로 겪은 그 사고다.


def test_a_failed_revival_logged_as_warning_is_still_counted():
    """이 fix의 전부 — 15:10:50 「3개 중 0개 회복」이 레벨 때문에 사라지면 안 된다."""
    from mahdi.main import LOG_CHAIN_PRIORITY_RETRY_FAILED

    rendered = LOG_CHAIN_PRIORITY_RETRY_FAILED % (3, 0, 0.0)
    parsed = _parse([f"2026-08-06 15:10:50,000 WARNING:mahdi.main:{rendered}"])
    assert parsed["priority_retry"]["cycles"] == 1
    assert parsed["priority_retry"]["failed_cycles"] == 1
    assert parsed["priority_retry"]["failed_minutes"] == ["15:10"]


def test_a_tight_budget_success_is_not_counted_as_a_failure():
    """**두 사건을 한 문턱으로 묶지 않는다** — 「간신히 성공」은 실패가 아니다."""
    from mahdi.main import LOG_CHAIN_PRIORITY_RETRY_BUDGET_FLOOR

    rendered = LOG_CHAIN_PRIORITY_RETRY_BUDGET_FLOOR % (3, 3, 2.9)
    parsed = _parse([f"2026-08-06 14:31:20,000 INFO:mahdi.main:{rendered}"])
    assert parsed["priority_retry"]["cycles"] == 1
    assert parsed["priority_retry"]["failed_cycles"] == 0


def test_all_three_retry_wordings_share_one_head():
    """머리가 갈라지면 파서가 조용히 일부만 센다 — 계약을 여기서 못박는다."""
    from mahdi import main

    head = main._LOG_CHAIN_PRIORITY_RETRY_HEAD
    for template in (
        main.LOG_CHAIN_PRIORITY_RETRY,
        main.LOG_CHAIN_PRIORITY_RETRY_FAILED,
        main.LOG_CHAIN_PRIORITY_RETRY_BUDGET_FLOOR,
    ):
        assert template.startswith(head)


def test_balance_poll_failure_is_counted_and_prints_zero_when_it_never_happens():
    """분자를 센다 — 그리고 **0인 날에도 키가 있어야** 「그 줄이 없던 버전」과 갈린다(규약 C)."""
    from mahdi.main import LOG_BALANCE_POLL_FAILED

    rendered = LOG_BALANCE_POLL_FAILED % ("ReadTimeout", "없음", "1.50", "12:33:32")
    parsed = _parse([f"2026-08-06 12:34:32,000 WARNING:mahdi.main:{rendered}"])
    assert parsed["qualitative"]["balance_poll_failed"] == 1

    quiet = _parse([_cycle_line("10:01")])
    assert quiet["qualitative"]["balance_poll_failed"] == 0
