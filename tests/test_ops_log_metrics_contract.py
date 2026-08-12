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


# ===== 포맷의 **변형**도 계약이다 (2026-08-10 / 규약 A 확장) =====
#
# 08-10에 이 파일이 있는데도 사고가 났다. `LOG_CHAIN_CYCLE_BREAKDOWN`의 정규식이 `, 재시도함`을
# **`rows` 뒤**로 기대했는데 포맷은 **`밀림` 뒤**에 찍고 있었다(포맷 07-28 → 정규식 08-01,
# 옮겨 적을 때부터 틀렸다). 아래 기존 테스트들이 **전부 그 자리에 `""`만 넣었기 때문에**
# 10일간 안 걸렸다 — 포맷은 검사했지만 **포맷의 변형**은 안 했다.
#
# 발현: 08-10 15:15, 그날 유일한 재시도 사이클이 통째로 사라져 사이클 494→493이 되고
# 그 분이 「결손」으로 오분류됐다. **하필 그 분이 옵션체인 전멸(rows=0)이라 그날 가장 중요한
# 사이클이었다.**
#
# 구조적 교훈: **조건부 조각은 상황이 나쁠 때만 채워진다.** 그 자리를 안 재면 파서는
# 정확히 가장 나쁜 순간에만 눈이 먼다. 그래서 조합을 여기에 **명시적으로 선언**한다 —
# 포맷에 새 조건부 조각이 생기면 이 목록에 안 넣는 한 커버되지 않는다는 사실이 눈에 보이도록.

# (retried 조각, 분= 라벨) 조합 — `LOG_CHAIN_CYCLE_BREAKDOWN`이 실제로 낼 수 있는 모든 모양.
_CYCLE_BREAKDOWN_VARIANTS = [
    ("", "10:11"),            # 평상시
    (", 재시도함", "10:11"),   # 전체 재시도가 일어난 사이클 — 08-10에 여기서 실명했다
    ("", None),               # 08-07 이전 로그(라벨 없음)
    (", 재시도함", None),      # 08-07 이전 + 재시도
]


@pytest.mark.parametrize("retried,minute", _CYCLE_BREAKDOWN_VARIANTS)
def test_chain_cycle_breakdown_parses_in_every_variant(retried, minute):
    """포맷이 낼 수 있는 **모든 모양**이 파서를 통과해야 한다."""
    body = main.LOG_CHAIN_CYCLE_BREAKDOWN % (
        33.90, 0.12, 0.05, 0.31, 20, 0.0, retried, "3건", minute or "",
    )
    if minute is None:  # 구 로그에는 ` 분=` 꼬리 자체가 없다
        body = body[: body.rindex(" 분=")]
    cycles = _parse(f"{_TS} INFO:mahdi.main:{body}")["cycles"]

    assert cycles["count"] == 1, f"retried={retried!r} minute={minute!r}에서 파서가 눈이 멀었다"
    assert cycles["rows_distribution"] == {20: 1}
    assert cycles["duplicate_poll_minutes"]["labelled"] == (0 if minute is None else 1)


def test_retried_cycle_is_not_reported_as_a_missing_minute():
    """08-10 15:15 재현 — 재시도 사이클이 안 읽히면 그 분이 「결손」으로 둔갑한다.

    그날 리포트는 이 분을 **인프라 결손**으로 올렸다. 실제로는 사이클이 완주했고(전멸이지만),
    결손 축이 재야 하는 것은 *사이클이 돌았는가*이지 *행이 남았는가*가 아니다
    (후자는 DB 축 `chain_minute_coverage`의 몫이다).
    """
    def cycle(rest, rows, retried, minute):
        return _emit("mahdi.main", "INFO", main.LOG_CHAIN_CYCLE_BREAKDOWN,
                     rest, 0.1, 0.0, 0.0, rows, 0.0, retried, "0건", minute)

    cycles = _parse(
        cycle(20.0, 20, "", "15:14"),
        cycle(49.56, 0, ", 재시도함", "15:15"),   # 그날 실제로 전멸한 사이클
        cycle(20.0, 20, "", "15:16"),
    )["cycles"]

    assert cycles["count"] == 3
    assert cycles["missing"]["count"] == 0, "재시도 사이클이 결손으로 둔갑했다"
    assert cycles["missing"]["infra_count"] == 0


