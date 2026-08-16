import logging
import threading
import time
from unittest import mock

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


def test_get_balance_sends_required_margin_and_settlement_fields():
    # 2026-07-28 6차 실측(docs/efriend xlsx): MGNA_DVSN/EXCC_STAT_CD/CTX_AREA_FK200/
    # CTX_AREA_NK200이 전부 Required — 빠지면 KIS가 "ERROR : INPUT_FIELD_NAME MGNA_DVSN"
    # (msg_cd=OPSQ2001)로 거부한다(실제 라이브 모의계좌 호출로 재현·확인함).
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

    assert "MGNA_DVSN=02" in captured["url"]
    assert "EXCC_STAT_CD=1" in captured["url"]
    assert "CTX_AREA_FK200=" in captured["url"]
    assert "CTX_AREA_NK200=" in captured["url"]


def test_get_balance_accepts_custom_margin_division_and_settlement_status():
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
    client.get_balance(margin_division="01", settlement_status="2")

    assert "MGNA_DVSN=01" in captured["url"]
    assert "EXCC_STAT_CD=2" in captured["url"]


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
    for _ in range(19):
        limiter.record_success()
    assert limiter._current_interval == pytest.approx(1.5)  # 임계값(20건) 미달 — 아직 그대로
    limiter.record_success()  # 20번째 연속 성공 — 이제 한 단계 되돌림
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


def test_rate_limiter_current_multiplier_reflects_backoff_state():
    limiter = _RateLimiter(min_interval=1.0)
    assert limiter.current_multiplier == pytest.approx(1.0)
    limiter.record_rate_limit_hit()
    assert limiter.current_multiplier == pytest.approx(1.5)
    limiter.record_rate_limit_hit()
    assert limiter.current_multiplier == pytest.approx(2.25)


def test_rate_limiter_current_multiplier_is_one_when_disabled():
    limiter = _RateLimiter(min_interval=0.0)
    assert limiter.current_multiplier == 1.0


def test_kis_rest_client_exposes_rate_limit_backoff_multiplier():
    client = KISRestClient(_settings(), _token_daemon(), min_request_interval=1.0)
    assert client.rate_limit_backoff_multiplier == pytest.approx(1.0)
    client._rate_limiter.record_rate_limit_hit()
    assert client.rate_limit_backoff_multiplier == pytest.approx(1.5)


def test_rate_limiter_logs_on_backoff_widen(caplog):
    limiter = _RateLimiter(min_interval=1.0)
    with caplog.at_level("INFO", logger="mahdi.broker.rest_client"):
        limiter.record_rate_limit_hit()
    assert "레이트리밋 백오프 확대" in caplog.text
    assert "1.50배" in caplog.text


def test_rate_limiter_logs_on_backoff_recovery(caplog):
    limiter = _RateLimiter(min_interval=1.0)
    limiter.record_rate_limit_hit()  # 1.0 -> 1.5
    with caplog.at_level("INFO", logger="mahdi.broker.rest_client"):
        for _ in range(19):
            limiter.record_success()
        assert "레이트리밋 백오프 회복" not in caplog.text  # 임계값(20건) 미달까지는 조용함
        limiter.record_success()  # 20번째 — 여기서만 로깅
    assert "레이트리밋 백오프 회복" in caplog.text


def test_get_widens_rate_limiter_on_egw00201_then_holds_after_one_success():
    # KISRestClient._get()을 통한 통합 검증 — 500+EGW00201을 실제로 받으면 다음 호출부터
    # 페이싱 간격이 넓어지고, 그 뒤 성공 1건만으로는(임계값 20건 미달) 아직 되돌아가지 않는다.
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
    assert client._rate_limiter._current_interval == pytest.approx(1.5)  # 아직 임계값(20건) 미달


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


# ===== 2026-07-31 운영점검 §2-1(b)/§4 우선순위 3: 느린 호출의 페이서/HTTP 구간 분리 계측 =====


