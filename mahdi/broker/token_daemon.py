"""OAuth 접근토큰 발급/캐싱/자동 갱신 — 모의/실전 겸용.

여러 프로세스가 동시에 토큰을 발급받으면 KIS가 직전 토큰을 무효화할 수 있어(레이트리밋),
프로세스 전역에서 TokenDaemon 인스턴스 하나만 공유해 캐싱해야 한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from mahdi.broker import tr_codes
from mahdi.config.settings import KISSettings


@dataclass
class AccessToken:
    token: str
    expires_at: float  # epoch seconds

    def is_expired(self, safety_margin_sec: float = 300.0) -> bool:
        return time.time() >= self.expires_at - safety_margin_sec


class TokenDaemon:
    def __init__(self, settings: KISSettings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=10.0)
        self._token: AccessToken | None = None

    @property
    def _domain(self) -> str:
        return tr_codes.VPS_REST_DOMAIN if self._settings.is_mock else tr_codes.REAL_REST_DOMAIN

    def get_token(self) -> str:
        """
        입력: 없음(설정에서 앱키/시크릿 사용).
        계산: 캐시된 토큰이 없거나 만료 임박(기본 5분 여유)이면 재발급 후 캐싱.
        해석: 정상 토큰 문자열 반환 — 상위 REST/WS 클라이언트가 Authorization 헤더에 사용.
        실패 조건: KIS 응답이 4xx/5xx면 httpx.HTTPStatusError를 그대로 전파한다.
        """
        if self._token is None or self._token.is_expired():
            self._token = self._issue_token()
        return self._token.token

    def _issue_token(self) -> AccessToken:
        response = self._client.post(
            f"{self._domain}{tr_codes.PATH_TOKEN}",
            json={
                "grant_type": "client_credentials",
                "appkey": self._settings.kis_app_key,
                "appsecret": self._settings.kis_app_secret,
            },
        )
        response.raise_for_status()
        body = response.json()
        expires_in = float(body.get("expires_in", 86400))
        return AccessToken(token=body["access_token"], expires_at=time.time() + expires_in)