def test_missing_minutes_use_the_label_axis_not_the_derived_start():
    """2026-08-10 — 파생 start(종료 − 소요 합)는 반올림으로 분 경계를 몇 ms 넘어간다.

    실측: 11:09 사이클의 파생 시작이 11:08:59.996(4ms 이르다)이라 11:08에 귀속됐고,
    11:09가 허위 결손으로 잡혔다. 아래는 그 산술을 그대로 재현한다 —
    소요 합 33.45초, 종료 11:09:33.446 → 파생 11:08:59.996.
    """
    lines = [
        "2026-08-05 11:08:19,474 INFO:mahdi.main:" + main.LOG_CHAIN_CYCLE_BREAKDOWN % (
            19.33, 0.09, 0.01, 0.0, 20, 0.0, "", "0건", "11:08"),
        "2026-08-05 11:09:33,446 INFO:mahdi.main:" + main.LOG_CHAIN_CYCLE_BREAKDOWN % (
            33.36, 0.06, 0.03, 0.0, 10, 0.0, "", "0건", "11:09"),
    ]
    cycles = _parse(*lines)["cycles"]

    assert cycles["count"] == 2
    assert cycles["missing"]["list"] == [], "파생 start 반올림이 허위 결손을 만들었다"


def test_legacy_logs_without_labels_still_fall_back_to_derived_start():
    """08-07 이전 로그에는 `분=`이 없다 — 그 날들을 재집계할 때 결손 축이 죽으면 안 된다."""
    legacy = [
        "2026-08-05 10:10:19,000 INFO:mahdi.main:옵션체인 사이클 소요 분해: "
        "REST수집 19.00초 + DB적재 0.00초 + 상태기록 0.00초 + 기타 0.00초 "
        "(rows=20, 밀림=0.0초, 타폴러동시호출추정=0건)",
        # 10:11이 통째로 없다 → 진짜 결손 1분
        "2026-08-05 10:12:19,000 INFO:mahdi.main:옵션체인 사이클 소요 분해: "
        "REST수집 19.00초 + DB적재 0.00초 + 상태기록 0.00초 + 기타 0.00초 "
        "(rows=20, 밀림=0.0초, 타폴러동시호출추정=0건)",
    ]
    cycles = _parse(*legacy)["cycles"]

    assert cycles["duplicate_poll_minutes"]["labelled"] == 0
    assert cycles["missing"]["list"] == ["10:11"], "라벨 없는 로그에서 폴백이 안 돈다"


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
    line = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 6, 14, "weekly_mon", "아니오")
    budget = _parse(line)["budget_exceeded"]
    assert budget["count"] == 1
    assert budget["skipped_legs_total"] == 6


# ===== 2026-08-11 Fix#1/#2 — 조기 포기와 컷 귀속 =====


def test_timeout_abort_line_is_parsed_and_counted_apart_from_budget():
    """조기 포기와 예산 초과는 **원인이 다르므로 지표도 갈려야 한다.**

    08-11 15:01~15:22의 22분은 "우리가 느렸다"가 아니라 "KIS가 4초 천장에 닿았다"였는데,
    종전 로그는 둘을 같은 줄로 냈다.
    """
    line = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_TIMEOUT_ABORT, 3, 17, 0, "regular,weekly_mon", "예")
    metrics = _parse(line)
    abort = metrics["timeout_abort"]
    assert abort["count"] == 1
    assert abort["skipped_legs_total"] == 17
    assert abort["minutes"] == ["10:11"]
    # 예산 초과로 새어 들어가면 안 된다.
    assert metrics["budget_exceeded"]["count"] == 0