def test_slow_call_log_splits_pacer_wait_from_http_time(caplog):
    # 07-31에 "호출 1건당 8~9초" 정체 7건이 나왔는데, httpx가 남기는 건 응답 완료 시점 한 줄뿐이라
    # 페이서 대기와 서버 응답을 구분할 수 없어 원인을 좁히지 못했다. 두 구간을 나눠 남긴다.
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.05)
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )

    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        # 임계를 낮춰 이번 호출이 반드시 걸리게 한다(운영 기본값 3.0초는 정상 호출을 안 남긴다).
        with mock.patch("mahdi.broker.rest_client.SLOW_CALL_LOG_THRESHOLD_SECONDS", 0.01):
            client.get_balance()

    records = [r for r in caplog.records if "느린 REST 호출" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "페이서대기" in message and "HTTP" in message


def test_normal_call_is_not_logged_at_default_threshold(caplog):
    # 하루 12,947건을 전부 남기면 07-31에 되찾은 로그 가독성(사람이 읽는 줄 6,161 → 2,963)을
    # 다시 잃는다 — 정상 호출은 남지 않아야 한다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )

    # 계측이 INFO로 내려갔으므로(2026-08-03 §2-8) 캡처도 INFO로 해야 "안 남는다"를 실제로 검증한다.
    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        client.get_balance()

    assert not [r for r in caplog.records if "느린 REST 호출" in r.getMessage()]


def test_slow_call_is_logged_even_when_the_request_raises(caplog):
    # 타임아웃으로 끝난 호출이야말로 계측이 필요하다(07-31 실측 9.5초 간격은 httpx timeout=10.0초
    # 직전까지 간 요청일 가능성이 있다) — finally에서 재고 넘긴다.
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.05)
        raise httpx.ReadTimeout("timeout", request=request)

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )

    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        with mock.patch("mahdi.broker.rest_client.SLOW_CALL_LOG_THRESHOLD_SECONDS", 0.01):
            with pytest.raises(httpx.ReadTimeout):
                client.get_balance()

    assert [r for r in caplog.records if "느린 REST 호출" in r.getMessage()]


def test_slow_call_log_attributes_pacer_wait_when_the_limiter_is_backed_off(caplog):
    # 페이서대기가 크면 "다른 폴러와의 예약 큐 경합", HTTP가 크면 "서버/커넥션 풀"이 범인이다 —
    # 이 구분이 §2-1(b)를 다음 거래일에 판정하기 위한 계측의 핵심이다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.05,
    )
    client._rate_limiter.wait()  # 다음 호출이 반드시 페이서에서 대기하도록 슬롯을 미리 예약

    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        with mock.patch("mahdi.broker.rest_client.SLOW_CALL_LOG_THRESHOLD_SECONDS", 0.01):
            client.get_balance()

    message = [r for r in caplog.records if "느린 REST 호출" in r.getMessage()][0].getMessage()
    pacer = float(message.split("페이서대기 ")[1].split("초")[0])
    http = float(message.split("+ HTTP ")[1].split("초")[0])
    assert pacer > http  # 이 시나리오의 지연은 전적으로 페이서 대기다


# ===== 2026-08-03 §4 우선순위 3: 커넥션 풀 재사용 실패 대응 =====


def test_get_retries_once_on_remote_protocol_error():
    """KIS가 먼저 닫은 keep-alive 커넥션을 재사용하면 RemoteProtocolError가 난다(08-03/07-31 각 8건).

    요청이 서버에 도달하지 않았고 GET이라 부작용이 없으므로 재시도가 안전하다.
    """
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return httpx.Response(200, json={"output1": {"ok": True}})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )

    assert client.get_quote("201S03", market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES) == {"output1": {"ok": True}}
    assert len(attempts) == 2


def test_get_retry_still_goes_through_the_pacer():
    # 재시도가 페이서를 건너뛰면 EGW00201(초당 거래건수 초과)을 우리 손으로 유발하게 된다.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("boom", request=request)
        return httpx.Response(200, json={})

    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.001,
    )
    before = client.rate_limit_total_calls
    client.get_quote("201S03", market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES)

    assert client.rate_limit_total_calls - before == 2, "재시도도 페이서 카운터를 통과해야 한다"


def test_get_propagates_remote_protocol_error_when_retry_also_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("boom", request=request)

    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    with pytest.raises(httpx.RemoteProtocolError):
        client.get_quote("201S03", market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES)


