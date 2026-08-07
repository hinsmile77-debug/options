"""로그를 **쓰는 쪽**과 **세는 쪽**의 계약 — 2026-08-04(운영점검보고서 §2-1 / Fix#1).

## 이 파일이 존재하는 이유

2026-08-03에 로그 위생을 위해 세 곳을 바꿨다. 셋 다 옳은 수정이었다:

  1. `느린 REST 호출` 로그를 WARNING → INFO로 (하루 933건의 WARNING이 진짜 경고를 파묻었다)
  2. `RemoteProtocolError`에 1회 재시도 도입 (트레이스백 대신 요약 한 줄)
  3. `HTTPStatusError`의 트레이스백 제거 (원인은 응답 바디에 다 있고 스택은 항상 같았다)

그런데 셋 다 `mahdi/ops/log_metrics.py`의 파서를 침묵시켰고, **아무도 몰랐다.** 08-04 자동
리포트는 이렇게 보고했다:

  | 지표                  | 리포트          | 실제      |
  |-----------------------|-----------------|-----------|
  | slow_calls.count      | **0건 (▼933 ✅)** | **362건** |
  | remote_protocol_error | **실측 없음**   | **25건**  |
  | http_status_error     | **키 없음(=0)** | (08-03 105건) |

그리고 `remote_protocol_error`는 가설 `2026-08-03-p3`(예측 ≤2건)의 판정 지표였다 — 실제로는
08-03의 8건에서 **3.1배 늘어난 명백한 반증**인데 "실측 없음"으로 넘어갔다.

더 나쁜 것은 `rest_client._log_if_slow`의 주석이 *"지표 집계는 계속 이 줄을 읽는다"* 고
**단언**하고 있었다는 점이다. 검증되지 않은 단언은 주석이 아니라 오보다.

## 이 파일이 하는 일

`log_metrics`는 **순수 파서로 남긴다**(브로커 계층을 import하지 않는다 — 그 설계 결정은
2026-08-01부터 유효하다). 대신 **테스트가 양쪽을 동시에 import해서** 이렇게 검증한다:

    emit 측 포맷 상수 → 실제 로그 줄 조립 → 파서에 먹임 → 카운트가 1인가?

포맷 문자열이나 로그 레벨을 바꾸면 이 테스트가 깨진다. 그것이 요점이다.
"""

from __future__ import annotations

from datetime import date

import pytest

from mahdi import main
from mahdi.broker import rest_client
from mahdi.features import options_intel
from mahdi.ops import db_metrics, log_metrics

TARGET = date(2026, 8, 5)
_TS = "2026-08-05 10:11:12,345"


def _emit(logger_name: str, level: str, fmt: str, *args) -> str:
    """emit 측 포맷 상수 + logging 레벨 → `observation_loop.log`에 실제로 남는 줄 한 개."""
    return f"{_TS} {level}:{logger_name}:{fmt % args}"


def _parse(*lines: str) -> dict:
    return log_metrics.parse_day(list(lines), TARGET)


# ===== 느린 REST 호출 (08-03 WARNING→INFO 변경으로 죽었던 계측) =====


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
def test_slow_call_line_is_counted_regardless_of_log_level(level: str):
    """회귀 방지 §2-1(1): 08-03의 WARNING→INFO 변경이 이 계측을 통째로 껐다.

    레벨은 사람이 읽는 우선순위일 뿐 계측의 정체성이 아니다 — 어느 레벨이어도 세야 한다.
    """
    line = _emit(
        "mahdi.broker.rest_client", level, rest_client.LOG_SLOW_CALL,
        7.25, 0.31, 6.94, 1.16, "GET", "inquire-price",
    )
    metrics = _parse(line)

    assert metrics["slow_calls"]["count"] == 1
    sample = metrics["slow_calls"]["samples"][0]
    assert sample["total"] == 7.25
    assert sample["pacer"] == 0.31
    assert sample["http"] == 6.94
    assert sample["endpoint"] == "inquire-price"


