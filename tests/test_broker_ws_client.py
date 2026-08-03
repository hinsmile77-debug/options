import asyncio
import json
import logging

import httpx
import pytest

from mahdi.broker.ws_client import ApprovalKeyIssuer, KISWebSocketClient, Subscription
from mahdi.config.settings import KISSettings


class FakeConnection:
    def __init__(self, incoming: list[str] | None = None):
        self.sent: list[str] = []
        self._incoming = list(incoming or [])
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._incoming:
            raise ConnectionError("연결 종료(테스트 픽스처 소진)")
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


def test_subscribe_sends_registration_envelope_and_tracks_active():
    conn = FakeConnection()
    client = KISWebSocketClient(approval_key="APV", connection=conn)
    sub = Subscription(tr_id="H0IOCNT0", tr_key="201W09")

    _run(client.subscribe(sub))

    assert (sub.tr_id, sub.tr_key) in client.active_subscriptions
    payload = json.loads(conn.sent[0])
    assert payload["header"]["approval_key"] == "APV"
    assert payload["header"]["tr_type"] == "1"
    assert payload["body"]["input"] == {"tr_id": "H0IOCNT0", "tr_key": "201W09"}


def test_subscribe_duplicate_is_noop():
    conn = FakeConnection()
    client = KISWebSocketClient(approval_key="APV", connection=conn)
    sub = Subscription(tr_id="H0IOCNT0", tr_key="201W09")

    _run(client.subscribe(sub))
    _run(client.subscribe(sub))

    assert len(conn.sent) == 1


def test_unsubscribe_sends_release_envelope_and_untracks():
    conn = FakeConnection()
    client = KISWebSocketClient(approval_key="APV", connection=conn)
    sub = Subscription(tr_id="H0IOCNT0", tr_key="201W09")
    _run(client.subscribe(sub))

    _run(client.unsubscribe(sub))

    assert (sub.tr_id, sub.tr_key) not in client.active_subscriptions
    payload = json.loads(conn.sent[-1])
    assert payload["header"]["tr_type"] == "2"


def test_unsubscribe_inactive_is_noop():
    conn = FakeConnection()
    client = KISWebSocketClient(approval_key="APV", connection=conn)
    sub = Subscription(tr_id="H0IOCNT0", tr_key="201W09")

    _run(client.unsubscribe(sub))

    assert conn.sent == []


def test_subscribe_raises_when_slot_limit_exceeded():
    conn = FakeConnection()
    client = KISWebSocketClient(approval_key="APV", connection=conn)
    for i in range(client.MAX_SUBSCRIPTIONS):
        _run(client.subscribe(Subscription(tr_id="H0IOCNT0", tr_key=f"CODE{i}")))

    with pytest.raises(ValueError):
        _run(client.subscribe(Subscription(tr_id="H0IOCNT0", tr_key="ONE_TOO_MANY")))


def test_listen_dispatches_json_and_raw_pipe_messages():
    json_msg = json.dumps({"header": {"tr_id": "PINGPONG"}})
    pipe_msg = "H0IOCNT0|001|201W09^350.5^10"
    conn = FakeConnection(incoming=[json_msg, pipe_msg])
    client = KISWebSocketClient(approval_key="APV", connection=conn)

    received = []

    async def handler(message: dict) -> None:
        received.append(message)

    with pytest.raises(ConnectionError):
        _run(client.listen(handler))

    assert received[0] == {"header": {"tr_id": "PINGPONG"}}
    assert received[1] == {"raw": pipe_msg}


def test_approval_key_issuer_returns_key_from_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"approval_key": "APV-123"})

    settings = KISSettings(KIS_APP_KEY="key", KIS_APP_SECRET="secret", KIS_ENV="vps")
    issuer = ApprovalKeyIssuer(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert issuer.issue() == "APV-123"


# ===== 2026-08-03 §4 우선순위 4: 구독 ACK =====


def test_parse_subscription_ack_reads_success_response():
    ack = KISWebSocketClient.parse_subscription_ack(
        {
            "header": {"tr_id": "H0UNMKO0", "tr_key": "005930"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
        }
    )
    assert ack is not None
    assert ack.tr_id == "H0UNMKO0"
    assert ack.tr_key == "005930"
    assert ack.succeeded is True


def test_parse_subscription_ack_reads_failure_response():
    ack = KISWebSocketClient.parse_subscription_ack(
        {
            "header": {"tr_id": "H0UNMKO0", "tr_key": "005930"},
            "body": {"rt_cd": "1", "msg_cd": "OPSP0009", "msg1": "ALREADY IN SUBSCRIBE"},
        }
    )
    assert ack is not None and ack.succeeded is False
    assert ack.msg_code == "OPSP0009"


def test_parse_subscription_ack_ignores_pingpong_and_data():
    # PINGPONG 등 body가 없는 제어 메시지는 구독 응답이 아니다.
    assert KISWebSocketClient.parse_subscription_ack({"header": {"tr_id": "PINGPONG"}}) is None
    assert KISWebSocketClient.parse_subscription_ack({"raw": "0|H0IFCNT0|001|101S03^090000^350"}) is None


def test_subscribe_and_unsubscribe_are_logged(caplog):
    # 2026-08-03 §2-8-3: 이 모듈에는 로거 자체가 없어 구독 상태 변화가 로그에 전혀 안 남았다.
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    with caplog.at_level(logging.INFO, logger="mahdi.broker.ws_client"):
        asyncio.run(ws.subscribe(Subscription("H0IOCNT0", "201S03C325")))
        asyncio.run(ws.unsubscribe(Subscription("H0IOCNT0", "201S03C325")))

    messages = [r.getMessage() for r in caplog.records]
    assert any("WS 구독 요청" in m for m in messages)
    assert any("WS 구독 해제" in m for m in messages)