def test_default_client_bounds_pool_and_keepalive():
    # 호출이 페이서로 직렬화되므로 커넥션이 여러 개 필요 없다 — 좁은 풀 + 긴 keep-alive가
    # RemoteProtocolError를 줄인다(08-03 §4 우선순위 3).
    #
    # 2026-08-04(§2-6 / Fix#8): read 10.0 → 4.0초. 08-04에 08-03의 판정표가 답을 냈다 —
    # 느린 호출 362건의 총 2,278초 중 **HTTP가 90%(2,050초)**, 페이서는 10%(229초)뿐이다.
    # read 10초는 느린 레그 한둘이 60초 사이클을 통째로 덮게 두고, 그것이 08-04 미회수 결손
    # 5분(14:31/15:11/15:15/15:17/15:19)을 만들었다. 참고: 08-03에 커넥션 풀을 좁히고
    # keep-alive를 늘린 조치(p3)는 `RemoteProtocolError` 8 → **25건**으로 오히려 악화됐다
    # (§2-1의 계측 실명 때문에 리포트에는 "실측 없음"으로 찍혔다) — 풀 설정 자체는 다음
    # 거래일 실측을 보고 다시 판단한다.
    from mahdi.broker.rest_client import _HTTP_LIMITS, _HTTP_READ_TIMEOUT_SECONDS, _HTTP_TIMEOUT

    assert _HTTP_LIMITS.max_connections == 4
    assert _HTTP_LIMITS.keepalive_expiry == 15.0
    assert _HTTP_TIMEOUT.connect == 3.0
    assert _HTTP_TIMEOUT.read == _HTTP_READ_TIMEOUT_SECONDS == 4.0
    # 옵션체인 사이클 예산(50초) 안에 최소 10레그(먼슬리 1북)는 돌 수 있어야 한다 —
    # 그러지 못하면 예산과 타임아웃이 서로를 무력화한다.
    from mahdi.main import OPTION_CHAIN_CYCLE_COLLECT_BUDGET_SECONDS

    assert _HTTP_READ_TIMEOUT_SECONDS * 10 <= OPTION_CHAIN_CYCLE_COLLECT_BUDGET_SECONDS


def test_slow_call_log_is_info_not_warning(caplog):
    # 진단 목적이 끝났다 — 하루 933건의 WARNING은 진짜 경고를 파묻는다(§2-8).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        client._log_if_slow("GET", "https://x/uapi/inquire-price?a=1", pacer_seconds=1.0, http_seconds=9.0)

    records = [r for r in caplog.records if "느린 REST 호출" in r.message]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_slow_call_threshold_ignores_calls_under_the_threshold(caplog):
    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        min_request_interval=0.0,
    )
    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        client._log_if_slow("GET", "https://x/uapi/inquire-price", pacer_seconds=1.0, http_seconds=1.5)

    assert not [r for r in caplog.records if "느린 REST 호출" in r.message]


# ===== 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 3 / Fix#2) — 엔드포인트별 read 타임아웃 =====


def test_endpoint_read_timeout_table_covers_slow_pollers():
    """08-04 Fix#8(read 4초)은 옵션체인을 근거로 정했는데 `httpx.Client` 전역이라 잔고 폴러까지
    깨뜨렸다(08-05 개장 후 8사이클 중 3건 ReadTimeout, 08-04 이전 실패 0건). 근거가 성립하는
    곳으로 범위를 좁힌 것이 이 표다 — 되돌림이 아니라 범위 축소다."""
    from mahdi.broker.rest_client import _SLOW_ENDPOINT_READ_TIMEOUT_SECONDS, timeout_for_url

    # 300초 주기 단발 호출 — p50이 옵션체인의 35배(0.70초 vs 0.02초)다.
    assert timeout_for_url(f"https://x{tr_codes.PATH_FUTUREOPTION_BALANCE}").read == 10.0
    # 만기유동성(북별 1개) — 08-05에 4.00초 천장에 닿아 2건 실패.
    assert timeout_for_url(f"https://x{tr_codes.PATH_FUTUREOPTION_ASKING_PRICE}").read == 10.0
    # 주문 — 타임아웃되면 접수 여부가 불명확해진다.
    assert timeout_for_url(f"https://x{tr_codes.PATH_FUTUREOPTION_ORDER}").read == 10.0
    assert timeout_for_url(f"https://x{tr_codes.PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL}").read == 10.0
    assert _SLOW_ENDPOINT_READ_TIMEOUT_SECONDS == 10.0


def test_option_chain_quote_keeps_the_four_second_budget():
    """Fix#8의 근거가 성립하는 유일한 곳 — 60초 주기에 20레그를 도는 폴러."""
    from mahdi.broker.rest_client import _HTTP_READ_TIMEOUT_SECONDS, timeout_for_url

    assert timeout_for_url(f"https://x{tr_codes.PATH_FUTUREOPTION_QUOTE}").read == _HTTP_READ_TIMEOUT_SECONDS == 4.0
    # 수급 폴러는 매분 3콜이고 p95가 0.09초라 기본값으로 충분하다.
    assert timeout_for_url(f"https://x{tr_codes.PATH_INVESTOR_FLOW_BY_MARKET}").read == 4.0