def test_slow_call_threshold_constant_is_the_one_the_report_quotes():
    """리포트가 인용하는 임계와 emit 측 임계가 같은 상수여야 한다.

    08-03에 3.0 → 5.0으로 올렸는데 `report.py`의 "임계(3초) 초과 호출 없음" 문자열만
    3초로 남아 있었다 — 08-04 리포트 §9가 틀린 임계를 인쇄했다.
    """
    from mahdi.ops import report

    rendered = "\n".join(report._render_slow_calls({"slow_calls": {"count": 0}}))
    assert f"{rest_client.SLOW_CALL_LOG_THRESHOLD_SECONDS:.0f}초" in rendered


# ===== RemoteProtocolError (08-03 재시도 도입으로 죽었던 계측) =====


def test_handled_remote_protocol_error_is_counted_without_a_traceback():
    """회귀 방지 §2-1(2): 예외를 **잡는 순간** 트레이스백 기반 카운터가 0이 된다.

    08-03이 1회 재시도를 붙이면서(옳은 수정) 이 사건은 트레이스백 대신 INFO 한 줄이 됐고,
    `_EXCEPTION_PREFIXES`는 트레이스백 마지막 줄만 세므로 실제 25건이 "실측 없음"이 됐다.
    """
    line = _emit(
        "mahdi.broker.rest_client", "INFO", rest_client.LOG_REMOTE_PROTOCOL_RETRY,
        "https://openapivts.koreainvestment.com:29443/uapi/domestic-futureoption/v1/quotations/inquire-price",
    )
    assert _parse(line)["qualitative"].get("remote_protocol_error") == 1


def test_traceback_and_handled_forms_accumulate_into_the_same_key():
    """한 사건이 어느 형태로 기록되든 하루 총계는 같아야 한다 — 전환기에 값이 반토막 나면 안 된다."""
    handled = _emit(
        "mahdi.broker.rest_client", "INFO", rest_client.LOG_REMOTE_PROTOCOL_RETRY, "https://example/x",
    )
    traceback_tail = "httpx.RemoteProtocolError: Server disconnected without sending a response."
    assert _parse(handled, traceback_tail)["qualitative"]["remote_protocol_error"] == 2


# ===== 백오프 (계측이 살아있는지 확인 — 08-03에 안 건드렸지만 같은 위험이 있다) =====


def test_backoff_expand_and_recover_lines_are_counted():
    lines = [
        _emit("mahdi.broker.rest_client", "INFO", rest_client.LOG_BACKOFF_EXPAND, 1.00, 1.50, 1.50),
        _emit("mahdi.broker.rest_client", "INFO", rest_client.LOG_BACKOFF_RECOVER, 1.50, 1.35, 1.35),
    ]
    backoff = _parse(*lines)["backoff"]
    assert backoff["expand"] == 1
    assert backoff["recover"] == 1
    assert backoff["max_multiplier"] == 1.50


# ===== KIS 에러 응답 (http_status_error 대체 계측) =====


def test_kis_error_response_is_counted_from_the_current_log_shape():
    """`http_status_error`는 08-03에 트레이스백이 사라져 영구히 0이 된다.

    지금 로그 모양(응답 바디를 실은 WARNING 한 줄)에서 다시 센다 — 계측을 잃지 않기 위함.
    """
    line = (
        f'{_TS} WARNING:mahdi.main:옵션 체인 폴링 실패: C01608875 — '
        '{"rt_cd":"1","msg1":"초당 거래건수를 초과하였습니다.","msg_cd":"EGW00201"}'
    )
    metrics = _parse(line)
    assert metrics["qualitative"]["kis_error_response"] == 1
    assert metrics["failures"]["옵션 체인 폴링 실패"] == 1


# ===== 0건 보고의 증명 (고도화#1 규약 C) =====


