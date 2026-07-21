import asyncio
import json
from contextlib import contextmanager

import pytest

from mahdi import notify


def _run(coro):
    return asyncio.run(coro)


class _FakeSlackSettings:
    def __init__(self, token: str = "xoxb-test", channel: str = "C123"):
        self.slack_bot_token = token
        self.slack_channel_id = channel
        self.slack_alerts_enabled_default = True

    @property
    def is_configured(self) -> bool:
        return bool(self.slack_bot_token and self.slack_channel_id)


@pytest.fixture(autouse=True)
def _reset_queue(monkeypatch):
    # 2026-07-19: _queue는 프로세스 전역 싱글톤이라 테스트 간 메시지가 누적되면 안 된다.
    monkeypatch.setattr(notify, "_queue", None)
    yield
    monkeypatch.setattr(notify, "_queue", None)


def test_notify_noop_when_slack_not_configured(monkeypatch):
    # .env에 토큰/채널이 없으면(is_configured=False) DB 조회조차 하지 않고 조용히 무시해야 한다.
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings(token="", channel=""))
    notify.notify("메시지")
    assert notify._get_queue().empty()


def test_notify_noop_when_db_toggle_disabled(monkeypatch):
    # COCKPIT 체크박스가 꺼둔 상태(is_slack_alerts_enabled=False)면 큐에 안 들어가야 한다.
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr(notify.db, "get_connection", fake_get_connection)
    monkeypatch.setattr(notify.db, "is_slack_alerts_enabled", lambda conn: False)

    notify.notify("메시지")
    assert notify._get_queue().empty()


def test_notify_enqueues_formatted_message_when_enabled(monkeypatch):
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr(notify.db, "get_connection", fake_get_connection)
    monkeypatch.setattr(notify.db, "is_slack_alerts_enabled", lambda conn: True)

    notify.notify("옵션 체인 결손", "WARNING")

    queued = notify._get_queue().get_nowait()
    assert "옵션 체인 결손" in queued
    assert "[마흐디]" in queued
    assert "⚠️" in queued  # WARNING 레벨 아이콘


def test_notify_swallows_db_errors_without_raising(monkeypatch):
    # 2026-07-19: DB가 죽어있어도(예: 관측 루프가 이미 DB 문제로 힘든 상황) 알림 시도 자체가
    # 예외를 던져 호출자(WS 재연결 로직 등)를 방해하면 안 된다.
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())

    def broken_get_connection(settings=None):
        raise ConnectionError("DB 다운")

    monkeypatch.setattr(notify.db, "get_connection", broken_get_connection)

    notify.notify("메시지")  # 예외가 전파되면 이 줄에서 테스트가 실패한다
    assert notify._get_queue().empty()


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.posted: list[tuple] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, content=None):
        self.posted.append((url, headers, content))
        return _FakeResponse(self._responses.pop(0))


def test_run_slack_worker_posts_message_and_rate_limits(monkeypatch):
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())
    fake_client = _FakeAsyncClient([{"ok": True}])
    monkeypatch.setattr(notify.httpx, "AsyncClient", fake_client)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-worker")  # 한 건 처리 후 워커 루프를 빠져나가기 위한 트릭

    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    notify._get_queue().put_nowait("테스트 메시지")

    with pytest.raises(RuntimeError, match="stop-worker"):
        _run(notify.run_slack_worker())

    assert len(fake_client.posted) == 1
    url, headers, body = fake_client.posted[0]
    assert url == notify._SLACK_POST_MESSAGE_URL
    assert headers["Authorization"] == "Bearer xoxb-test"
    # 2026-07-19: httpx의 json= 편의 파라미터는 charset을 안 붙여 Slack이 한글을 깨진 인코딩으로
    # 잘못 해석하는 문제(실제 채널로 테스트 발송해 확인)가 있어 UTF-8 바이트 + 명시적 charset을 쓴다.
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == {"channel": "C123", "text": "테스트 메시지"}
    assert sleep_calls == [1.0]  # Slack 레이트리밋 간격


class _FakeSyncResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeSyncClient:
    def __init__(self, response: dict):
        self._response = response
        self.posted: list[tuple] = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, content=None):
        self.posted.append((url, headers, content))
        return _FakeSyncResponse(self._response)


def test_notify_sync_noop_when_slack_not_configured(monkeypatch):
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings(token="", channel=""))
    fake_client = _FakeSyncClient({"ok": True})
    monkeypatch.setattr(notify.httpx, "Client", fake_client)

    notify.notify_sync("메시지")

    assert fake_client.posted == []  # 설정이 없으면 DB 조회도, 전송도 하지 않는다


def test_notify_sync_noop_when_db_toggle_disabled(monkeypatch):
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr(notify.db, "get_connection", fake_get_connection)
    monkeypatch.setattr(notify.db, "is_slack_alerts_enabled", lambda conn: False)
    fake_client = _FakeSyncClient({"ok": True})
    monkeypatch.setattr(notify.httpx, "Client", fake_client)

    notify.notify_sync("메시지")

    assert fake_client.posted == []


def test_notify_sync_posts_immediately_without_event_loop(monkeypatch):
    # scripts/log_marketclose_stop.py처럼 실행 중인 asyncio 이벤트 루프가 없는 일회성 스크립트에서도
    # 큐에 쌓아두기만 하고 끝나버리지 않고(아무도 안 비움) 그 자리에서 바로 전송돼야 한다.
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr(notify.db, "get_connection", fake_get_connection)
    monkeypatch.setattr(notify.db, "is_slack_alerts_enabled", lambda conn: True)
    fake_client = _FakeSyncClient({"ok": True})
    monkeypatch.setattr(notify.httpx, "Client", fake_client)

    notify.notify_sync("장마감 종료 후에도 프로세스가 남아있습니다", "WARNING")

    assert len(fake_client.posted) == 1
    url, headers, body = fake_client.posted[0]
    assert url == notify._SLACK_POST_MESSAGE_URL
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    payload = json.loads(body.decode("utf-8"))
    assert payload["channel"] == "C123"
    assert "장마감 종료 후에도 프로세스가 남아있습니다" in payload["text"]
    assert "⚠️" in payload["text"]


def test_notify_sync_swallows_errors_without_raising(monkeypatch):
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())

    def broken_get_connection(settings=None):
        raise ConnectionError("DB 다운")

    monkeypatch.setattr(notify.db, "get_connection", broken_get_connection)

    notify.notify_sync("메시지")  # 예외가 전파되면 이 줄에서 테스트가 실패한다


def test_run_slack_worker_continues_after_api_error(monkeypatch):
    # Slack API가 error를 반환해도(예: 채널 미초대) 워커 태스크 자체는 죽지 않고 다음 메시지를
    # 계속 처리해야 한다 — 이 태스크가 죽으면 이후 모든 알림이 영구히 멈춘다.
    monkeypatch.setattr(notify, "get_slack_settings", lambda: _FakeSlackSettings())
    fake_client = _FakeAsyncClient([{"ok": False, "error": "not_in_channel"}, {"ok": True}])
    monkeypatch.setattr(notify.httpx, "AsyncClient", fake_client)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise RuntimeError("stop-worker")

    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    notify._get_queue().put_nowait("첫 메시지(실패)")
    notify._get_queue().put_nowait("둘째 메시지(성공)")

    with pytest.raises(RuntimeError, match="stop-worker"):
        _run(notify.run_slack_worker())

    assert len(fake_client.posted) == 2
