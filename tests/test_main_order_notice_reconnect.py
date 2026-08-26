"""체결통보 끊김·재연결 — **연속과 누적을 가르고, 사건의 끝을 남긴다**
(2026-08-26 §1-2 / P1-1 · §1-9 / P2-4).

08-26에 끊김이 **8건** 났고 재연결이 8/8 매번 2초에 성립했다. 그런데 로그는
① 끊김 줄의 `누적 N회` 자리에 **연결이 서면 리셋되는 값**을 싣고 있었고(그래서 종일 1이었다),
② 붙은 줄이 아예 없어 무통보 구간이 2초인지 10분인지 알 수 없었다.

두 사실 다 사람이 시각을 손으로 맞춰 봐서 알아냈다.
"""

from __future__ import annotations

import logging
from datetime import datetime

from mahdi import main


def _log(down_since, today, last=None):
    return main._log_order_notice_reconnected(down_since, today, last)


def _at(hh, mm, ss):
    return datetime(2026, 8, 26, hh, mm, ss)


def test_the_reconnect_line_closes_the_silent_window(monkeypatch, caplog):
    """08-26 14:00:01 끊김 → 14:00:03 재연결. **그 2초가 이 줄로 남는다.**"""
    monkeypatch.setattr(main.db, "local_now", lambda: _at(14, 0, 3))
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        _log(_at(14, 0, 1), 3)

    body = caplog.records[-1].getMessage()
    assert "체결통보 재연결 성립" in body
    assert "직전 끊김으로부터 2초" in body
    assert "오늘 누적 3회" in body
    assert "[14:00:01 ~ 14:00:03]" in body


def test_a_long_outage_looks_different_from_a_blink(monkeypatch, caplog):
    """**「한 번 끊기고 붙은 것」과 「10분째 못 붙는 것」이 같은 문구였다** — 그것이 1-2다."""
    monkeypatch.setattr(main.db, "local_now", lambda: _at(14, 10, 1))
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        _log(_at(14, 0, 1), 1)

    assert "직전 끊김으로부터 600초" in caplog.records[-1].getMessage()


def test_the_first_five_of_the_day_are_never_suppressed(monkeypatch, caplog):
    """하루 5건까지는 그대로 찍는다 — 08-26의 8건 같은 날에 앞머리를 잃으면 안 된다."""
    monkeypatch.setattr(main.db, "local_now", lambda: _at(14, 0, 3))
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        last = None
        for n in range(1, 6):
            last = _log(_at(14, 0, 1), n, last)

    assert len(caplog.records) == 5


def test_a_reconnect_storm_is_throttled_after_the_quiet_threshold(monkeypatch, caplog):
    """⚠ **매 재연결마다 찍으면 폭주 시 소음이 된다** — 6건째부터 5분에 한 줄이다."""
    monkeypatch.setattr(main.db, "local_now", lambda: _at(14, 0, 3))
    clock = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        last = _log(_at(14, 0, 1), main.ORDER_NOTICE_RECONNECT_QUIET_AFTER, None)
        before = len(caplog.records)
        for n in range(1, 20):
            clock["t"] += 10.0
            last = _log(_at(14, 0, 1), main.ORDER_NOTICE_RECONNECT_QUIET_AFTER + n, last)

    assert len(caplog.records) - before == 0, "5분 창 안에서는 한 줄도 더 안 나온다"


def test_the_stream_down_line_now_separates_streak_from_the_day():
    """P2-4 — `연속`과 `오늘`이 **다른 자리**에 실린다. 부분문자열은 그대로다."""
    body = main.LOG_ORDER_NOTICE_STREAM_DOWN % ("ConnectionClosed", 2.0, 1, 8)
    assert body.startswith("체결통보 스트림 끊김")
    assert "연속 1회 · 오늘 8회" in body
