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


def test_get_quote_sends_expected_headers_and_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"output1": {}})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.get_quote("201S03", market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES)

    assert result == {"output1": {}}
    assert "FID_INPUT_ISCD=201S03" in captured["url"]
    assert "FID_COND_MRKT_DIV_CODE=F" in captured["url"]
    assert captured["headers"]["tr_id"] == tr_codes.TR_OPTION_QUOTE["vps"]
    assert captured["headers"]["authorization"] == "Bearer tok"


def test_get_asking_price_sends_expected_tr_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"output2": {}})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.get_asking_price("201S11305")

    assert captured["headers"]["tr_id"] == tr_codes.TR_OPTION_ASKING_PRICE["vps"]


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


def test_submit_order_includes_required_fields_kis_would_otherwise_reject():
    # ORD_PRCS_DVSN_CD와 ORD_DVSN_CD는 "선물옵션 주문" 문서 기준 Required=Y — 누락 시 KIS가 거부한다.
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"rt_cd": "0"})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.submit_order(symbol="201W09", side="BUY", qty=1, price=350.0, order_dvsn_cd="01")

    body = captured[0]
    assert body["ORD_PRCS_DVSN_CD"] == "02"
    assert body["ORD_DVSN_CD"] == "01"


def test_get_investor_flow_always_uses_real_domain_even_for_mock_account():
    # "모의 TR_ID/Domain: 모의투자 미지원"이지만 계좌 무관 공개 데이터라 실전 도메인 호출이
    # 그대로 성공한다(2026-07-06 실측) — 모의(vps) 설정으로 만든 클라이언트라도 이 호출만은
    # REAL_REST_DOMAIN을 써야 한다.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"output": [{"frgn_ntby_tr_pbmn": "-682279"}]})

    client = KISRestClient(_settings(), _token_daemon(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.get_investor_flow(tr_codes.FID_MRKT_DIV_DERIVATIVES, tr_codes.FID_INVESTOR_FLOW_FUTURES)

    assert result == {"output": [{"frgn_ntby_tr_pbmn": "-682279"}]}
    assert captured["url"].startswith(tr_codes.REAL_REST_DOMAIN)
    assert "FID_INPUT_ISCD=K2I" in captured["url"]
    assert "FID_INPUT_ISCD_2=F001" in captured["url"]
    assert captured["headers"]["tr_id"] == tr_codes.TR_INVESTOR_FLOW_BY_MARKET


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
