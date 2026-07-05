"""KIS WebSocket 실시간 수집기 — 접속키 발급 → 구독 → 메시지 디스패치.

세션당 구독 슬롯이 제한적이므로(KIS 공지 기준 최대 약 41건), 슬롯 롤링 관리는
mahdi.data.subscription_manager가 담당하고 이 클래스는 순수 WS 송수신·구독 등록/해제만 책임진다.
연결 객체(_WSConnection)는 주입받아, 실제 네트워크 없이도 단위 테스트가 가능하게 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import httpx

from mahdi.broker import tr_codes
from mahdi.config.settings import KISSettings


class WSConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Subscription:
    tr_id: str
    tr_key: str  # 종목코드 등


class ApprovalKeyIssuer:
    """WS 접속키(approval_key) 발급 — REST 엔드포인트를 통해서만 발급 가능."""

    def __init__(self, settings: KISSettings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=10.0)

    @property
    def _domain(self) -> str:
        return tr_codes.VPS_REST_DOMAIN if self._settings.is_mock else tr_codes.REAL_REST_DOMAIN

    def issue(self) -> str:
        """
        계산: PATH_WS_APPROVAL POST 호출로 approval_key 발급.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파.
        """
        response = self._client.post(
            f"{self._domain}{tr_codes.PATH_WS_APPROVAL}",
            json={
                "grant_type": "client_credentials",
                "appkey": self._settings.kis_app_key,
                "secretkey": self._settings.kis_app_secret,
            },
        )
        response.raise_for_status()
        return response.json()["approval_key"]


MessageHandler = Callable[[dict], Awaitable[None]]


class KISWebSocketClient:
    """approval_key 발급 후 실시간 구독을 관리하는 얇은 WS 래퍼."""

    MAX_SUBSCRIPTIONS = 41  # KIS 공지 기준 세션당 최대 실시간 등록 건수

    def __init__(self, approval_key: str, connection: WSConnection) -> None:
        self.approval_key = approval_key
        self._conn = connection
        self._active: set[tuple[str, str]] = set()

    @property
    def active_subscriptions(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._active)

    def _envelope(self, tr_type: str, sub: Subscription) -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": self.approval_key,
                    "custtype": "P",
                    "tr_type": tr_type,  # "1"=등록, "2"=해제
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": sub.tr_id, "tr_key": sub.tr_key}},
            }
        )

    async def subscribe(self, sub: Subscription) -> None:
        """
        입력: 구독할 TR ID/종목코드.
        계산: 활성 구독 집합에 추가하고 등록(tr_type=1) 메시지 송신. 이미 활성 구독이면 아무 것도 안 함.
        실패 조건: 활성 구독 수가 MAX_SUBSCRIPTIONS에 도달하면 ValueError — 호출측
                  subscription_manager가 롤링으로 기존 구독을 먼저 해제해야 한다.
        """
        key = (sub.tr_id, sub.tr_key)
        if key in self._active:
            return
        if len(self._active) >= self.MAX_SUBSCRIPTIONS:
            raise ValueError(f"구독 슬롯 한도({self.MAX_SUBSCRIPTIONS}) 초과 — 롤링 해제 필요")
        await self._conn.send(self._envelope("1", sub))
        self._active.add(key)

    async def unsubscribe(self, sub: Subscription) -> None:
        """활성 구독이 아니면 아무 것도 하지 않는다(멱등)."""
        key = (sub.tr_id, sub.tr_key)
        if key not in self._active:
            return
        await self._conn.send(self._envelope("2", sub))
        self._active.discard(key)

    async def listen(self, handler: MessageHandler) -> None:
        """
        수신 루프 — KIS 실시간 데이터는 파이프(|) 구분 텍스트, 구독 응답/PINGPONG은 JSON으로 온다.
        실패 조건: 연결이 끊기면 호출측 _conn.recv()가 예외를 던져 루프가 종료된다(재연결은
                  상위 Data Layer가 담당).
        """
        while True:
            raw = await self._conn.recv()
            message = json.loads(raw) if raw.startswith("{") else {"raw": raw}
            await handler(message)
