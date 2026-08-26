"""`mahdi.notify` — **꺼진 토글이 버린 경보가 로그에 남는가** (2026-08-26 §1-17 / P1-5).

08-26에 워치독이 DEGRADED 77분 동안 CRITICAL 경보를 **8회** 냈고, `if not enabled: return`이
로그 한 줄도 안 남기고 전부 버렸다. 그래서 그날 장후 회차는 「경보를 냈는데 안 갔다」와
「애초에 안 냈다」를 구분할 방법이 없었다.

⛔ **이 파일은 Slack을 켜는 것과 아무 상관이 없다.** `slack_alert_settings.enabled`는 여전히
`false`이고(2026-08-01 보류 결정), 바뀐 것은 **버렸다는 사실이 기록되는 것 하나**다.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from mahdi import notify


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _reset_throttle():
    """억제 상태는 모듈 전역이다 — 테스트끼리 새어 나가면 안 된다."""
    notify._toggle_off_last_logged_at.clear()
    notify._toggle_off_suppressed.clear()
    yield
    notify._toggle_off_last_logged_at.clear()
    notify._toggle_off_suppressed.clear()


@contextmanager
def _toggle_off(monkeypatch, *, configured: bool = True):
    """`.env`는 채워져 있고 DB 토글만 꺼진 상태 — 08-26의 이 PC가 정확히 그랬다."""
    class _Settings:
        is_configured = configured
        slack_channel_id = "C1"
        slack_bot_token = "x"

    monkeypatch.setattr(notify, "get_slack_settings", lambda: _Settings())
    monkeypatch.setattr(notify.db, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(notify.db, "is_slack_alerts_enabled", lambda _c: False)
    yield


def test_a_dropped_alert_leaves_exactly_one_line(monkeypatch, caplog):
    with _toggle_off(monkeypatch), caplog.at_level(logging.INFO, logger="mahdi.notify"):
        notify.notify("관측 루프 적재 정지(no_ingest) — 77분째", "CRITICAL")

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    assert "알림 스킵(토글 꺼짐)" in lines[0]
    assert "level=CRITICAL" in lines[0]


def test_notify_sync_drops_leave_a_line_too(monkeypatch, caplog):
    """08-26의 8건은 **`notify_sync()`** 경로였다(워치독은 일회성 스크립트다)."""
    with _toggle_off(monkeypatch), caplog.at_level(logging.INFO, logger="mahdi.notify"):
        notify.notify_sync("관측 루프 적재 정지(no_ingest)", "CRITICAL")

    assert any("알림 스킵(토글 꺼짐)" in r.getMessage() for r in caplog.records)


def test_a_burst_of_the_same_level_is_throttled_but_not_lost(monkeypatch, caplog):
    """**억제해도 건수는 안 잃는다** — 다음 줄이 「N건 추가 억제됨」으로 도로 실어 준다.

    억제가 지표를 먹으면 이 fix가 스스로를 눈멀게 한다(08-06 Fix#4가 그 자리에서 배운 것).
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(notify.time, "monotonic", lambda: clock["t"])
    with _toggle_off(monkeypatch), caplog.at_level(logging.INFO, logger="mahdi.notify"):
        for _ in range(4):
            notify.notify("적재 정지", "CRITICAL")
        clock["t"] += notify._TOGGLE_OFF_LOG_WINDOW_SECONDS + 1
        notify.notify("적재 정지", "CRITICAL")

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2, "창 안의 넷은 한 줄로 접히고, 창이 지난 뒤 한 줄이 더 나온다"
    assert "추가 억제됨" not in lines[0]
    assert "3건 추가 억제됨" in lines[1]


def test_levels_are_throttled_apart(monkeypatch, caplog):
    """레벨이 다르면 다른 사건이다 — CRITICAL이 INFO를 가려서는 안 된다."""
    monkeypatch.setattr(notify.time, "monotonic", lambda: 1000.0)
    with _toggle_off(monkeypatch), caplog.at_level(logging.INFO, logger="mahdi.notify"):
        notify.notify("적재 정지", "CRITICAL")
        notify.notify("재개", "INFO")

    assert len(caplog.records) == 2


def test_a_missing_env_is_a_different_event_and_stays_silent(monkeypatch, caplog):
    """`.env`가 비어 있으면 토글 분기에 **닿지도 않는다** — 그것은 다른 사건이다.

    이 축이 재는 것은 「토글이 꺼져서 버렸다」이고, 미설정은 `is_configured`가 먼저 잡는다.
    두 상태를 같은 축으로 세면 「스위치를 켜면 풀리는 것」과 「값을 채워야 풀리는 것」이 섞인다.
    """
    with _toggle_off(monkeypatch, configured=False), caplog.at_level(logging.INFO, logger="mahdi.notify"):
        notify.notify("적재 정지", "CRITICAL")

    assert not [r for r in caplog.records if "알림 스킵" in r.getMessage()]


def test_the_message_body_is_truncated_so_the_line_does_not_swallow_the_log(monkeypatch, caplog):
    with _toggle_off(monkeypatch), caplog.at_level(logging.INFO, logger="mahdi.notify"):
        notify.notify("가" * 500, "WARNING")

    body = caplog.records[0].getMessage()
    assert body.count("가") == 120
