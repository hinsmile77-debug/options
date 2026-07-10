import threading
import time

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

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
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

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    client.get_asking_price("201S11305")

    assert captured["headers"]["tr_id"] == tr_codes.TR_OPTION_ASKING_PRICE["vps"]


def test_get_overseas_future_price_sends_expected_tr_id_and_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"output1": {"last_price": "17.50"}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    result = client.get_overseas_future_price("VXN26")

    assert result == {"output1": {"last_price": "17.50"}}
    assert "SRS_CD=VXN26" in captured["url"]
    assert captured["headers"]["tr_id"] == tr_codes.TR_OVERSEAS_FUTUREOPTION_PRICE
    assert captured["url"].startswith(tr_codes.VPS_REST_DOMAIN)


def test_get_overseas_daily_chartprice_sends_expected_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"output1": {}, "output2": []})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    client.get_overseas_daily_chartprice(
        tr_codes.FID_MRKT_DIV_OVERSEAS_TREASURY, tr_codes.FID_INPUT_ISCD_US10Y, "20260601", "20260710"
    )

    assert captured["headers"]["tr_id"] == tr_codes.TR_OVERSEAS_INDEX_DAILY_CHARTPRICE
    assert "FID_COND_MRKT_DIV_CODE=I" in captured["url"]
    assert "FID_INPUT_ISCD=Y0202" in captured["url"]
    assert "FID_INPUT_DATE_1=20260601" in captured["url"]
    assert "FID_INPUT_DATE_2=20260710" in captured["url"]
    assert "FID_PERIOD_DIV_CODE=D" in captured["url"]


def test_get_balance_uses_account_settings():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    client.get_balance()

    assert "CANO=12345678" in captured["url"]


def test_submit_order_maps_sell_and_buy_direction_codes():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"rt_cd": "0"})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
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

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
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

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    result = client.get_investor_flow(tr_codes.FID_MRKT_DIV_DERIVATIVES, tr_codes.FID_INVESTOR_FLOW_FUTURES)

    assert result == {"output": [{"frgn_ntby_tr_pbmn": "-682279"}]}
    assert captured["url"].startswith(tr_codes.REAL_REST_DOMAIN)
    assert "FID_INPUT_ISCD=K2I" in captured["url"]
    assert "FID_INPUT_ISCD_2=F001" in captured["url"]
    assert captured["headers"]["tr_id"] == tr_codes.TR_INVESTOR_FLOW_BY_MARKET


def test_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.get_balance()


def test_uses_real_domain_when_env_is_prod():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(KIS_ENV="prod"),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    client.get_balance()
    assert "openapi.koreainvestment.com" in captured["url"]
    assert "vts" not in captured["url"]


def test_requests_are_paced_to_respect_shared_rate_limit():
    # 2026-07-08 실측: 옵션체인/수급 폴링 루프가 동시에 REST를 쏘면 KIS가 500을 대량 반환함
    # ([[DECISION_LOG]] 참고) — min_request_interval이 실제로 호출 사이를 벌리는지 검증한다.
    call_times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_times.append(time.monotonic())
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.2,
    )
    for _ in range(3):
        client.get_balance()

    assert len(call_times) == 3
    # 개별 간격이 아니라 총 스팬으로 검증 — 타이머 해상도 지터에 흔들리지 않게 한다.
    assert call_times[-1] - call_times[0] >= 0.2 * 2 * 0.8


def test_rate_limiter_serializes_concurrent_threads():
    # asyncio.to_thread로 여러 폴링 루프가 동시에 호출하는 실제 상황을 스레드로 재현 —
    # 스레드 두 개가 거의 동시에 호출해도 최소 간격이 지켜져야 한다.
    call_times: list[float] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            call_times.append(time.monotonic())
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.15,
    )

    threads = [threading.Thread(target=client.get_balance) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    call_times.sort()
    assert len(call_times) == 4
    # 예약(reservation)은 락 밑에서 정확히 min_interval 간격으로 결정되지만, 실제 time.sleep()
    # 기상 시각은 스레드별로 몇 ms씩 흔들릴 수 있다(Windows 타이머 해상도) — 개별 간격 하나하나가
    # 아니라 첫 호출~마지막 호출 총 스팬으로 검증해 그 지터에 흔들리지 않게 한다.
    total_span = call_times[-1] - call_times[0]
    assert total_span >= 0.15 * 3 * 0.8
