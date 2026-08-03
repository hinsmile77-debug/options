"""KIS WebSocket 실시간 수집기 — 접속키 발급 → 구독 → 메시지 디스패치.

세션당 구독 슬롯이 제한적이므로(KIS 공지 기준 최대 약 41건), 슬롯 롤링 관리는
mahdi.data.subscription_manager가 담당하고 이 클래스는 순수 WS 송수신·구독 등록/해제만 책임진다.
연결 객체(_WSConnection)는 주입받아, 실제 네트워크 없이도 단위 테스트가 가능하게 한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import httpx

from mahdi.broker import tr_codes
from mahdi.config.settings import KISSettings

# 2026-08-03(운영점검보고서 §2-8-3 / §4 우선순위 4) — 이 모듈에는 **로거 자체가 없었다.**
# 08-03 하루 전체에서 로그를 낸 것은 httpx / mahdi.main / mahdi.broker.rest_client 셋뿐이고,
# WS 연결·구독·해제는 단 한 줄도 남지 않았다. 그래서 "WS는 붙어 있는데 특정 구독만 조용히
# 실패/해제된" 상태를 로그만으로는 절대 알 수 없었다(§2-4의 H0UNMKO0 수신 0건이 그 사례).
# 볼륨은 하루 수십 줄 수준이다 — 구독은 ATM 롤링 때만 움직인다.
logger = logging.getLogger("mahdi.broker.ws_client")


class WSConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Subscription:
    tr_id: str
    tr_key: str  # 종목코드 등


@dataclass(frozen=True, slots=True)
class SubscriptionAck:
    """KIS가 구독 등록/해제 요청에 되돌려주는 제어 메시지(JSON) 1건.

    2026-08-03(§4 우선순위 4). 실시간 데이터는 파이프(|) 구분 텍스트로 오고, 구독 응답은 JSON으로
    같은 소켓에 온다 — 종전에는 `mahdi.main`의 핸들러가 *"JSON 제어 메시지(구독 응답/PINGPONG)는
    무시"* 하고 버렸다. 그런데 **이 응답이야말로 "구독이 실제로 성립했다"는 유일한 증거**다.
    """

    tr_id: str
    tr_key: str
    rt_cd: str      # "0"=성공
    msg_code: str
    message: str

    @property
    def succeeded(self) -> bool:
        return self.rt_cd == "0"


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
        logger.info(
            "WS 구독 요청: %s %s (활성 %d/%d)", sub.tr_id, sub.tr_key, len(self._active), self.MAX_SUBSCRIPTIONS
        )

    async def unsubscribe(self, sub: Subscription) -> None:
        """활성 구독이 아니면 아무 것도 하지 않는다(멱등)."""
        key = (sub.tr_id, sub.tr_key)
        if key not in self._active:
            return
        await self._conn.send(self._envelope("2", sub))
        self._active.discard(key)
        logger.info(
            "WS 구독 해제: %s %s (활성 %d/%d)", sub.tr_id, sub.tr_key, len(self._active), self.MAX_SUBSCRIPTIONS
        )

    @staticmethod
    def parse_subscription_ack(message: dict) -> SubscriptionAck | None:
        """
        입력: `listen()`이 넘긴 JSON 제어 메시지(파이프 텍스트가 아닌 것).
        계산: KIS 구독 응답 형태(`header.tr_id`/`header.tr_key` + `body.rt_cd`/`msg_cd`/`msg1`)면
             `SubscriptionAck`으로 만든다.
        해석: 2026-08-03(§4 우선순위 4). **구독 성립 여부를 아는 유일한 경로**다 —
             `market_halt_status.last_message_at`(H0UNMKO0 데이터 수신)에는 임계를 걸 수 없다
             (정상일에도 하루 0~2건이라 상시 오경보가 된다. 08-03은 0건, 07-31은 1건이었다).
             "데이터가 안 온다"와 "구독이 안 걸렸다"를 구분하려면 구독 쪽을 따로 봐야 한다.
        실패 조건: PINGPONG 등 body가 없는 제어 메시지면 None(호출측이 조용히 무시한다).
        """
        body = message.get("body")
        if not isinstance(body, dict) or "rt_cd" not in body:
            return None
        header = message.get("header") or {}
        return SubscriptionAck(
            tr_id=str(header.get("tr_id", "")),
            tr_key=str(header.get("tr_key", "")),
            rt_cd=str(body.get("rt_cd", "")),
            msg_code=str(body.get("msg_cd", "")),
            message=str(body.get("msg1", "")),
        )

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
