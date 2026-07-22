import threading
import time

import httpx
import pytest

from mahdi.broker import tr_codes
from mahdi.broker.rest_client import KISRestClient, _is_kis_rate_limit_error, _RateLimiter
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


# --- _is_kis_rate_limit_error (2026-07-20 고도화: 적응형 레이트리미터) -----------------------------

def _http_error_with_body(status_code: int, json_body: dict | None = None, content: bytes | None = None):
    request = httpx.Request("GET", "https://example.com")
    if json_body is not None:
        response = httpx.Response(status_code, json=json_body, request=request)
    else:
        response = httpx.Response(status_code, content=content, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status()가 예외를 던지지 않음")


def test_is_kis_rate_limit_error_true_for_egw00201():
    exc = _http_error_with_body(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다"})
    assert _is_kis_rate_limit_error(exc) is True


def test_is_kis_rate_limit_error_false_for_other_500():
    # 계좌 미승인(CBOT 등) 같은 페이싱과 무관한 500까지 백오프 대상으로 삼으면 안 된다.
    exc = _http_error_with_body(500, {"rt_cd": "1", "msg_cd": "EGW00552", "msg1": "CBOT SUB거래소 신청 계좌가 아닙니다."})
    assert _is_kis_rate_limit_error(exc) is False


def test_is_kis_rate_limit_error_false_for_non_json_body():
    exc = _http_error_with_body(500, content=b"not json")
    assert _is_kis_rate_limit_error(exc) is False


# --- _RateLimiter 적응형 백오프 -------------------------------------------------------------------

def test_rate_limiter_widens_interval_on_rate_limit_hit():
    limiter = _RateLimiter(min_interval=1.0)
    assert limiter._current_interval == pytest.approx(1.0)
    limiter.record_rate_limit_hit()
    assert limiter._current_interval == pytest.approx(1.5)
    limiter.record_rate_limit_hit()
    assert limiter._current_interval == pytest.approx(2.25)


def test_rate_limiter_caps_interval_at_max_multiplier():
    limiter = _RateLimiter(min_interval=1.0)
    for _ in range(20):  # 반복 적중해도 상한(min_interval의 4배)을 넘지 않아야 함
        limiter.record_rate_limit_hit()
    assert limiter._current_interval == pytest.approx(4.0)


def test_rate_limiter_recovers_toward_min_after_sustained_success():
    limiter = _RateLimiter(min_interval=1.0)
    limiter.record_rate_limit_hit()  # 1.0 -> 1.5로 넓어짐
    for _ in range(7):
        limiter.record_success()
    assert limiter._current_interval == pytest.approx(1.5)  # 임계값(8건) 미달 — 아직 그대로
    limiter.record_success()  # 8번째 연속 성공 — 이제 한 단계 되돌림
    assert limiter._current_interval == pytest.approx(1.5 * 0.9)


def test_rate_limiter_never_recovers_below_min_interval():
    limiter = _RateLimiter(min_interval=1.0)
    limiter._current_interval = 1.05  # 되돌림 한 스텝이면 min 밑으로 내려갈 수 있는 경계 상황
    for _ in range(20):
        limiter.record_success()
    assert limiter._current_interval == pytest.approx(1.0)  # min 밑으로는 절대 안 내려감


def test_rate_limiter_record_success_is_noop_when_not_widened():
    limiter = _RateLimiter(min_interval=1.0)
    for _ in range(100):
        limiter.record_success()
    assert limiter._current_interval == pytest.approx(1.0)  # 넓어진 적이 없으면 아무 효과 없음


def test_rate_limiter_disabled_when_min_interval_is_zero():
    limiter = _RateLimiter(min_interval=0.0)
    limiter.record_rate_limit_hit()  # 레이트리밋 자체가 꺼져 있으므로(테스트에서 흔히 씀) 무효과
    assert limiter._current_interval == 0.0


def test_get_widens_rate_limiter_on_egw00201_then_holds_after_one_success():
    # KISRestClient._get()을 통한 통합 검증 — 500+EGW00201을 실제로 받으면 다음 호출부터
    # 페이싱 간격이 넓어지고, 그 뒤 성공 1건만으로는(임계값 8건 미달) 아직 되돌아가지 않는다.
    responses = iter(
        [
            httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다"}),
            httpx.Response(200, json={"output": {}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=1.0,
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.get_balance()
    assert client._rate_limiter._current_interval == pytest.approx(1.5)

    client.get_balance()
    assert client._rate_limiter._current_interval == pytest.approx(1.5)  # 아직 임계값 미달


def test_get_does_not_widen_rate_limiter_on_unrelated_500():
    # CBOT 미승인처럼 페이싱과 무관한 500은 백오프를 키우면 안 된다(전체 호출이 불필요하게 느려짐).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00552", "msg1": "CBOT SUB거래소 신청 계좌가 아닙니다."})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=1.0,
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.get_balance()
    assert client._rate_limiter._current_interval == pytest.approx(1.0)