def test_parser_audit_flags_a_blind_counter():
    """감사층 자체의 회귀 방지 — **이 테스트가 08-04를 재현한다.**

    파서가 못 알아보는 형태(레벨은 맞지만 문구가 바뀐 줄)를 주면, 엄격 카운터는 0인데
    느슨 토큰은 잡히므로 `blind`에 떠야 한다.
    """
    mutated = f"{_TS} INFO:mahdi.broker.rest_client:느린 REST 호출 7.25초 (형식이 바뀌었다)"
    audit = _parse(mutated)["parser_audit"]

    assert "slow_calls" in audit["blind"]
    assert audit["blind"]["slow_calls"] == {"strict": 0, "loose": 1}


def test_parser_audit_is_silent_when_the_parser_agrees_with_the_log():
    line = _emit(
        "mahdi.broker.rest_client", "INFO", rest_client.LOG_SLOW_CALL,
        7.25, 0.31, 6.94, 1.16, "GET", "inquire-price",
    )
    assert _parse(line)["parser_audit"]["blind"] == {}


def test_parser_audit_does_not_fire_on_a_genuinely_quiet_day():
    """오탐 방지: 아무 일도 안 일어난 날은 엄격 0 · 느슨 0이라 경고가 없어야 한다."""
    quiet = f"{_TS} INFO:mahdi.main:직전 정상 기동: 2026-08-04 07:31:00 (24.0시간 전)"
    assert _parse(quiet)["parser_audit"]["blind"] == {}


# ===== mahdi.main 쪽 지표성 로그 (2026-08-04 고도화#1 규약 A 확장) =====
#
# 08-04에는 `rest_client`만 상수화했는데, 아래 여섯 줄은 **리포트 §1~§4·§8·§9-1·§14-2의 거의
# 전부**를 떠받친다 — 여기가 깨지면 잃는 것이 더 크다.


def test_chain_cycle_breakdown_line_is_parsed():
    line = _emit(
        "mahdi.main", "INFO", main.LOG_CHAIN_CYCLE_BREAKDOWN,
        33.90, 0.12, 0.05, 0.31, 20, 0.0, "", "3건", "10:11",
    )
    cycles = _parse(line)["cycles"]
    assert cycles["count"] == 1
    assert cycles["rest_seconds"]["mean"] == 33.9
    # `rows_distribution`은 2026-08-04 Fix#8의 검증 지표다 — 수집 예산이 걸리면 20 미만
    # 사이클이 나타나야 하고, 하나도 없으면 예산이 안 걸린 것이다.
    assert cycles["rows_distribution"] == {20: 1}
    assert cycles["duplicate_poll_minutes"] == {"count": 0, "list": [], "labelled": 1}


def test_two_cycles_writing_the_same_minute_are_counted(monkeypatch):
    """2026-08-07 §2-1 / Fix#3 — 덮어쓴 분은 결손보다 나쁘다(행 수가 정상이라 안 보인다).

    08-07 15:18에 DB가 0행인데 로그에는 사이클이 완주해 있었다. 그 사이클이 15:17:59.99x에
    깨어 `poll_time`이 15:17로 내려깎였고 직전 분의 행을 UPSERT로 덮어썼다.
    """
    lines = [
        _emit("mahdi.main", "INFO", main.LOG_CHAIN_CYCLE_BREAKDOWN,
              17.1, 0.08, 0.01, 0.0, 10, 0.0, "", "0건", "15:17"),
        _emit("mahdi.main", "INFO", main.LOG_CHAIN_CYCLE_BREAKDOWN,
              25.7, 0.06, 0.03, 0.0, 10, 0.0, "", "0건", "15:17"),
    ]
    dup = _parse(*lines)["cycles"]["duplicate_poll_minutes"]

    assert dup["count"] == 1
    assert dup["list"] == ["15:17"]
    assert dup["labelled"] == 2