def test_failure_budget_abort_is_counted_apart_from_consecutive_timeouts():
    """고도화 A — 두 조기 종료는 **다른 병**이다.

    연속 타임아웃은 KIS가 천장에 닿아 전멸하는 패턴이고, 누적 실패는 성공/실패가 섞여 절반이
    죽는 패턴이다. 08-11 14시대가 후자였다(예산 초과 20건 / 전멸 1건) — 한 지표로 세면
    "무엇이 이 분을 얇게 만들었는가"에 답할 수 없다.
    """
    line = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_FAILURE_BUDGET, 6, 8, 12.5, 6, "weekly_mon", "아니오")
    metrics = _parse(line)

    assert metrics["failure_budget_abort"]["count"] == 1
    assert metrics["failure_budget_abort"]["skipped_legs_total"] == 8
    assert metrics["failure_budget_abort"]["priority_cut_minutes"] == 0
    # 다른 두 지표로 새어 들어가면 안 된다.
    assert metrics["timeout_abort"]["count"] == 0
    assert metrics["budget_exceeded"]["count"] == 0


def test_cut_books_label_tells_whether_the_monthly_book_was_reached():
    """Fix#2 — 먼슬리가 컷당한 분을 지표로 센다. 08-06엔 이 값을 사람이 손으로 셌다."""
    weekly_only = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 4, 16, "weekly_mon", "아니오")
    reached_monthly = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 12, 8, "regular,weekly_mon", "예")

    assert _parse(weekly_only)["budget_exceeded"]["priority_cut_minutes"] == 0
    assert _parse(reached_monthly)["budget_exceeded"]["priority_cut_minutes"] == 1
    assert _parse(weekly_only, reached_monthly)["budget_exceeded"]["labelled"] == 2


def test_unlabelled_old_logs_report_none_not_zero():
    """규약 C — 라벨이 없는 08-10 이전 로그에서 0을 내면 "컷이 없었다"는 거짓말이 된다."""
    legacy = (
        f"{_TS} WARNING:mahdi.main:옵션체인 수집 예산(50초) 초과 — "
        "남은 6레그를 포기하고 14레그로 이번 분을 마감합니다 (다음 분 사이클을 정시에 시작하기 위함, §2-6/Fix#8)"
    )
    budget = _parse(legacy)["budget_exceeded"]
    assert budget["count"] == 1  # 줄 자체는 여전히 읽힌다(하위호환)
    assert budget["priority_cut_minutes"] is None
    assert budget["labelled"] == 0


# ===== 2026-08-11 Fix#7 — 밀림 계측 감사가 매일 거짓 ⚠를 냈다 =====


def test_other_pollers_overrun_does_not_trip_the_option_chain_audit():
    """08-11 실사고 재현 — 만기유동성 밀림 1건이 `overrun` 감사를 «파서 0 / 실재 1»로 띄웠다.

    **파서는 옳았다.** `overrun`은 설계상 옵션체인 전용이고, 그 1건은 다른 폴러 것이다.
    틀린 것은 느슨 토큰(`"스케줄이"`)이었고 여섯 폴러가 같은 문장을 쓴다.
    """
    other = (
        f"{_TS} WARNING:mahdi.main:만기 유동성 폴링 사이클이 주기(60초)를 초과해 "
        "스케줄이 1.0초 밀렸습니다 — 위상 격자의 다음 틱까지 59.0초 대기"
    )
    metrics = _parse(other)

    assert metrics["overrun"]["count"] == 0, "옵션체인 전용 지표에 다른 폴러가 새어 들어가면 안 된다"
    # 그런데 그 사건이 사라지면 안 된다 — 종전에는 오경보로만 존재했다.
    assert metrics["overrun_by_poller"]["만기 유동성 폴링"]["count"] == 1
    assert metrics["overrun_by_poller"]["만기 유동성 폴링"]["max_seconds"] == 1.0
    # 감사가 조용해야 한다(이것이 이 fix의 주장 지표다).
    audit = metrics.get("parser_audit") or {}
    assert "overrun" not in audit, f"거짓 계측 감사가 남아 있다: {audit}"


