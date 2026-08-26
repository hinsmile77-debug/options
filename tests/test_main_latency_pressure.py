"""`p50 ÷ read timeout`을 **창 집계 자리에서** 판정한다 (2026-08-26 §1-12 / P1-4).

08-26에 그 비율이 13:34 **0.74** → 13:44:45 **0.89**(경고선) → 14:04 **1.01**(제한시간 추월)로
갔고, 14:01~15:25는 **85분 연속 rows=0**이었다. 프로그램은 그 값을 창마다 계산할 재료를 다
갖고 있으면서 **문턱을 갖고 있지 않았다** — 사람이 15:46 지표로 표를 만들어야만 보였다.

⛔ **판정만 하고 아무것도 안 바꾼다.** 지연을 보고 폴링을 바꾸면 되먹임이 생기고, 2026-07-08에
페이서를 나눴다가 500 폭주로 203분을 잃었다.
"""

from __future__ import annotations

import logging

import pytest

from mahdi import main


def _stats(p50: float, *, n: int = 45, timeout: float | None = 4.0, censored: int | None = 32):
    return {
        main.REST_LATENCY_PRESSURE_ENDPOINT: {
            "n": n, "p50": p50, "p95": p50, "p99": p50, "max": p50,
            "timeout": timeout, "censored": censored,
        }
    }


def _run(stats, grade=None, at=None):
    return main._log_rest_latency_pressure(stats, grade, at)


def test_a_calm_window_says_nothing():
    """장전·장후에 매 5분 빈 줄을 찍지 않는다 — 08-26 오전의 0.01배가 그 구간이다."""
    grade, _ = _run(_stats(0.04))
    assert grade is None


def test_the_warning_line_appears_at_the_0_8_crossing(caplog):
    """13:44:45의 **0.89**가 이 자리다. 그 줄이 있었다면 회복까지 102분을 예고로 가졌다."""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, _ = _run(_stats(3.56))

    assert grade == "경고"
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert "지연 경고선 돌파" in record.getMessage()
    assert "0.89배" in record.getMessage()


def test_crossing_the_timeout_itself_is_an_error(caplog):
    """14:04의 **1.01배** — 중앙값 호출이 타임아웃을 넘겼다. 수집은 느려진 것이 아니라 비었다."""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, _ = _run(_stats(4.04))

    assert grade == "위험"
    assert caplog.records[-1].levelno == logging.ERROR


def test_the_line_carries_the_call_count(caplog):
    """08-26에 창당 호출이 90건대 → 45건으로 반토막 났다.

    그것 없이는 「검열 32건(71%)」이 **무엇의** 71%인지 모른다.
    """
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        _run(_stats(3.56, n=45, censored=32))

    body = caplog.records[-1].getMessage()
    assert "검열 32건(71%)" in body
    assert "호출 45건" in body


def test_an_uncounted_censoring_is_not_printed_as_zero(caplog):
    """규약 C — **「안 셌다」와 「0건」은 다른 사건이다.**"""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        _run(_stats(3.56, censored=None))

    assert "검열 셈 없음" in caplog.records[-1].getMessage()


def test_a_grade_change_is_never_suppressed(caplog):
    """경고 → 위험 전환은 **그 자체가 사건**이다. 억제 창 안이어도 찍는다."""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, at = _run(_stats(3.56))
        grade2, _ = _run(_stats(4.04), grade, at)

    assert (grade, grade2) == ("경고", "위험")
    assert len(caplog.records) == 2


def test_the_same_grade_is_reprinted_once_per_window(monkeypatch, caplog):
    """08-26처럼 22창 연속이어도 창당 한 줄이다 — 대가 축의 상한은 하루 30줄이다."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, at = _run(_stats(3.56))
        clock["t"] += 60.0                       # 창보다 짧다 — 억제된다
        grade, at = _run(_stats(3.60), grade, at)
        clock["t"] += main.REST_LATENCY_PRESSURE_REPEAT_SECONDS
        grade, at = _run(_stats(3.62), grade, at)

    assert len(caplog.records) == 2


def test_dropping_back_to_safe_releases_the_grade(caplog):
    """안전한 창으로 내려오면 등급을 푼다 — 다음 상승이 다시 「전환」이 되어야 한다."""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, at = _run(_stats(3.56))
        grade, at = _run(_stats(0.5), grade, at)
        assert grade is None
        grade, _ = _run(_stats(3.56), grade, at)

    assert grade == "경고"
    assert len(caplog.records) == 2


def test_a_window_without_the_chain_endpoint_is_silent(caplog):
    """그 창에 옵션체인 호출이 없으면 판정 자체가 성립하지 않는다 — 0으로 찍지 않는다."""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, _ = _run({"inquire-balance": {"n": 3, "p50": 9.0, "p95": 9.0, "p99": 9.0,
                                             "max": 9.0, "timeout": 10.0, "censored": 0}})

    assert grade is None
    assert not caplog.records


def test_a_missing_timeout_does_not_fabricate_a_ratio(caplog):
    """타임아웃 값을 못 구하면 비율도 없다 — 없는 것을 만들어 내지 않는다."""
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        grade, _ = _run(_stats(3.56, timeout=None))

    assert grade is None
    assert not caplog.records


@pytest.mark.parametrize("ratio_source", [main.REST_LATENCY_PRESSURE_WARN_RATIO])
def test_the_threshold_is_the_one_from_08_14(ratio_source):
    """**임계를 새로 정하지 않았다** — 0.8은 08-14 §2-2 / Fix#3이 정한 값이다."""
    assert ratio_source == 0.8