def test_cycle_lines_without_a_minute_label_still_parse():
    """08-07 이전 로그에는 `분=` 라벨이 없다 — 그 날들을 재집계할 때 파서가 눈이 멀면 안 된다.

    08-04 §2-1에서 정확히 그 사고를 겪었다(문구가 바뀌자 계측이 통째로 죽고, 그 0건이
    리포트에 「개선」으로 표시됐다). `labelled=0`이 "라벨이 없었다"를 드러낸다.
    """
    legacy = (
        f"{_TS} INFO:mahdi.main:옵션체인 사이클 소요 분해: "
        "REST수집 19.30초 + DB적재 0.09초 + 상태기록 0.03초 + 기타 0.00초 "
        "(rows=20, 밀림=0.0초, 타폴러동시호출추정=0건)"
    )
    cycles = _parse(legacy)["cycles"]

    assert cycles["count"] == 1
    assert cycles["duplicate_poll_minutes"] == {"count": 0, "list": [], "labelled": 0}


def test_chain_overrun_line_is_parsed():
    line = _emit(
        "mahdi.main", "WARNING", main.LOG_CHAIN_OVERRUN,
        60, 18.3, 41.7, 78.3, 0.12, 20, "", "7건",
    )
    overrun = _parse(line)["overrun"]
    assert overrun["count"] == 1
    assert overrun["max_seconds"] == 18.3


def test_chain_catchup_line_is_parsed():
    line = _emit("mahdi.main", "INFO", main.LOG_CHAIN_CATCHUP, "14:31", 10, "14:30")
    catchups = _parse(line)["catchups"]
    assert catchups["count"] == 1
    assert catchups["minutes"] == ["14:31"]


def test_budget_exceeded_line_is_parsed():
    """2026-08-04 Fix#8 — 이 줄이 없으면 "예산이 실제로 걸렸는가"를 셀 수 없다."""
    line = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 6, 14)
    budget = _parse(line)["budget_exceeded"]
    assert budget["count"] == 1
    assert budget["skipped_legs_total"] == 6


def test_atm_roll_lines_are_deduplicated_across_books():
    """회귀 방지 §2-2 / 고도화#3: 롤링은 **북마다 1줄**이라 한 이벤트가 3줄로 나온다.

    세 줄의 타임스탬프는 밀리초가 다르므로 시각까지 넣어 비교하면 중복이 안 걸린다 —
    08-04 실측 582줄은 실제로 **194 이벤트**다.
    """
    lines = [
        f"2026-08-05 10:11:12,{ms} INFO:mahdi.main:"
        + main.LOG_ATM_ROLL % (1001.25, "997.5~1007.5", "1000.0~1010.0")
        for ms in ("345", "372", "401")
    ]
    rolls = _parse(*lines)["atm_rolls"]
    assert rolls["count"] == 1
    assert rolls["round_trips"] == 0


def test_atm_roll_round_trip_is_counted():
    lines = [
        f"2026-08-05 10:11:12,345 INFO:mahdi.main:"
        + main.LOG_ATM_ROLL % (1001.3, "997.5~1007.5", "1000.0~1010.0"),
        f"2026-08-05 10:12:12,345 INFO:mahdi.main:"
        + main.LOG_ATM_ROLL % (1000.9, "1000.0~1010.0", "997.5~1007.5"),
    ]
    rolls = _parse(*lines)["atm_rolls"]
    assert rolls["count"] == 2
    assert rolls["round_trips"] == 1
    assert rolls["round_trip_pct"] == 50.0