def test_timeout_key_is_full_path_not_last_segment():
    """국내 선물옵션 시세와 해외선물 시세는 **마지막 경로 조각이 둘 다 `inquire-price`** 다.
    조각으로 키를 잡으면 서로 다른 엔드포인트가 같은 타임아웃을 공유하게 된다."""
    from mahdi.broker.rest_client import timeout_for_url

    assert tr_codes.PATH_FUTUREOPTION_QUOTE.rsplit("/", 1)[-1] == "inquire-price"
    assert tr_codes.PATH_OVERSEAS_FUTUREOPTION_PRICE.rsplit("/", 1)[-1] == "inquire-price"
    assert tr_codes.PATH_FUTUREOPTION_QUOTE != tr_codes.PATH_OVERSEAS_FUTUREOPTION_PRICE
    # 등록 경로끼리 서로의 suffix가 아니어야 순회 순서에 결과가 좌우되지 않는다.
    assert not tr_codes.PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL.endswith(tr_codes.PATH_FUTUREOPTION_ORDER)


def test_timeout_for_url_tolerates_query_string():
    from mahdi.broker.rest_client import timeout_for_url

    assert timeout_for_url(f"https://x{tr_codes.PATH_FUTUREOPTION_BALANCE}?CANO=1").read == 10.0