def test_option_chain_overrun_still_counts_in_both_places():
    """옵션체인 밀림은 종전 지표와 새 분해에 **둘 다** 잡혀야 한다(하위호환)."""
    line = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_OVERRUN, 60.0, 3.5, 56.5, 20.0, 0.1, 18, "", "0")
    metrics = _parse(line)

    assert metrics["overrun"]["count"] == 1
    assert metrics["overrun"]["max_seconds"] == 3.5
    assert metrics["overrun_by_poller"]["옵션 체인 폴링"]["count"] == 1


def test_log_metrics_priority_series_matches_main():
    """`log_metrics`는 `mahdi.main`을 임포트하지 않는다(순수 파서). 복제한 상수가 갈라지면
    `priority_cut_minutes`가 조용히 항상 0이 된다 — 그것을 여기서 막는다."""
    assert log_metrics.PRIORITY_SERIES_LABEL == main.OPTION_CHAIN_PRIORITY_SERIES


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
    truncated = _emit("mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 3, 17, "weekly_mon", "아니오")
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


# ===== 0행 분의 원인 분해 (2026-08-10) =====
#
# DB 축의 `zero_row_count` 하나에 세 원인이 만난다. 08-10에 그 값이 1이 되어 08-07 Fix#3의
# 불변식이 반증으로 찍혔는데, 그 1건은 그 fix와 무관한 「수집 전멸」이었다.


def _cycle_line(minute: str, rows: int, retried: str = "") -> str:
    return _emit("mahdi.main", "INFO", main.LOG_CHAIN_CYCLE_BREAKDOWN,
                 20.0, 0.1, 0.0, 0.0, rows, 0.0, retried, "0건", minute)


def test_log_axis_exposes_cycles_that_ran_with_zero_rows():
    """DB 축과 대조하려면 로그 축이 「돌았는데 0행」을 따로 내놓아야 한다."""
    cycles = _parse(
        _cycle_line("15:14", 20),
        _cycle_line("15:15", 0, ", 재시도함"),
        _cycle_line("15:16", 20),
    )["cycles"]

    assert cycles["zero_row_minutes"] == ["15:15"]
    assert cycles["minutes_with_cycle"] == ["15:14", "15:15", "15:16"]


def test_zero_row_cause_split_separates_a_wiped_collection_from_a_missing_cycle():
    """08-10 15:15 재현 — 「수집 전멸」과 「사이클 없음」은 다른 사건이다."""
    cycles = _parse(
        _cycle_line("15:14", 20),
        _cycle_line("15:15", 0, ", 재시도함"),   # 돌았는데 0행
        # 15:16은 사이클 자체가 없다
        _cycle_line("15:17", 20),
    )["cycles"]
    coverage = {"available": True, "zero_row_minutes": ["15:15", "15:16"]}

    causes = db_metrics.attribute_zero_row_causes(coverage, cycles)

    assert causes["collection_wiped"] == ["15:15"]
    assert causes["no_cycle"] == ["15:16"]
    assert causes["written_elsewhere"] == []
    assert causes["labelled"] is True


def test_zero_row_cause_split_flags_rows_written_to_a_neighbour_minute():
    """사이클도 돌고 행도 있었는데 라벨이 옆 분으로 간 경우 — 08-07 Fix#3이 겨냥한 원인이다."""
    cycles = _parse(_cycle_line("15:14", 20), _cycle_line("15:15", 20))["cycles"]
    coverage = {"available": True, "zero_row_minutes": ["15:15"]}

    causes = db_metrics.attribute_zero_row_causes(coverage, cycles)

    assert causes["written_elsewhere"] == ["15:15"]
    assert causes["collection_wiped"] == []


def test_zero_row_cause_split_returns_none_without_both_axes():
    """지어내지 않는다 — 한쪽이라도 없으면 분해를 내지 않는다."""
    cycles = _parse(_cycle_line("15:14", 20))["cycles"]
    assert db_metrics.attribute_zero_row_causes(None, cycles) is None
    assert db_metrics.attribute_zero_row_causes({"available": False}, cycles) is None
    assert db_metrics.attribute_zero_row_causes({"available": True, "zero_row_minutes": []}, None) is None


def test_zero_row_cause_split_marks_unlabelled_legacy_days():
    """08-07 이전 로그는 파생 start 기반이라 정확도가 낮다 — 그 사실이 값에 남아야 한다."""
    legacy = (
        f"{_TS} INFO:mahdi.main:옵션체인 사이클 소요 분해: "
        "REST수집 19.00초 + DB적재 0.00초 + 상태기록 0.00초 + 기타 0.00초 "
        "(rows=0, 밀림=0.0초, 타폴러동시호출추정=0건)"
    )
    cycles = _parse(legacy)["cycles"]
    # 파생 start = 10:11:12.345 − 19.0초 = 10:10:53 → "10:10". 라벨이 없으면 이렇게 **한 분
    # 앞으로 밀릴 수 있다** — 그것이 `labelled=False`가 경고하는 부정확성이다.
    assert cycles["zero_row_minutes"] == ["10:10"]
    causes = db_metrics.attribute_zero_row_causes(
        {"available": True, "zero_row_minutes": ["10:10"]}, cycles
    )

    assert causes["collection_wiped"] == ["10:10"], "라벨이 없어도 파생 start로 분해는 된다"
    assert causes["labelled"] is False, "정확도가 낮다는 사실이 값에 남아야 한다"


# ===== §14 「GEX 입력이 없던 분」 · §5 북 수 정규화 (2026-08-10) =====


def test_signal_reach_renders_the_gex_input_missing_row():
    """08-10 15:15의 전멸은 §14 어디에도 안 보였다 — 이제 불변식 줄로 보인다."""
    from mahdi.ops import report

    rendered = "\n".join(report._render_signal_reach({
        "signal_reach": {
            "available": True, "decisions": 494, "member_count_max": 4,
            "gamma_flip_count": 0, "gamma_flip_pct": 0.0,
            "chain_leg_median": 10.0, "chain_leg_max": 10,
            "chain_age_seconds_median": 72.0, "chain_age_seconds_max": 190.0,
            "chain_leg_over_design_minutes": 0, "chain_leg_excess_max": 0,
            "gex_input_missing_minutes": 1,
            "warnings": [], "notes": [],
        }
    }))
    assert "GEX 입력이 없던 분" in rendered
    assert "1분" in rendered


def test_rest_table_shows_per_book_calls_and_warns_when_the_book_count_moved():
    """08-10 재현 — 북 3→2로 옵션체인 호출이 준 것을 fix 효과로 읽을 뻔했다."""
    from mahdi.ops import report

    metrics = {"rest": {"total_calls": 7488, "span_seconds": 29640, "by_group": {"옵션체인": 7488}}}
    today = {"book_coverage": [{"series": "regular"}, {"series": "weekly_mon"}]}
    yesterday = {"db": {"book_coverage": [{"series": "regular"}, {"series": "w1"}, {"series": "w2"}]}}

    rendered = "\n".join(report._render_rest(metrics, today, yesterday))

    assert "북당(옵션체인만)" in rendered
    assert "3,744" in rendered, "7,488 / 북 2개"
    assert "바뀌었다" in rendered


def test_rest_table_says_totals_are_comparable_when_the_book_count_held():
    from mahdi.ops import report

    metrics = {"rest": {"total_calls": 100, "span_seconds": 600, "by_group": {"옵션체인": 100}}}
    books = {"book_coverage": [{"series": "regular"}, {"series": "weekly_mon"}]}

    rendered = "\n".join(report._render_rest(metrics, books, {"db": books}))

    assert "전일과 같다" in rendered


def test_rest_table_omits_the_per_book_column_without_db_metrics():
    """DB 집계가 없는 날(`--no-db`)에는 북 수를 모른다 — 지어내지 않는다."""
    from mahdi.ops import report

    metrics = {"rest": {"total_calls": 100, "span_seconds": 600, "by_group": {"옵션체인": 100}}}
    rendered = "\n".join(report._render_rest(metrics))

    assert "북당" not in rendered


# ===== 2026-08-12 Fix#5 — 「먼슬리가 잘렸다」와 「먼슬리가 **먼저** 잘렸다」는 다른 질문이다 =====
#
# 08-12에 `priority_cut_minutes = 2`가 불변식 위반처럼 보고됐다. 실측하니 둘 다(12:49:53 /
# 13:51:53) **홀수분의 꼬리 컷**이었다 — 그 분에는 위클리가 애초에 due가 아니라 사이클 전체가
# 먼슬리였고, 50초 예산 끝에서 남은 2~3레그가 잘린 것이다. **자를 것이 먼슬리밖에 없는 분에서
# 먼슬리를 자르는 것은 순서 문제가 아니다.** 규약 G가 막는 것과 같은 형태의 오류였다.


def test_priority_violation_label_separates_the_ordering_breach_from_a_tail_cut():
    """불변식은 `priority_before_others_minutes`이고, `priority_cut_minutes`는 참고값이다."""
    tail_cut = _emit(
        "mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 3, 7, "regular", "아니오"
    )
    ordering_breach = _emit(
        "mahdi.main", "WARNING", main.LOG_CHAIN_BUDGET_EXCEEDED, 50.0, 12, 8,
        "regular,weekly_mon", "예",
    )

    only_tail = _parse(tail_cut)["budget_exceeded"]
    assert only_tail["priority_cut_minutes"] == 1, "먼슬리가 잘린 것은 사실이다(참고값)"
    assert only_tail["priority_before_others_minutes"] == 0, (
        "홀수분 꼬리 컷을 위반으로 세면 08-12의 오독이 재현된다"
    )

    both = _parse(tail_cut, ordering_breach)["budget_exceeded"]
    assert both["priority_cut_minutes"] == 2
    assert both["priority_before_others_minutes"] == 1


def test_the_violation_label_is_read_from_every_cut_line():
    """세 컷 로그(예산/연속 타임아웃/실패 예산)가 **같은 라벨**을 실어야 한다."""
    timeout = _emit(
        "mahdi.main", "WARNING", main.LOG_CHAIN_TIMEOUT_ABORT, 3, 17, 0, "regular,weekly_mon", "예"
    )
    failure = _emit(
        "mahdi.main", "WARNING", main.LOG_CHAIN_FAILURE_BUDGET, 6, 8, 12.5, 6, "regular", "예"
    )
    assert _parse(timeout)["timeout_abort"]["priority_before_others_minutes"] == 1
    assert _parse(failure)["failure_budget_abort"]["priority_before_others_minutes"] == 1


def test_logs_without_the_violation_label_report_none_not_zero():
    """규약 C — 08-11 로그에는 이 라벨이 없다. 그때 0을 내면 「위반이 없었다」는 거짓말이 된다."""
    legacy = (
        f"{_TS} WARNING:mahdi.main:옵션체인 수집 예산(50초) 초과 — "
        "남은 6레그를 포기하고 14레그로 이번 분을 마감합니다 "
        "(다음 분 사이클을 정시에 시작하기 위함, §2-6/Fix#8) 컷당한북=regular"
    )
    budget = _parse(legacy)["budget_exceeded"]
    assert budget["priority_cut_minutes"] == 1  # 구 라벨은 여전히 읽힌다
    assert budget["priority_before_others_minutes"] is None
