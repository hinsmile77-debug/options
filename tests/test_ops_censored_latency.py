"""검열된 p50은 **중앙값이 아니라 하한**이다 (2026-08-23 / 08-21 §1-14 · §5 고도화#1).

## 08-21에 네 회차가 같은 숫자를 잘못 읽었다

그날 지연창 98개 중 상당수가 p50 **4.03~4.05초**를 냈다. `inquire-price`의 read timeout이
**4.0초**이므로 그 값은 실제 중앙값이 아니라 **타임아웃 벽에 눌린 값**이다(우측 검열) —
4초를 넘는 호출은 전부 4.00~4.06으로 기록되므로 그 통로로는 상한을 알 수 없다.

그런데 리포트는 그것을 「4.03초」라고 평범하게 인쇄했고, 그 위에서 08-20·08-21 세 회차가
**「read timeout 4.0 → 6.0초」** 를 손익표에 올렸다. 그 처방은 *"조금만 더 기다리면 온다"*를
전제하는데, **몇 %가 더 들어오는지 계산할 재료가 애초에 없었다.**

## 재료는 이미 로그에 있었다

`rest_client._log_if_slow`는 타임아웃으로 끝난 호출을 **임계와 무관하게 반드시 남긴다**
(08-05 Fix#4). 슬로우 임계 3.0초 < read timeout 4.0초이므로 검열된 호출은 전부 그 줄에 있다.
새 계측을 만들지 않고 그 줄을 창에 붙이기만 하면 된다.
"""

from __future__ import annotations

from datetime import date

from mahdi.broker import rest_client
from mahdi.ops import log_metrics, report

_TARGET = date(2026, 8, 21)
_ENDPOINT = "inquire-price"


def _latency_line(hhmmss: str, n: int, p50: float) -> str:
    from mahdi import main

    body = f"{_ENDPOINT}={n}건 {p50:.2f}/4.05/4.06/4.06초"
    return f"2026-08-21 {hhmmss},000 INFO:mahdi.main:" + (main.LOG_REST_LATENCY % (300.0, body))


def _slow_line(hhmmss: str, http_seconds: float) -> str:
    return (
        f"2026-08-21 {hhmmss},000 INFO:mahdi.broker.rest_client:"
        + (rest_client.LOG_SLOW_CALL % (http_seconds + 0.5, 0.5, http_seconds, 1.0, "GET", _ENDPOINT))
    )


def _parse(*lines: str) -> dict:
    return log_metrics.parse_day(list(lines), _TARGET)["rest_latency"]


def test_a_window_pressed_against_the_timeout_is_listed_as_censored():
    """p50 4.03 / timeout 4.0 — **08-21의 그 값 그대로**다."""
    lat = _parse(_latency_line("13:05:00", 60, 4.03))
    assert lat["censored_window_count"] == 1
    window = lat["censored_windows"][0]
    assert window["at"].startswith("13:05") and window["read_timeout"] == 4.0


def test_a_healthy_window_is_untouched():
    """검열이 없는 날 표기가 바뀌지 않는다 — 이 fix의 **대가**가 그것이다."""
    lat = _parse(_latency_line("08:05:00", 60, 0.02))
    assert lat["censored_window_count"] == 0
    assert "✅" in "".join(report._render_censored_windows(lat))


def test_the_censoring_ratio_comes_from_the_slow_call_lines():
    """새 계측을 만들지 않는다 — 타임아웃 호출은 이미 `LOG_SLOW_CALL`에 전부 있다."""
    lines = [
        _latency_line("13:05:00", 4, 4.03),
        *[_slow_line("13:0%d:30" % i, 4.02) for i in range(1, 4)],
    ]
    window = _parse(*lines)["censored_windows"][0]
    assert window["censored"] == 3
    assert window["censored_pct"] == 75.0


def test_calls_below_the_timeout_are_not_counted_as_censored():
    """3.5초는 느린 호출이지만 **벽에 안 닿았다** — 세면 검열 비율이 부풀려진다."""
    lines = [_latency_line("13:05:00", 4, 4.03), _slow_line("13:02:30", 3.5)]
    assert _parse(*lines)["censored_windows"][0]["censored"] == 0


def test_a_call_from_another_window_does_not_leak_in():
    """창은 `[직전 창 끝, 이 창 끝)`이다 — 경계가 새면 비율이 통째로 틀린다."""
    lines = [
        _latency_line("13:05:00", 10, 4.03),
        _latency_line("13:10:00", 10, 4.03),
        _slow_line("13:07:30", 4.02),
    ]
    windows = {w["at"][:5]: w["censored"] for w in _parse(*lines)["censored_windows"]}
    assert windows["13:05"] == 0 and windows["13:10"] == 1


def test_a_day_without_slow_call_lines_says_it_did_not_measure():
    """「검열 0%」와 「안 셌다」는 다르다(규약 C) — 후자를 0으로 인쇄하면 깨끗한 날로 읽힌다."""
    lat = _parse(_latency_line("13:05:00", 60, 4.03))
    assert lat["censored_measured"] is False
    assert "못 셌다" in "".join(report._render_censored_windows(lat))


def test_the_report_prints_a_floor_and_says_the_lever_cannot_be_priced():
    """**`≥`가 말하는 것**: 이 값으로는 「6초로 늘리면 몇 %가 더 들어오는가」를 못 구한다."""
    lines = [_latency_line("13:05:00", 4, 4.03), *[_slow_line("13:0%d:30" % i, 4.02) for i in (1, 2)]]
    rendered = "".join(report._render_censored_windows(_parse(*lines)))
    assert "≥4.0초" in rendered
    assert "계산할 수 없다" in rendered
    assert "레버보다 계측이 먼저다" in rendered


def test_the_slow_call_threshold_is_below_the_read_timeout():
    """**이 방법이 성립하는 유일한 근거.** 임계가 타임아웃 위로 올라가면 검열이 조용히 안 세어진다.

    (타임아웃 호출은 `_log_if_slow(timed_out=True)`로 임계와 무관하게 남지만, 그 보장이
     깨지는 날에도 이 부등식이 살아 있으면 계측은 계속 맞는다 — 그물을 둘 두는 것이다.)
    """
    assert rest_client.SLOW_CALL_LOG_THRESHOLD_SECONDS < log_metrics.read_timeout_for_label(_ENDPOINT)