def test_get_balance_actually_sends_the_ten_second_read_timeout():
    """표만 맞고 요청에 안 실리면 의미가 없다 — 실제 나가는 요청의 타임아웃을 확인한다."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"output2": []})

    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    client.get_balance()

    assert captured["timeout"]["read"] == 10.0
    assert captured["timeout"]["connect"] == 3.0


def test_get_quote_actually_sends_the_four_second_read_timeout():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"output1": {}})

    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    client.get_quote("201S03")

    assert captured["timeout"]["read"] == 4.0


# ===== 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 4 / Fix#4) — 임계-물리한계 정합성 =====


def test_slow_call_threshold_must_stay_below_every_read_timeout():
    """**이 파일에서 가장 중요한 불변식이다.**

    08-04 Fix#8이 read를 4.0초로 낮추면서 임계 5.0초가 **물리적 상한 위로 올라갔다.**
    그 순간 `slow_calls`는 구조적으로 0이 됐고(08-05에 ReadTimeout 21건이 실재하는데
    리포트 §9는 "임계 초과 호출 없음"), §0 가설 검정은 그 0을 "반증"으로 찍었다.
    계측 감사는 이걸 못 잡는다 — 로그에 실재하지도 않으므로 "파서 정상"으로 통과한다.

    임계와 타임아웃 중 **어느 쪽을 바꿔도** 이 테스트가 깨져야 그 실수가 반복되지 않는다.
    """
    from mahdi.broker.rest_client import (
        _ENDPOINT_READ_TIMEOUT_SECONDS,
        _HTTP_READ_TIMEOUT_SECONDS,
        SLOW_CALL_LOG_THRESHOLD_SECONDS,
    )

    every_read_timeout = [_HTTP_READ_TIMEOUT_SECONDS, *_ENDPOINT_READ_TIMEOUT_SECONDS.values()]
    assert SLOW_CALL_LOG_THRESHOLD_SECONDS < min(every_read_timeout), (
        "느린 호출 임계가 read 타임아웃보다 크면 그 지표는 구조적으로 0건이 된다 "
        "(08-04 Fix#8이 정확히 그렇게 자기 근거가 된 계측을 침묵시켰다)"
    )


def test_timed_out_call_is_always_logged_even_below_the_threshold(caplog):
    """타임아웃은 정의상 가장 극단적인 느린 호출인데, 총 소요가 타임아웃 값에서 잘리는 탓에
    임계 아래로 떨어질 수 있다 — 임계에 의존하지 않고 구조적으로 보장한다."""
    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        min_request_interval=0.0,
    )
    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        client._log_if_slow(
            "GET", "https://x/uapi/inquire-price", pacer_seconds=0.0, http_seconds=0.5, timed_out=True
        )

    assert [r for r in caplog.records if "느린 REST 호출" in r.message]


def test_read_timeout_produces_an_attributable_slow_call_line(caplog):
    """08-05 실측 재현: ReadTimeout 21건이 §9 "페이서 vs HTTP 귀속" 표에 **한 건도** 안 잡혔다.
    타임아웃이 나면 그 호출의 페이서/HTTP 분해가 반드시 남아야 한다 — §2-6이 "밀림의 90%는
    KIS 지연"이라고 귀속시킨 근거가 바로 그 표다."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = KISRestClient(
        _settings(), _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    with caplog.at_level(logging.INFO, logger="mahdi.broker.rest_client"):
        with pytest.raises(httpx.ReadTimeout):
            client.get_quote("201S03")

    slow = [r for r in caplog.records if "느린 REST 호출" in r.message]
    assert len(slow) == 1
    # 파서(`log_metrics._SLOW_CALL_RE`)가 읽는 형태 그대로여야 한다.
    assert "페이서대기" in slow[0].message and "HTTP" in slow[0].message


def test_slow_call_line_still_matches_the_ops_parser_after_the_threshold_change():
    """임계를 바꿔도 포맷 계약은 그대로다 — 08-03에 레벨만 바꾸고 파서를 안 고쳐
    362건이 0건으로 보고된 전례가 있다."""
    from mahdi.ops.log_metrics import _SLOW_CALL_RE
    from mahdi.broker.rest_client import LOG_SLOW_CALL

    rendered = LOG_SLOW_CALL % (4.02, 0.02, 4.00, 1.00, "GET", "inquire-price")
    line = f"2026-08-06 09:20:31,123 INFO:mahdi.broker.rest_client:{rendered}"
    assert _SLOW_CALL_RE.match(line) is not None


# ===== 2026-08-05(§2 이상점 4 후속) — 계측 엔드포인트 라벨 충돌 =====


def test_domestic_and_overseas_inquire_price_get_distinct_labels():
    """둘 다 마지막 경로 조각이 `inquire-price`다. 섞이면 §9-1이 옵션체인 2,825건과
    항상 실패하는 해외선물 호출(~88건/일)을 한 행에 담고, 그 위에 얹힌 자동 대응 규칙
    (p95 2.5초 이틀 연속 → 위클리 폴링 축소)이 엉뚱한 폴러를 줄이게 된다."""
    from mahdi.broker.rest_client import endpoint_label

    assert endpoint_label(f"https://x{tr_codes.PATH_FUTUREOPTION_QUOTE}") == "inquire-price"
    assert endpoint_label(f"https://x{tr_codes.PATH_OVERSEAS_FUTUREOPTION_PRICE}") == "overseas-inquire-price"


def test_every_known_endpoint_path_gets_a_unique_label():
    """새 엔드포인트의 마지막 조각이 기존과 겹치면 여기서 걸린다 — 값이 아니라 **관계**를 지킨다.
    (Fix#4의 임계-타임아웃 불변식과 같은 방식.)"""
    from mahdi.broker.rest_client import endpoint_label

    paths = [v for k, v in vars(tr_codes).items() if k.startswith("PATH_") and isinstance(v, str)]
    labels = [endpoint_label(f"https://x{p}") for p in paths]
    duplicates = {lab for lab in labels if labels.count(lab) > 1}
    assert not duplicates, f"라벨이 겹친다: {duplicates} — _ENDPOINT_LABEL_OVERRIDES에 추가할 것"


def test_endpoint_label_stays_parseable_by_the_ops_report():
    r"""`log_metrics._REST_LATENCY_ITEM_RE`가 `[\w-]+`로 읽는다 — 슬래시가 섞이면 파서가 눈이 먼다."""
    import re
    from mahdi.broker.rest_client import endpoint_label

    paths = [v for k, v in vars(tr_codes).items() if k.startswith("PATH_") and isinstance(v, str)]
    for path in paths:
        assert re.fullmatch(r"[\w-]+", endpoint_label(f"https://x{path}")), path


# ===== 2026-08-06(운영점검 장전편 §2-1 / Fix#1) — 페이서는 완료 시각 기준으로 다음 슬롯을 민다 =====


class _FakeClock:
    """단조 시계를 손으로 굴린다 — 실제로 자면 테스트가 느려지고, 무엇보다 **구/신 구현이
    구분되지 않는다**(둘 다 "대충 1초쯤 기다린다"로 보인다). 잠든 시간을 그대로 시각에
    더해주는 이 시계라야 "무엇을 기준으로 예약했는가"가 드러난다."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_pacer_reserves_next_slot_from_completion_not_from_start():
    """**이 fix의 유일한 직접 지표다.**

    종전 구현은 다음 슬롯을 **호출 시작 시각** 기준으로 잡았다. 앞 호출이 0.5초 걸리면 다음
    호출은 시작 +1.0초 = **완료 +0.5초**에 나간다. KIS는 자기 처리 완료 기준으로 초당 건수를
    세는 것으로 보이므로 그것이 창 안으로 들어간다.

    08-05 전량(12,561콜) + 08-06 장전(752콜) 대조에서 예외가 하나도 없었다 — EGW00201 65건이
    **전부** 직전 호출과의 완료 간격 1.00초 미만이었고, 1.00초 이상인 **10,811건에서는 0건**이다.

    따라서 이 테스트가 요구하는 것은 하나다: **완료로부터 min_interval이 지나야 다음이 나간다.**
    """
    clock = _FakeClock()
    with mock.patch("mahdi.broker.rest_client.time", clock):
        limiter = _RateLimiter(1.0)
        limiter.wait()  # 유휴 뒤 첫 호출 — 즉시 나간다
        clock.now += 0.5  # 이 호출이 0.5초 걸렸다
        limiter.record_completion()
        completed_at = clock.now

        limiter.wait()
        gap = clock.now - completed_at

    assert gap == pytest.approx(1.0), (
        f"완료로부터 {gap:.2f}초 만에 다음 호출이 나갔다(시작 기준이면 0.5초) — "
        "이 간격이 1.00초 밑이면 EGW00201이 다시 하루 57건 난다"
    )


def test_pacer_completion_never_pulls_the_reservation_earlier():
    """`record_completion()`은 **늦추기만** 한다.

    백오프 확대 직후처럼 `wait()`가 이미 더 먼 미래를 예약해 둔 상황에서 완료 시각 기준으로
    덮어쓰면 그 보수성이 깎인다 — 두 경로가 서로의 안전마진을 지우면 안 된다.
    """
    clock = _FakeClock()
    with mock.patch("mahdi.broker.rest_client.time", clock):
        limiter = _RateLimiter(1.0)
        limiter.record_rate_limit_hit()  # 간격 1.5초로 확대
        limiter.wait()
        reserved = limiter._next_allowed
        limiter.record_completion()  # 완료가 즉시라면 now+1.5 < reserved
        assert limiter._next_allowed >= reserved, "완료 기준 재예약이 기존 예약을 앞당겼다"


def test_pacer_completion_is_recorded_even_when_the_call_raises():
    """타임아웃/500으로 끝난 호출도 KIS 입장에서는 **처리한 호출**이다.

    예외 경로에서 밀지 않으면, 실패가 잦은 구간에서 정확히 그 구간의 페이싱이 느슨해진다 —
    이미 레이트리밋에 걸려 있는 상황을 우리 손으로 악화시키는 셈이다.
    """
    settings = _settings()
    daemon = mock.Mock(spec=TokenDaemon)
    daemon.get_token.return_value = "tok"
    client = mock.Mock(spec=httpx.Client)
    client.get.side_effect = httpx.ReadTimeout("timeout")
    rest = KISRestClient(settings, daemon, client=client)

    with mock.patch.object(rest._rate_limiter, "record_completion") as recorded:
        with pytest.raises(httpx.ReadTimeout):
            rest._send_get(f"{tr_codes.VPS_REST_DOMAIN}/uapi/domestic-stock/v1/quotations/inquire-price")
    recorded.assert_called_once()


# ===== 2026-08-16 (Block C) — 취소·정정·조회 =====
#
# 이 셋은 오늘까지 **한 번도 호출된 적이 없다**(주문이 나간 적이 없다). 그래서 이 절이 검사하는
# 것은 "동작하는가"가 아니라 **"공식 문서가 Required로 적은 필드를 전부 보내는가"** 다 —
# 잔고 조회가 필수 파라미터 4개 누락으로 **항상 실패**하고 있던 것을 2026-07-28에야 발견했고,
# 그 실패는 이런 테스트가 없어서 넉 달을 살아남았다.


def _capturing_client(response: dict | None = None):
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        entry = {"url": str(request.url), "headers": request.headers}
        if request.content:
            entry["body"] = _json.loads(request.content)
        captured.append(entry)
        return httpx.Response(200, json=response if response is not None else {"rt_cd": "0"})

    client = KISRestClient(
        _settings(),
        _token_daemon(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0.0,
    )
    return client, captured


def test_cancel_order_sends_every_required_field_with_the_documented_cancel_values():
    """"선물옵션 정정취소주문" 시트가 취소에 대해 못박은 값 셋:

        UNIT_PRICE = 0        ("취소 시에도 0 입력")
        KRX_NMPR_CNDT_CD = 0  ("취소시 0으로 입력")
        ORD_QTY = 0           ("전량일경우 0으로 입력")

    그리고 `NMPR_TYPE_CD`/`ORD_DVSN_CD`는 취소에서도 Required다 — 비우면 KIS가 거부한다.
    """
    client, captured = _capturing_client()
    client.cancel_order("0000001666")

    body = captured[0]["body"]
    assert body["RVSE_CNCL_DVSN_CD"] == tr_codes.RVSE_CNCL_CANCEL == "02"
    assert body["ORGN_ODNO"] == "0000001666"
    assert body["ORD_QTY"] == "0"
    assert body["UNIT_PRICE"] == "0"
    assert body["KRX_NMPR_CNDT_CD"] == "0"
    assert body["RMN_QTY_YN"] == "Y"  # 전량
    assert body["ORD_PRCS_DVSN_CD"] == "02"
    # Required인데 빠지기 쉬운 둘 — 없으면 거부당한다.
    assert body["NMPR_TYPE_CD"] and body["ORD_DVSN_CD"]
    assert "order-rvsecncl" in captured[0]["url"]
    assert captured[0]["headers"]["tr_id"] == tr_codes.TR_ORDER_MODIFY_CANCEL["vps"] == "VTTO1103U"


def test_partial_cancel_flips_the_remaining_quantity_flag():
    """전량(0)과 일부(N개)는 `RMN_QTY_YN`으로 갈린다 — 문서: Y=전량 / N=일부."""
    client, captured = _capturing_client()
    client.cancel_order("0000001666", qty=1)

    assert captured[0]["body"]["ORD_QTY"] == "1"
    assert captured[0]["body"]["RMN_QTY_YN"] == "N"


def test_modify_order_differs_from_cancel_by_exactly_one_field():
    """정정과 취소는 같은 TR·같은 경로이고 `RVSE_CNCL_DVSN_CD` 한 글자만 다르다.
    본문을 두 곳에서 만들면 Required 목록이 갈리므로 한 함수가 만든다."""
    client, captured = _capturing_client()
    client.cancel_order("0000001666")
    client.modify_order("0000001666", qty=0, price=3.55)

    cancel_body, modify_body = captured[0]["body"], captured[1]["body"]
    assert cancel_body["RVSE_CNCL_DVSN_CD"] == "02"
    assert modify_body["RVSE_CNCL_DVSN_CD"] == "01"
    differing = {k for k in cancel_body if cancel_body[k] != modify_body.get(k)}
    assert differing == {"RVSE_CNCL_DVSN_CD", "UNIT_PRICE"}  # 정정은 가격이 바뀐다


def test_inquire_ccnl_sends_the_required_blank_fields_rather_than_omitting_them():
    """`PDNO`/`MKET_ID_CD`/`CTX_AREA_*200`은 **Required인데 공란이 정상값**이다.
    Required니까 값을 채워야 한다고 오해하거나, 공란이니까 빼도 된다고 오해하면 둘 다 거부당한다.
    """
    client, captured = _capturing_client({"rt_cd": "0", "output1": []})
    client.inquire_ccnl("20260818", "20260818")

    url = captured[0]["url"]
    assert "inquire-ccnl" in url
    for required in ("CANO", "ACNT_PRDT_CD", "STRT_ORD_DT", "END_ORD_DT", "SLL_BUY_DVSN_CD",
                     "CCLD_NCCS_DVSN", "SORT_SQN", "STRT_ODNO", "PDNO", "MKET_ID_CD",
                     "CTX_AREA_FK200", "CTX_AREA_NK200"):
        assert f"{required}=" in url, f"{required}가 쿼리에 없다 — KIS가 거부한다"
    assert "STRT_ORD_DT=20260818" in url
    assert captured[0]["headers"]["tr_id"] == tr_codes.TR_ORDER_CCNL_INQUIRY["vps"] == "VTTO5201R"


def test_get_order_fill_status_finds_the_order_by_the_lowercase_field():
    """조회 응답은 **소문자** `odno`다(제출 응답은 대문자 `ODNO`) — 대소문자를 섞으면 조용히 못 찾는다."""
    from datetime import date

    client, _ = _capturing_client({
        "rt_cd": "0",
        "output1": [
            {"odno": "0000000001", "ord_qty": "1", "qty": "1", "tot_ccld_qty": "0", "rjct_qty": "0"},
            {"odno": "0000001666", "ord_qty": "2", "qty": "0", "tot_ccld_qty": "2",
             "rjct_qty": "0", "avg_idx": "3.55"},
        ],
    })
    status = client.get_order_fill_status("0000001666", as_of=date(2026, 8, 18))

    assert status == {"state": "FILLED", "filled_px": 3.55, "filled_qty": 2}


def test_get_order_fill_status_returns_pending_and_warns_when_the_order_is_absent(caplog):
    """접수 직후 조회에는 안 보일 수 있다 — PENDING(전이 없음)이 안전한 쪽이다.
    **다만 조용히 넘기지 않는다**: 영구히 안 보이면 그것은 접수 실패이고 사람이 알아야 한다."""
    from datetime import date

    client, _ = _capturing_client({"rt_cd": "0", "output1": []})
    with caplog.at_level("WARNING"):
        status = client.get_order_fill_status("0000009999", as_of=date(2026, 8, 18))

    assert status["state"] == "PENDING"
    assert "0000009999" in caplog.text


def test_parse_fill_status_reads_qty_as_remaining_not_as_order_quantity():
    """**`qty`는 「잔량」이다.** 주문수량으로 착각하면 CANCELLED와 PENDING이 뒤집힌다.

    같은 주문수량 2계약인데 잔량만 다른 두 행이 서로 다른 상태로 갈려야 한다.
    """
    from mahdi.broker.rest_client import parse_fill_status

    untouched = {"ord_qty": "2", "qty": "2", "tot_ccld_qty": "0", "rjct_qty": "0"}
    cancelled = {"ord_qty": "2", "qty": "0", "tot_ccld_qty": "0", "rjct_qty": "0"}

    assert parse_fill_status(untouched)["state"] == "PENDING"
    assert parse_fill_status(cancelled)["state"] == "CANCELLED"


def test_parse_fill_status_treats_any_rejection_as_rejected_first():
    """거부가 최우선이다 — 일부 체결 + 일부 거부도 거부로 읽는다(모르면 안전한 쪽)."""
    from mahdi.broker.rest_client import parse_fill_status

    row = {"ord_qty": "2", "qty": "0", "tot_ccld_qty": "1", "rjct_qty": "1", "avg_idx": "3.5"}
    assert parse_fill_status(row)["state"] == "REJECTED"


def test_parse_fill_status_handles_zero_padded_strings_and_blanks():
    """KIS 수치는 전부 문자열이고 `0000000002`처럼 패딩되며 공란이 올 수 있다."""
    from mahdi.broker.rest_client import parse_fill_status

    row = {"ord_qty": "0000000002", "qty": "0000000000", "tot_ccld_qty": "0000000002",
           "rjct_qty": "", "avg_idx": "007840000"}
    assert parse_fill_status(row) == {"state": "FILLED", "filled_px": 7840000.0, "filled_qty": 2}


def test_parse_fill_status_reports_no_price_when_nothing_filled():
    """체결이 0이면 평균지수가 와도 체결가로 쓰지 않는다 — 「못 쟀다」와 「0에 체결」은 다르다."""
    from mahdi.broker.rest_client import parse_fill_status

    row = {"ord_qty": "2", "qty": "2", "tot_ccld_qty": "0", "rjct_qty": "0", "avg_idx": "3.55"}
    assert parse_fill_status(row)["filled_px"] is None


def test_format_order_price_emits_plain_zero_not_zero_point_zero():
    """`str(0.0)`은 `"0.0"`이다. 문서는 취소 시 `UNIT_PRICE`에 **"0"** 을 요구한다 —
    이 한 글자가 8/18 실측을 실패시킬 수 있었다(테스트가 실제로 잡아냈다)."""
    from mahdi.broker.rest_client import format_order_price

    assert format_order_price(0.0) == "0"
    assert format_order_price(350.0) == "350"      # 정수는 소수점 없이
    assert format_order_price(3.55) == "3.55"      # 소수는 그대로
    assert format_order_price(350.25) == "350.25"  # 선물 0.05틱
    assert format_order_price(0.01) == "0.01"      # 옵션 최소 호가
    assert "e" not in format_order_price(0.0001)   # 지수 표기가 새지 않는다


def test_submit_and_cancel_use_the_same_price_format():
    """두 경로가 다른 형식을 보내면 한쪽만 거부당하고 원인 규명이 길어진다."""
    client, captured = _capturing_client()
    client.submit_order(symbol="201W09", side="BUY", qty=1, price=350.0)
    client.cancel_order("0000001666")

    assert captured[0]["body"]["UNIT_PRICE"] == "350"
    assert captured[1]["body"]["UNIT_PRICE"] == "0"
