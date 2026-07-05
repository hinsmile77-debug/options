import httpx
import pytest

from mahdi.broker import tr_codes
from mahdi.broker.rest_client import KISRestClient
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.config.settings import KISSettings


def _settings(**overrides) -> KISSettings:
    defaults = dict(KIS_APP_KEY="key", KIS_APP_SECRET="secret", KIS_ACCOUNT_NO="12345678", KIS_ENV="vps")
    defaults.update(overrides)
    return KISSettings(**defaults)


def _token_daemon() -> TokenDaemon:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400})

    return TokenDaemon(_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_get_option_chain_sends_expected_headers_and_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"output": []})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.get_option_chain("201")

    assert result == {"output": []}
    assert "FID_INPUT_ISCD=201" in captured["url"]
    assert captured["headers"]["tr_id"] == tr_codes.TR_OPTION_CHAIN["vps"]
    assert captured["headers"]["authorization"] == "Bearer tok"


def test_get_balance_uses_account_settings():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.get_balance()

    assert "CANO=12345678" in captured["url"]


def test_submit_order_maps_sell_and_buy_direction_codes():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"rt_cd": "0"})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.submit_order(symbol="201W09", side="SELL", qty=1, price=350.0)
    client.submit_order(symbol="201W09", side="BUY", qty=1, price=350.0)

    assert captured[0]["SLL_BUY_DVSN_CD"] == "01"
    assert captured[1]["SLL_BUY_DVSN_CD"] == "02"


def test_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError):
        client.get_balance()


def test_uses_real_domain_when_env_is_prod():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(KIS_ENV="prod"), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.get_balance()
    assert "openapi.koreainvestment.com" in captured["url"]
    assert "vts" not in captured["url"]
