import time

import httpx
import pytest

from mahdi.broker.token_daemon import TokenDaemon
from mahdi.config.settings import KISSettings


def _settings() -> KISSettings:
    return KISSettings(KIS_APP_KEY="key", KIS_APP_SECRET="secret", KIS_ENV="vps")


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_token_issues_and_caches():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 86400})

    daemon = TokenDaemon(_settings(), client=_client_with(handler))
    assert daemon.get_token() == "tok-1"
    assert daemon.get_token() == "tok-1"
    assert call_count["n"] == 1  # 두번째 호출은 캐시 사용, 재요청 없음


def test_get_token_reissues_when_expired():
    tokens = iter(["tok-1", "tok-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 86400})

    daemon = TokenDaemon(_settings(), client=_client_with(handler))
    assert daemon.get_token() == "tok-1"

    # 캐시된 토큰을 강제로 만료시켜 재발급 경로를 검증
    daemon._token.expires_at = time.time() - 1
    assert daemon.get_token() == "tok-2"


def test_get_token_propagates_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid appkey"})

    daemon = TokenDaemon(_settings(), client=_client_with(handler))
    with pytest.raises(httpx.HTTPStatusError):
        daemon.get_token()


def test_uses_mock_domain_when_env_is_vps():
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400})

    daemon = TokenDaemon(_settings(), client=_client_with(handler))
    daemon.get_token()
    assert "openapivts.koreainvestment.com" in requested_urls[0]
