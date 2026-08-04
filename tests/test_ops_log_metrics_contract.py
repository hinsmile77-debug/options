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

from mahdi.broker import rest_client
from mahdi.ops import log_metrics

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