def test_rest_latency_line_is_parsed_and_warns_above_threshold():
    """2026-08-04 고도화#5 — §2-6이 밀림의 90%를 KIS 지연으로 귀속시킨 뒤 만든 계측."""
    body = "inquire-price=250건 1.10/2.80/4.50/6.20초 inquire-asking-price=30건 0.90/1.20/1.50/1.80초"
    line = _emit("mahdi.main", "INFO", main.LOG_REST_LATENCY, 300.0, body)
    lat = _parse(line)["rest_latency"]

    assert lat["endpoints"]["inquire-price"]["calls"] == 250
    assert lat["endpoints"]["inquire-price"]["p95"] == 2.80
    assert lat["endpoints"]["inquire-price"]["max"] == 6.20
    # p95 2.80초 > 임계 2.5초 → 사전 대응 규칙의 발동 후보로 표시돼야 한다.
    assert [w["endpoint"] for w in lat["warnings"]] == ["inquire-price"]
    assert lat["warnings"][0]["hour"] == "10"


def test_rest_latency_is_silent_when_every_endpoint_is_fast():
    body = "inquire-price=250건 1.10/1.80/2.10/2.40초"
    line = _emit("mahdi.main", "INFO", main.LOG_REST_LATENCY, 300.0, body)
    assert _parse(line)["rest_latency"]["warnings"] == []


# ===== 2026-08-05: 이벤트 캘린더 미기입 (수기 방식을 고른 대가를 드러내는 계측) =====


def test_event_calendar_not_covered_line_is_counted():
    """수기 캘린더의 실패 모드는 "안 채우는 것"이 아니라 **"안 채운 걸 모르는 것"** 이다.

    안 채우면 `event_proximity_minutes`가 None으로 돌아가고, 그것은 2026-08-05 이전
    (페널티 한 번도 안 걸림)과 **완전히 같은 상태**인데 지표상으로는 아무 일도 없어 보인다.
    이 줄이 §11 정성 항목에 매일 찍히는 것이 그 유일한 방어선이다.
    """
    line = _emit(
        "mahdi.main", "WARNING", main.LOG_EVENT_CALENDAR_NOT_COVERED,
        "not_covered", date(2026, 8, 13), date(2026, 8, 20),
    )
    metrics = _parse(line)

    assert metrics["qualitative"]["event_calendar_not_covered"] == 1


def test_event_calendar_marker_survives_the_parser_audit():
    """0건 보고가 "안 일어났다"인지 "파서가 눈이 멀었다"인지 가르는 감사 토큰이 있어야 한다."""
    assert "event_calendar_not_covered" in log_metrics._PARSER_AUDIT_TOKENS
    token = log_metrics._PARSER_AUDIT_TOKENS["event_calendar_not_covered"]
    # 감사 토큰은 포맷이 바뀌어도 살아남을 만큼 짧아야 하고, 실제 줄에 들어 있어야 한다.
    assert token in main.LOG_EVENT_CALENDAR_NOT_COVERED


# ===== 감마플립 레그 범위 기각 (2026-08-05 §2-5 / Fix#1) =====


def test_gamma_flip_out_of_leg_range_line_is_counted():
    """기각 건수가 §11에 매일 찍혀야 "flip이 왜 안 나오는가"를 사후에 답할 수 있다.

    08-05에는 반대 방향의 침묵이 사고였다 — flip 22건이 **적재됐고** 그중 21건이 레그 범위
    밖이었는데, 그 사실을 드러내는 계측이 없어 자동 리포트 §14는 "산출률 4.5%, 행사가 창이
    스팟을 따라가는지 확인"이라는 **정반대 방향의 경고**를 냈다.
    """
    line = _emit(
        "mahdi.features.options_intel", "WARNING",
        options_intel.LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE,
        956.18, 1042.5, 1052.5, 2.5, 1000.03, 5.0,
    )
    metrics = _parse(line)

    assert metrics["qualitative"]["gamma_flip_out_of_leg_range"] == 1


def test_gamma_flip_rejection_marker_is_a_prefix_of_the_emitted_format():
    """마커는 포맷 상수에서 파생돼야 한다(규약 A) — 문구를 바꾸면 이 테스트가 깨진다."""
    marker = log_metrics._QUALITATIVE_MARKERS["gamma_flip_out_of_leg_range"]
    assert marker in options_intel.LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE


