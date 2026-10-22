"""2026-10-22 제4부 Fix A — 잔고 응답에 output2가 없을 때 **KIS가 뭐라고 했는지**를 남긴다.

10-13–10-22 여드레 동안 잔고 폴링이 전량 실패했는데 예외 문구가 「output2가 없습니다」뿐이라
원인을 못 갈랐다. 이 파일은 세 가지를 고정한다:

  1. 예외 메시지 괄호 안에 `rt_cd`/`msg_cd`/`msg1`/응답 키가 실린다.
  2. **판정 무변경** — 앞 문장·실패 조건·정상 응답 파싱은 한 글자도 안 바뀐다.
  3. 쓰는 쪽(예외 메시지 + `main.LOG_BALANCE_POLL_FAILED`)과 세는 쪽(`log_metrics`)의 계약 —
     새 마커는 1로, 기존 축(`balance_poll_failed` · `failures_by_cause` · `kis_error_response`)은
     전과 같은 값으로 센다.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from mahdi import main
from mahdi.execution import account_tracker
from mahdi.execution.account_tracker import parse_balance_response
from mahdi.ops import log_metrics

_TS = datetime(2026, 10, 22, 10, 0)
_LOG_TS = "2026-10-22 10:04:28,961"
_TARGET = date(2026, 10, 22)
_LEGACY_MESSAGE = "get_balance() 응답에 output2가 없습니다"

_REJECTED = {
    "rt_cd": "1",
    "msg_cd": "OPSQ2001",
    "msg1": "ERROR : INPUT_FIELD_NAME MGNA_DVSN",
}


def _raised_message(response: dict) -> str:
    with pytest.raises(ValueError) as info:
        parse_balance_response(response, _TS)
    return str(info.value)


def test_missing_output2_message_carries_the_kis_code():
    message = _raised_message(_REJECTED)
    assert "rt_cd=1" in message
    assert "msg_cd=OPSQ2001" in message
    assert "msg1=ERROR : INPUT_FIELD_NAME MGNA_DVSN" in message
    assert "키=msg1,msg_cd,rt_cd" in message


def test_the_legacy_sentence_is_kept_as_the_head_of_the_message():
    # 기존 문구를 부분문자열로 읽는 사람·도구가 있다 — 괄호 안만 붙였다.
    assert _raised_message(_REJECTED).startswith(_LEGACY_MESSAGE + " (")


def test_missing_fields_are_written_as_none_not_invented():
    message = _raised_message({"output1": []})
    assert "rt_cd=없음 · msg_cd=없음 · msg1=없음 · 키=output1" in message


def test_empty_response_says_it_had_no_keys():
    assert "키=없음" in _raised_message({})


def test_quoted_rt_cd_token_is_never_emitted():
    # `"rt_cd"`는 log_metrics._KIS_ERROR_BODY_TOKEN이다 — 실리면 파서 감사가 거짓으로 오른다.
    assert log_metrics._KIS_ERROR_BODY_TOKEN not in _raised_message(_REJECTED)


def test_judgement_unchanged_a_response_with_output2_still_parses_the_same():
    snapshot = parse_balance_response(
        {"rt_cd": "0", "msg_cd": "MCA00000", "output1": [], "output2": {"prsm_dpast": "1000"}}, _TS
    )
    assert snapshot.prsm_dpast == 1000.0
    assert snapshot.same_direction_buy_count == 0
    assert snapshot.same_direction_sell_count == 0


def test_judgement_unchanged_empty_output2_dict_is_still_not_a_failure():
    # 실패 조건은 `output2 is None` 그대로다 — 빈 dict는 전처럼 0으로 파싱된다.
    assert parse_balance_response({"output2": {}}, _TS).prsm_dpast == 0.0


def _failure_record(exception_message: str) -> list[str]:
    """`poll_account_balance_cycle`이 실제로 남기는 경고 한 줄 + 트레이스백."""
    head = f"{_LOG_TS} WARNING:mahdi.main:" + main.LOG_BALANCE_POLL_FAILED % (
        "ValueError", "ValueError", "1.00배", "없음",
    )
    return [
        head,
        "Traceback (most recent call last):",
        '  File "mahdi/main.py", line 5063, in poll_account_balance_cycle',
        "    snapshot = parse_balance_response(response, poll_time)",
        f"ValueError: {exception_message}",
    ]


def test_contract_new_marker_counts_one_per_failure():
    message = _raised_message(_REJECTED)
    metrics = log_metrics.parse_day(_failure_record(message) * 2, _TARGET)
    assert metrics["qualitative"]["balance_poll_failed"] == 2
    assert metrics["qualitative"]["balance_output2_missing_coded"] == 2


def test_contract_marker_is_a_substring_of_the_emitted_format():
    marker = log_metrics._QUALITATIVE_MARKERS["balance_output2_missing_coded"]
    assert marker in account_tracker.LOG_BALANCE_OUTPUT2_MISSING


def test_judgement_unchanged_existing_axes_read_the_same_as_the_legacy_message():
    new = log_metrics.parse_day(_failure_record(_raised_message(_REJECTED)), _TARGET)
    old = log_metrics.parse_day(_failure_record(_LEGACY_MESSAGE), _TARGET)
    for key in ("balance_poll_failed", "kis_error_response"):
        assert new["qualitative"].get(key, 0) == old["qualitative"].get(key, 0), key
    assert new["failures"] == old["failures"]
    assert new["failures_by_cause"] == old["failures_by_cause"]


def test_legacy_log_prints_zero_not_missing():
    # 규약 C — 옛 문구의 날도 키가 실려야 「안 실렸다」와 「안 셌다」가 갈린다.
    metrics = log_metrics.parse_day(_failure_record(_LEGACY_MESSAGE), _TARGET)
    assert metrics["qualitative"]["balance_output2_missing_coded"] == 0
    assert metrics["qualitative"]["balance_poll_failed"] == 1