def test_gamma_flip_rejection_survives_the_parser_audit():
    """감사 토큰은 엄격 마커보다 **짧아야** 침묵을 잡는다(규약 C)."""
    token = log_metrics._PARSER_AUDIT_TOKENS["gamma_flip_out_of_leg_range"]
    strict = log_metrics._QUALITATIVE_MARKERS["gamma_flip_out_of_leg_range"]
    assert token in options_intel.LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE
    assert len(token) < len(strict), "감사 토큰이 엄격 마커만큼 길면 같이 눈이 먼다"


def test_gamma_flip_rejection_counter_and_db_invariant_are_different_questions():
    """로그 마커(기각된 것)와 §14 불변식(적재된 것 중 범위 밖)은 **서로를 대체하지 못한다.**

    로그 0건은 "기각할 것이 없었다"이지 "flip이 건강하다"가 아니다. 두 계측이 같은 상수
    (`GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS`)를 공유해야 경계가 갈라지지 않는다 —
    갈라지면 §14가 **정상 통과한 flip을 위반으로 신고한다**.
    """
    assert (
        db_metrics.GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS
        is options_intel.GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS
    )


# ===== 0행 사이클 (2026-08-05 §2-6 / Fix#2) =====


def test_empty_chain_cycle_line_is_counted():
    """`rows=0`으로 끝난 사이클은 결손 지표(로그 기준)가 세지 않는 **유일한 손실 유형**이다.

    08-05 14:31: KIS가 53초간 전 레그를 타임아웃시켜 그 분의 체인이 통째로 사라졌는데,
    사이클은 정상 실행됐으므로 `cycles.missing`에 안 잡혔다 — DB에는 0행인데 §4는 결손 1분만
    보고했다(실제 0행 분 4분). 이 마커가 그 구멍을 메운다.
    """
    line = _emit("mahdi.main", "ERROR", main.LOG_CHAIN_CYCLE_EMPTY, 20, 53.14, "안 함")
    metrics = _parse(line)

    assert metrics["qualitative"]["chain_cycle_empty"] == 1


def test_empty_chain_cycle_is_distinguishable_from_a_partial_truncation():
    """"조금 잘렸다"와 "통째로 날아갔다"가 같은 줄로 보고되면 안 된다 — 08-05의 실제 실패다."""
    truncated = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 3, 17)
    wiped = _emit("mahdi.main", "ERROR", main.LOG_CHAIN_CYCLE_EMPTY, 20, 53.14, "안 함")
    metrics = _parse(truncated, wiped)

    assert metrics["qualitative"]["chain_cycle_empty"] == 1, "전멸만 세어야 한다"
    assert metrics["budget_exceeded"]["count"] == 1, (
        "예산 초과는 절단 줄에서만 센다 — 두 계측이 독립이어야 "
        "'예산이 걸렸는가'와 '데이터가 남았는가'를 따로 물을 수 있다"
    )


def test_empty_chain_cycle_survives_the_parser_audit():
    token = log_metrics._PARSER_AUDIT_TOKENS["chain_cycle_empty"]
    strict = log_metrics._QUALITATIVE_MARKERS["chain_cycle_empty"]
    assert token in main.LOG_CHAIN_CYCLE_EMPTY
    assert len(token) < len(strict)


# ===== 로그 줄 구성 항등식 / 트레이스백 표본 (2026-08-05 §2-4 / Fix#4) =====


_TRACEBACK_BODY = [
    "Traceback (most recent call last):",
    r'  File "C:\a\httpx\_transports\default.py", line 101, in map_httpcore_exceptions',
    "    yield",
    "httpx.ReadTimeout: The read operation timed out",
]


def test_traceback_body_is_not_counted_as_a_human_line():
    """**08-05의 거짓 반증을 재현하는 테스트다.**

    종전 `human_lines`는 httpx가 아닌 모든 줄을 셌다. 그래서 트레이스백 본문
    (`  File "...", line 101, in ...`)이 "사람이 읽는 줄"이 됐고, 08-05에 21,176줄 중
    16,577줄(78%)이 그것이었다. 자동 리포트 §0은 그 값으로 `2026-08-04-p4`를 반증 판정했는데
    **실측 4,599줄은 예측치(<=6,500)를 통과했다** — 08-04 Fix#6의 성공이 실패로 보고된 것이다.
    """
    record = _emit("mahdi.main", "WARNING", "%s", "옵션 체인 폴링 실패: C01608A15")
    metrics = _parse(record, *_TRACEBACK_BODY)

    assert metrics["log_volume"]["human_lines"] == 1, "레코드 한 줄만 사람 로그다"
    assert metrics["log_volume"]["traceback_lines"] == 4


def test_log_line_composition_is_an_identity():
    """`총계 = httpx + 사람 + 트레이스백`. 리포트 §11이 이 셋을 나란히 찍는 근거다.

    08-05에 이 줄만 있었으면 트레이스백 폭증이 `human_lines` 반증으로 둔갑한 것이 즉시 보였다.
    """
    lines = [
        _emit("httpx", "INFO", "HTTP Request: GET %s \"HTTP/1.1 200 OK\"", "https://x/y"),
        _emit("mahdi.main", "WARNING", "%s", "옵션 체인 폴링 실패: C01608A15"),
        *_TRACEBACK_BODY,
    ]
    lv = _parse(*lines)["log_volume"]

    assert lv["httpx_lines"] + lv["human_lines"] + lv["traceback_lines"] == lv["total_lines"]
    assert (lv["httpx_lines"], lv["human_lines"], lv["traceback_lines"]) == (1, 1, 4)


def test_exception_summarised_without_a_traceback_still_counts_into_its_type():
    """트레이스백을 생략해도 **유형별 카운터는 살아 있어야 한다.**

    이것이 08-04 §2-1의 핵심 교훈이다: 로그 형태를 바꾼 커밋이 자기 계측을 껐고, 리포트는
    그것을 "개선"으로 표시했다. Fix#4는 트레이스백을 줄이는 fix이므로 **같은 함정 위를 지난다.**
    """
    line = _emit(
        "mahdi.main", "WARNING", main.LOG_KIS_FAILURE_TRACEBACK_OMITTED,
        "옵션 체인 폴링 실패: C01608A15", "The read operation timed out", "httpx.ReadTimeout", 42,
    )
    assert _parse(line)["qualitative"]["read_timeout"] == 1


def test_a_summarised_exception_is_not_double_counted_with_a_traceback_tail():
    """요약 마커와 트레이스백 마지막 줄이 겹치면 한 사건이 두 번 세어진다 — 겹치면 안 된다."""
    summarised = _emit(
        "mahdi.main", "WARNING", main.LOG_KIS_FAILURE_TRACEBACK_OMITTED,
        "옵션 체인 폴링 실패: X", "timed out", "httpx.ReadTimeout", 42,
    )
    tail = "httpx.ReadTimeout: The read operation timed out"

    assert _parse(summarised)["qualitative"]["read_timeout"] == 1
    assert _parse(tail)["qualitative"]["read_timeout"] == 1
    assert _parse(summarised, tail)["qualitative"]["read_timeout"] == 2, "각각 1건씩, 두 사건이다"


def test_traceback_omitted_marker_is_derived_from_the_emit_format():
    """규약 A — 파서의 마커가 emit 측 포맷에서 실제로 나오는 문자열이어야 한다."""
    rendered = main.LOG_KIS_FAILURE_TRACEBACK_OMITTED % ("msg", "exc", "httpx.ReadTimeout", 1)
    assert log_metrics._TRACEBACK_OMITTED_MARKER + "httpx.ReadTimeout" in rendered
