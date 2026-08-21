import asyncio
import itertools
import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import pytest

from mahdi.broker import tr_codes
from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data import yfinance_fallback
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.features.options_intel import OptionLeg, calculate_gex, legs_from_chain_rows
from mahdi.features.orderflow import calculate_vpin
from mahdi.fusion.signal_layer import SignalInputs, build_member_scores
from mahdi.logutil import WarningThrottle
from mahdi.risk.engine import RiskEngine
from mahdi.risk.market_halt import MarketHaltMonitor
import mahdi.main as mahdi_main
from mahdi.main import (
    _advance_fixed_tick,
    _books_due_this_cycle,
    _macro_items_due,
    _atm_liquidity_window,
    _build_account_state_for_candidate,
    _build_signal_inputs,
    _parse_asking_price_leg,
    _parse_futures_tick,
    _parse_market_operation,
    _parse_option_quote,
    _parse_overseas_daily_last_price,
    _parse_overseas_future_last_price,
    _parse_tick,
    poll_account_balance_cycle,
    poll_expiry_liquidity,
    poll_investor_flow,
    poll_macro_snapshot,
    poll_option_chain,
    poll_signal_fusion_cycle,
    run_observation_loop,
    run_observation_loop_forever,
)

# 2026-08-21 — 공식 문서(docs/efriend, 시트 「지수옵션 실시간체결가」)의 Response Body는
# idx 0..57로 **58필드**다. 종전 45는 우리가 그때까지 읽던 범위였을 뿐 프레임의 실제 폭이
# 아니었다 — 누적 체결량(48/49)을 읽기 시작하며 실제 폭으로 맞춘다.
_NUM_FIELDS = 58  # 공식 문서 실측(index 0..57)
# 2026-07-31: 매크로 항목별 갱신 주기(ZN 1시간 / MOVE·일봉 6시간)와 무관하게 "매 사이클 전부
# 조회"로 고정하고 싶은 테스트용 — 스트릭/알림처럼 주기와 별개인 동작을 검증할 때 쓴다.
_MACRO_ITEMS_EVERY_CYCLE = {item: 0.0 for item in mahdi_main.MACRO_ITEM_REFRESH_SECONDS}
_FALLBACK_PRICE = 99.9  # yfinance 폴백 스텁이 돌려줄 값(실제 값과 구분되는 임의 숫자)
_FUT_NUM_FIELDS = 50  # 공식 문서 시트 「지수선물 실시간체결가」 실측(index 0..49)


class _FakeRegimeStateMachine:
    """run_observation_loop 테스트용 — 실제 RegimeStateMachine 대신 DB 접근 없이 고정값만 반환."""

    def update_bar(self, bar) -> None:
        pass

    def step(self, conn, timestamp) -> RegimeState:
        return RegimeState(regime=RegimeLabel.RANGE_BALANCED, prob_vector=(0.0,) * 8, stability_flag=False, is_warmup=True)


def _make_h0ifcnt0(
    hhmmss: str,
    price: float,
    volume: float,
    ask: float,
    bid: float,
    ask_qty: float,
    bid_qty: float,
    symbol: str = "101S03",
    with_ws_envelope: bool = False,
) -> str:
    """H0IFCNT0(지수선물 실시간체결가) 실측 필드 순서로 캐럿(^) 구분 메시지를 합성한다."""
    fields = ["0"] * _FUT_NUM_FIELDS
    fields[0] = symbol  # FUTS_SHRN_ISCD
    fields[1] = hhmmss  # BSOP_HOUR
    fields[5] = str(price)  # FUTS_PRPR
    fields[9] = str(volume)  # LAST_CNQN
    fields[34] = str(ask)  # FUTS_ASKP1
    fields[35] = str(bid)  # FUTS_BIDP1
    fields[36] = str(ask_qty)  # ASKP_RSQN1
    fields[37] = str(bid_qty)  # BIDP_RSQN1
    body = "^".join(fields)
    return f"0|H0IFCNT0|001|{body}" if with_ws_envelope else body


def _make_h0iocnt0(
    hhmmss: str,
    price: float,
    volume: float,
    ask: float,
    bid: float,
    ask_qty: float,
    bid_qty: float,
    symbol: str = "201S03C325",
    with_ws_envelope: bool = False,
) -> str:
    """H0IOCNT0 실측 필드 순서에 맞춰 캐럿(^) 구분 메시지를 합성한다 (사용 안 하는 필드는 0).

    with_ws_envelope=True면 KIS가 실제로 붙이는 "암호화유무|TR_ID|데이터건수|" 헤더까지 포함한다.
    """
    fields = ["0"] * _NUM_FIELDS
    fields[0] = symbol  # MKSC_SHRN_ISCD
    fields[1] = hhmmss  # BSOP_HOUR
    fields[2] = str(price)  # OPTN_PRPR
    fields[9] = str(volume)  # LAST_CNQN
    fields[41] = str(ask)  # OPTN_ASKP1
    fields[42] = str(bid)  # OPTN_BIDP1
    fields[43] = str(ask_qty)  # ASKP_RSQN1
    fields[44] = str(bid_qty)  # BIDP_RSQN1
    body = "^".join(fields)
    return f"0|H0IOCNT0|001|{body}" if with_ws_envelope else body


def _run(coro):
    return asyncio.run(coro)


def test_parse_tick_valid_h0iocnt0_format():
    raw = _make_h0iocnt0(
        "093015", price=350.5, volume=10, ask=350.6, bid=350.4, ask_qty=120, bid_qty=100, symbol="201S03C325"
    )
    parsed = _parse_tick(raw, today=date(2026, 7, 6))
    assert parsed is not None
    symbol, tick = parsed
    assert symbol == "201S03C325"
    assert tick.timestamp.hour == 9 and tick.timestamp.minute == 30 and tick.timestamp.second == 15
    assert tick.price == 350.5
    assert tick.volume == 10
    assert tick.bid_px == 350.4
    assert tick.bid_qty == 100
    assert tick.ask_px == 350.6
    assert tick.ask_qty == 120


def test_parse_tick_strips_ws_envelope_header_from_symbol():
    # 실제 KIS WS 프레임은 "암호화유무|TR_ID|데이터건수|실제데이터" 헤더가 붙어서 온다.
    # 헤더를 안 벗기면 0번 필드(종목코드)에 헤더 전체가 달라붙어 DB VARCHAR(20)을 넘긴다
    # (2026-07-06 실거래 중 StringDataRightTruncation으로 발견).
    raw = _make_h0iocnt0(
        "093015", price=350.5, volume=10, ask=350.6, bid=350.4, ask_qty=120, bid_qty=100,
        symbol="201S03C325", with_ws_envelope=True,
    )
    parsed = _parse_tick(raw, today=date(2026, 7, 6))
    assert parsed is not None
    symbol, tick = parsed
    assert symbol == "201S03C325"
    assert len(symbol) <= 20
    assert tick.price == 350.5


def test_parse_tick_invalid_format_returns_none():
    assert _parse_tick("garbage") is None
    assert _parse_tick("1^2") is None


def test_parse_futures_tick_valid_h0ifcnt0_format():
    raw = _make_h0ifcnt0(
        "093015", price=350.5, volume=10, ask=350.6, bid=350.4, ask_qty=120, bid_qty=100, symbol="101S03"
    )
    parsed = _parse_futures_tick(raw, today=date(2026, 7, 6))
    assert parsed is not None
    symbol, tick = parsed
    assert symbol == "101S03"
    assert tick.timestamp.hour == 9 and tick.timestamp.minute == 30 and tick.timestamp.second == 15
    assert tick.price == 350.5
    assert tick.volume == 10
    assert tick.bid_px == 350.4
    assert tick.bid_qty == 100
    assert tick.ask_px == 350.6
    assert tick.ask_qty == 120


def test_parse_futures_tick_strips_ws_envelope_header():
    raw = _make_h0ifcnt0(
        "093015", price=350.5, volume=10, ask=350.6, bid=350.4, ask_qty=120, bid_qty=100,
        symbol="101S03", with_ws_envelope=True,
    )
    parsed = _parse_futures_tick(raw, today=date(2026, 7, 6))
    assert parsed is not None
    symbol, tick = parsed
    assert symbol == "101S03"
    assert tick.price == 350.5


def test_parse_futures_tick_invalid_format_returns_none():
    assert _parse_futures_tick("garbage") is None
    assert _parse_futures_tick("1^2") is None


def _make_h0unmko0(
    mkop_cls_code: str, trht_yn: str = "N", tr_susp_reas_cntt: str = "", vi_cls_code: str = "0",
    with_ws_envelope: bool = False,
) -> str:
    """H0UNMKO0(국내주식 장운영정보) Layout 순서(TRHT_YN..EXCH_CLS_CODE, 9필드)로 합성한다."""
    fields = ["0"] * 9
    fields[0] = trht_yn
    fields[1] = tr_susp_reas_cntt
    fields[2] = mkop_cls_code
    fields[7] = vi_cls_code
    body = "^".join(fields)
    return f"0|H0UNMKO0|001|{body}" if with_ws_envelope else body


def test_parse_market_operation_valid_h0unmko0_format():
    raw = _make_h0unmko0("174", trht_yn="Y", vi_cls_code="1")
    status = _parse_market_operation(raw)
    assert status is not None
    assert status.mkop_cls_code == "174"
    assert status.trht_yn == "Y"
    assert status.vi_cls_code == "1"


def test_parse_market_operation_strips_ws_envelope_header():
    raw = _make_h0unmko0("175", with_ws_envelope=True)
    status = _parse_market_operation(raw)
    assert status is not None
    assert status.mkop_cls_code == "175"


def test_parse_market_operation_invalid_format_returns_none():
    assert _parse_market_operation("garbage") is None
    assert _parse_market_operation("1^2") is None


class FakeConnection:
    def __init__(self, incoming: list[str]):
        self.sent: list[str] = []
        self._incoming = list(incoming)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._incoming:
            raise ConnectionError("픽스처 소진")
        return self._incoming.pop(0)

    async def close(self) -> None:
        pass


class FakeRestClient:
    def __init__(self, spot: float):
        self._spot = spot
        self.calls = 0

    def get_quote(self, symbol: str, market_div_code: str) -> dict:
        self.calls += 1
        return {"output3": {"bstp_nmix_prpr": str(self._spot)}}


def test_run_observation_loop_writes_bar_and_regime_on_minute_rollover(monkeypatch):
    # 09:00:00~09:00:20 3틱 → 09:01:00 진입 틱으로 flush 유도 (BSOP_HOUR 필드로 결정론적 제어)
    incoming = [
        _make_h0iocnt0("090000", 350.0, 10, 350.05, 349.95, 100, 100),
        _make_h0iocnt0("090010", 350.5, 20, 350.55, 350.45, 100, 100),
        _make_h0iocnt0("090020", 350.2, 5, 350.25, 350.15, 100, 100),
        _make_h0iocnt0("090100", 351.0, 8, 351.05, 350.95, 100, 100),  # 다음 분 → flush 트리거
    ]
    conn = FakeConnection(incoming)
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    subscription_manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    rest_client = FakeRestClient(spot=350.0)

    written_bars = []
    written_regimes = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def fake_insert_market_raw_1m(conn, row):
        written_bars.append(row)

    def fake_insert_regime_state(conn, **kwargs):
        written_regimes.append(kwargs)

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", fake_insert_market_raw_1m)
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", fake_insert_regime_state)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda conn, underlying, symbol, updated_at: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )

    assert rest_client.calls == 1
    assert subscription_manager.desired_strikes  # 초기 ATM 구독이 수행됨
    assert len(written_bars) == 1
    bar = written_bars[0]
    assert bar["symbol"] == "201S03C325"
    assert bar["open"] == 350.0
    assert bar["close"] == 350.2
    # 2026-07-10: 레짐은 선물봉 완성 시에만 갱신한다 — 이 테스트의 틱은 전부 옵션(futures_symbol과
    # 다른 심볼)이라 옵션봉은 market_raw_1m에 적재되지만 regime_state는 갱신되지 않아야 한다.
    assert len(written_regimes) == 0


def test_run_observation_loop_keeps_different_symbols_in_separate_bars(monkeypatch):
    # 서로 다른 두 종목(콜/풋)의 틱이 같은 분에 섞여 들어와도 각자 별도 봉으로 집계돼야 한다 —
    # 예전에는 aggregator를 하나만 써서 종목이 뒤섞였다(2026-07-06 실데이터로 발견한 버그).
    incoming = [
        _make_h0iocnt0("090000", 60.0, 10, 60.05, 59.95, 100, 100, symbol="201S03C325"),
        _make_h0iocnt0("090010", 40.0, 10, 40.05, 39.95, 100, 100, symbol="201S03P325"),
        _make_h0iocnt0("090020", 62.0, 5, 62.05, 61.95, 100, 100, symbol="201S03C325"),
        _make_h0iocnt0("090030", 41.0, 5, 41.05, 40.95, 100, 100, symbol="201S03P325"),
        _make_h0iocnt0("090100", 63.0, 8, 63.05, 62.95, 100, 100, symbol="201S03C325"),  # 다음 분 → flush 트리거
    ]
    conn = FakeConnection(incoming)
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    subscription_manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    rest_client = FakeRestClient(spot=350.0)

    written_bars = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: written_bars.append(row))
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", lambda conn, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda conn, underlying, symbol, updated_at: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )

    assert len(written_bars) == 1  # 09:00분 봉은 콜만 flush됨(풋은 아직 진행 중인 분이라 미완성)
    call_bar = next(b for b in written_bars if b["symbol"] == "201S03C325")
    assert call_bar["open"] == 60.0
    assert call_bar["close"] == 62.0  # 40.0/41.0(풋) 값이 섞이면 안 됨


class _SingleUseConnectionCM:
    """websockets.connect()의 `async with` 반환값을 흉내낸다 — 한 번만 __aenter__되는 1회용."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnectCall:
    """connect(url) 호출마다 순서대로 다음 항목을 반환(또는 예외면 즉시 발생)하는 팩토리 —
    실제 소켓 없이 WS 재연결 시나리오(연속 실패·성공 후 재끊김 등)를 결정론적으로 재현한다."""

    def __init__(self, items: list):
        self._items = list(items)
        self.call_count = 0

    def __call__(self, url):
        self.call_count += 1
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _SingleUseConnectionCM(item)


def _patch_run_observation_loop_db(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", lambda conn, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda conn, underlying, symbol, updated_at: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)


def test_run_observation_loop_forever_reconnects_and_resubscribes_after_disconnect(monkeypatch):
    # 2026-07-16 점검 §3-1B/§5-2: "WS 연결이 끊기면 재연결 로직이 아예 없어 그대로 죽는다"는
    # 문제 — 재연결 후 새 연결에 구독(선물 + ATM 옵션 전 종목)이 처음부터 다시 나가는지 검증한다.
    rest_client = FakeRestClient(spot=350.0)

    first_conn = FakeConnection([])  # recv() 즉시 ConnectionError(끊김 시뮬레이션)
    ws_client = KISWebSocketClient(approval_key="APV1", connection=first_conn)
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    second_conn = FakeConnection([])  # 재연결 성공 직후에도 바로 다시 끊김(연속 끊김까지 검증)
    fake_connect = _FakeConnectCall([second_conn, RuntimeError("세 번째 연결 시도는 테스트 범위 밖")])

    _patch_run_observation_loop_db(monkeypatch)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    liveness = mahdi_main.WsLiveness(market_op_subscribed_at=datetime(2026, 8, 4, 7, 31))

    with pytest.raises(RuntimeError, match="세 번째 연결 시도는 테스트 범위 밖"):
        _run(
            run_observation_loop_forever(
                ws_client, [manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                approval_key="APV1", connect=fake_connect,
                ws_liveness=liveness,
            )
        )

    assert fake_connect.call_count == 2  # 재연결 성공 1회 + 그다음 재연결 시도(실패로 테스트 종료)
    # 2026-08-03 §4 우선순위 4: 새 연결은 KIS 쪽 구독 상태가 전부 초기화된 상태다 — 재구독 ACK이
    # 다시 올 때까지는 "구독 성립"이 아니다. 직전 연결의 시각을 그대로 두면 끊긴 구독이 살아 있는
    # 것처럼 보인다(이 테스트의 second_conn은 ACK을 돌려주지 않는다).
    assert liveness.market_op_subscribed_at is None
    assert liveness.reconnect_count == 1
    # 연결에 성공하면 backoff가 초기값으로 리셋된다 — 두 번의 끊김 모두 "첫 끊김"이라 둘 다 5초.
    assert sleep_calls == [5.0, 5.0]

    # 재연결된 새 연결(second_conn)에 futures 구독 + ATM 옵션 전 종목이 처음부터 다시 나가야 한다
    # (연결이 끊겼다 다시 붙으면 서버 쪽 구독 상태는 사라지므로, 스팟이 그대로여도 재구독 필요 —
    # RollingSubscriptionManager.rebind()가 없으면 diff 로직 때문에 아무것도 재전송되지 않는다).
    assert any("101S03" in msg for msg in second_conn.sent)  # 선물 구독
    assert manager.desired_strikes == frozenset({347.5, 350.0, 352.5})
    subscribe_msgs = [m for m in second_conn.sent if '"tr_type": "1"' in m]
    assert len(subscribe_msgs) == 8  # 3 strikes x (C,P) = 6 + 선물 1 + 장운영정보(H0UNMKO0) 1

    # 2026-07-19(§5-4): "연결됨→끊김"(최초) → "끊김→재연결 성공" → "연결됨→끊김"(재재연결 전
    # 두 번째 끊김) 세 번의 상태 전환마다 Slack 알림이 한 번씩만 나가야 한다(재시도마다 매번X).
    assert [level for _, level in notify_calls] == ["CRITICAL", "INFO", "CRITICAL"]
    assert "끊김" in notify_calls[0][0]
    assert "재연결 성공" in notify_calls[1][0]
    assert "끊김" in notify_calls[2][0]


def test_run_observation_loop_forever_backoff_caps_and_resets_after_success(monkeypatch):
    # connect() 자체가 반복 실패하면(네트워크 장애 등) 백오프가 계속 커지되 상한(60초)을 넘지
    # 않고, 한 번이라도 연결에 성공하면 다음 끊김부터 다시 초기값(5초)으로 리셋되는지 확인한다.
    rest_client = FakeRestClient(spot=350.0)
    first_conn = FakeConnection([])
    ws_client = KISWebSocketClient(approval_key="APV1", connection=first_conn)
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    ok_conn = FakeConnection([])
    fake_connect = _FakeConnectCall(
        [
            OSError("연결 거부"),
            OSError("연결 거부"),
            ok_conn,  # 3번째 시도에서 연결 성공(들어가자마자 다시 끊김) → backoff 리셋 확인용
            RuntimeError("종료용"),
        ]
    )

    _patch_run_observation_loop_db(monkeypatch)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="종료용"):
        _run(
            run_observation_loop_forever(
                ws_client, [manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                approval_key="APV1", connect=fake_connect,
            )
        )

    # 5(최초 끊김) → 10(1차 재연결 실패 후) → 20(2차 재연결 실패 후) → 연결 성공(리셋) → 5(성공 후 재끊김)
    assert sleep_calls == [5.0, 10.0, 20.0, 5.0]


def test_run_observation_loop_forever_propagates_non_connection_errors(monkeypatch):
    # DB 오류/ValueError(설정 문제 등) 같은 "연결 문제가 아닌" 예외는 재시도 없이 그대로 전파해야
    # 한다 — 재연결로 해결되지 않는 코드/설정 문제를 조용히 계속 삼키면 안 된다.
    rest_client = FakeRestClient(spot=350.0)
    conn = FakeConnection([])
    ws_client = KISWebSocketClient(approval_key="APV1", connection=conn)
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def raise_config_error(conn, underlying, symbol, updated_at):
        raise ValueError("설정 오류")

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", raise_config_error)

    async def fake_sleep(seconds):
        raise AssertionError("연결 문제가 아닌 예외에 재시도(sleep)가 호출되면 안 됨")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    def unexpected_connect(url):
        raise AssertionError("연결 문제가 아닌 예외에 재연결이 시도되면 안 됨")

    with pytest.raises(ValueError, match="설정 오류"):
        _run(
            run_observation_loop_forever(
                ws_client, [manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                approval_key="APV1", connect=unexpected_connect,
            )
        )


# 2026-07-06 실제 KIS 모의투자 get_quote() 응답에서 그대로 가져온 값(그릭스 필드명 실측: gama/delta_val 등).
_SAMPLE_OPTION_QUOTE = {
    "output1": {
        "hts_kor_isnm": "C 202607 1,340.0",
        "futs_prpr": "40.65",
        "hts_otst_stpl_qty": "363",
        "otst_stpl_qty_icdc": "-18",
        "delta_val": "0.4850",
        "gama": "0.0047",
        "theta": "-5.7158",
        "vega": "0.4821",
        "hist_vltl": "70.6184",
        "hts_ints_vltl": "90.1284",
        "acpr": "1340.00",
        "futs_last_tr_date": "20260709",
        "acml_vol": "30",
    },
    "output3": {"bstp_nmix_prpr": "1333.77"},
}


def test_parse_option_quote_valid_response():
    poll_time = datetime(2026, 7, 6, 9, 31)
    parsed = _parse_option_quote(_SAMPLE_OPTION_QUOTE, strike=1340.0, option_type="C", poll_time=poll_time)
    assert parsed is not None
    row, spot = parsed
    assert spot == 1333.77
    assert row["strike"] == 1340.0
    assert row["option_type"] == "C"
    assert row["expiry"] == date(2026, 7, 9)
    assert row["delta"] == 0.4850
    assert row["gamma"] == 0.0047
    assert row["theta"] == -5.7158
    assert row["vega"] == 0.4821
    assert row["iv"] == pytest.approx(0.901284)
    assert row["rv_5d"] == pytest.approx(0.706184)
    assert row["oi"] == 363
    assert row["oi_change"] == -18
    assert row["volume"] == 30
    assert row["vrp"] == pytest.approx(0.901284 - 0.706184)

    t_years = (date(2026, 7, 9) - date(2026, 7, 6)).days / 365.0
    expected_leg = OptionLeg(strike=1340.0, option_type="c", oi=363.0, iv=0.901284, t_years=t_years, gamma=0.0047)
    assert row["gex"] == pytest.approx(calculate_gex([expected_leg], 1333.77))


def test_parse_option_quote_missing_field_returns_none():
    assert _parse_option_quote({}, strike=1340.0, option_type="C", poll_time=datetime(2026, 7, 6, 9, 31)) is None


def test_parse_option_quote_carries_raw_kis_output1_for_diagnostics():
    # 2026-07-16: DB 삽입 실패(NumericValueOutOfRange 등) 시 "무엇이" 이상값이었는지 되짚어볼
    # 수 있게, 파싱 전 원본 output1을 row에 함께 실어 나른다 — DB 컬럼이 아니므로 _upsert()가
    # 무시하고(insert_option_analysis_1m 쿼리에 안 섞임), 실패 로그에서만 쓰인다.
    poll_time = datetime(2026, 7, 6, 9, 31)
    row, _ = _parse_option_quote(_SAMPLE_OPTION_QUOTE, strike=1340.0, option_type="C", poll_time=poll_time)
    assert row["_raw_kis_output1"] == _SAMPLE_OPTION_QUOTE["output1"]


class _FakeMaster:
    def option_symbol(
        self, option_type: str, strike: float, underlying: str = "KOSPI200", series: str = "regular"
    ) -> str | None:
        return f"SYM{int(strike)}{option_type}"


class _FakeSubscriptionManagerWithStrikes:
    @property
    def desired_strikes(self) -> frozenset[float]:
        return frozenset({1340.0})


class _FakeRestClientChain:
    def __init__(self, resp: dict):
        self._resp = resp
        self.calls: list[str] = []
        self.rate_limit_backoff_multiplier = 1.0

    @property
    def rate_limit_total_calls(self) -> int:
        return len(self.calls)

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        return self._resp


def test_poll_option_chain_writes_legs_and_spot_once_per_cycle(monkeypatch):
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)
    written_rows: list[dict] = []
    written_spots: list[float] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: written_rows.append(row))
    monkeypatch.setattr(
        "mahdi.main.db.insert_underlying_spot",
        lambda conn, timestamp, underlying, spot: written_spots.append(spot),
    )
    # 2026-08-05(§2 이상점 8): 스팟 적재에 장중 조건이 생겼다 — 벽시계에 의존하면 이 테스트가
    # 장전에만 실패하는 시한폭탄이 된다. 시각을 고정한다.
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 5, 10, 0))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=1,
            )
        )

    assert len(rest_client.calls) == 2  # 1개 행사가 x (C, P)
    assert len(written_rows) == 2
    assert written_spots == [1333.77]  # 사이클당 한 번만 적재(레그마다 중복 적재 안 함)


def test_poll_option_chain_does_not_store_the_pre_open_spot(monkeypatch):
    """08-05 §2 이상점 8 — KOSPI200 지수는 09:00 이전에 체결되지 않는데
    `output3.bstp_nmix_prpr`는 장전에도 **전일 종가**를 돌려준다. 그것을 매분 새 행으로
    적재하면 07:31~09:00 약 90분치가 "지금 시장"인 것처럼 쌓이고, 행 자체는 1분 전 것이라
    신선도로는 절대 걸러낼 수 없다.

    08-05 실측: 07:31~09:00 내내 정확히 1000.0300(= 08-04 종가), 09:01에 1042.91로 점프 —
    오버나이트 갭 +4.29%를 장전 90분 동안 못 본 채 GEX/VRP를 계산했다.
    """
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)
    written_rows: list[dict] = []
    written_spots: list[float] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: written_rows.append(row))
    monkeypatch.setattr(
        "mahdi.main.db.insert_underlying_spot",
        lambda conn, timestamp, underlying, spot: written_spots.append(spot),
    )
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 5, 8, 50))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=1,
            )
        )

    assert written_spots == []  # 장전 스팟은 남기지 않는다
    assert len(written_rows) == 2  # 그릭스/IV는 그대로 적재한다(옵션은 장전에도 호가가 있다)


class _FakeConnWithRollback:
    """단순 object()와 달리 rollback()을 지원 — DB 삽입 실패 시 트랜잭션 복구 경로를 검증한다."""

    def __init__(self):
        self.rollback_calls = 0

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_poll_option_chain_skips_bad_leg_and_continues_after_db_error(monkeypatch, caplog):
    # 2026-07-06 실운영 중 실제로 발생: 위클리 도입 후 얇은 종목의 IV 등이 DECIMAL(8,6) 범위를
    # 넘겨 psycopg.errors.NumericValueOutOfRange가 나면서 관측 루프 전체(선물 틱 수신 포함)가
    # 죽었다 — 레그 하나의 DB 삽입 실패가 rollback 후 다음 레그로 계속 이어져야 한다.
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)
    written_rows: list[dict] = []
    fake_conn = _FakeConnWithRollback()

    @contextmanager
    def fake_get_connection(settings=None):
        yield fake_conn

    call_count = {"n": 0}

    def fake_insert(conn, row):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("numeric field overflow")
        written_rows.append(row)

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", fake_insert)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client, [(_FakeSubscriptionManagerWithStrikes(), "regular")], _FakeMaster(), interval_seconds=1
                )
            )

    assert call_count["n"] == 2  # 1개 행사가 x (C, P) 둘 다 시도됨
    assert len(written_rows) == 1  # 첫 레그만 실패, 둘째 레그는 정상 적재됨(루프가 안 죽음)
    assert fake_conn.rollback_calls == 1

    # 2026-07-16: strike/type만으론 "어떤 값이" 범위를 넘었는지 알 수 없었다 — 실패 로그에
    # KIS 원본 응답(hts_ints_vltl 등 raw 필드)이 그대로 남아야 다음 재발 시 원인을 바로 특정할 수 있다.
    failure_records = [r for r in caplog.records if "옵션 체인 적재 실패" in r.getMessage()]
    assert len(failure_records) == 1
    assert "hts_ints_vltl" in failure_records[0].getMessage()
    assert _SAMPLE_OPTION_QUOTE["output1"]["hts_ints_vltl"] in failure_records[0].getMessage()


class _FakeRestClientChainFlaky:
    """처음 fail_calls건은 실패, 이후는 성공 — 사이클 전체 실패 후 재시도 복구를 재현한다."""

    def __init__(self, resp: dict, fail_calls: int):
        self._resp = resp
        self._fail_calls = fail_calls
        self.calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        if len(self.calls) <= self._fail_calls:
            raise RuntimeError("KIS 500")
        return self._resp


def test_poll_option_chain_retries_once_when_entire_cycle_fails(monkeypatch):
    # 2026-07-08 실측: 레이트리밋 버스트로 사이클 내 모든 종목 조회가 한꺼번에 실패하는 경우가
    # 있었다 — 다음 60초 사이클까지 기다리지 않고 짧게 대기 후 재시도해 복구되는지 검증한다.
    rest_client = _FakeRestClientChainFlaky(_SAMPLE_OPTION_QUOTE, fail_calls=2)  # 1차 시도(2건) 전부 실패
    written_rows: list[dict] = []
    written_spots: list[float] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: written_rows.append(row))
    monkeypatch.setattr(
        "mahdi.main.db.insert_underlying_spot",
        lambda conn, timestamp, underlying, spot: written_spots.append(spot),
    )
    # 2026-08-05(§2 이상점 8): 스팟 적재는 이제 장중 조건부다 — 시각을 고정한다.
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 5, 10, 0))

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if seconds != 5.0:  # retry_backoff_seconds가 아니라 정규 interval_seconds 사이클이면 루프 종료
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=1,
            )
        )

    assert len(rest_client.calls) == 4  # 1차 시도 2건 실패 + 재시도 2건 성공
    assert len(written_rows) == 2  # 재시도로 복구된 데이터가 결국 적재됨
    assert written_spots == [1333.77]
    assert 5.0 in sleep_calls  # 재시도 backoff가 실제로 대기했다


def test_poll_option_chain_gives_up_after_retry_still_fails(monkeypatch):
    rest_client = _FakeRestClientChainFlaky(_SAMPLE_OPTION_QUOTE, fail_calls=999)  # 항상 실패
    written_rows: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: written_rows.append(row))
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    async def fake_sleep(seconds):
        if seconds != 5.0:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=1,
            )
        )

    assert len(rest_client.calls) == 4  # 1차 2건 + 재시도 2건, 전부 실패 시도
    assert written_rows == []  # 재시도까지 실패하면 이번 사이클은 조용히 포기(다음 사이클엔 정상 진행)


def test_poll_option_chain_sends_gap_alert_after_5min_then_recovery_notice(monkeypatch):
    # 2026-07-19(§5-4): "option_analysis_1m이 5분 이상 결손"되면 Slack 경고를 한 번만 보내고
    # (매 60초 사이클마다 반복 경고하면 스팸), 데이터가 다시 들어오면 복구 알림을 한 번 보낸다.
    # _collect_option_chain_cycle 자체를 페이크로 바꿔 REST/파싱 세부사항과 분리해서 검증한다.
    base = datetime(2026, 7, 19, 9, 0)
    poll_times = [
        base,                          # iter0: 성공 → last_success_time 확정
        base + timedelta(minutes=1),   # iter1: 실패 시작(아직 5분 미만)
        base + timedelta(minutes=6),   # iter2: 마지막 성공 대비 6분 경과 → 경고 발송
        base + timedelta(minutes=7),   # iter3: 여전히 실패 — 중복 경고 없어야 함
        base + timedelta(minutes=8),   # iter4: 복구 → 복구 알림
    ]
    # 2026-08-06 고도화#1 — 4번째 반환값은 "놓친 먼슬리 레그"다(이 테스트는 그 경로를 안 탄다).
    outcomes = [
        (["row0"], 350.0, True, []),
        ([], None, True, []),
        ([], None, True, []),
        ([], None, True, []),
        (["row4"], 350.0, True, []),
    ]
    idx = {"i": -1}

    def fake_local_now():
        idx["i"] += 1
        return poll_times[idx["i"]]

    async def fake_collect(rest_client, books, master, underlying, poll_time, warning_throttle, deadline=None):
        return outcomes[idx["i"]]

    monkeypatch.setattr("mahdi.main.db.local_now", fake_local_now)
    monkeypatch.setattr("mahdi.main._collect_option_chain_cycle", fake_collect)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    # 재시도 백오프를 사이클 종료 지연(next_tick 스케줄링 — interval_seconds=1에선 대략 1,2,3...초로
    # 매 사이클 늘어남)과 값으로 혼동되지 않을 만큼 확실히 다른 값으로 지정 — 그래야 아래 fake_sleep이
    # "재시도 대기"와 "사이클 종료 후 다음 틱 대기"를 값만으로 안전하게 구분할 수 있다.
    distinctive_retry_backoff = 999.0
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if seconds == distinctive_retry_backoff:
            return  # 재시도 백오프 — 그냥 통과
        if idx["i"] >= len(poll_times) - 1:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client=None,
                books=[],
                master=None,
                interval_seconds=1,
                retry_backoff_seconds=distinctive_retry_backoff,
            )
        )

    assert idx["i"] == len(poll_times) - 1
    assert len(notify_calls) == 2  # 경고 1건 + 복구 1건(iter1은 아직 5분 미만이라 경고 없음)
    gap_message, gap_level = notify_calls[0]
    assert gap_level == "WARNING"
    assert "결손" in gap_message
    recovery_message, recovery_level = notify_calls[1]
    assert recovery_level == "INFO"
    assert "복구" in recovery_message


class _FakeLoop:
    """asyncio.get_running_loop()를 대체 — .time() 호출마다 미리 정한 값을 순서대로 반환한다."""

    def __init__(self, times: list[float]):
        self._times = iter(times)

    def time(self) -> float:
        return next(self._times)


# 위상 0초·주기 60초 격자 위에 정확히 앉은 시각(초=0). 2026-07-31 §4 우선순위 5로 폴러 스케줄이
# 벽시계 격자에 앵커되면서, 고정 틱 테스트는 이벤트 루프 시계(_FakeLoop)뿐 아니라 **벽시계도**
# 고정해야 결정론적이 된다 — 그러지 않으면 실행 시각에 따라 첫 대기가 0~60초로 흔들린다.
_GRID_ALIGNED_NOW = datetime(2026, 7, 31, 10, 30, 0)


def _pin_wall_clock(monkeypatch, now: datetime = _GRID_ALIGNED_NOW) -> None:
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: now)


def test_poll_option_chain_uses_fixed_tick_schedule_not_sleep_after_work(monkeypatch):
    # 2026-07-09: "작업 후 interval만큼 sleep"이면 사이클 소요시간만큼 실제 주기가 매번 밀려
    # poll_time(분 단위)이 분 경계를 건너뛰는 유실이 발생했다 — 절대시각 고정 틱(next_tick)으로
    # 바꿔, 사이클이 예정보다 늦게 끝나면 그만큼 다음 대기를 짧게 잡아 스케줄을 보정하는지 검증한다.
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    _pin_wall_clock(monkeypatch)
    # 1번째 사이클 종료 시각=1000.0 -> next_tick=1000+60=1060(정상 60초 대기 예상).
    # 2번째 사이클 종료 시각=1200.0(가상으로 사이클이 오래 걸려 예정 틱 1120을 80초 지나침)
    # -> 2026-07-30(운영점검 §4 Fix#3)부터는 현재 시각으로 재기준(delay=0)하지 않고 **원래 위상
    #    격자의 다음 틱**으로 스냅한다: 1120 + 60*2 = 1240 -> delay=40.0.
    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=60,
            )
        )

    # 정상 사이클은 60초 대기, 밀린 사이클은 따라잡지 않되 위상 격자를 지켜 다음 틱까지 대기.
    assert sleep_calls == [60.0, 40.0]


def test_poll_option_chain_records_rate_limiter_status_each_cycle(monkeypatch):
    # 2026-07-23(운영점검보고서 §2-1/§4 Fix#4): COCKPIT이 관측 루프 프로세스의 실시간 배율을
    # 직접 읽을 수 없으므로, 매 사이클마다 db.record_rate_limiter_status()로 남겨야 한다 —
    # 이번 사이클이 60초 주기를 30초 넘겨 밀렸다면 그 overrun_seconds도 함께 기록돼야 한다.
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)
    rest_client.rate_limit_backoff_multiplier = 2.25

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.record_rate_limiter_status",
        lambda conn, checked_at, multiplier, overrun, total_calls=None: recorded.append(
            (checked_at, multiplier, overrun, total_calls)
        ),
    )
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    _pin_wall_clock(monkeypatch)
    # 1번째 사이클 종료 시각=1000.0 -> next_tick=1000+60=1060, 정상 60초 대기(overrun=0).
    # 2번째 사이클 종료 시각=1200.0 -> next_tick=1060+60=1120을 이미 지나쳐 80초 밀림.
    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        if len(recorded) >= 2:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=60,
            )
        )

    assert len(recorded) == 2
    assert recorded[0][1] == pytest.approx(2.25)
    assert recorded[0][2] == pytest.approx(0.0)
    assert recorded[1][2] == pytest.approx(80.0)


def test_poll_option_chain_appends_rate_limiter_status_history_each_cycle(monkeypatch):
    # 2026-07-29(운영점검보고서 §2-5/Fix#3) — 싱글턴 기록(record_rate_limiter_status)만으로는
    # 시계열 조회가 안 돼, 같은 값을 append-only 히스토리 테이블에도 남겨야 한다.
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)
    rest_client.rate_limit_backoff_multiplier = 1.75

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)

    appended: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.append_rate_limiter_status_history",
        lambda conn, recorded_at, multiplier, overrun, total_calls=None: appended.append(
            (recorded_at, multiplier, overrun, total_calls)
        ),
    )

    fake_loop = _FakeLoop([1000.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client,
                [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                _FakeMaster(),
                interval_seconds=60,
            )
        )

    assert len(appended) == 1
    assert appended[0][1] == pytest.approx(1.75)
    assert appended[0][2] == pytest.approx(0.0)


def test_poll_option_chain_warns_when_rate_limiter_status_write_is_slow(monkeypatch, caplog):
    # 2026-07-24(운영점검보고서 §2-1/§4 Fix#1): record_rate_limiter_status() 기록이 07-23 밤
    # 처음 추가된 뒤 스케줄 밀림이 07-24까지 3일 연속 악화됐다 — 이 동기 DB 왕복 자체가 원인
    # 후보인지 다음 점검에서 실측으로 가리려면 소요시간이 임계(RATE_LIMITER_STATUS_WRITE_SLOW_
    # SECONDS=0.2초)를 넘을 때 경고가 남아야 한다. time.monotonic() 자체를 모킹하면 실제
    # asyncio 이벤트 루프 내부(진짜 to_thread/executor를 쓰는 이 테스트에서는 스케줄링에
    # time.monotonic()을 광범위하게 씀)까지 같은 전역 함수를 공유해 얼마나 소진되는지 예측할 수
    # 없다 — 대신 record_rate_limiter_status 자체가 실제로 느리게(time.sleep) 걸리게 해서
    # 실측 경과시간으로 검증한다.
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr(
        "mahdi.main.db.record_rate_limiter_status", lambda *a, **k: time.sleep(0.25)
    )
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    fake_loop = _FakeLoop([1000.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client,
                    [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                    _FakeMaster(),
                    interval_seconds=60,
                )
            )

    assert "레이트리밋 근접도 기록" in caplog.text


def test_poll_option_chain_does_not_warn_when_rate_limiter_status_write_is_fast(monkeypatch, caplog):
    # 위 테스트의 반대 경우 — 정상 속도(임계 0.2초 미만)면 계측 경고가 남으면 안 된다(잡음 방지).
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    fake_loop = _FakeLoop([1000.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client,
                    [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                    _FakeMaster(),
                    interval_seconds=60,
                )
            )

    assert "레이트리밋 근접도 기록" not in caplog.text


def test_poll_option_chain_logs_cycle_breakdown_each_cycle(monkeypatch, caplog):
    # 2026-07-28(운영점검보고서 2026-07-27 §4 Fix#1 후속): record_rate_limiter_status() 소요
    # 시간만 계측해서는 07-27 실측(슬로우 경고 0건)처럼 그 가설이 반증돼도 다음 후보(REST수집/
    # 옵션체인 DB적재)를 못 좁힌다 — 매 사이클(정상 사이클 포함) INFO로 4구간(REST수집/DB적재/
    # 상태기록/기타) 분해를 남겨 다음 점검에서 비교 기준선으로 쓴다. DB 삽입을 실제로 느리게
    # 만들어(time.sleep) 그 소요시간이 insert_seconds로 정확히 반영되는지 실측 경과시간으로
    # 검증한다(위 슬로우 경고 테스트와 동일한 이유로 time.monotonic() 자체는 모킹하지 않는다).
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def slow_insert(conn, row):
        time.sleep(0.05)

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", slow_insert)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    fake_loop = _FakeLoop([1000.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client,
                    [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                    _FakeMaster(),
                    interval_seconds=60,
                )
            )

    breakdown_records = [r for r in caplog.records if "옵션체인 사이클 소요 분해" in r.getMessage()]
    assert len(breakdown_records) == 1
    (
        collect_seconds, insert_seconds, db_write_seconds, other_seconds,
        rows_count, overrun, suffix, other_poller_calls_text,
        # 2026-08-07 Fix#3 — 이 사이클이 적재한 분 라벨. 파서가 「덮어쓴 분」을 세는 근거다.
        poll_minute,
    ) = breakdown_records[0].args
    assert rows_count == 2  # 1개 행사가 x (C, P)
    assert insert_seconds >= 0.09  # 레그 2건 x 0.05초 슬립 누적(오버헤드 감안 여유)
    assert overrun == pytest.approx(0.0)
    assert suffix == ""
    # rate_limit_total_calls(=len(calls))가 own_calls_expected(2)와 정확히 일치 — 다른 폴러의
    # 동시 호출이 끼어들지 않은 정상 사이클이라는 뜻(2026-07-28 신규 계측).
    assert other_poller_calls_text == "0건"
    # 라벨은 HH:MM 형태여야 한다 — `log_metrics._CYCLE_RE`가 그 형태로만 잡는다.
    assert re.fullmatch(r"\d\d:\d\d", poll_minute)


class _FakeRestClientChainWithForeignCalls:
    """rate_limit_total_calls가 이 폴러 자신의 호출 수보다 더 많이 늘어나는 상황(다른 폴러가
    같은 공유 _RateLimiter에 동시에 끼어든 상황, 2026-07-28 재수사 가설)을 재현한다 — 실제
    KISRestClient는 poll_investor_flow/poll_expiry_liquidity/poll_macro_snapshot과 하나의
    _RateLimiter를 asyncio.gather로 공유하므로, 옵션체인 사이클 도중 다른 폴러의 호출도 같은
    카운터를 함께 증가시킬 수 있다."""

    def __init__(self, resp: dict, foreign_calls_per_own_call: int):
        self._resp = resp
        self._foreign_calls_per_own_call = foreign_calls_per_own_call
        self.calls: list[str] = []
        self.rate_limit_backoff_multiplier = 1.0
        self.rate_limit_total_calls = 0

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        self.rate_limit_total_calls += 1 + self._foreign_calls_per_own_call
        return self._resp


def test_poll_option_chain_breakdown_detects_other_poller_contention(monkeypatch, caplog):
    # 2026-07-28(운영점검보고서 2026-07-27 §4 Fix#1 재수사): REST수집 소요시간이 자기 몫(30콜)
    # 만으로 설명 안 되는 게 poll_investor_flow 등 다른 폴러가 같은 공유 _RateLimiter에 동시에
    # 끼어들기 때문인지 확인하려면, rate_limit_total_calls의 사이클 전후 차이가 자기 예상 호출
    # 수(own_calls_expected)를 초과하는 만큼을 "타폴러동시호출추정"으로 남겨야 한다.
    rest_client = _FakeRestClientChainWithForeignCalls(_SAMPLE_OPTION_QUOTE, foreign_calls_per_own_call=3)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    fake_loop = _FakeLoop([1000.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client,
                    [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                    _FakeMaster(),
                    interval_seconds=60,
                )
            )

    breakdown_records = [r for r in caplog.records if "옵션체인 사이클 소요 분해" in r.getMessage()]
    assert len(breakdown_records) == 1
    # 1개 행사가 x (C, P) = own_calls_expected 2건, 콜당 foreign 3건 -> 총 증가분 2*(1+3)=8,
    # 자기 몫 2를 뺀 6건이 다른 폴러가 끼어든 것으로 추정돼야 한다.
    assert breakdown_records[0].args[-2] == "6건"   # 마지막은 2026-08-07 Fix#3의 분 라벨


def test_poll_option_chain_overrun_warning_includes_rest_db_breakdown(monkeypatch, caplog):
    # §4 Fix#1 후속 — 밀린 사이클의 WARNING 자체에도 REST/DB 구간별 소요시간을 함께 남겨,
    # "언제 얼마나 밀렸는지"와 "그 사이클에서 어느 구간이 오래 걸렸는지"를 로그 한 줄로 바로
    # 연결해서 볼 수 있게 한다(따로 떨어진 INFO 분해 로그를 시각으로 대조할 필요가 없어짐).
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    # 1번째 사이클 종료 시각=1000.0 -> next_tick=1060(정상 60초 대기, overrun=0).
    # 2번째 사이클 종료 시각=1200.0 -> next_tick=1060을 이미 지나쳐 140초 밀림.
    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client,
                    [(_FakeSubscriptionManagerWithStrikes(), "regular")],
                    _FakeMaster(),
                    interval_seconds=60,
                )
            )

    overrun_records = [
        r for r in caplog.records if "스케줄이" in r.getMessage() and "밀렸습니다" in r.getMessage()
    ]
    assert len(overrun_records) == 1
    message = overrun_records[0].getMessage()
    assert "REST수집" in message
    assert "DB적재" in message
    assert "rows=2" in message


class _FakeInvestorFlowRestClient:
    """섹터(F001/OC01/OP01)별로 다른 응답을 돌려주고, 지정한 섹터는 예외를 던진다."""

    def __init__(self, responses: dict, failing_sectors: set[str] = frozenset(), exc: Exception | None = None):
        self._responses = responses
        self._failing_sectors = failing_sectors
        self._exc = exc if exc is not None else RuntimeError("KIS 500")
        self.calls: list[tuple[str, str]] = []

    def get_investor_flow(self, market_code: str, sector_code: str) -> dict:
        self.calls.append((market_code, sector_code))
        if sector_code in self._failing_sectors:
            raise self._exc
        return self._responses[sector_code]


def _investor_flow_response(frgn: float, orgn: float, prsn: float) -> dict:
    return {
        "output": [
            {
                "frgn_ntby_tr_pbmn": str(frgn),
                "orgn_ntby_tr_pbmn": str(orgn),
                "prsn_ntby_tr_pbmn": str(prsn),
            }
        ],
        "rt_cd": "0",
    }


def test_poll_investor_flow_sums_futures_call_put_segments(monkeypatch):
    rest_client = _FakeInvestorFlowRestClient(
        {
            "F001": _investor_flow_response(-100.0, 200.0, -50.0),
            "OC01": _investor_flow_response(-30.0, 40.0, -5.0),
            "OP01": _investor_flow_response(-20.0, 10.0, 15.0),
        }
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def fake_insert_investor_flow(conn, timestamp, underlying, foreign_net, institution_net, individual_net):
        written.append(
            {"foreign_net": foreign_net, "institution_net": institution_net, "individual_net": individual_net}
        )

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_investor_flow", fake_insert_investor_flow)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_investor_flow(rest_client, interval_seconds=1))

    assert len(rest_client.calls) == 3  # 선물/콜/풋 3개 세그먼트
    assert len(written) == 1
    assert written[0]["foreign_net"] == pytest.approx(-150.0)
    assert written[0]["institution_net"] == pytest.approx(250.0)
    assert written[0]["individual_net"] == pytest.approx(-40.0)


def test_poll_investor_flow_continues_when_one_segment_fails(monkeypatch):
    rest_client = _FakeInvestorFlowRestClient(
        {
            "F001": _investor_flow_response(-100.0, 200.0, -50.0),
            "OP01": _investor_flow_response(-20.0, 10.0, 15.0),
        },
        failing_sectors={"OC01"},
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def fake_insert_investor_flow(conn, timestamp, underlying, foreign_net, institution_net, individual_net):
        written.append({"foreign_net": foreign_net})

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_investor_flow", fake_insert_investor_flow)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_investor_flow(rest_client, interval_seconds=1))

    assert len(rest_client.calls) == 3  # 실패한 OC01도 시도는 함
    assert len(written) == 1
    assert written[0]["foreign_net"] == pytest.approx(-120.0)  # F001 + OP01만 합산(OC01 실패분 제외)


def test_poll_investor_flow_segment_failure_logs_kis_response_body_and_is_throttled(monkeypatch, caplog):
    # 2026-07-20 고도화: poll_option_chain에 이미 적용한 "응답 바디 로깅 + 스로틀"을
    # poll_investor_flow에도 표준화 — 이전엔 그냥 "KIS 500"만 남고 레이트리밋인지 다른 원인인지
    # 알 수 없었다.
    exc = _http_status_error(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다"})
    rest_client = _FakeInvestorFlowRestClient(
        {"F001": _investor_flow_response(-100.0, 200.0, -50.0)},
        failing_sectors={"OC01", "OP01"},
        exc=exc,
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def fake_insert_investor_flow(conn, timestamp, underlying, foreign_net, institution_net, individual_net):
        written.append({"foreign_net": foreign_net})

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_investor_flow", fake_insert_investor_flow)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(poll_investor_flow(rest_client, interval_seconds=1))

    assert len(rest_client.calls) == 3  # 실패한 OC01/OP01도 둘 다 시도됨
    assert len(written) == 1  # F001만 성공해도 적재는 됨

    failure_records = [r for r in caplog.records if "투자자 수급 폴링 실패" in r.getMessage()]
    assert len(failure_records) == 1  # 같은 60초 창 안에서 두 번째(OP01) 실패는 억제됨
    logged_message = failure_records[0].getMessage()
    assert "EGW00201" in logged_message
    assert "초당 거래건수를 초과하였습니다" in logged_message


class _FakeInvestorFlowRestClientFlaky:
    """처음 fail_calls건은 (섹터 무관) 실패, 이후는 성공 — 사이클 전체 실패 후 재시도 복구를 재현."""

    def __init__(self, responses: dict, fail_calls: int):
        self._responses = responses
        self._fail_calls = fail_calls
        self.calls: list[tuple[str, str]] = []

    def get_investor_flow(self, market_code: str, sector_code: str) -> dict:
        self.calls.append((market_code, sector_code))
        if len(self.calls) <= self._fail_calls:
            raise RuntimeError("KIS 500")
        return self._responses[sector_code]


def test_poll_investor_flow_retries_once_when_all_segments_fail(monkeypatch):
    # 2026-07-08 실측: 레이트리밋 버스트로 세 세그먼트가 한꺼번에 실패하는 경우가 있었다 —
    # 다음 60초 사이클까지 기다리지 않고 짧게 대기 후 재시도해 복구되는지 검증한다.
    rest_client = _FakeInvestorFlowRestClientFlaky(
        {
            "F001": _investor_flow_response(-100.0, 200.0, -50.0),
            "OC01": _investor_flow_response(-30.0, 40.0, -5.0),
            "OP01": _investor_flow_response(-20.0, 10.0, 15.0),
        },
        fail_calls=3,  # 1차 시도(3개 세그먼트) 전부 실패
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def fake_insert_investor_flow(conn, timestamp, underlying, foreign_net, institution_net, individual_net):
        written.append({"foreign_net": foreign_net})

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_investor_flow", fake_insert_investor_flow)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if seconds != 5.0:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_investor_flow(rest_client, interval_seconds=1))

    assert len(rest_client.calls) == 6  # 1차 3건 실패 + 재시도 3건 성공
    assert len(written) == 1
    assert written[0]["foreign_net"] == pytest.approx(-150.0)
    assert 5.0 in sleep_calls  # 재시도 backoff가 실제로 대기했다


def test_run_observation_loop_computes_vpin_for_futures_symbol(monkeypatch):
    # VPIN은 옵션이 아니라 선물(기초자산)에만 적용한다(2026-07-06 결정) — 등거래량 버킷 2개가
    # 닫힌 뒤 선물 1분봉이 flush될 때 market_raw_1m.vpin에 실제 계산값이 실리는지 확인.
    # 옵션 틱 1개를 섞어 넣어도 선물 집계/버킷과 뒤섞이지 않아야 한다.
    futures_symbol = "101S03"
    incoming = [
        _make_h0ifcnt0("090000", 350.0, 30, 350.05, 349.95, 100, 100, symbol=futures_symbol),
        _make_h0ifcnt0("090005", 352.0, 25, 352.05, 351.95, 100, 100, symbol=futures_symbol),  # 누적 55 → 버킷1 닫힘
        _make_h0iocnt0("090006", 60.0, 5, 60.05, 59.95, 100, 100, symbol="201S03C325"),  # 옵션 틱 — 섞이면 안 됨
        _make_h0ifcnt0("090010", 352.0, 20, 352.05, 351.95, 100, 100, symbol=futures_symbol),
        _make_h0ifcnt0("090015", 340.0, 35, 340.05, 339.95, 100, 100, symbol=futures_symbol),  # 누적 55 → 버킷2 닫힘
        _make_h0ifcnt0("090100", 345.0, 5, 345.05, 344.95, 100, 100, symbol=futures_symbol),  # 다음 분 → 선물 09:00봉 flush
    ]
    conn = FakeConnection(incoming)
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    subscription_manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    rest_client = FakeRestClient(spot=350.0)

    written_bars = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: written_bars.append(row))
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", lambda conn, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda conn, underlying, symbol, updated_at: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol=futures_symbol,
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )

    futures_bars = [b for b in written_bars if b["symbol"] == futures_symbol]
    assert len(futures_bars) == 1
    bar = futures_bars[0]
    assert bar["open"] == 350.0
    assert bar["close"] == 340.0
    assert bar["volume"] == pytest.approx(30 + 25 + 20 + 35)

    ret1 = (352.0 - 350.0) / 350.0
    ret2 = (340.0 - 352.0) / 352.0
    expected_vpin = calculate_vpin([ret1, ret2], [55.0, 55.0])
    assert expected_vpin > 0  # 두 버킷 수익률 부호/크기가 달라 표준편차>0 → 0이 아닌 값이어야 의미 있는 검증
    assert bar["vpin"] == pytest.approx(expected_vpin)

    option_bars = [b for b in written_bars if b["symbol"] != futures_symbol]
    assert option_bars == []  # 옵션 틱이 1개뿐이라 아직 봉이 안 닫힘 — 선물과 안 섞였는지만 확인


def test_run_observation_loop_computes_vpin_for_option_symbol_too(monkeypatch):
    # 2026-07-06: VPIN을 선물에만 적용했다가, 사용자 요청으로 옵션에도 종목 구분 없이 통일 적용.
    # 옵션 심볼도 등거래량 버킷 2개를 닫으면 VPIN이 계산돼 봉에 실려야 한다.
    futures_symbol = "101S03"  # 이 테스트의 어느 틱도 이 심볼을 쓰지 않음(옵션 경로만 검증)
    incoming = [
        _make_h0iocnt0("090000", 60.0, 30, 60.05, 59.95, 100, 100, symbol="201S03C325"),
        _make_h0iocnt0("090005", 62.0, 25, 62.05, 61.95, 100, 100, symbol="201S03C325"),  # 누적 55 → 버킷1 닫힘
        _make_h0iocnt0("090010", 62.0, 20, 62.05, 61.95, 100, 100, symbol="201S03C325"),
        _make_h0iocnt0("090015", 58.0, 35, 58.05, 57.95, 100, 100, symbol="201S03C325"),  # 누적 55 → 버킷2 닫힘
        _make_h0iocnt0("090100", 59.0, 5, 59.05, 58.95, 100, 100, symbol="201S03C325"),  # 다음 분 → 09:00봉 flush
    ]
    conn = FakeConnection(incoming)
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    subscription_manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    rest_client = FakeRestClient(spot=350.0)

    written_bars = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: written_bars.append(row))
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", lambda conn, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda conn, underlying, symbol, updated_at: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol=futures_symbol,
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )

    assert len(written_bars) == 1
    bar = written_bars[0]
    assert bar["symbol"] == "201S03C325"

    ret1 = (62.0 - 60.0) / 60.0
    ret2 = (58.0 - 62.0) / 62.0
    expected_vpin = calculate_vpin([ret1, ret2], [55.0, 55.0])
    assert expected_vpin > 0
    assert bar["vpin"] == pytest.approx(expected_vpin)


def test_atm_liquidity_window_trims_to_center_each_side():
    # ATM±3(7개) 중에서 ATM±2(5개)만 남아야 함 — strikes_around_atm()이 만드는 대칭 격자를 가정.
    strikes = frozenset({345.0, 347.5, 350.0, 352.5, 355.0, 357.5, 360.0})
    assert _atm_liquidity_window(strikes, each_side=2) == [347.5, 350.0, 352.5, 355.0, 357.5]


def test_atm_liquidity_window_empty_strikes_returns_empty():
    assert _atm_liquidity_window(frozenset(), each_side=2) == []


_SAMPLE_ASKING_PRICE = {
    "output1": {"acml_vol": "120"},
    "output2": {"futs_askp1": "10.10", "futs_bidp1": "9.90", "askp_rsqn1": "30", "bidp_rsqn1": "40"},
}


def test_parse_asking_price_leg_computes_pct_spread_not_dollar_spread():
    parsed = _parse_asking_price_leg(_SAMPLE_ASKING_PRICE)
    assert parsed is not None
    assert parsed["spread_pct"] == pytest.approx((10.10 - 9.90) / 10.00)  # Cao-Wei: %스프레드
    assert parsed["depth"] == pytest.approx(70.0)
    assert parsed["volume"] == pytest.approx(120.0)


def test_parse_asking_price_leg_returns_none_when_nothing_usable():
    # acml_vol 필드 자체가 없고(파싱 불가) 양쪽 호가도 0이라 mid<=0(스프레드도 못 구함) —
    # 이 레그에서 얻을 게 정말 하나도 없는 경우만 None이어야 한다.
    empty = {"output1": {}, "output2": {"futs_askp1": "0.00", "futs_bidp1": "0.00", "askp_rsqn1": "0", "bidp_rsqn1": "0"}}
    assert _parse_asking_price_leg(empty) is None


def test_parse_asking_price_leg_keeps_zero_volume_as_valid_value():
    # acml_vol="0"은 "그날 정말 0계약 체결"이라는 유효한 값이지 파싱 실패가 아니다 —
    # None(unparseable)과 혼동해 버리면 안 된다.
    resp = {"output1": {"acml_vol": "0"}, "output2": {"futs_askp1": "0.00", "futs_bidp1": "0.00", "askp_rsqn1": "0", "bidp_rsqn1": "0"}}
    parsed = _parse_asking_price_leg(resp)
    assert parsed is not None
    assert parsed["volume"] == pytest.approx(0.0)
    assert parsed["spread_pct"] is None


def test_parse_asking_price_leg_keeps_volume_when_quote_missing():
    # 2026-07-10 발견: 위클리(목)처럼 얇은 종목은 순간적으로 양쪽 호가가 비어도 그날 누적거래량
    # (acml_vol)은 이미 찍혀 있을 수 있다 — 호가가 없다고 거래량까지 버리면 안 된다.
    resp = {"output1": {"acml_vol": "4"}, "output2": {"futs_askp1": None, "futs_bidp1": None}}
    parsed = _parse_asking_price_leg(resp)
    assert parsed is not None
    assert parsed["volume"] == pytest.approx(4.0)
    assert parsed["spread_pct"] is None
    assert parsed["depth"] is None


def test_parse_asking_price_leg_keeps_spread_when_volume_missing():
    resp = {
        "output1": {},
        "output2": {"futs_askp1": "10.10", "futs_bidp1": "9.90", "askp_rsqn1": "30", "bidp_rsqn1": "40"},
    }
    parsed = _parse_asking_price_leg(resp)
    assert parsed is not None
    assert parsed["spread_pct"] == pytest.approx((10.10 - 9.90) / 10.00)
    assert parsed["depth"] == pytest.approx(70.0)
    assert parsed["volume"] is None


class _FakeMasterForLiquidity:
    """C는 ATM 종목만 정상 응답, 그 외에는 SYM{strike}{type} 형태로 심볼을 낸다."""

    def option_symbol(
        self, option_type: str, strike: float, underlying: str = "KOSPI200", series: str = "regular"
    ) -> str | None:
        return f"SYM{int(strike)}{option_type}{series[0]}"


class _FakeSubscriptionManagerForLiquidity:
    def __init__(self, strikes: frozenset[float]):
        self._strikes = strikes

    @property
    def desired_strikes(self) -> frozenset[float]:
        return self._strikes


class _FakeRestClientForLiquidity:
    """get_quote는 만기 확인용 앵커 1건, get_asking_price는 각 레그마다 호출된다."""

    def __init__(self, quote_resp: dict, asking_resp: dict):
        self._quote_resp = quote_resp
        self._asking_resp = asking_resp
        self.quote_calls: list[str] = []
        self.asking_calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.quote_calls.append(symbol)
        return self._quote_resp

    def get_asking_price(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.asking_calls.append(symbol)
        return self._asking_resp


def test_poll_expiry_liquidity_aggregates_one_row_per_book_across_its_slots(monkeypatch):
    # 2026-07-31(§4 우선순위 1): 3북 33콜을 한 사이클에 몰아 쏘던 것을 **북 하나씩 홀수분 슬롯**으로
    # 흩었다. 그래서 "한 사이클에 북 전부"가 아니라 "10분 창을 돌면 북마다 정확히 1행"이 계약이다.
    rest_client = _FakeRestClientForLiquidity(_SAMPLE_OPTION_QUOTE, _SAMPLE_ASKING_PRICE)
    written_rows: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_expiry_liquidity_1m", lambda conn, row: written_rows.append(row))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    strikes = frozenset({1330.0, 1332.5, 1335.0, 1337.5, 1340.0, 1342.5, 1345.0})
    books = [
        (_FakeSubscriptionManagerForLiquidity(strikes), "regular"),
        (_FakeSubscriptionManagerForLiquidity(strikes), "weekly"),
    ]

    # 두 북의 슬롯 분(minute % 10 == 1, 3)에서 각각 한 사이클씩 돌린다.
    for slot_minute in mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES[: len(books)]:
        _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 10, 20 + slot_minute, 0))
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert len(written_rows) == 2  # 북(regular, weekly)당 1행
    series_seen = {row["series"] for row in written_rows}
    assert series_seen == {"regular", "weekly"}
    for row in written_rows:
        assert row["expiry"] == date(2026, 7, 9)
        assert row["atm_spread_pct"] == pytest.approx((10.10 - 9.90) / 10.00)
        assert row["depth"] == pytest.approx(70.0 * 5 * 2)  # ATM±2(5개 행사가) x (C,P)
        assert row["volume"] == pytest.approx(120.0 * 5 * 2)

    # 만기 확인용 get_quote는 북당 1건만 호출돼야 함(ATM 앵커 1건, 레그마다 반복 호출 아님)
    assert len(rest_client.quote_calls) == 2
    assert len(rest_client.asking_calls) == 5 * 2 * 2  # 2북 x ATM±2(5) x (C,P)


def test_poll_expiry_liquidity_does_nothing_outside_its_book_slots(monkeypatch):
    # 핵심 회귀 방지(§2-1 원인 a): 슬롯이 아닌 분에는 REST를 단 한 건도 쏘지 않아야 하고,
    # "구독 없음 → 2초 재확인" 경로로 새서 위상 격자를 리셋해서도 안 된다.
    rest_client = _FakeRestClientForLiquidity(_SAMPLE_OPTION_QUOTE, _SAMPLE_ASKING_PRICE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_expiry_liquidity_1m", lambda conn, row: None)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    strikes = frozenset({1330.0, 1332.5, 1335.0, 1337.5, 1340.0})
    books = [(_FakeSubscriptionManagerForLiquidity(strikes), "regular")]

    _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 10, 22, 0))  # minute % 10 == 2 → 슬롯 아님
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=60))

    assert rest_client.quote_calls == []
    assert rest_client.asking_calls == []
    assert sleep_calls != [2.0]  # 워밍업 재확인 경로가 아니라 정상 스케줄 대기여야 한다


def test_expiry_liquidity_slots_assign_exactly_one_book_per_minute_and_cover_all_books():
    # 슬롯 튜플과 북 목록이 어긋나면(북 추가 시 슬롯을 안 늘리면) 그 북은 영영 조회되지 않는다.
    books = [(object(), "regular"), (object(), "weekly_mon"), (object(), "weekly_thu")]
    assert len(mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES) >= len(books), (
        "북이 슬롯보다 많다 — 초과분은 영영 조회되지 않는다"
    )
    assert len(set(mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES)) == len(
        mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES
    ), "두 북이 같은 분에 배정됐다 — 버스트를 나눈 의미가 없다"

    seen: list[str] = []
    for minute in range(mahdi_main.EXPIRY_LIQUIDITY_WINDOW_MINUTES):
        due = mahdi_main._expiry_liquidity_books_due(books, datetime(2026, 7, 31, 10, minute, 0))
        assert len(due) <= 1, f"minute={minute}에 북 2개 이상이 동시에 발사된다: {due}"
        seen.extend(series for _manager, series in due)
    assert seen == ["regular", "weekly_mon", "weekly_thu"]  # 10분 창 한 바퀴에 북마다 정확히 1회


def test_expiry_liquidity_slots_are_all_odd_minutes():
    # 짝수분은 옵션체인이 3북 30레그를 쓰고(공칭 0~30초), 홀수분은 먼슬리 10레그(0~10초)뿐이라
    # 47초가 빈다 — 축소안 (a)가 만든 그 여유에 이 폴러를 넣는 것이 §4 우선순위 1의 핵심이다.
    for slot in mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES:
        assert slot % 2 == 1, f"슬롯 {slot}분이 짝수 — 옵션체인 30레그 사이클과 겹친다"


def test_poll_expiry_liquidity_skips_book_with_no_strikes(monkeypatch):
    rest_client = _FakeRestClientForLiquidity(_SAMPLE_OPTION_QUOTE, _SAMPLE_ASKING_PRICE)
    written_rows: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_expiry_liquidity_1m", lambda conn, row: written_rows.append(row))

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 10, 21, 0))  # minute % 10 == 1 → "regular" 슬롯
    books = [(_FakeSubscriptionManagerForLiquidity(frozenset()), "regular")]

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert written_rows == []
    assert sleep_calls == [2.0]  # 구독 행사가가 아직 없을 때는 2초 재확인 경로를 탄다


def test_poll_expiry_liquidity_skips_bad_book_and_continues_after_db_error(monkeypatch):
    rest_client = _FakeRestClientForLiquidity(_SAMPLE_OPTION_QUOTE, _SAMPLE_ASKING_PRICE)
    written_rows: list[dict] = []
    fake_conn = _FakeConnWithRollback()

    @contextmanager
    def fake_get_connection(settings=None):
        yield fake_conn

    call_count = {"n": 0}

    def fake_insert(conn, row):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("some db error")
        written_rows.append(row)

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_expiry_liquidity_1m", fake_insert)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    strikes = frozenset({1330.0, 1332.5, 1335.0, 1337.5, 1340.0, 1342.5, 1345.0})
    books = [
        (_FakeSubscriptionManagerForLiquidity(strikes), "regular"),
        (_FakeSubscriptionManagerForLiquidity(strikes), "weekly"),
    ]

    # 2026-07-31: 북마다 슬롯이 다른 분이므로 "첫 북 실패 → 둘째 북 정상"은 두 사이클에 걸쳐 일어난다.
    for slot_minute in mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES[: len(books)]:
        _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 10, 20 + slot_minute, 0))
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert call_count["n"] == 2  # 북 2개 각각 1행씩 시도됨
    assert len(written_rows) == 1  # 첫 북만 실패, 둘째 북은 정상 적재됨(루프가 안 죽음)
    assert fake_conn.rollback_calls == 1


class _FakeRestClientForLiquidityAlwaysFailsAskingPrice:
    """get_quote(앵커)는 정상 응답, get_asking_price(레그)는 항상 지정된 예외를 던진다."""

    def __init__(self, quote_resp: dict, exc: Exception):
        self._quote_resp = quote_resp
        self._exc = exc
        self.asking_calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        return self._quote_resp

    def get_asking_price(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.asking_calls.append(symbol)
        raise self._exc


def test_poll_expiry_liquidity_leg_fetch_failure_logs_kis_response_body_and_is_throttled(monkeypatch, caplog):
    # 2026-07-20 고도화: poll_option_chain과 동일하게 응답 바디 로깅 + 스로틀을 표준화.
    exc = _http_status_error(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다"})
    rest_client = _FakeRestClientForLiquidityAlwaysFailsAskingPrice(_SAMPLE_OPTION_QUOTE, exc)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_expiry_liquidity_1m", lambda conn, row: None)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    strikes = frozenset({1330.0, 1332.5, 1335.0, 1337.5, 1340.0})  # ATM±2, 5개 행사가
    _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 10, 21, 0))  # minute % 10 == 1 → "regular" 슬롯
    books = [(_FakeSubscriptionManagerForLiquidity(strikes), "regular")]

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert len(rest_client.asking_calls) == 5 * 2  # ATM±2(5) x (C,P) 전부 시도됨

    failure_records = [r for r in caplog.records if "만기 유동성 폴링 실패" in r.getMessage()]
    assert len(failure_records) == 1  # 같은 60초 창 안에서 나머지 9건은 억제됨
    logged_message = failure_records[0].getMessage()
    assert "EGW00201" in logged_message
    assert "초당 거래건수를 초과하였습니다" in logged_message


class _FakeRestClientForLiquidityAlwaysFailsQuote:
    """get_quote(앵커/만기확인)는 항상 지정된 예외, get_asking_price(레그)는 정상 응답."""

    def __init__(self, exc: Exception, asking_resp: dict):
        self._exc = exc
        self._asking_resp = asking_resp
        self.quote_calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.quote_calls.append(symbol)
        raise self._exc

    def get_asking_price(self, symbol: str, market_div_code: str | None = None) -> dict:
        return self._asking_resp


def test_poll_expiry_liquidity_anchor_fetch_failure_is_logged_with_response_body(monkeypatch, caplog):
    # 2026-07-20 고도화: 이전엔 앵커(만기확인용 get_quote) 실패가 완전히 조용히 삼켜져
    # (parsed_anchor=None) 로그에 아무 흔적도 안 남았다 — 원인 추적이 불가능한 사각지대였다.
    exc = _http_status_error(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다"})
    rest_client = _FakeRestClientForLiquidityAlwaysFailsQuote(exc, _SAMPLE_ASKING_PRICE)
    written_rows: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_expiry_liquidity_1m", lambda conn, row: written_rows.append(row))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    strikes = frozenset({1330.0, 1332.5, 1335.0, 1337.5, 1340.0})
    _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 10, 21, 0))  # minute % 10 == 1 → "regular" 슬롯
    books = [(_FakeSubscriptionManagerForLiquidity(strikes), "regular")]

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert len(rest_client.quote_calls) == 1  # 앵커 1건 시도됨
    assert written_rows == []  # 만기를 못 구해 그 북은 건너뜀(적재 없음)

    failure_records = [r for r in caplog.records if "만기 유동성 만기확인 조회 실패" in r.getMessage()]
    assert len(failure_records) == 1
    assert "EGW00201" in failure_records[0].getMessage()


def test_parse_overseas_future_last_price_strips_padding():
    # KIS는 숫자 필드를 앞공백으로 패딩해 돌려준다(실측: "          17.50") — float()가 알아서 처리.
    assert _parse_overseas_future_last_price({"output1": {"last_price": "          17.50"}}) == 17.50


def test_parse_overseas_future_last_price_missing_field_returns_none():
    assert _parse_overseas_future_last_price({}) is None
    assert _parse_overseas_future_last_price({"output1": {"last_price": "N/A"}}) is None


def test_parse_overseas_daily_last_price_valid_response():
    # 국채구분(I)·환율구분(X) 등 공통 스키마 — US10Y/USDKRW 둘 다 같은 파서를 쓴다.
    assert _parse_overseas_daily_last_price({"output1": {"ovrs_nmix_prpr": "4.5400"}}) == pytest.approx(4.54)


def test_parse_overseas_daily_last_price_missing_field_returns_none():
    assert _parse_overseas_daily_last_price({}) is None


class _FakeOverseasFutureMaster:
    def __init__(self, mapping: dict[str, tuple[str | None, str | None]]):
        self._mapping = mapping

    def front_two_codes(self, product_code: str) -> tuple[str | None, str | None]:
        return self._mapping.get(product_code, (None, None))


class _FakeOverseasRestClient:
    def __init__(
        self,
        future_prices: dict[str, dict],
        daily_chart: dict | None = None,
        usdkrw_daily_chart: dict | None = None,
        failing: set[str] = frozenset(),
    ):
        self._future_prices = future_prices
        self._daily_chart = daily_chart
        self._usdkrw_daily_chart = usdkrw_daily_chart
        self._failing = failing
        self.future_calls: list[str] = []
        self.daily_calls: list[tuple[str, str]] = []

    def get_overseas_future_price(self, srs_cd: str) -> dict:
        self.future_calls.append(srs_cd)
        if srs_cd in self._failing:
            raise RuntimeError("KIS 500")
        return self._future_prices[srs_cd]

    def get_overseas_daily_chartprice(self, market_div_code, symbol, date_from, date_to, period_div_code="D") -> dict:
        self.daily_calls.append((market_div_code, symbol))
        if market_div_code == tr_codes.FID_MRKT_DIV_OVERSEAS_FX:
            if "USDKRW" in self._failing:
                raise RuntimeError("KIS 500")
            return self._usdkrw_daily_chart
        if "US10Y" in self._failing:
            raise RuntimeError("KIS 500")
        return self._daily_chart


def _future_price_response(last_price: float) -> dict:
    return {"output1": {"last_price": str(last_price)}, "rt_cd": "0"}


def _daily_chart_response(prpr: float) -> dict:
    return {"output1": {"ovrs_nmix_prpr": str(prpr)}, "rt_cd": "0"}


def _fallback_stub(zn=None, es=None, move=None):
    """mahdi.main.yfinance_fallback.fetch_last_close 대체용 — 심볼별로 다른 값/실패를 지정한다.
    지정하지 않은 심볼은 전부 None(폴백도 실패)을 반환한다."""
    responses = {
        yfinance_fallback.ZN_FALLBACK_SYMBOL: zn,
        yfinance_fallback.ES_FALLBACK_SYMBOL: es,
        yfinance_fallback.MOVE_FALLBACK_SYMBOL: move,
    }

    def _fetch(symbol: str) -> float | None:
        return responses.get(symbol)

    return _fetch


def test_poll_macro_snapshot_computes_term_structure_and_writes_row(monkeypatch):
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert set(rest_client.future_calls) == {"VXN26", "VXQ26", "CNHN26"}
    assert len(written) == 1
    row = written[0]
    assert row["vix_front"] == 17.50
    assert row["vix_next"] == 17.80
    assert row["vix_term_structure"] == pytest.approx(17.80 / 17.50 - 1)
    assert row["usdcnh"] == 6.7803
    assert row["us10y_yield"] == pytest.approx(4.54)
    assert row["zn_front"] is None  # 마스터에 ZN 매핑이 없고(CBOT 미구독) yfinance 폴백도 실패
    assert row["zn_front_source"] is None
    assert row["quality_flag"] == 0


def test_poll_macro_snapshot_sends_cbot_alert_once_when_zn_front_stays_none(monkeypatch):
    # 2026-07-19(§5-4): KIS·yfinance 폴백 둘 다 실패해 zn_front=None인 상태가 이어지면 Slack으로
    # 한 번만 알린다 — 5분마다 반복 알리면 하루 종일 스팸이 되므로, 이 프로세스 실행(거래일)당
    # 최초 1회만 보내야 한다. 2026-07-24(§2-3/§4 Fix#3) 갱신: ZN_DUAL_FAILURE_ALERT_STREAK(=2)
    # 연속 실패해야 알리도록 바뀌어, 이 테스트가 2사이클을 돌리는 것 자체가 정확히 그 스트릭
    # 조건을 채우는 경로가 됐다(1사이클만으로는 알림이 안 감 — 별도 테스트로 검증).
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 2:  # 두 번째 사이클까지 돌려 중복 알림이 없는지 확인
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        # 2026-07-31: ZN 조회는 기본 1시간 주기라, 스트릭/알림 로직만 보는 이 테스트는
        # 전 항목을 매 사이클 조회하도록 고정한다(조회 주기 자체는 별도 테스트에서 검증).
        _run(poll_macro_snapshot(
            rest_client, master, interval_seconds=1, item_refresh_seconds=_MACRO_ITEMS_EVERY_CYCLE
        ))

    assert len(written) == 2  # 두 사이클 모두 적재는 성공(zn_front만 None)
    assert len(notify_calls) == 1  # 두 번째 사이클에서 재알림 없이 딱 한 번만
    message, level = notify_calls[0]
    assert level == "WARNING"
    assert "ZN" in message


def test_poll_macro_snapshot_does_not_alert_zn_on_single_blip(monkeypatch):
    # 2026-07-24(운영점검보고서 §2-3/§4 Fix#3): 3일 연속 13:01 재현이 확정됐지만, 매 사이클 첫
    # 실패에 바로 알리면 몇 분짜리 일시적 블립까지 결손으로 보고하게 된다 — 연속 실패가
    # ZN_DUAL_FAILURE_ALERT_STREAK(=2) 미만(=1회)이면 아직 알리면 안 된다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")  # 딱 1사이클만 돌린다

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["zn_front"] is None
    assert notify_calls == []  # 1회 실패만으로는 알림 없음


def test_poll_macro_snapshot_zn_streak_resets_after_recovery(monkeypatch):
    # 2026-07-24(§4 Fix#3): 연속 실패 스트릭이 중간에 한 번 성공하면 리셋돼야 한다 — 실패 1회 +
    # 성공 1회 + 실패 2회를 돌리면, 마지막 두 번의 연속 실패에서만 스트릭이 다시 쌓이므로(성공
    # 사이클이 카운트를 끊으므로) 세 번째 사이클(성공 이후 첫 실패)에서는 아직 알리면 안 되고
    # 네 번째 사이클(성공 이후 두 번째 연속 실패)에서 비로소 알려야 한다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))

    # 사이클 순서대로 ZN 폴백값을 실패/성공/실패/실패로 지정 — fetch_last_close가 사이클마다
    # 한 번씩(ZN만) 호출되는 것을 이용해 호출 순번으로 결과를 결정한다.
    zn_results = iter([None, 108.50, None, None])

    def _fetch(symbol: str) -> float | None:
        if symbol == yfinance_fallback.ZN_FALLBACK_SYMBOL:
            return next(zn_results)
        return None

    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fetch)

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 4:  # 4사이클(실패/성공/실패/실패) 전부 돌린다
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        # 2026-07-31: 스트릭 리셋 동작만 보는 테스트라 ZN을 매 사이클 조회하도록 고정한다.
        _run(poll_macro_snapshot(
            rest_client, master, interval_seconds=1, item_refresh_seconds=_MACRO_ITEMS_EVERY_CYCLE
        ))

    assert len(written) == 4
    assert [row["zn_front"] for row in written] == [None, 108.50, None, None]
    assert len(notify_calls) == 1  # 성공으로 스트릭이 끊긴 뒤 다시 2연속 실패한 4번째 사이클에서만 알림


def test_poll_macro_snapshot_sends_insert_failure_alert_after_streak(monkeypatch):
    # 2026-07-21: macro_snapshot_5m INSERT가 연속 실패하면(예: 마이그레이션 라이브 미적용으로
    # UndefinedColumn) 로그에만 남기지 않고 MACRO_SNAPSHOT_INSERT_FAILURE_ALERT_STREAK회
    # 연속 실패한 시점에 한 번 Slack으로 알린다 — 1회만 실패했을 때는 아직 알리지 않는다(일시적
    # DB 지연과 구분).
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )

    class _FakeConn:
        def __init__(self):
            self.rollback_calls = 0

        def rollback(self):
            self.rollback_calls += 1

    conns: list[_FakeConn] = []

    @contextmanager
    def fake_get_connection(settings=None):
        conn = _FakeConn()
        conns.append(conn)
        yield conn

    def fake_insert(conn, row):
        raise RuntimeError('column "usdkrw" does not exist')

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", fake_insert)
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 3:  # 3번째 사이클까지 돌려 3회 연속 실패 확인
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(conns) == 3
    assert all(c.rollback_calls == 1 for c in conns)  # 매 실패 사이클마다 rollback 필수(트랜잭션 중단 방지)

    insert_failure_notifications = [(m, lvl) for m, lvl in notify_calls if "적재" in m and "실패" in m]
    assert len(insert_failure_notifications) == 1  # 2회차에 딱 한 번만, 3회차엔 재알림 없음
    message, level = insert_failure_notifications[0]
    assert level == "WARNING"
    assert "2회" in message


def test_poll_macro_snapshot_sends_recovery_alert_after_insert_failure(monkeypatch):
    # 2026-07-21: 연속 실패로 알림이 나간 뒤 다음 사이클에서 적재가 다시 성공하면 복구 알림을
    # 보내고 스트릭/알림 상태를 리셋한다 — gap_alerted(poll_option_chain)와 동일한 "지속되면
    # 알리고, 회복되면 알린다" 패턴.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )

    class _FakeConn:
        def rollback(self):
            pass

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConn()

    written: list[dict] = []
    insert_attempt = {"n": 0}

    def fake_insert(conn, row):
        insert_attempt["n"] += 1
        if insert_attempt["n"] <= 2:
            raise RuntimeError('column "usdkrw" does not exist')
        written.append(row)

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", fake_insert)
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 3:  # 실패 2회 + 성공 1회
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1  # 3번째 사이클에서만 적재 성공
    messages = [m for m, _ in notify_calls]
    assert any("적재" in m and "실패" in m for m in messages)
    assert any("복구" in m for m in messages)


def test_poll_macro_snapshot_includes_zn_front_when_cbot_enabled(monkeypatch):
    # 2026-07-10 사용자가 계좌에 CBOT 거래소 신청을 완료한 뒤의 경로 — ZN 근월물이 마스터에
    # 매핑되면 5분마다 zn_front도 함께 조회·적재돼야 한다. KIS 조회가 성공하면 yfinance 폴백은
    # 아예 호출되지 않아야 한다(2026-07-20 폴백 추가 — 불필요한 외부 호출 방지).
    master = _FakeOverseasFutureMaster(
        {"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26"), "ZN": ("ZNU26", "ZNZ26")}
    )
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
            "ZNU26": _future_price_response(110.25),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    fallback_calls: list[str] = []

    def _record_fallback_call(symbol: str) -> float | None:
        fallback_calls.append(symbol)
        return None  # ES/MOVE는 이 테스트에서 KIS 경로가 없으니 폴백이 호출돼도 됨 — ZN만 안 되면 됨

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _record_fallback_call)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert "ZNU26" in rest_client.future_calls
    assert "ZNZ26" not in rest_client.future_calls  # 차근월물은 조회하지 않음(VIX와 달리 급변 감지엔 근월물 하나면 충분)
    assert len(written) == 1
    assert written[0]["zn_front"] == 110.25
    assert written[0]["zn_front_source"] == "kis"
    # KIS 조회가 성공했으면 ZN에 대해서는 yfinance 폴백을 호출하면 안 된다(불필요한 외부 호출 방지).
    assert yfinance_fallback.ZN_FALLBACK_SYMBOL not in fallback_calls


def test_poll_macro_snapshot_continues_when_zn_fails_but_others_succeed(monkeypatch):
    # CBOT 신청 직후 일시적 오류 등으로 ZN만 실패해도 나머지 필드는 그대로 적재돼야 한다.
    # yfinance 폴백도 함께 실패하는 경우를 가정(폴백 성공 케이스는 별도 테스트에서 검증).
    master = _FakeOverseasFutureMaster(
        {"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26"), "ZN": ("ZNU26", "ZNZ26")}
    )
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
        failing={"ZNU26"},
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["zn_front"] is None
    assert written[0]["zn_front_source"] is None
    assert written[0]["vix_front"] == 17.50  # ZN 실패가 다른 필드를 막지 않음


def test_poll_macro_snapshot_uses_yfinance_fallback_when_kis_zn_fails(monkeypatch):
    # 2026-07-20: CME|CBOT가 KIS 유료 항목(월 228.8불)이라 모의투자 개발 단계에서는 미구독 —
    # KIS ZN 조회가 실패하면 yfinance 폴백값으로 zn_front를 채우고, 출처를 zn_front_source에
    # 남겨 실제 CBOT 체결가와 구분할 수 있어야 한다.
    master = _FakeOverseasFutureMaster(
        {"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26"), "ZN": ("ZNU26", "ZNZ26")}
    )
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
        failing={"ZNU26"},
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub(zn=108.50))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["zn_front"] == 108.50
    assert written[0]["zn_front_source"] == "yfinance_fallback"


def test_poll_macro_snapshot_continues_when_us10y_fails(monkeypatch):
    # CBOT 미신청 계좌 등으로 US10Y만 실패해도 VIX/USDCNH는 그대로 적재돼야 한다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", None)})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=None,
        failing={"US10Y"},
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["us10y_yield"] is None
    assert written[0]["vix_front"] == 17.50


def test_poll_macro_snapshot_skips_write_when_all_futures_fail(monkeypatch):
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", None)})
    rest_client = _FakeOverseasRestClient(
        future_prices={},
        daily_chart=_daily_chart_response(4.54),
        failing={"VXN26", "VXQ26", "CNHN26"},
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert written == []
    assert rest_client.daily_calls == []  # 선물 3건이 전부 실패하면 US10Y/USDKRW 조회 자체를 시도하지 않음


def test_poll_macro_snapshot_collects_usdkrw_daily_level(monkeypatch):
    # 2026-07-20: USDKRW는 해외주식 도메인(환율구분 X, FX@KRW)이라 CBOT 같은 계좌 게이트가 없다 —
    # US10Y와 동일하게 계좌 제약 없이 무료로 얻어야 한다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
        usdkrw_daily_chart=_daily_chart_response(1352.30),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert (tr_codes.FID_MRKT_DIV_OVERSEAS_FX, tr_codes.FID_INPUT_ISCD_USDKRW) in rest_client.daily_calls
    assert len(written) == 1
    assert written[0]["usdkrw"] == pytest.approx(1352.30)


def test_poll_macro_snapshot_continues_when_usdkrw_fails(monkeypatch):
    # USDKRW 조회만 실패해도(레이트리밋 등) 나머지 필드는 그대로 적재돼야 한다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
        failing={"USDKRW"},
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub())

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["usdkrw"] is None
    assert written[0]["us10y_yield"] == pytest.approx(4.54)  # USDKRW 실패가 US10Y를 막지 않음


def test_poll_macro_snapshot_includes_es_front_when_kis_succeeds(monkeypatch):
    # 2026-07-20: ES(CME E-mini S&P500)도 마스터에 매핑되면 ZN과 동일하게 KIS를 우선 사용해야 한다.
    master = _FakeOverseasFutureMaster(
        {"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26"), "ES": ("ESU26", "ESZ26")}
    )
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
            "ESU26": _future_price_response(5123.25),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    fallback_calls: list[str] = []

    def _record_fallback_call(symbol: str) -> float | None:
        fallback_calls.append(symbol)
        return None

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _record_fallback_call)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert "ESU26" in rest_client.future_calls
    assert "ESZ26" not in rest_client.future_calls  # 근월물만 조회
    assert len(written) == 1
    assert written[0]["es_front"] == 5123.25
    assert written[0]["es_front_source"] == "kis"
    assert yfinance_fallback.ES_FALLBACK_SYMBOL not in fallback_calls


def test_poll_macro_snapshot_uses_yfinance_fallback_when_kis_es_fails(monkeypatch):
    # ES(CME|CME)도 ZN(CME|CBOT)과 동일하게 KIS 유료 항목 — 미구독 상태에서는 yfinance 폴백을 쓴다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub(es=5100.00))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["es_front"] == 5100.00
    assert written[0]["es_front_source"] == "yfinance_fallback"


def test_poll_macro_snapshot_collects_move_index_via_yfinance_only(monkeypatch):
    # MOVE(ICE BofA MOVE Index)는 장외 파생 인덱스라 KIS 해외선물옵션 마스터파일에 상품 자체가
    # 없다 — KIS 시도 없이 처음부터 yfinance 폴백만으로 채워져야 한다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))
    monkeypatch.setattr("mahdi.main.yfinance_fallback.fetch_last_close", _fallback_stub(move=95.30))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 1
    assert written[0]["move_index"] == pytest.approx(95.30)
    assert written[0]["move_index_source"] == "yfinance_fallback"


def test_configure_logging_uses_rotating_file_handler(monkeypatch, tmp_path):
    # 2026-07-19(§5-5 "로그 위생"): logs/observation_loop.log가 로테이션 없이 105MB까지
    # 누적됐던 문제 — Python 로깅이 파일당 LOG_MAX_BYTES로 회전시키는 RotatingFileHandler를
    # 실제로 구성하는지 검증한다(실제 프로젝트 logs/ 디렉터리는 건드리지 않도록 tmp_path로 치환).
    import mahdi.main as mahdi_main

    fake_log_dir = tmp_path / "logs"
    fake_log_file = fake_log_dir / "observation_loop.log"
    monkeypatch.setattr(mahdi_main, "LOG_DIR", fake_log_dir)
    monkeypatch.setattr(mahdi_main, "LOG_FILE", fake_log_file)

    basic_config_calls = []
    monkeypatch.setattr(mahdi_main.logging, "basicConfig", lambda **kwargs: basic_config_calls.append(kwargs))

    mahdi_main._configure_logging()

    assert fake_log_dir.exists()  # mkdir(parents=True, exist_ok=True) 확인
    assert len(basic_config_calls) == 1
    handlers = basic_config_calls[0]["handlers"]
    assert len(handlers) == 2

    file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    file_handler = file_handlers[0]
    assert file_handler.maxBytes == mahdi_main.LOG_MAX_BYTES
    assert file_handler.backupCount == mahdi_main.LOG_BACKUP_COUNT
    assert Path(file_handler.baseFilename) == fake_log_file.resolve()

    stream_handlers = [h for h in handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)]
    assert len(stream_handlers) == 1

    # 2026-07-29(운영점검보고서 §2-5): 타임스탬프가 없어 WARNING 발생 시각을 로그만으로 특정할
    # 수 없었던 문제 — asctime이 포맷에 포함되는지 확인.
    assert "%(asctime)s" in basic_config_calls[0]["format"]


def test_log_startup_gap_writes_marker_when_none_exists(monkeypatch, tmp_path, caplog):
    # 2026-07-20 고도화: 마커 파일이 아직 없으면(최초 실행) 비교 없이 정보만 남기고, 이번 기동
    # 시각으로 마커를 새로 만든다.
    import mahdi.main as mahdi_main

    fake_log_dir = tmp_path / "logs"
    fake_marker = fake_log_dir / ".last_successful_start.txt"
    monkeypatch.setattr(mahdi_main, "LOG_DIR", fake_log_dir)
    monkeypatch.setattr(mahdi_main, "LAST_START_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 7, 30, 0)
    monkeypatch.setattr(mahdi_main.db, "local_now", lambda: now)

    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        mahdi_main._log_startup_gap_since_last_run()

    assert "직전 정상 기동 기록 없음" in caplog.text
    assert fake_marker.exists()
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()


def test_log_startup_gap_reports_elapsed_hours_and_updates_marker(monkeypatch, tmp_path, caplog):
    # 07-17(금) 15:45 장마감 자동 종료가 스케줄대로 실행되지 못했던 사례처럼, 예약 실행이
    # 하루 이상 건너뛰면 다음 정상 기동 시점에 경과 시간이 로그에 그대로 남아야 한다.
    import mahdi.main as mahdi_main

    fake_log_dir = tmp_path / "logs"
    fake_log_dir.mkdir()
    fake_marker = fake_log_dir / ".last_successful_start.txt"
    last = datetime(2026, 7, 17, 7, 30, 0)
    fake_marker.write_text(last.isoformat(), encoding="utf-8")
    monkeypatch.setattr(mahdi_main, "LOG_DIR", fake_log_dir)
    monkeypatch.setattr(mahdi_main, "LAST_START_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 7, 30, 0)  # 정확히 3일(72시간) 뒤
    monkeypatch.setattr(mahdi_main.db, "local_now", lambda: now)

    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        mahdi_main._log_startup_gap_since_last_run()

    assert "직전 정상 기동: 2026-07-17 07:30:00 (72.0시간 전)" in caplog.text
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()  # 마커가 이번 기동 시각으로 갱신됨


def test_log_startup_gap_handles_corrupted_marker_and_recovers(monkeypatch, tmp_path, caplog):
    # 마커 파일 내용이 파싱 불가해도(수동 편집 실수 등) 관측 루프 기동 자체는 죽으면 안 되고,
    # 다음 기동을 위해 마커는 정상값으로 복구돼야 한다.
    import mahdi.main as mahdi_main

    fake_log_dir = tmp_path / "logs"
    fake_log_dir.mkdir()
    fake_marker = fake_log_dir / ".last_successful_start.txt"
    fake_marker.write_text("이건 타임스탬프가 아님", encoding="utf-8")
    monkeypatch.setattr(mahdi_main, "LOG_DIR", fake_log_dir)
    monkeypatch.setattr(mahdi_main, "LAST_START_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 7, 30, 0)
    monkeypatch.setattr(mahdi_main.db, "local_now", lambda: now)

    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        mahdi_main._log_startup_gap_since_last_run()

    assert "직전 기동 기록 확인 실패" in caplog.text
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()  # 손상된 마커도 이번 기록으로 복구됨


def test_poll_option_chain_throttles_repeated_leg_insert_failure_warnings(monkeypatch, caplog):
    # 2026-07-19(§5-5): 얇은 옵션 종목의 NumericValueOutOfRange(§3-1)는 한 사이클 안에서
    # 레그마다 반복 재발할 수 있다(실측 3,416회) — 60초 창 안에서는 최초 1건만 실제로 로깅돼야
    # 로그 파일이 그 반복으로 다시 파묻히지 않는다.
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)
    fake_conn = _FakeConnWithRollback()

    @contextmanager
    def fake_get_connection(settings=None):
        yield fake_conn

    call_count = {"n": 0}

    def fake_insert(conn, row):
        call_count["n"] += 1
        raise ValueError("numeric field overflow")  # 이번 사이클의 두 레그(C/P) 모두 실패

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", fake_insert)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client, [(_FakeSubscriptionManagerWithStrikes(), "regular")], _FakeMaster(), interval_seconds=1
                )
            )

    assert call_count["n"] == 2  # 콜/풋 둘 다 삽입 시도는 됨(실패 자체는 억제 대상 아님)
    failure_records = [r for r in caplog.records if "옵션 체인 적재 실패" in r.getMessage()]
    assert len(failure_records) == 1  # 같은 60초 창 안에서 두 번째(풋) 실패는 로깅 억제됨


def _http_status_error(status_code: int, json_body: dict) -> httpx.HTTPStatusError:
    """실제 KIS 500 응답처럼 msg_cd/msg1이 담긴 응답 바디를 가진 httpx.HTTPStatusError를 만든다."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, json=json_body, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status()가 예외를 던지지 않음")


class _FakeRestClientChainAlwaysFails:
    """get_quote() 호출마다 항상 지정된 예외를 던진다 — 레그별 조회 실패 로깅 검증용."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        raise self._exc


def test_poll_option_chain_leg_fetch_failure_logs_kis_response_body_and_is_throttled(monkeypatch, caplog):
    # 2026-07-20: get_quote() 500을 그냥 재로깅하면 "Server error 500"만 남고 레이트리밋(EGW00201)인지
    # 다른 원인인지 로그만으로 구분할 수 없었다 — httpx 응답 바디(msg_cd/msg1)를 함께 남겨야 한다.
    # 또한 이 실패는 사이클 전체 실패(§3-1과 별개)로 재시도까지 이어지면 레그당 최대 4번(1차 2건 +
    # 재시도 2건) 반복될 수 있어, §5-5와 동일하게 60초당 최초 1건만 실제로 로깅돼야 한다.
    exc = _http_status_error(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다"})
    rest_client = _FakeRestClientChainAlwaysFails(exc)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)

    async def fake_sleep(seconds):
        if seconds != 5.0:  # retry_backoff_seconds(기본값)면 통과시켜 재시도가 실제로 일어나게 함
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        with pytest.raises(RuntimeError, match="stop-loop"):
            _run(
                poll_option_chain(
                    rest_client, [(_FakeSubscriptionManagerWithStrikes(), "regular")], _FakeMaster(), interval_seconds=1
                )
            )

    assert len(rest_client.calls) == 4  # 1차 시도(콜/풋) + 재시도(콜/풋) 전부 시도됨

    fetch_failure_records = [r for r in caplog.records if "옵션 체인 폴링 실패" in r.getMessage()]
    assert len(fetch_failure_records) == 1  # 같은 60초 창 안에서 나머지 3건은 억제됨
    logged_message = fetch_failure_records[0].getMessage()
    assert "EGW00201" in logged_message  # 응답 바디(KIS 원인 코드)가 로그에 남음
    assert "초당 거래건수를 초과하였습니다" in logged_message


# --- 옵션체인 수집 예산(2026-08-04 운영점검보고서 §2-6 / Fix#8) ------------------------------


_OPTION_QUOTE_FIXTURE = {
    "output1": {
        "futs_last_tr_date": "20260813", "gama": "0.01", "hts_ints_vltl": "18.5",
        "hist_vltl": "15.0", "hts_otst_stpl_qty": "1000", "delta_val": "0.5",
        "theta": "-0.1", "vega": "0.2", "otst_stpl_qty_icdc": "10", "acml_vol": "100",
    },
    "output3": {"bstp_nmix_prpr": "1000.0"},
}


class _FakeManagerManyStrikes:
    def __init__(self, strikes: frozenset[float]):
        self._strikes = strikes

    @property
    def desired_strikes(self) -> frozenset[float]:
        return self._strikes


class _FakeRestClientCountingQuotes:
    """호출 때마다 monotonic 시계를 진행시켜 "느린 KIS 응답"을 흉내낸다."""

    def __init__(self, clock: list[float], seconds_per_call: float, resp: dict):
        self._clock = clock
        self._seconds_per_call = seconds_per_call
        self._resp = resp
        self.calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        self._clock[0] += self._seconds_per_call
        return self._resp


def _collect_with_budget(monkeypatch, seconds_per_call: float, budget: float):
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])
    rest_client = _FakeRestClientCountingQuotes(clock, seconds_per_call, _OPTION_QUOTE_FIXTURE)
    books = [(_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "regular")]
    rows, _spot, any_strikes, _missing = _run(
        _collect_option_chain_cycle(
            rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 5, 10, 0),
            WarningThrottle(60.0), deadline=clock[0] + budget,
        )
    )
    return rest_client, rows, any_strikes


def test_collect_option_chain_cycle_stops_calling_once_the_budget_is_spent(monkeypatch, caplog):
    """회귀 방지 §2-6: 느린 레그가 사이클 전체를 잡아먹어 다음 분을 덮는 것을 막는다.

    08-04 실측 — 밀림 48건의 REST수집 평균이 78.3초(레그당 3.9초, 정상 1.7초)였고,
    미회수 결손 5분이 전부 그 시간대에 몰렸다.
    """
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rest_client, rows, any_strikes = _collect_with_budget(monkeypatch, seconds_per_call=10.0, budget=50.0)

    assert any_strikes
    assert len(rest_client.calls) == 5  # 10초 x 5건 = 50초에서 예산 소진, 나머지 5레그는 미호출
    assert len(rows) == 5
    budget_records = [r for r in caplog.records if "수집 예산" in r.getMessage()]
    assert len(budget_records) == 1  # 레그마다가 아니라 사이클당 1줄
    assert "남은 5레그" in budget_records[0].getMessage()


def test_collect_option_chain_cycle_collects_everything_when_it_fits(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rest_client, rows, _ = _collect_with_budget(monkeypatch, seconds_per_call=1.0, budget=50.0)

    assert len(rest_client.calls) == 10  # 5행사가 x (C,P)
    assert len(rows) == 10
    assert [r for r in caplog.records if "수집 예산" in r.getMessage()] == []


# ===== 2026-08-11 Fix#1/#2 — 연속 타임아웃 조기 포기 + 컷 귀속 =====
#
# 08-11 15:01~15:22에 22분 연속으로 적재가 0행이었다. KIS 지연이 4초를 넘기자 read 타임아웃이
# 전 호출을 실패로 바꿨고, **실패한 호출도 성공한 호출과 똑같이 예산을 먹으므로** 사이클이
# 50초를 다 태우고 0행을 남기는 상태에 고정됐다.


class _FakeRestClientAlwaysTimingOut:
    """모든 레그가 `httpx.ReadTimeout`으로 끝나는 KIS — 08-11 15시대의 재현."""

    def __init__(self, clock: list[float], seconds_per_call: float) -> None:
        self._clock = clock
        self._seconds_per_call = seconds_per_call
        self.calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        self._clock[0] += self._seconds_per_call
        raise httpx.ReadTimeout("The read operation timed out")


def _collect_all_timeouts(monkeypatch, books, budget: float = 50.0):
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])
    rest_client = _FakeRestClientAlwaysTimingOut(clock, seconds_per_call=5.0)
    rows, _spot, _any, missing = _run(
        _collect_option_chain_cycle(
            rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 11, 15, 1),
            # 실패 경로를 타므로 진짜 로거가 필요하다(`WarningThrottle(60.0)`은 성공 경로 전용).
            WarningThrottle(logging.getLogger("mahdi.main"), 60.0), deadline=clock[0] + budget,
        )
    )
    return rest_client, rows, missing


def test_consecutive_read_timeouts_abort_the_cycle_instead_of_burning_the_budget(monkeypatch, caplog):
    """08-11 15:01 재현 — 10레그를 다 부르지 않고 3회 연속 타임아웃에서 접어야 한다.

    되돌리면(조기 포기 제거) 호출이 10건이 되어 이 테스트가 깨진다. 그것이 이 테스트의 목적이다.
    """
    books = [(_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "regular")]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rest_client, rows, missing = _collect_all_timeouts(monkeypatch, books)

    assert len(rest_client.calls) == mahdi_main.OPTION_CHAIN_CONSECUTIVE_TIMEOUT_ABORT  # 3건에서 접는다
    assert rows == []
    # 접었어도 남은 먼슬리 레그는 재시도 대상으로 그대로 넘어간다(고도화#1 경로를 안 깬다).
    assert len(missing) == 10

    aborts = [r for r in caplog.records if "연속 타임아웃" in r.getMessage()]
    assert len(aborts) == 1  # 레그마다가 아니라 사이클당 1줄
    assert "컷당한북=regular" in aborts[0].getMessage()
    # 예산 초과와 **다른 줄**이어야 한다 — 원인이 다르므로 지표도 갈려야 한다.
    assert [r for r in caplog.records if "수집 예산" in r.getMessage()] == []


def test_a_single_timeout_does_not_abort_the_cycle(monkeypatch, caplog):
    """단발 지터로 접으면 안 된다 — 카운터는 **연속**이고 성공하면 0으로 돌아간다."""
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])

    class _FlakyOnce:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
            self.calls.append(symbol)
            clock[0] += 1.0
            if len(self.calls) in (1, 3, 5):  # 사이사이 실패 — 연속이 아니다
                raise httpx.ReadTimeout("The read operation timed out")
            return _OPTION_QUOTE_FIXTURE

    rest_client = _FlakyOnce()
    books = [(_FakeManagerManyStrikes(frozenset({1000.0, 1002.5, 1005.0})), "regular")]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rows, _spot, _any, _missing = _run(
            _collect_option_chain_cycle(
                rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 11, 10, 0),
                WarningThrottle(logging.getLogger("mahdi.main"), 60.0), deadline=clock[0] + 50.0,
            )
        )

    assert len(rest_client.calls) == 6  # 3행사가 x (C,P) — 하나도 안 건너뛴다
    assert len(rows) == 3
    assert [r for r in caplog.records if "연속 타임아웃" in r.getMessage()] == []


def test_cumulative_failures_abort_even_when_they_are_not_consecutive(monkeypatch, caplog):
    """고도화 A — Fix#1의 **연속** 카운터가 못 보는 패턴을 잡는다.

    08-11 14시대가 이 형태였다: 예산 초과 20건인데 전멸은 1건뿐이었고, 나머지는 성공/실패가
    섞여 얇아진 분이다. 성공 하나에 연속 카운터가 0으로 돌아가므로 Fix#1만으로는 안 접힌다.
    """
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])

    class _AlternatingFailure:
        """실패-성공을 번갈아 낸다 — 연속은 항상 1이지만 누적은 쌓인다."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
            self.calls.append(symbol)
            clock[0] += 1.0
            if len(self.calls) % 2 == 1:
                raise httpx.ReadTimeout("The read operation timed out")
            return _OPTION_QUOTE_FIXTURE

    rest_client = _AlternatingFailure()
    # 12레그(6행사가 x C/P) — 홀수 호출이 실패하므로 6번째 실패가 11번째 호출에서 난다.
    # 10레그로는 실패가 5건이라 예산(6)에 애초에 못 닿는다.
    books = [
        (_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0, 1007.5})), "regular")
    ]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rows, _spot, _any, _missing = _run(
            _collect_option_chain_cycle(
                rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 11, 14, 30),
                WarningThrottle(logging.getLogger("mahdi.main"), 60.0), deadline=clock[0] + 50.0,
            )
        )

    # 실패 6건째에서 접는다 — 시간 예산(50초)은 아직 한참 남아 있다.
    assert len(rest_client.calls) == 2 * mahdi_main.OPTION_CHAIN_CYCLE_FAILURE_BUDGET_LEGS - 1
    assert len(rows) == mahdi_main.OPTION_CHAIN_CYCLE_FAILURE_BUDGET_LEGS - 1
    aborts = [r for r in caplog.records if "실패 예산" in r.getMessage()]
    assert len(aborts) == 1
    # 연속 타임아웃 줄이 아니어야 한다 — 원인이 다르다.
    assert [r for r in caplog.records if "연속 타임아웃" in r.getMessage()] == []


def test_budget_cut_names_the_book_it_reached(monkeypatch, caplog):
    """Fix#2 — 컷이 **어느 북에 닿았는가**를 로그가 말한다.

    08-06이 "예산 컷이 먼슬리에 닿은 분 3분"을 손으로 세어 고도화#1의 방향을 정했는데,
    그 실측이 지표로는 없었다. 먼슬리 우선 순서상 컷은 뒤쪽 북부터 닿으므로 **여기 `regular`가
    들어오면 그 자체가 사건**이다.
    """
    books = [
        (_FakeManagerManyStrikes(frozenset({1000.0, 1002.5})), "regular"),
        (_FakeManagerManyStrikes(frozenset({1000.0, 1002.5})), "weekly_mon"),
    ]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rest_client, rows, _ = _collect_with_budget_books(monkeypatch, books, seconds_per_call=10.0, budget=40.0)

    assert len(rows) == 4  # 먼슬리 4레그는 지켜졌다
    cut = [r for r in caplog.records if "수집 예산" in r.getMessage()]
    assert len(cut) == 1
    # 위클리만 잘렸다 — 먼슬리가 이 목록에 들어오면 순서 보장이 깨진 것이다.
    assert "컷당한북=weekly_mon" in cut[0].getMessage()


def _collect_with_budget_books(monkeypatch, books, seconds_per_call: float, budget: float):
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])
    rest_client = _FakeRestClientCountingQuotes(clock, seconds_per_call, _OPTION_QUOTE_FIXTURE)
    rows, _spot, any_strikes, _missing = _run(
        _collect_option_chain_cycle(
            rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 11, 10, 0),
            WarningThrottle(60.0), deadline=clock[0] + budget,
        )
    )
    return rest_client, rows, any_strikes


def test_collect_option_chain_cycle_without_deadline_is_unbounded(monkeypatch):
    """`deadline=None`(기본값)은 종전과 완전히 동일하게 동작해야 한다 — 백테스트/테스트 경로 보호."""
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])
    rest_client = _FakeRestClientCountingQuotes(clock, 999.0, _OPTION_QUOTE_FIXTURE)
    rows, _spot, _any, _missing = _run(
        _collect_option_chain_cycle(
            rest_client, [(_FakeManagerManyStrikes(frozenset({1000.0, 1002.5})), "regular")],
            _FakeMaster(), "KOSPI200", datetime(2026, 8, 5, 10, 0), WarningThrottle(60.0),
        )
    )
    assert len(rows) == 4  # 예산이 없으면 아무리 느려도 전부 모은다


# --- Signal Fusion 라이브 배선(2026-07-28 2차, ADVISORY 전용) --------------------------------


def test_build_signal_inputs_computes_gex_and_gamma_flip_from_chain_and_spot(monkeypatch):
    chain_rows = [
        {"strike": 350.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
        {"strike": 350.0, "option_type": "P", "oi": 80.0, "iv": 0.20, "gamma": 0.018,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 350.5)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, -100.0, -200.0))
    # 위 만기(2026-08-13)가 지나면 `signal_book_legs()`가 이 북을 떨어뜨려 gex가 None이 된다 —
    # 코드 변경 없이 실패하는 픽스처 부패다. 근거는 `_CHAIN_FIXTURE_NOW` 주석.
    # 이 테스트는 `_patch_chain`을 쓰지 않으므로 여기서 직접 고정한다.
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: _CHAIN_FIXTURE_NOW)

    regime_state = RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=(1.0,) + (0.0,) * 7)
    inputs, chain_inputs = _build_signal_inputs(
        conn=object(), regime_state=regime_state, underlying="KOSPI200"
    )

    assert inputs.regime_state is regime_state
    assert inputs.spot == 350.5
    assert inputs.gex is not None  # 콜/풋 감마가 있으니 계산됨
    assert inputs.foreign_net_flow == 500.0
    assert inputs.ofi is None  # futures_symbol을 안 넘겼으므로 이 사이클만 미가용
    assert inputs.queue_imbalance is None  # 호가 잔량 미적재 — 지어내지 않는다
    # 2026-08-03 §5-1: 판단 행에 남길 체인 입력 관측치를 함께 돌려준다(마이그레이션 022).
    assert chain_inputs["gex"] == inputs.gex
    assert chain_inputs["gamma_flip"] == inputs.gamma_flip
    assert chain_inputs["chain_leg_count"] == 2
    # 2026-08-04 Fix#5(마이그레이션 023): 어느 북으로 GEX를 냈는지 함께 남긴다.
    assert chain_inputs["gex_expiry"] == date(2026, 8, 13)


# --- 판단 파이프라인 배선(2026-08-04 운영점검보고서 §2-4/§2-5/§2-8, Fix#2·#3·#4·#5) ---------


def _chain_row(strike: float, opt: str, oi: float, expiry: date, *, iv: float = 0.18,
               gamma: float = 0.02) -> dict:
    return {"strike": strike, "option_type": opt, "oi": oi, "iv": iv, "gamma": gamma,
            "gex": 0.0, "expiry": expiry, "timestamp": datetime(2026, 8, 5, 10, 0)}


_MONTHLY = date(2026, 8, 13)
_WEEKLY = date(2026, 8, 6)


def _two_book_chain() -> list[dict]:
    """먼슬리는 풋 편중(GEX 음수), 위클리는 콜 편중(GEX 양수) — 08-04 실측과 같은 배치."""
    return [
        *[_chain_row(k, t, oi, _MONTHLY) for k, t, oi in
          ((345.0, "C", 10), (345.0, "P", 900), (350.0, "C", 20), (350.0, "P", 800),
           (355.0, "C", 30), (355.0, "P", 700))],
        *[_chain_row(k, t, oi, _WEEKLY) for k, t, oi in
          ((345.0, "C", 1500), (345.0, "P", 10), (350.0, "C", 1400), (350.0, "P", 20),
           (355.0, "C", 1300), (355.0, "P", 30))],
    ]


# 이 픽스처들의 만기(`_MONTHLY`/`_WEEKLY`)는 **고정 날짜**이고 `_build_signal_inputs()`는
# `signal_book_legs(chain_rows, now.date())`로 만기 지난 북을 떨어뜨린다. 그래서 `local_now()`를
# 막지 않으면 이 테스트들은 **2026-08-13이 지난 날부터 조용히 실패한다** — 실제로 그렇게 됐다
# (2026-08-16에 `gex_expiry`가 None이 되어 3건 실패, 원인은 코드가 아니라 픽스처의 부패였다).
#
# 개별 테스트가 자기 시각을 쓰려면 `now=`로 넘기거나 이 헬퍼 **뒤에** 직접 setattr 하면 된다
# (뒤에 부른 monkeypatch가 이긴다 — 현재 그렇게 하는 테스트 셋이 있다).
#
# 같은 함정을 이 파일이 이미 두 번 주석으로 경고했다(§3420 "기본값을 `db.local_now()`로 두면"·
# §3460 "테스트를 오후에 돌렸는지에 따라 ENTER가 REJECT로 바뀐다") — 그 경고를 **헬퍼로 강제**한다.
_CHAIN_FIXTURE_NOW = datetime(2026, 8, 5, 10, 0)


def _patch_chain(monkeypatch, chain_rows, spot=350.5, micro=None, now=_CHAIN_FIXTURE_NOW):
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: now)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: spot)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr(
        "mahdi.main.db.latest_market_microstructure", lambda conn, symbol, as_of=None: micro
    )


def test_build_signal_inputs_uses_only_the_monthly_book(monkeypatch):
    """회귀 방지 §2-8(Fix#5): 세 만기를 평탄화해 합산하면 부호가 반대인 북끼리 상쇄된다.

    08-04 실측 — 위클리 08-06 GEX +90.8B(콜 편중)와 먼슬리 08-13 −51.0B(풋 편중)가 섞여
    라이브 GEX가 하루 동안 −33.5B ~ +99.1B를 오갔고(양수 233분 / 음수 259분), GEX 부호가
    곧 `options_flow`의 회귀/증폭 판정이라 **신호 부호가 그 상쇄에 좌우됐다.**
    v6 §11.4는 먼슬리를 주 입력으로 규정한다 — 코드를 문서에 맞춘다.
    """
    _patch_chain(monkeypatch, _two_book_chain())

    inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert chain_inputs["gex_expiry"] == _MONTHLY
    assert chain_inputs["chain_leg_count"] == 6  # 위클리 6레그는 판단에 안 쓴다
    assert inputs.gex is not None and inputs.gex < 0  # 먼슬리 단독 = 풋 편중 = 음수
    # 두 북을 합치면 양수가 되어 부호가 뒤집힌다 — 그것이 08-04까지의 동작이었다.
    both = calculate_gex(legs_from_chain_rows(_two_book_chain(), today=date(2026, 8, 5)), 350.5)
    assert both > 0


def test_build_signal_inputs_wires_ofi_from_market_raw_when_futures_symbol_is_known(monkeypatch):
    """회귀 방지 §2-5(Fix#2): `ofi=None` 하드코딩이 앙상블 멤버 하나를 통째로 죽였다.

    docstring은 *"라이브 집계 파이프라인이 없어"* 라고 적었지만 `market_raw_1m.ofi`는 08-04에
    선물 410분 **전부** 채워져 있었다(404분이 0 아님).
    """
    _patch_chain(monkeypatch, _two_book_chain(), micro={"ofi": -12.5, "microprice": 350.4,
                                                        "bid_ask_spread": 0.05, "vpin": 0.4,
                                                        "timestamp": datetime(2026, 8, 5, 10, 0)})

    inputs, _ = _build_signal_inputs(
        conn=object(), regime_state=None, underlying="KOSPI200", futures_symbol="A01609"
    )

    assert inputs.ofi == -12.5
    assert build_member_scores(inputs).orderflow_ofi_vpin == -1.0  # 부호만 쓴다


def test_build_signal_inputs_leaves_ofi_none_when_the_bar_is_stale(monkeypatch):
    """신선도 창 밖이면 `latest_market_microstructure()`가 None을 준다 — 옛 값을 쓰지 않는다."""
    _patch_chain(monkeypatch, _two_book_chain(), micro=None)

    inputs, _ = _build_signal_inputs(
        conn=object(), regime_state=None, underlying="KOSPI200", futures_symbol="A01609"
    )

    assert inputs.ofi is None
    assert build_member_scores(inputs).orderflow_ofi_vpin is None


def test_build_signal_inputs_fills_charm_and_gamma_wall(monkeypatch):
    """회귀 방지 §2-4(Fix#3/#4): `options_flow`의 두 진입로가 **둘 다** 끊겨 있었다.

    - Charm 경로: `total_charm`/`charm_active`를 아예 안 채웠고, 채웠어도 0이었다
      (`option_analysis_1m.charm` 컬럼은 존재하지만 08-04에 9,288행 전부 NULL).
    - 감마플립 경로: 이 북에서 flip이 구조적으로 안 나온다(§2-3) → 감마 월로 폴백한다.
    """
    # 시각은 `_patch_chain`에 넘긴다 — 헬퍼가 `local_now()`를 고정하므로 **앞에서** 따로
    # setattr 하면 헬퍼가 그것을 덮어쓴다(그렇게 뒀다가 charm_active가 False로 떨어졌다).
    _patch_chain(monkeypatch, _two_book_chain(), now=datetime(2026, 8, 5, 14, 30))

    inputs, _ = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert inputs.gamma_wall is not None
    assert inputs.charm_active is True  # 14:00 이후 (v6 §13.2)
    assert inputs.total_charm is not None and inputs.total_charm != 0.0
    # flip이 없어도 wall 폴백으로 멤버가 산다 — 이것이 Fix#3의 전부다.
    assert inputs.gamma_flip is None
    assert build_member_scores(inputs).options_flow is not None


def test_options_flow_falls_back_to_none_when_the_gate_is_off(monkeypatch):
    """게이트를 끄면 08-04 이전 동작과 **완전히 동일**해야 한다 — 되돌릴 수 있어야 한다."""
    monkeypatch.setattr("mahdi.fusion.signal_layer.OPTIONS_FLOW_GAMMA_WALL_FALLBACK", False)
    inputs = SignalInputs(gex=-1.0, gamma_flip=None, gamma_wall=350.0, spot=355.0)
    assert build_member_scores(inputs).options_flow is None


def test_member_unavailable_reasons_names_the_missing_ingredient():
    """2026-08-04 고도화#2 — 숫자 하나(`available_member_count`)로는 어느 멤버가 왜 죽었는지 모른다."""
    from mahdi.main import _member_unavailable_reasons

    # 2026-08-05 §2-8: 판단 시각이 필수 인자가 됐다 — 연속거래 중의 시각을 명시한다.
    # (기본값을 `db.local_now()`로 두면 이 테스트가 15:35 이후에만 다른 답을 낸다.)
    reasons = _member_unavailable_reasons(SignalInputs(), datetime(2026, 8, 5, 14, 0))

    assert reasons["xgboost_tabular"] == "미학습(Phase 3)"
    assert reasons["lstm_temporal"] == "미학습(Phase 3)"
    assert reasons["regime_hmm"] == "regime_state 없음"
    assert reasons["orderflow_ofi_vpin"] == "ofi/queue_imbalance 없음"
    assert reasons["flow_position"] == "foreign_net_flow 없음"
    assert "gex" in reasons["options_flow"]


def test_build_signal_inputs_handles_missing_chain_and_flow_gracefully(monkeypatch):
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: [])
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: None)

    inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert inputs.regime_state is None
    assert inputs.gex is None
    assert inputs.gamma_flip is None
    assert inputs.spot is None
    assert inputs.foreign_net_flow is None
    # 체인이 비면 레그 수는 0이고 나이는 None이다 — 없는 값을 0으로 채우면 "계산했는데 0"과
    # 구분되지 않는다.
    assert chain_inputs["chain_leg_count"] == 0
    assert chain_inputs["chain_oldest_leg_age_seconds"] is None


class _FakeRegimeStateMachineWithLastState:
    def __init__(self, last_state):
        self.last_state = last_state


def _patch_signal_fusion_cycle_db_defaults(monkeypatch):
    """2026-07-30(운영점검 §4 Fix#4/#6): poll_signal_fusion_cycle이 이제 **진입 여부와 무관하게**
    매 사이클 거래정지 상태와 계좌 스냅샷을 조회하고 risk_snapshots를 남긴다 — 그 기본 스텁을
    한 곳에 모은다(개별 테스트는 이 호출 뒤에 필요한 것만 다시 덮어쓰면 된다).

    2026-08-06(§2-2 / Fix#1): **시각도 여기서 고정한다.** 진입 컷오프(14:50)가 생긴 뒤로
    `local_now()`를 안 막으면 *테스트를 오후에 돌렸는지에 따라* ENTER가 REJECT로 바뀐다 —
    실행 시각에 따라 결과가 달라지는 테스트는 회귀를 못 잡는다. 10:00은 컷오프 한참 전이다."""
    monkeypatch.setattr("mahdi.main.db.latest_market_halt_state", lambda conn: None)
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: None)
    monkeypatch.setattr("mahdi.main.db.insert_risk_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 10, 0))
    # 2026-08-11 고도화 D — 쿨다운 입력. 호출측이 try/except로 감싸지만, 스텁을 두는 쪽이
    # 빠르고 "이 폴러가 무엇을 조회하는가"를 이 헬퍼 한 곳에서 읽을 수 있게 한다.
    monkeypatch.setattr("mahdi.main.db.minutes_since_last_entry_by_strategy", lambda conn, now: {})


def test_poll_signal_fusion_cycle_logs_decision_and_respects_fixed_tick_schedule(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: [])
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: None)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (ts, conviction, decision, reject_reason, risk_gate_state, exec_mode)
        ),
    )

    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        if len(recorded) >= 2:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    regime_state_machine = _FakeRegimeStateMachineWithLastState(None)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=60))

    assert len(recorded) == 2
    _ts, conviction, decision, _reject_reason, _risk_gate_state, exec_mode = recorded[0]
    assert conviction == "NO_TRADE"  # 신호 원재료가 전부 없으니 NO_TRADE
    assert decision == "REJECT"
    assert exec_mode == "ADVISORY"  # 이번 증분은 항상 ADVISORY — 실주문 없음


def test_poll_signal_fusion_cycle_marks_entry_when_strong_aligned_signal(monkeypatch):
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    chain_rows = [
        {"strike": 95.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    # 2026-07-28 8차: is_entry일 때 계좌 추적기를 조회하므로, 이 테스트는 "추적기 미준비"
    # 경로로 흘려보내 기존 검증 범위(진입 후보 로깅 자체)를 그대로 유지한다.
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: None)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (ts, conviction, decision, reject_reason, risk_gate_state, exec_mode)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    regime_state = RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector))
    regime_state_machine = _FakeRegimeStateMachineWithLastState(regime_state)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=60))

    assert len(recorded) == 1
    _ts, _conviction, decision, reject_reason, risk_gate_state, _exec_mode = recorded[0]
    assert decision == "ENTER"
    assert reject_reason is None
    assert risk_gate_state["direction"] > 0


# --- 2026-08-18 마이그레이션 032 — 옵션 가격 필드 -------------------------------------------
#
# 필드명(`futs_prpr`)은 08-18 07:31 실측 키 목록으로 확정했다. 공식 문서에는 `optn_prpr`가
# 있지만 우리 TR 응답에는 없다 — 문서와 실측이 갈리면 실측을 따른다.


def test_a_missing_price_field_empties_only_the_price_not_the_whole_leg():
    """가격이 없다고 그릭스까지 버리면 GEX가 죽는다 — 가격이 필요한 것은 진입 계획뿐이다."""
    from mahdi.main import _parse_option_quote

    resp = {
        "output1": {
            "futs_last_tr_date": "20260820", "gama": "0.02", "hts_ints_vltl": "18.0",
            "hist_vltl": "16.0", "hts_otst_stpl_qty": "100", "delta_val": "0.5",
            "theta": "-100.0", "vega": "1.0", "otst_stpl_qty_icdc": "0", "acml_vol": "10",
        },
        "output3": {"bstp_nmix_prpr": "1089.6"},
    }
    row, spot = _parse_option_quote(resp, 1090.0, "C", datetime(2026, 8, 18, 10, 0))

    assert row["price"] is None          # 가격만 빈다
    assert row["gamma"] == 0.02 and row["oi"] == 100  # 나머지는 살아 있다
    assert spot == 1089.6


def test_the_price_is_read_from_futs_prpr():
    from mahdi.main import _parse_option_quote

    resp = {
        "output1": {
            "futs_last_tr_date": "20260820", "gama": "0.02", "hts_ints_vltl": "18.0",
            "hist_vltl": "16.0", "hts_otst_stpl_qty": "100", "delta_val": "0.5",
            "theta": "-100.0", "vega": "1.0", "otst_stpl_qty_icdc": "0", "acml_vol": "10",
            "futs_prpr": "7.25",
        },
        "output3": {"bstp_nmix_prpr": "1089.6"},
    }
    row, _ = _parse_option_quote(resp, 1090.0, "C", datetime(2026, 8, 18, 10, 0))

    assert row["price"] == 7.25


def test_a_price_at_the_underlying_level_warns_because_the_field_would_be_wrong(caplog):
    """잘못된 필드를 조용히 넣으면 그 값이 나중에 **지정가**가 된다."""
    import mahdi.main as main_module

    monkey_reset = main_module._price_field_warned
    main_module._price_field_warned = False
    try:
        with caplog.at_level("WARNING", logger="mahdi.main"):
            main_module._warn_if_price_looks_like_underlying(1089.0, 1089.6)
        assert "옵션 가격 필드 의심" in caplog.text
    finally:
        main_module._price_field_warned = monkey_reset


def test_a_normal_premium_is_silent():
    import mahdi.main as main_module

    monkey_reset = main_module._price_field_warned
    main_module._price_field_warned = False
    try:
        main_module._warn_if_price_looks_like_underlying(7.25, 1089.6)
        assert main_module._price_field_warned is False  # 경고 안 함
    finally:
        main_module._price_field_warned = monkey_reset


# --- §11.5 종목 선택기 라이브 배선(2026-08-17) ------------------------------------------------
#
# 스펙에 절이 생긴 것과 코드가 도는 것은 다르다 — 워치독이 08-06에 만들어져 08-11까지 한 번도
# 안 돈 것과 같은 형태를 여기서 반복하지 않기 위한 테스트다. 「선택기가 돌았는가」와
# 「무엇을 골랐는가」를 각각 고정한다.


def _run_fusion_cycle_capturing_selection(monkeypatch, *, chain_rows, spot, regime_state):
    """진입 판단 1사이클을 돌리고 `selected_instruments` 인자로 넘어간 값을 돌려준다."""
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: spot)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: None)
    monkeypatch.setattr(
        "mahdi.main.db.latest_expiry_liquidity",
        lambda conn, underlying: [{"series": "regular", "expiry": date(2026, 8, 18)}],
    )
    monkeypatch.setattr("mahdi.main.db.entry_strategies_used_today", lambda conn, day: frozenset())

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    captured: list[dict | None] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: captured.append(selected_instruments),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(
            _FakeRegimeStateMachineWithLastState(regime_state), interval_seconds=60
        ))
    assert len(captured) == 1
    return captured[0]


def _trend_up_regime():
    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    return RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector))


def _selection_chain_rows(expiry=date(2026, 8, 18)):
    """ATM 100 부근의 최소 체인 — 만기는 2026-08-18(화요일, 대체공휴일 이월분).

    `rv_5d`가 iv보다 높아 VRP가 저평가로 떨어지고, 그래야 TREND_STRONG 행의 §11.4 매트릭스가
    `atm_long` 셀을 연다(적정이면 `itm_debit`라 델타 밴드 규칙이 대신 걸린다).
    """
    rows = []
    for strike in (95.0, 100.0, 105.0):
        for option_type in ("C", "P"):
            rows.append({
                "strike": strike, "option_type": option_type, "oi": 100.0, "iv": 0.18,
                "rv_5d": 0.30, "gamma": 0.02, "gex": 0.0, "expiry": expiry, "delta": 0.5,
                "volume": 10, "spread_state": 1, "price": 7.25,
            })
    return rows


def test_the_selector_records_a_concrete_instrument_on_an_entry_minute(monkeypatch):
    """§11.4까지는 전략 «이름»만 나왔다 — 이 테스트가 그 뒤에 종목이 붙는 것을 고정한다."""
    record = _run_fusion_cycle_capturing_selection(
        monkeypatch, chain_rows=_selection_chain_rows(), spot=100.0, regime_state=_trend_up_regime()
    )
    assert record is not None
    assert record["book_expiry"] == "2026-08-18"
    (candidate,) = record["candidates"]
    (leg,) = candidate["legs"]
    assert leg["strike"] == 100.0  # ATM
    assert leg["rule"] == "atm@100"
    # 근거가 함께 남아야 한다 — 계산해 놓고 버리면 "무엇이 이 행사가를 골랐나"에 못 답한다.
    assert leg["oi"] == 100 and leg["delta"] is not None


def test_a_minute_with_no_entry_strategy_still_says_why(monkeypatch):
    """NULL은 「선택기가 안 돌았다」다. 「돌릴 것이 없었다」와 뭉개지면 이 기록은 쓸모없다."""
    record = _run_fusion_cycle_capturing_selection(
        monkeypatch, chain_rows=[], spot=None, regime_state=None
    )
    assert record == {
        "candidates": [], "book_expiry": None, "reason": "no_entry_strategy", "rejected": [],
        # 2026-08-18 로테이션(§3) — 관망 분에도 판정을 싣되, 체인이 비면 「판정 못 했다」가
        # None 그대로 남는다.
        "target_series": None, "target_series_reason": None, "volume_leader_series": None,
    }


def test_a_seriesless_chain_keeps_the_expiry_day_book_out_of_candidates(monkeypatch):
    """series 미상 폴백(마이그레이션 033 이전 행)에서는 종전 규칙대로 만기 당일 북을 제외한다.

    2026-08-18 로테이션 규칙 1 이후 만기 당일 북 **채택**은 series를 아는 정상 경로에서
    허용된다(`ALLOW_EXPIRY_DAY_TARGET`, 근거는 instrument_selection의 그 상수 주석) — 폴백은
    어느 북인지도 모르는 상태라 0DTE 후보를 만들지 않는 종전 불변식을 유지한다.
    """
    today = date(2026, 8, 6)  # `_patch_signal_fusion_cycle_db_defaults`가 고정하는 날짜
    record = _run_fusion_cycle_capturing_selection(
        monkeypatch, chain_rows=_selection_chain_rows(expiry=today), spot=100.0,
        regime_state=_trend_up_regime(),
    )
    assert record["candidates"] == []
    assert record["reason"] == "no_eligible_book"


# --- ExecutionEngine 그림자 배선(2026-08-17) --------------------------------------------------
#
# RiskEngine을 07-28에 그림자로 먼저 붙인 것과 같은 방식이다 — 주문은 내지 않고 "지금 실행
# 게이트를 거쳤다면 어떻게 됐을지"를 남긴다. 그 전례가 08-06 컷오프 결함을 사후 추적이 아니라
# **기록으로** 잡게 해 줬다.


def _run_fusion_cycle_capturing_risk_gate(monkeypatch, chain_rows=None, **kwargs):
    rows = _selection_chain_rows() if chain_rows is None else chain_rows
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kw: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.entry_strategies_used_today", lambda conn, day: frozenset())
    monkeypatch.setattr(
        "mahdi.main.db.latest_expiry_liquidity",
        lambda conn, underlying: [{"series": "regular", "expiry": date(2026, 8, 18)}],
    )
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: _ACCOUNT_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.account_balance_snapshot_before",
                        lambda conn, before: _BASELINE_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.max_account_balance_ever", lambda conn: 110.0)
    monkeypatch.setattr("mahdi.main.db.daily_trade_counts_by_strategy", lambda conn, day: {})
    for name, value in kwargs.items():
        monkeypatch.setattr(f"mahdi.main.{name}", value)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    captured: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: captured.append((risk_gate_state, exec_mode)),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(
            _FakeRegimeStateMachineWithLastState(_trend_up_regime()), interval_seconds=60
        ))
    assert len(captured) == 1
    return captured[0]


def test_the_execution_facade_is_evaluated_in_the_shadow_and_submits_nothing(monkeypatch):
    risk_gate_state, exec_mode = _run_fusion_cycle_capturing_risk_gate(monkeypatch)
    shadow = risk_gate_state["execution_engine"]
    assert shadow["evaluated"] is True
    # 이 파일에 order_manager.submit() 호출부가 없다는 사실을 값으로 남긴다 —
    # 「승인됐다」를 「주문했다」로 읽지 않게.
    assert shadow["order_submitted"] is False
    assert shadow["mode"] == "ADVISORY"
    assert exec_mode == "ADVISORY"


def test_the_shadow_carries_the_selected_symbol_so_the_two_layers_can_be_compared(monkeypatch):
    risk_gate_state, _ = _run_fusion_cycle_capturing_risk_gate(monkeypatch)
    shadow = risk_gate_state["execution_engine"]
    # 마스터가 없으므로 단축코드는 못 찾는다 — 그 사실이 값으로 드러나야 한다.
    assert shadow["symbol_resolved"] is False
    # ADVISORY라 계획 자체를 안 만든다(모드 게이트) — 가격 유무와 무관하다.
    assert shadow["entry_plan"] is None


def test_the_shadow_carries_the_real_premium_as_the_reference_price(monkeypatch):
    """2026-08-18 마이그레이션 032 — 가격이 없어 막혀 있던 자리가 풀렸다."""
    risk_gate_state, _ = _run_fusion_cycle_capturing_risk_gate(monkeypatch)
    shadow = risk_gate_state["execution_engine"]

    assert shadow["reference_price"] == 7.25
    # 가격이 있으면 «막혔다»가 비어야 한다 — 이 키가 채워져 있으면 그 분은 지정가를 못 만든다.
    assert shadow["entry_plan_blocked_by"] is None


def test_a_minute_without_a_premium_says_so_instead_of_pricing_at_zero(monkeypatch):
    """0.0을 기준가로 쓰면 그 순간부터 기록이 허구가 되고, 모드를 올린 날 그 허구가 주문이 된다."""
    rows = [{**r, "price": None} for r in _selection_chain_rows()]
    risk_gate_state, _ = _run_fusion_cycle_capturing_risk_gate(monkeypatch, chain_rows=rows)
    shadow = risk_gate_state["execution_engine"]

    assert shadow["reference_price"] is None
    assert shadow["entry_plan_blocked_by"] == "option_price_missing_this_minute"


def test_a_configured_auto_mode_is_still_recorded_as_advisory_while_unwired(monkeypatch):
    """설정을 FULL_AUTO로 올려도 주문 경로가 없으면 기록은 실제(ADVISORY)를 말해야 한다."""
    from mahdi.config.settings import get_strategy_params

    monkeypatch.setattr(
        "mahdi.main.get_strategy_params",
        lambda: {**get_strategy_params(), "hybrid_mode": {"default": "FULL_AUTO"}},
    )
    risk_gate_state, exec_mode = _run_fusion_cycle_capturing_risk_gate(monkeypatch)
    assert exec_mode == "ADVISORY"
    assert risk_gate_state["execution_engine"]["configured_mode"] == "FULL_AUTO"
    assert risk_gate_state["execution_engine"]["mode"] == "ADVISORY"


def test_poll_signal_fusion_cycle_continues_after_cycle_failure(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        raise RuntimeError("DB 연결 실패")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    regime_state_machine = _FakeRegimeStateMachineWithLastState(None)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=1))

    assert len(sleep_calls) == 1  # DB 실패해도 로깅만 하고 다음 사이클 대기로 넘어감


# --- 계좌 손익/포지션 추적기 라이브 배선(2026-07-28 8차) --------------------------------------


_ACCOUNT_SNAPSHOT_ROW = {
    "timestamp": None, "prsm_dpast": 110.0, "evlu_pfls_amt_smtl": 10.0, "trad_pfls_amt_smtl": 0.0,
    "dnca_cash": 100.0, "ord_psbl_cash": 90.0, "mgna_tota": 5.0,
    "same_direction_buy_count": 0, "same_direction_sell_count": 0,
}
_BASELINE_SNAPSHOT_ROW = {**_ACCOUNT_SNAPSHOT_ROW, "prsm_dpast": 100.0}


def test_build_account_state_for_candidate_none_when_tracker_not_ready(monkeypatch):
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: None)
    result = _build_account_state_for_candidate(
        conn=object(), candidate_side="BUY", poll_time=datetime(2026, 7, 28, 10, 0)
    )
    assert result is None


def test_build_account_state_for_candidate_computes_state_from_snapshots(monkeypatch):
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: _ACCOUNT_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.account_balance_snapshot_before", lambda conn, before: _BASELINE_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.max_account_balance_ever", lambda conn: 110.0)
    monkeypatch.setattr("mahdi.main.db.daily_trade_counts_by_strategy", lambda conn, day: {})

    state = _build_account_state_for_candidate(
        conn=object(), candidate_side="BUY", poll_time=datetime(2026, 7, 28, 10, 0)
    )

    assert state is not None
    assert state.daily_pnl_pct == pytest.approx(0.10)
    assert state.drawdown_pct == pytest.approx(0.0)
    assert state.same_direction_positions == 0


def test_poll_signal_fusion_cycle_calls_risk_engine_when_account_tracker_ready(monkeypatch):
    chain_rows = [
        {"strike": 95.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: _ACCOUNT_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.account_balance_snapshot_before", lambda conn, before: _BASELINE_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.max_account_balance_ever", lambda conn: 110.0)
    monkeypatch.setattr("mahdi.main.db.daily_trade_counts_by_strategy", lambda conn, day: {})
    # 2026-07-29: is_entry 경로에서 RiskEngine.evaluate_entry에 market_halted를 넘기려고
    # latest_market_halt_state를 조회한다 — 이 테스트는 정상(halt 이력 없음) 케이스만 검증한다.
    monkeypatch.setattr("mahdi.main.db.latest_market_halt_state", lambda conn: None)
    # 2026-08-06 Fix#1 — 컷오프 이전 시각 고정(안 그러면 오후에 돌릴 때 REJECT가 된다).
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 10, 0))
    # 2026-08-11 고도화 D — 쿨다운 입력. 호출측이 try/except로 감싸지만, 스텁을 두는 쪽이
    # 빠르고 "이 폴러가 무엇을 조회하는가"를 이 헬퍼 한 곳에서 읽을 수 있게 한다.
    monkeypatch.setattr("mahdi.main.db.minutes_since_last_entry_by_strategy", lambda conn, now: {})

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (decision, risk_gate_state)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    regime_state = RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector))
    regime_state_machine = _FakeRegimeStateMachineWithLastState(regime_state)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=60))

    assert len(recorded) == 1
    decision, risk_gate_state = recorded[0]
    assert decision == "ENTER"
    assert risk_gate_state["risk_engine"]["approved"] is True
    assert risk_gate_state["risk_engine"]["approved_size"] > 0


# ===== 2026-08-06 §2-2 / Fix#1 — v6 §4.2 신규 진입 컷오프의 라이브 배선 =====
#
# 위 테스트와 **입력이 완전히 같고 시각만 다르다.** 그것이 이 게이트가 하는 일의 전부다.


def _run_signal_fusion_at(monkeypatch, now: datetime) -> list[tuple]:
    """진입 후보가 확실히 나오는 입력으로 한 사이클을 돌려 기록된 판단을 돌려준다."""
    chain_rows = [
        {"strike": 95.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: _ACCOUNT_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.account_balance_snapshot_before", lambda conn, before: _BASELINE_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.max_account_balance_ever", lambda conn: 110.0)
    monkeypatch.setattr("mahdi.main.db.daily_trade_counts_by_strategy", lambda conn, day: {})
    monkeypatch.setattr("mahdi.main.db.latest_market_halt_state", lambda conn: None)
    monkeypatch.setattr("mahdi.main.db.insert_risk_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: now)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (decision, reject_reason, risk_gate_state)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    regime_state = RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector))
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(
            _FakeRegimeStateMachineWithLastState(regime_state), interval_seconds=60
        ))
    return recorded


def test_poll_signal_fusion_cycle_blocks_entry_after_the_cutoff(monkeypatch):
    """08-06 15:30에 실제로 기록된 ENTER가 이제 `entry_cutoff` REJECT가 된다.

    `decision` 컬럼까지 바뀌어야 하는 이유: 그 컬럼은 팔레트 결과만 보고 정해지고 리스크
    엔진 결과는 `risk_gate_state`에만 들어간다 — 08-06 ENTER 62건은 전부
    `risk_engine.approved=true`였다. 엔진에만 게이트를 두면 표가 안 바뀐다.
    """
    recorded = _run_signal_fusion_at(monkeypatch, datetime(2026, 8, 6, 15, 30))
    assert len(recorded) == 1
    decision, reject_reason, _ = recorded[0]
    assert decision == "REJECT"
    assert reject_reason == "entry_cutoff"


def test_poll_signal_fusion_cycle_allows_entry_just_before_the_cutoff(monkeypatch):
    """같은 입력, 14:49 — 컷오프는 시각 말고 아무것도 바꾸지 않는다."""
    recorded = _run_signal_fusion_at(monkeypatch, datetime(2026, 8, 6, 14, 49))
    assert len(recorded) == 1
    decision, reject_reason, _ = recorded[0]
    assert decision == "ENTER"
    assert reject_reason is None


def test_entry_cutoff_does_not_overwrite_an_existing_reject_reason(monkeypatch):
    """이미 거부된 판단의 사유는 덮지 않는다.

    덮으면 `entry_cutoff` 건수가 *"진입할 뻔했는데 막힌 분"* 이 아니라 *"컷오프 이후의 모든 분"*
    이 되어, 팔레트 상태에 따라 요동치는 숫자가 된다 — 그러면 검정할 수 없다.
    """
    # 진입 후보가 없는 입력(체인 없음) — 컷오프 이후여도 사유는 팔레트/메타라벨 쪽이어야 한다.
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 15, 30))
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: [])
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: None)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (decision, reject_reason)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(
            _FakeRegimeStateMachineWithLastState(None), interval_seconds=60
        ))

    assert len(recorded) == 1
    assert recorded[0][0] == "REJECT"
    assert recorded[0][1] != "entry_cutoff"  # 후보가 없던 쪽은 원래 사유 그대로


def test_risk_engine_also_receives_the_decision_time(monkeypatch):
    """이중 방어 — 판단 층이 이미 막았어도 엔진 호출에 `now`가 실려야 한다.

    비어 있으면 Phase 2에서 실행 엔진이 이 호출을 복사해 갈 때 시각 게이트가 조용히 빠진다.
    """
    seen: dict = {}
    real_evaluate = RiskEngine.evaluate_entry

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        return real_evaluate(self, *args, **kwargs)

    monkeypatch.setattr(RiskEngine, "evaluate_entry", spy)
    _run_signal_fusion_at(monkeypatch, datetime(2026, 8, 6, 10, 0))
    assert seen.get("now") == datetime(2026, 8, 6, 10, 0)


def test_poll_signal_fusion_cycle_rejects_entry_when_market_halted(monkeypatch):
    # 2026-08-06 Fix#1 — 컷오프 이전 시각으로 고정(위 헬퍼와 같은 이유. 이 테스트는 halt 쪽을
    # 보려고 헬퍼를 안 쓰고 직접 스텁을 깐다).
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 10, 0))
    # 2026-08-11 고도화 D — 쿨다운 입력. 호출측이 try/except로 감싸지만, 스텁을 두는 쪽이
    # 빠르고 "이 폴러가 무엇을 조회하는가"를 이 헬퍼 한 곳에서 읽을 수 있게 한다.
    monkeypatch.setattr("mahdi.main.db.minutes_since_last_entry_by_strategy", lambda conn, now: {})
    chain_rows = [
        {"strike": 95.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: _ACCOUNT_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.account_balance_snapshot_before", lambda conn, before: _BASELINE_SNAPSHOT_ROW)
    monkeypatch.setattr("mahdi.main.db.max_account_balance_ever", lambda conn: 110.0)
    monkeypatch.setattr("mahdi.main.db.daily_trade_counts_by_strategy", lambda conn, day: {})
    monkeypatch.setattr(
        "mahdi.main.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 29, 9, 5, 0), "is_halted": True,
            "mkop_cls_code": "174", "label": "서킷브레이크 발동", "halted_since": datetime(2026, 7, 29, 9, 5, 0),
        },
    )

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (decision, risk_gate_state)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    regime_state = RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector))
    regime_state_machine = _FakeRegimeStateMachineWithLastState(regime_state)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=60))

    assert len(recorded) == 1
    decision, risk_gate_state = recorded[0]
    assert decision == "ENTER"  # Signal Fusion 판단 자체는 그대로(진입 후보였음)
    assert risk_gate_state["risk_engine"]["approved"] is False
    assert risk_gate_state["risk_engine"]["reject_reasons"] == ["market_halt"]


def test_poll_signal_fusion_cycle_marks_account_tracker_not_ready(monkeypatch):
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    chain_rows = [
        {"strike": 95.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.latest_account_balance_snapshot", lambda conn: None)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (decision, risk_gate_state)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    regime_state = RegimeState(regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector))
    regime_state_machine = _FakeRegimeStateMachineWithLastState(regime_state)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=60))

    assert recorded[0][1]["risk_engine"] == "account_tracker_not_ready"


class _FakeBalanceRestClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0

    def get_balance(self):
        self.calls += 1
        return self.response


_SAMPLE_BALANCE_RESPONSE = {
    "rt_cd": "0",
    "output1": [],
    "output2": {"prsm_dpast": "50000000", "evlu_pfls_amt_smtl": "0", "trad_pfls_amt_smtl": "0",
                "dnca_cash": "50000000", "ord_psbl_cash": "50000000", "mgna_tota": "0"},
}


def test_poll_account_balance_cycle_records_snapshot_each_cycle(monkeypatch):
    rest_client = _FakeBalanceRestClient(_SAMPLE_BALANCE_RESPONSE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[dict] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_account_balance_snapshot", lambda conn, row: recorded.append(row)
    )

    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        if len(recorded) >= 2:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_account_balance_cycle(rest_client, interval_seconds=60))

    assert len(recorded) == 2
    assert recorded[0]["prsm_dpast"] == 50000000.0
    assert rest_client.calls == 2


def test_poll_account_balance_cycle_also_persists_the_position_detail(monkeypatch):
    """2026-08-16 (Block B) — 잔고 합계와 **종목별 보유 상세**를 같은 사이클에 함께 남긴다.

    배선이 빠지면 `position_snapshots`가 영원히 비고, 자동 리포트 §16-1은 매일 「행 0」을
    인쇄한다 — 그 0은 「체결이 없었다」와 구별되지 않으므로 아무도 이상을 못 느낀다.
    같은 종류의 실패가 08-04에 있었다(`ofi=None` 하드코딩: 데이터는 있는데 안 읽었다).
    """
    response = {
        "rt_cd": "0",
        "output1": [
            {"shtn_pdno": "101S03", "sll_buy_dvsn_name": "BUY", "cblc_qty": "1",
             "ccld_avg_unpr1": "352.10", "lqd_psbl_qty": "1"},
        ],
        "output2": dict(_SAMPLE_BALANCE_RESPONSE["output2"]),
    }
    rest_client = _FakeBalanceRestClient(response)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    balances: list[dict] = []
    positions: list[list[dict]] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_account_balance_snapshot", lambda conn, row: balances.append(row)
    )
    monkeypatch.setattr(
        "mahdi.main.db.insert_position_snapshots", lambda conn, rows: positions.append(rows) or len(rows)
    )

    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_account_balance_cycle(rest_client, interval_seconds=60))

    assert len(positions) == 1
    (row,) = positions[0]
    assert row["symbol"] == "101S03" and row["side"] == "BUY"
    assert row["raw"]["ccld_avg_unpr1"] == "352.10"  # 원본 보존(R8)
    # **두 표가 같은 시각을 갖는다** — 갈리면 §16-1이 「두 축이 갈렸다」를 오탐한다.
    assert row["timestamp"] == balances[0]["timestamp"]


def test_poll_account_balance_cycle_writes_no_position_rows_for_a_flat_account(monkeypatch):
    """포지션이 없으면 빈 행을 지어내지 않는다 — 개시 전에는 이것이 정상 경로다."""
    rest_client = _FakeBalanceRestClient(_SAMPLE_BALANCE_RESPONSE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_account_balance_snapshot", lambda conn, row: None)

    calls: list[list[dict]] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_position_snapshots", lambda conn, rows: calls.append(rows) or 0
    )

    fake_loop = _FakeLoop([1000.0, 1200.0])
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_account_balance_cycle(rest_client, interval_seconds=60))

    assert calls == [[]]


def test_poll_account_balance_cycle_continues_after_failure(monkeypatch):
    class _FailingRestClient:
        def get_balance(self):
            raise RuntimeError("KIS 500")

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_account_balance_cycle(_FailingRestClient(), interval_seconds=1))

    assert len(sleep_calls) == 1  # 실패해도 로깅만 하고 다음 사이클 대기로 넘어감


# ===== 2026-07-30 운영점검 Fix#3: 스케줄 밀림 시 위상 격자 스냅 =====


def test_grid_poll_minute_lifts_a_cycle_that_woke_just_before_the_boundary():
    """2026-08-07 §A-1 / Fix#3 — 격자점 직전에 깬 사이클이 직전 분에 적재되던 것.

    08-07 실측: option_analysis_1m의 07:30이 20행, **07:31이 0행**, 07:32가 20행이었다.
    로그 축은 결손 0분(07:31:19에 rows=20으로 완주)이라 두 축이 어긋났다.
    """
    from mahdi.main import _grid_poll_minute

    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 30, 59, 900_000)) == datetime(2026, 8, 7, 7, 31)
    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 30, 58, 100_000)) == datetime(2026, 8, 7, 7, 31)


def test_grid_poll_minute_floors_everything_outside_the_snap_window():
    """스냅 폭 밖은 그대로 내려깎는다 — 늦게 시작한 사이클을 **없는 미래**로 밀면 안 된다."""
    from mahdi.main import _grid_poll_minute

    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 31, 0)) == datetime(2026, 8, 7, 7, 31)
    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 31, 0, 500)) == datetime(2026, 8, 7, 7, 31)
    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 31, 30)) == datetime(2026, 8, 7, 7, 31)
    # 경계 정확히 2.0초 전은 스냅 대상(<=), 2.1초 전은 아니다.
    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 31, 58)) == datetime(2026, 8, 7, 7, 32)
    assert _grid_poll_minute(datetime(2026, 8, 7, 7, 31, 57, 900_000)) == datetime(2026, 8, 7, 7, 31)


def test_grid_poll_minute_snap_window_is_far_below_the_poll_interval():
    """스냅 폭이 폴링 주기에 가까워지면 사이클의 분 라벨이 통째로 한 칸 밀린다."""
    from mahdi.main import OPTION_CHAIN_POLL_INTERVAL_SECONDS, POLL_TIME_BOUNDARY_SNAP_SECONDS

    assert 0 < POLL_TIME_BOUNDARY_SNAP_SECONDS <= OPTION_CHAIN_POLL_INTERVAL_SECONDS / 10


def test_advance_fixed_tick_first_cycle_schedules_one_full_interval():
    # 2026-07-31 §4 우선순위 5: 첫 틱은 "지금 + 주기"가 아니라 **벽시계 격자의 다음 지점**이다.
    # 정확히 격자 위(09:00:00, 위상 0초·주기 60초)면 이중 실행을 피해 한 주기 뒤를 잡는다.
    next_tick, delay, overrun = _advance_fixed_tick(
        None, 60.0, 1000.0, 0.0, wall_now=datetime(2026, 7, 31, 9, 0, 0)
    )
    assert (next_tick, delay, overrun) == (1060.0, 60.0, 0.0)


def test_advance_fixed_tick_first_cycle_anchors_to_wall_clock_grid_not_now():
    # 09:00:20에 첫 사이클이 끝났으면 다음 틱은 "지금+60초"(09:01:20)가 아니라 격자 위의 09:01:00.
    # 이 성질이 없으면 구독 워밍업으로 next_tick이 리셋될 때마다 위상이 임의로 옮겨간다
    # (2026-07-31 §2-3에서 실측된 설계 오프셋 35/155/275초 → 실측 95/198/305초의 원인).
    next_tick, delay, overrun = _advance_fixed_tick(
        None, 60.0, 1000.0, 0.0, wall_now=datetime(2026, 7, 31, 9, 0, 20)
    )
    assert (next_tick, delay, overrun) == (1040.0, 40.0, 0.0)


@pytest.mark.parametrize(
    "wall, interval, phase, expected",
    [
        # 위상 15초·주기 60초 → 격자는 매 분 15초
        (datetime(2026, 7, 31, 9, 0, 0), 60.0, 15.0, 15.0),
        (datetime(2026, 7, 31, 9, 0, 20), 60.0, 15.0, 55.0),
        # 매크로: 위상 168초(2분 48초)·주기 300초 → 09:02:48, 09:07:48, ...
        (datetime(2026, 7, 31, 9, 0, 0), 300.0, 168.0, 168.0),
        (datetime(2026, 7, 31, 9, 2, 48), 300.0, 168.0, 300.0),
        (datetime(2026, 7, 31, 9, 3, 48), 300.0, 168.0, 240.0),
        # 계좌잔고: 위상 288초(4분 48초)·주기 300초 → 09:04:48, 09:09:48, ...
        (datetime(2026, 7, 31, 9, 5, 0), 300.0, 288.0, 288.0),
    ],
)
def test_seconds_until_next_wall_tick_is_anchored_to_midnight(wall, interval, phase, expected):
    assert mahdi_main._seconds_until_next_wall_tick(interval, phase, wall) == pytest.approx(expected)


def test_seconds_until_next_wall_tick_is_always_positive_and_within_one_interval():
    # 어떤 시각에서 출발하든 (0, interval] 안에 들어와야 한다 — 0이면 방금 끝난 사이클과 같은 분에
    # 곧바로 한 번 더 도는 이중 실행이 되고, interval을 넘으면 격자를 건너뛴다.
    for second in range(0, 3600, 7):
        wall = datetime(2026, 7, 31, 9, 0, 0) + timedelta(seconds=second)
        for interval, phase in ((60.0, 0.0), (60.0, 15.0), (300.0, 168.0), (300.0, 288.0)):
            wait = mahdi_main._seconds_until_next_wall_tick(interval, phase, wall)
            assert 0 < wait <= interval


def _poller_for_wall_slot_test(name, rest_client_holder):
    """벽시계 정렬 테스트용 폴러 팩토리 — 폴러마다 필요한 가짜 의존성이 달라 여기서 묶는다."""
    strikes = frozenset({1330.0, 1332.5, 1335.0, 1337.5, 1340.0})
    if name == "option_chain":
        rest_client_holder.append(_FakeRestClientChain(_SAMPLE_OPTION_QUOTE))
        return poll_option_chain(
            rest_client_holder[0], [(_FakeSubscriptionManagerWithStrikes(), "regular")],
            _FakeMaster(), interval_seconds=60, phase_offset_seconds=0.0,
        )
    if name == "expiry_liquidity":
        rest_client_holder.append(_FakeRestClientForLiquidity(_SAMPLE_OPTION_QUOTE, _SAMPLE_ASKING_PRICE))
        return poll_expiry_liquidity(
            rest_client_holder[0], [(_FakeSubscriptionManagerForLiquidity(strikes), "regular")],
            _FakeMasterForLiquidity(), interval_seconds=60, phase_offset_seconds=15.0,
        )
    if name == "investor_flow":
        rest_client_holder.append(_FakeInvestorFlowRestClient({}))
        return poll_investor_flow(rest_client_holder[0], interval_seconds=60, phase_offset_seconds=40.0)
    if name == "macro_snapshot":
        rest_client_holder.append(_FakeOverseasRestClient(future_prices={}))
        return poll_macro_snapshot(
            rest_client_holder[0], _FakeOverseasFutureMaster({}),
            interval_seconds=300, phase_offset_seconds=168.0,
        )
    if name == "account_balance":
        rest_client_holder.append(_FakeBalanceRestClient(_SAMPLE_BALANCE_RESPONSE))
        return poll_account_balance_cycle(
            rest_client_holder[0], interval_seconds=300, phase_offset_seconds=288.0
        )
    if name == "signal_fusion":
        rest_client_holder.append(None)
        return poll_signal_fusion_cycle(
            _FakeRegimeStateMachineWithLastState(None), interval_seconds=60, phase_offset_seconds=10.0
        )
    raise AssertionError(name)


@pytest.mark.parametrize(
    "poller, interval, phase, expected_wait",
    [
        # 벽시계를 09:00:00으로 고정했을 때, 각 폴러가 자기 격자의 첫 지점까지 기다려야 하는 초.
        ("option_chain", 60.0, 0.0, 60.0),  # 정확히 격자 위 → 이중 실행 회피로 한 주기 뒤
        ("expiry_liquidity", 60.0, 15.0, 15.0),
        ("investor_flow", 60.0, 40.0, 40.0),
        ("macro_snapshot", 300.0, 168.0, 168.0),
        ("account_balance", 300.0, 288.0, 288.0),
        ("signal_fusion", 60.0, 10.0, 10.0),
    ],
)
def test_every_poller_waits_for_its_wall_clock_slot_before_first_cycle(
    monkeypatch, wall_tick_alignment_enabled, poller, interval, phase, expected_wait
):
    # 2026-07-31(§2-3/§4 우선순위 5): 종전 `sleep(startup_offset_seconds)`는 격자 원점을 **기동
    # 시각**에 못박아, 설계 오프셋 35/155/275초가 실측 95/198/305초로 어긋나 있었다(매일 다름).
    # 이제 모든 폴러는 벽시계 자정 기준 격자의 첫 지점까지 기다린 뒤 첫 사이클에 들어간다.
    # (이 테스트만 conftest의 정렬 무력화를 opt-out 한다 — `wall_tick_alignment_enabled` 픽스처)
    _pin_wall_clock(monkeypatch, datetime(2026, 7, 31, 9, 0, 0))

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    holder: list = []
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(_poller_for_wall_slot_test(poller, holder))

    assert sleep_calls == [expected_wait]  # 첫 대기가 곧 격자 정렬 대기 — 사이클 진입 전이다


def test_advance_fixed_tick_normal_cycle_keeps_absolute_grid():
    # 사이클이 10초 걸렸어도 다음 틱은 1060 그대로 — 절대시각 고정 틱(2026-07-09 도입)의 핵심.
    next_tick, delay, overrun = _advance_fixed_tick(1000.0, 60.0, 1010.0)
    assert (next_tick, delay, overrun) == (1060.0, 50.0, 0.0)


def test_advance_fixed_tick_overrun_snaps_to_grid_instead_of_rebasing_to_now():
    # 예정 틱 1060을 140초 지나쳐 끝남 → 종전에는 next_tick=1200(현재 시각)으로 위상을 옮겼다.
    # 이제는 격자를 유지해 1060 + 60*3 = 1240으로 스냅하고 40초 대기한다.
    next_tick, delay, overrun = _advance_fixed_tick(1000.0, 60.0, 1200.0)
    assert next_tick == 1240.0
    assert delay == 40.0
    assert overrun == 140.0


def test_advance_fixed_tick_keeps_phase_stable_across_many_overruns():
    # 위상 고착 회귀 방지: 몇 번을 밀리든 next_tick은 항상 원래 격자(1000 + 60k) 위에 있어야 한다.
    next_tick = 1000.0
    for elapsed in (1201.0, 1337.0, 1400.0, 1999.0):
        next_tick, _delay, _overrun = _advance_fixed_tick(next_tick, 60.0, elapsed)
        assert (next_tick - 1000.0) % 60.0 == pytest.approx(0.0)
        assert next_tick > elapsed


def test_advance_fixed_tick_exactly_on_time_does_not_count_as_overrun():
    next_tick, delay, overrun = _advance_fixed_tick(1000.0, 60.0, 1060.0)
    assert (next_tick, delay, overrun) == (1060.0, 0.0, 0.0)


# ===== 2026-07-30 운영점검 Fix#1: 관망 전용 팔레트는 ENTER가 아니다 =====


def test_poll_signal_fusion_cycle_rejects_when_palette_only_says_wait(monkeypatch):
    # 07-30에 419건 연속 ENTER를 만든 조합(RANGE_BALANCED + VRP 적정 → ["wait_and_see"])을
    # 그대로 재현해, 이제는 REJECT + reject_reason="strategy_palette:wait_only"가 되는지 검증한다.
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    # 행사가 105 = 스팟(100) **위** — 2026-08-04 Fix#3으로 감마 월이 기준선이 되면서
    # `options_flow`가 이 사이클에도 살아난다(종전에는 gamma_flip이 없어 항상 None이었다).
    # 스팟이 월 아래이고 GEX가 양수면 회귀 방향 = 강세라, 외국인 순매수(+500)와 **부호가 같다**.
    # 행사가를 95로 두면 두 멤버가 반대 부호가 되어 `conflict_resolution:no_clear_consensus`로
    # 먼저 걸리고, 이 테스트가 겨누는 팔레트 경로에 도달하지 못한다 — 그것도 정상 동작이지만
    # 여기서 검증하려는 것은 **팔레트가 관망만 줄 때** 무엇이 기록되는가다.
    chain_rows = [
        {"strike": 105.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 0.0, "expiry": date(2026, 8, 13)},
    ]
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: 100.0)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    recorded: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(
            (decision, reject_reason, risk_gate_state)
        ),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.RANGE_BALANCED] = 1.0
    regime_state = RegimeState(regime=RegimeLabel.RANGE_BALANCED, prob_vector=tuple(prob_vector))
    regime_state_machine = _FakeRegimeStateMachineWithLastState(regime_state)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(regime_state_machine, interval_seconds=60))

    decision, reject_reason, risk_gate_state = recorded[0]
    assert decision == "REJECT"
    assert reject_reason == "strategy_palette:wait_only"
    # 팔레트 원문은 그대로 남고(COCKPIT이 "관망 중"을 표시할 수 있어야 함), 진입 대상만 빈 목록.
    assert risk_gate_state["allowed_strategies"] == ["wait_and_see"]
    assert risk_gate_state["entry_strategies"] == []
    assert "risk_engine" not in risk_gate_state  # 진입이 아니므로 RiskEngine 호출 자체가 없어야 한다


# ===== 2026-07-30 운영점검 Fix#6: risk_snapshots 매 사이클 적재 =====


def test_poll_signal_fusion_cycle_records_risk_snapshot_even_when_rejecting(monkeypatch):
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: [])
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: None)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_signal_decision", lambda *a, **k: None)

    snapshots: list[tuple] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_risk_snapshot",
        lambda conn, ts, greeks, loss_buffer, cb_state: snapshots.append((ts, greeks, loss_buffer, cb_state)),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))

    assert len(snapshots) == 1  # REJECT 사이클에도 남아야 한다(07-30엔 하루 419건 평가에 0행이었음)
    _ts, greeks, loss_buffer, cb_state = snapshots[0]
    # 보유 포트폴리오 그릭스는 아직 개념 자체가 없다 — 지어내지 않고 명시해야 한다.
    assert greeks["scope"] == "market"
    assert greeks["portfolio"] is None
    assert cb_state["decision"] == "REJECT"
    assert cb_state["market_halted"] is False
    assert cb_state["account_tracker_ready"] is False
    assert loss_buffer is None  # 계좌 스냅샷이 없으면 손실 여유를 계산하지 않는다


def test_poll_signal_fusion_cycle_risk_snapshot_failure_does_not_break_cycle(monkeypatch):
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: [])
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: None)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    decisions: list = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, **kwargs: decisions.append(kwargs["decision"]),
    )

    def boom(*args, **kwargs):
        raise RuntimeError("risk_snapshots 쓰기 실패")

    monkeypatch.setattr("mahdi.main.db.insert_risk_snapshot", boom)

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))

    # 스냅샷 기록이 실패해도 판단 자체(signal_decisions)는 이미 남았고 루프는 정상 진행해야 한다.
    assert decisions == ["REJECT"]


# ===== 2026-07-30 운영점검 Fix#4: CB 감지 생존 계측 =====


def _run_observation_loop_with_market_operation_messages(monkeypatch, incoming_raws: list[str]) -> list[tuple]:
    """H0UNMKO0 메시지를 흘려보내고 upsert_market_halt_state 호출 인자를 순서대로 돌려준다."""
    conn = FakeConnection(incoming_raws)
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    subscription_manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    rest_client = FakeRestClient(spot=350.0)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    halt_writes: list[tuple] = []
    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", lambda conn, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_market_halt_event_history", lambda *a, **k: None)
    monkeypatch.setattr(
        "mahdi.main.db.upsert_market_halt_state",
        lambda conn, updated_at, is_halted, code, label, halted_since: halt_writes.append(
            (is_halted, code, label)
        ),
    )

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )
    return halt_writes


def test_run_observation_loop_records_market_halt_baseline_row_on_subscribe(monkeypatch):
    # 07-30 하루 전체에서 market_halt_status가 0행이라 "CB가 없었다"와 "감지기가 죽었다"를
    # 구분할 수 없었다 — 구독 직후 현재 상태("정상")를 반드시 한 번 남겨야 한다.
    halt_writes = _run_observation_loop_with_market_operation_messages(monkeypatch, [])

    assert halt_writes  # 메시지가 하나도 없어도 기준행은 남는다
    is_halted, code, label = halt_writes[0]
    assert is_halted is False
    assert code is None
    assert label == "정상"


def test_market_operation_message_without_transition_only_touches_last_message_at(monkeypatch):
    # 2026-07-31(§2-2/§4 우선순위 4): 차단 여부가 안 바뀌는 코드(정상 세션 전환/VI 등)는
    # **상태 행을 건드리지 않고** "방금 수신했다"는 사실(last_message_at)만 남긴다 —
    # updated_at은 이제 독립 하트비트(poll_market_halt_heartbeat)의 몫이라, 여기서 같이 갱신하면
    # "감지기 생존"과 "메시지 수신"이 다시 한 값으로 뭉개진다.
    seen_calls: list[datetime] = []
    monkeypatch.setattr("mahdi.main.db.mark_market_halt_message_seen", lambda conn, at: seen_calls.append(at))

    halt_writes = _run_observation_loop_with_market_operation_messages(
        monkeypatch, [_make_h0unmko0("11", with_ws_envelope=True)]
    )

    assert len(halt_writes) == 1  # 구독 직후 기준행뿐 — 전이가 없으므로 상태 행 갱신 없음
    assert len(seen_calls) == 1  # 수신 시각만 기록


def test_market_operation_message_seen_write_is_throttled(monkeypatch):
    # 매 수신마다 DB에 쓰면 안 된다 — 스로틀 창(MARKET_HALT_MESSAGE_TOUCH_SECONDS) 안에
    # 연달아 온 메시지는 한 번만 기록한다.
    seen_calls: list[datetime] = []
    monkeypatch.setattr("mahdi.main.db.mark_market_halt_message_seen", lambda conn, at: seen_calls.append(at))

    _run_observation_loop_with_market_operation_messages(
        monkeypatch,
        [
            _make_h0unmko0("11", with_ws_envelope=True),
            _make_h0unmko0("11", with_ws_envelope=True),
            _make_h0unmko0("11", with_ws_envelope=True),
        ],
    )

    assert len(seen_calls) == 1  # 나머지 2건은 스로틀 창 안이라 생략


def test_market_halt_heartbeat_writes_state_without_any_message(monkeypatch):
    # 핵심 회귀 방지(§2-2): H0UNMKO0은 세션 전이 시에만 온다(07-31 실측 하루 2건). 메시지가
    # 한 건도 없어도 하트비트는 계속 돌아야 하며, 그 값이 "관측 루프가 살아있다"의 유일한 증거다.
    writes: list[tuple] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr(
        "mahdi.main.db.upsert_market_halt_state",
        lambda conn, at, is_halted, code, label, since: writes.append((is_halted, code, label)),
    )

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(mahdi_main.poll_market_halt_heartbeat(MarketHaltMonitor(), interval_seconds=300.0))

    assert writes == [(False, None, "정상"), (False, None, "정상")]
    assert sleep_calls == [300.0, 300.0]


def test_market_halt_heartbeat_survives_db_failure(monkeypatch):
    # 하트비트가 죽어도 CB 감지 자체(WS 핸들러)는 계속 동작해야 하므로, DB 실패는 삼키고 다음 주기로.
    @contextmanager
    def fake_get_connection(settings=None):
        raise RuntimeError("DB 연결 실패")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(mahdi_main.poll_market_halt_heartbeat(MarketHaltMonitor(), interval_seconds=300.0))

    assert sleep_calls == [300.0]  # 예외가 위로 새지 않고 다음 주기를 기다린다


def test_market_operation_halt_transition_still_records_and_wins_over_heartbeat(monkeypatch):
    # 생존 계측을 붙였다고 원래 목적(전이 기록)이 흐려지면 안 된다.
    halt_writes = _run_observation_loop_with_market_operation_messages(
        monkeypatch,
        [
            _make_h0unmko0("174", with_ws_envelope=True),  # 서킷브레이크 발동
            _make_h0unmko0("175", with_ws_envelope=True),  # 해제
        ],
    )

    assert halt_writes[0] == (False, None, "정상")  # 기준행
    assert halt_writes[1] == (True, "174", "서킷브레이크 발동")
    assert halt_writes[2] == (False, "175", "서킷브레이크 해제")


# ===== 2026-07-31 운영점검 §4 우선순위 2: 밀린 분 먼슬리 전용 캐치업 =====


def _catchup_harness(monkeypatch, *, loop_times, overran_at, now_at, inserted):
    """밀림 1회를 재현하는 공통 배선 — 반환값은 sleep 호출 목록."""
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr(
        "mahdi.main.db.insert_option_analysis_1m", lambda conn, row: inserted.append(row)
    )
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)

    # db.local_now() 호출 순서: ①사이클1 poll_time ②사이클1 _advance_fixed_tick의 첫 틱 앵커
    # ③사이클2(밀리는 사이클) poll_time ④캐치업의 "지금 몇 분인가" 판정 — ④부터 다음 분이다.
    now_values = itertools.chain([overran_at] * 3, itertools.repeat(now_at))
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: next(now_values))
    fake_loop = _FakeLoop(loop_times)  # 인스턴스 1개를 공유해야 시각 시퀀스가 소비된다
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: fake_loop)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:  # 사이클 1의 정상 대기는 통과시키고 밀린 사이클까지 돌린다
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    books = [
        (_FakeSubscriptionManagerWithStrikes(), "regular"),
        (_FakeSubscriptionManagerWithStrikes(), "weekly_mon"),
    ]
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_option_chain(rest_client, books, _FakeMaster(), interval_seconds=60))
    return sleep_calls


def test_option_chain_catches_up_the_skipped_minute_with_monthly_book_only(monkeypatch):
    # 2026-07-30 Fix#3(위상 격자 스냅)은 위상 고착을 해결한 대신 **밀림 1회 = 결손 1분**을
    # 확정시켰다(07-31 실측: 밀림 46건 → 결손 47분, 먼슬리 커버리지 95.0% → 90.5%).
    # 격자를 되돌리지 않고, 다음 틱까지의 대기 시간 안에서 건너뛴 분을 먼슬리만으로 메운다.
    inserted: list[dict] = []
    # 1번째 사이클 종료 1000.0 → next_tick 1060 / 2번째 종료 1200.0 → 1120을 80초 지나쳐 밀림.
    sleep_calls = _catchup_harness(
        monkeypatch,
        loop_times=[1000.0, 1200.0],
        overran_at=datetime(2026, 7, 31, 10, 30, 0),
        now_at=datetime(2026, 7, 31, 10, 31, 0),  # 밀린 사이에 벽시계는 다음 분으로 넘어갔다
        inserted=inserted,
    )

    catchup_rows = [row for row in inserted if row["timestamp"] == datetime(2026, 7, 31, 10, 31, 0)]
    assert catchup_rows, "건너뛴 분이 회수되지 않았다"
    # 먼슬리 1북만 회수한다 — 위클리는 축소안 (a)가 이미 격분 해상도를 선택했으므로 일관.
    assert {row["option_type"] for row in catchup_rows} == {"C", "P"}
    assert len(catchup_rows) == len(_FakeSubscriptionManagerWithStrikes().desired_strikes) * 2
    assert sleep_calls[-1] <= 40.0  # 회수에 쓴 시간만큼 남은 대기가 줄어든다


def test_option_chain_does_not_catch_up_when_remaining_delay_is_too_short(monkeypatch):
    # 회수 사이클까지 밀려 연쇄되는 것을 막는다 — 남은 대기가 임계 미만이면 시도하지 않는다.
    inserted: list[dict] = []
    # 2번째 종료 1175.0 → 1120을 55초 지나침 → 스냅 1180 → delay 5.0초(임계 25초 미만).
    _catchup_harness(
        monkeypatch,
        loop_times=[1000.0, 1175.0],
        overran_at=datetime(2026, 7, 31, 10, 30, 0),
        now_at=datetime(2026, 7, 31, 10, 31, 0),
        inserted=inserted,
    )

    assert not [row for row in inserted if row["timestamp"] == datetime(2026, 7, 31, 10, 31, 0)]


def test_option_chain_does_not_catch_up_when_no_minute_was_actually_skipped(monkeypatch):
    # 밀렸어도 벽시계 분이 그대로면(=실제로는 안 건너뛰었으면) 같은 분을 두 번 쓰지 않는다.
    inserted: list[dict] = []
    _catchup_harness(
        monkeypatch,
        loop_times=[1000.0, 1200.0],
        overran_at=datetime(2026, 7, 31, 10, 30, 0),
        now_at=datetime(2026, 7, 31, 10, 30, 0),  # 분이 안 바뀜
        inserted=inserted,
    )

    assert len(inserted) == len([r for r in inserted if r["timestamp"] == datetime(2026, 7, 31, 10, 30, 0)])
    # 회수분이 없으므로 30레그(짝수분 3북)와 10레그(홀수분)만 존재해야 한다 — 중복 적재 없음
    assert all(row["timestamp"] == datetime(2026, 7, 31, 10, 30, 0) for row in inserted)


# ===== 2026-07-31 운영점검 §4 우선순위 5: 폴러 "점유 구간" 충돌 검사 =====
#
# 종전 두 테스트(test_five_minute_pollers_are_spread.../test_sixty_second_pollers_do_not_collide...)는
# 폴러를 **점(발사 시각)** 으로만 모델링했고, 그래서 07-31에 실제로 일어난 충돌을 못 잡았다:
#   - 만기유동성은 30콜을 쏘느라 중앙 55.5초(최대 109초)를 점유하는데 그 길이를 아무도 모델링하지
#     않았다 → 1:35에 시작해 2분대 옵션체인 사이클을 통째로 덮었다(밀림 17건).
#   - `offset % 60 >= 30` 규칙의 전제("옵션체인은 매 분 t=0~30초를 쓴다")도 틀렸다 —
#     짝수분 옵션체인은 실측 중앙 39.2초·p90 59.0초를 쓴다.
# 그래서 점이 아니라 **구간**으로 바꾸고, 구간 길이를 상수(북 수·행사가 폭·기준 페이서 간격)에서
# 직접 계산한다. 앞으로 행사가 폭이나 북 구성을 바꾸면 이 테스트가 따라온다.


def _nominal_poller_occupancy(minute: int) -> list[tuple[str, float, float]]:
    """이 분(0~9, minute % 10)에 발사되는 폴러들의 (이름, 시작초, 종료초) 목록.

    점유 길이는 **백오프가 없을 때**(기준 페이서 1.0초/콜)의 공칭값이다 — 실측은 백오프 배율에
    비례해 늘어나며, 그래서 총 REST 수요 예산(07-31 실측 43.6%)을 함께 관리해야 한다.
    """
    from mahdi.broker.rest_client import DEFAULT_MIN_REQUEST_INTERVAL_SECONDS as PACE

    strikes_per_book = mahdi_main.STRIKES_EACH_SIDE * 2 + 1
    legs_per_book = strikes_per_book * 2  # 콜/풋
    liquidity_legs = (mahdi_main.LIQUIDITY_ATM_EACH_SIDE * 2 + 1) * 2 + 1  # 호가 + 만기확인 앵커 1건

    occupancy: list[tuple[str, float, float]] = []

    def add(name: str, phase: float, calls: int) -> None:
        start = phase % 60.0
        occupancy.append((name, start, start + calls * PACE))

    # 옵션체인: 먼슬리는 매 분, 위클리는 각자 배정된 분 패리티에만(2026-08-03 §4 우선순위 2).
    due_books = 1 + sum(
        1
        for phase in mahdi_main.OPTION_CHAIN_SLOW_SERIES_PHASE.values()
        if minute % mahdi_main.OPTION_CHAIN_SLOW_SERIES_EVERY_N_MINUTES == phase
    )
    add("option_chain", mahdi_main.OPTION_CHAIN_PHASE_OFFSET_SECONDS, due_books * legs_per_book)
    # 투자자수급: 매 분 3세그먼트(선물/콜/풋).
    add("investor_flow", mahdi_main.INVESTOR_FLOW_PHASE_OFFSET_SECONDS, 3)
    # 만기유동성: 북별 홀수분 슬롯에서 1북씩.
    if minute in mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES:
        add("expiry_liquidity", mahdi_main.EXPIRY_LIQUIDITY_PHASE_OFFSET_SECONDS, liquidity_legs)
    # 매크로/계좌잔고: 300초 주기라 10분 창에서 두 번(minute, minute+5) 발사된다.
    macro_minute = int(mahdi_main.MACRO_SNAPSHOT_PHASE_OFFSET_SECONDS // 60)
    if minute % 5 == macro_minute % 5:
        # 최대 7콜(VIX 근월/차근월 + USDCNH + ZN + ES + US10Y + USDKRW) — 저빈도 항목이 다 겹치는 날.
        add("macro_snapshot", mahdi_main.MACRO_SNAPSHOT_PHASE_OFFSET_SECONDS, 7)
    balance_minute = int(mahdi_main.ACCOUNT_BALANCE_PHASE_OFFSET_SECONDS // 60)
    if minute % 5 == balance_minute % 5:
        add("account_balance", mahdi_main.ACCOUNT_BALANCE_PHASE_OFFSET_SECONDS, 1)
    return occupancy


def test_poller_occupancy_windows_do_not_overlap():
    # 공통 주기(LCM)는 10분 — 그 창의 모든 분에 대해 폴러 점유 구간이 겹치지 않아야 한다.
    for minute in range(10):
        windows = sorted(_nominal_poller_occupancy(minute), key=lambda w: w[1])
        for (a_name, _a_start, a_end), (b_name, b_start, _b_end) in zip(windows, windows[1:]):
            assert a_end <= b_start, (
                f"minute%10={minute}: {a_name}({_a_start:.0f}~{a_end:.0f}초)와 "
                f"{b_name}({b_start:.0f}초~)의 점유 구간이 겹친다"
            )


def test_poller_occupancy_fits_inside_the_minute():
    # 마지막 폴러가 분 경계를 넘기면 다음 분 옵션체인(위상 0초)과 겹친다.
    for minute in range(10):
        windows = _nominal_poller_occupancy(minute)
        last_end = max(end for _name, _start, end in windows)
        assert last_end <= 60.0, f"minute%10={minute}: 점유가 {last_end:.0f}초로 분을 넘긴다"


def test_option_chain_load_is_flat_across_every_minute():
    """2026-08-03 §4 우선순위 2의 핵심 계약 — 어느 분에도 옵션체인 부하가 몰리지 않아야 한다.

    종전에는 위클리 2북이 모두 짝수분에 실려 짝수분 30레그 / 홀수분 10레그였고, 밀림 39건의
    100%가 짝수 mod10, 결손 41분 중 39분이 홀수분이었다(짝수분 초과분이 다음 분을 스킵시킨다).
    """
    per_minute = []
    for minute in range(10):
        chain = [w for w in _nominal_poller_occupancy(minute) if w[0] == "option_chain"]
        assert len(chain) == 1
        per_minute.append(chain[0][2] - chain[0][1])
    assert len(set(per_minute)) == 1, f"분마다 옵션체인 점유가 다르다: {per_minute}"


def test_weekly_books_split_across_minute_parities():
    # 두 위클리 북이 같은 패리티를 쓰면 평탄화가 무너진다 — 상수 수준에서 못 박는다.
    phases = list(mahdi_main.OPTION_CHAIN_SLOW_SERIES_PHASE.values())
    assert sorted(phases) == list(range(mahdi_main.OPTION_CHAIN_SLOW_SERIES_EVERY_N_MINUTES))


def test_low_frequency_pollers_share_a_phase_on_disjoint_minutes():
    # 만기유동성/매크로/계좌잔고는 같은 30초 위상을 공유한다 — 발사 분 집합이 서로소라 안전하다.
    # (겹치면 test_poller_occupancy_windows_do_not_overlap이 잡지만, 의도를 여기에 남긴다.)
    expiry = set(mahdi_main.EXPIRY_LIQUIDITY_BOOK_SLOT_MINUTES)
    macro = {m for m in range(10) if m % 5 == int(mahdi_main.MACRO_SNAPSHOT_PHASE_OFFSET_SECONDS // 60) % 5}
    balance = {m for m in range(10) if m % 5 == int(mahdi_main.ACCOUNT_BALANCE_PHASE_OFFSET_SECONDS // 60) % 5}

    assert expiry & macro == set()
    assert expiry & balance == set()
    assert macro & balance == set()


# ===== 2026-07-31 총 REST 수요 축소안 (a): 위클리 2북 격분 폴링 =====


def _books_for_cadence_test():
    return [
        (_FakeSubscriptionManagerWithStrikes(), "regular"),
        (_FakeSubscriptionManagerWithStrikes(), "weekly_mon"),
        (_FakeSubscriptionManagerWithStrikes(), "weekly_thu"),
    ]


def test_books_due_pairs_monthly_with_weekly_mon_on_even_minutes():
    # 2026-08-03 §4 우선순위 2: 매 분 먼슬리 1북 + 위클리 1북 = 20레그로 평탄해진다.
    due = _books_due_this_cycle(_books_for_cadence_test(), datetime(2026, 7, 31, 9, 30))
    assert [series for _m, series in due] == ["regular", "weekly_mon"]


def test_books_due_pairs_monthly_with_weekly_thu_on_odd_minutes():
    due = _books_due_this_cycle(_books_for_cadence_test(), datetime(2026, 7, 31, 9, 31))
    assert [series for _m, series in due] == ["regular", "weekly_thu"]  # 먼슬리는 언제나 매분


def test_books_due_halves_weekly_call_volume_over_an_hour():
    # 축소안 (a)의 정량 목표: 위클리 호출량이 정확히 절반이 되는지(먼슬리는 그대로).
    books = _books_for_cadence_test()
    counts = {"regular": 0, "weekly_mon": 0, "weekly_thu": 0}
    for minute in range(60):
        for _m, series in _books_due_this_cycle(books, datetime(2026, 7, 31, 9, minute)):
            counts[series] += 1
    assert counts["regular"] == 60
    assert counts["weekly_mon"] == 30
    assert counts["weekly_thu"] == 30


def test_books_due_keeps_unknown_series_every_cycle():
    # 새 북을 추가했을 때 조용히 폴링에서 빠지는 것보다, 모르면 매 사이클 도는 쪽이 안전하다.
    books = [(_FakeSubscriptionManagerWithStrikes(), "quarterly_new")]
    assert len(_books_due_this_cycle(books, datetime(2026, 7, 31, 9, 31))) == 1


def test_books_due_preserves_input_order_so_monthly_is_polled_first():
    # _update_atm_iv가 rows 기준으로 ATM을 고르므로 주력 북(먼슬리)이 먼저 조회돼야 한다.
    due = _books_due_this_cycle(_books_for_cadence_test(), datetime(2026, 7, 31, 9, 30))
    assert due[0][1] == "regular"


def test_poll_option_chain_uses_weekly_thu_on_odd_minutes(monkeypatch):
    # 실제 폴러 경로에서도 위클리 심볼이 조회되지 않는지 확인한다(헬퍼 단위테스트만으로는
    # 호출측이 due_books를 실제로 쓰는지 보장되지 않는다).
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    requested_series: list[str] = []

    class _SeriesRecordingMaster:
        def option_symbol(self, option_type, strike, underlying="KOSPI200", series="regular"):
            requested_series.append(series)
            return f"SYM{int(strike)}{option_type}"

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 7, 31, 9, 31, 5))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_option_chain(rest_client, _books_for_cadence_test(), _SeriesRecordingMaster(), interval_seconds=60))

    assert set(requested_series) == {"regular", "weekly_thu"}


def test_poll_option_chain_uses_weekly_mon_on_even_minutes(monkeypatch):
    rest_client = _FakeRestClientChain(_SAMPLE_OPTION_QUOTE)

    requested_series: list[str] = []

    class _SeriesRecordingMaster:
        def option_symbol(self, option_type, strike, underlying="KOSPI200", series="regular"):
            requested_series.append(series)
            return f"SYM{int(strike)}{option_type}"

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_option_analysis_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_underlying_spot", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.record_rate_limiter_status", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.append_rate_limiter_status_history", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 7, 31, 9, 30, 5))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_option_chain(rest_client, _books_for_cadence_test(), _SeriesRecordingMaster(), interval_seconds=60))

    assert set(requested_series) == {"regular", "weekly_mon"}


# ===== 2026-07-31 매크로 항목별 갱신 주기 분리 =====


def test_macro_items_due_returns_everything_on_first_cycle():
    # 기동 직후에는 종전과 동일하게 모든 값이 한 번에 채워져야 한다(마지막 조회 기록이 없음).
    assert _macro_items_due({}, 1000.0) == frozenset(mahdi_main.MACRO_ITEM_REFRESH_SECONDS)


def test_macro_items_due_excludes_items_within_their_refresh_window():
    last = {item: 1000.0 for item in mahdi_main.MACRO_ITEM_REFRESH_SECONDS}
    # 5분(=매크로 폴러 1사이클) 뒤 — 가장 짧은 주기(ZN 1시간)에도 한참 못 미친다.
    assert _macro_items_due(last, 1000.0 + 300.0) == frozenset()


def test_macro_items_due_reopens_zn_after_an_hour_but_not_the_daily_series():
    last = {item: 1000.0 for item in mahdi_main.MACRO_ITEM_REFRESH_SECONDS}
    due = _macro_items_due(last, 1000.0 + 3600.0)
    assert "zn" in due and "es_kis_probe" in due
    assert "daily_series" not in due and "move" not in due  # 6시간 주기라 아직


def test_macro_items_due_reopens_daily_series_after_six_hours():
    last = {item: 1000.0 for item in mahdi_main.MACRO_ITEM_REFRESH_SECONDS}
    assert _macro_items_due(last, 1000.0 + 21600.0) == frozenset(mahdi_main.MACRO_ITEM_REFRESH_SECONDS)


def test_macro_refresh_periods_are_multiples_of_the_poll_interval():
    # 폴러가 5분마다 도는데 주기가 그 배수가 아니면 실제 갱신 간격이 들쭉날쭉해진다.
    interval = mahdi_main.MACRO_SNAPSHOT_POLL_INTERVAL_SECONDS
    for item, period in mahdi_main.MACRO_ITEM_REFRESH_SECONDS.items():
        assert period % interval == 0, f"{item} 주기 {period}초가 폴링 주기 {interval}초의 배수가 아니다"


def _macro_cycle_recorder(monkeypatch, written):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_macro_snapshot_5m", lambda conn, row: written.append(row))


def test_poll_macro_snapshot_skips_low_frequency_items_after_first_cycle(monkeypatch):
    # 핵심 회귀 방지: ZN의 KIS 호출(하루 99건 100% 실패)과 US10Y/USDKRW 일봉 조회가 매 사이클
    # 반복되지 않아야 한다. ES 값(yfinance)과 VIX/USDCNH는 매 사이클 그대로 채워져야 한다.
    master = _FakeOverseasFutureMaster(
        {"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26"),
         "ZN": ("ZNU26", "ZNZ26"), "ES": ("ESU26", "ESZ26")}
    )
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
            "ZNU26": _future_price_response(108.50),
            "ESU26": _future_price_response(7380.0),
        },
        daily_chart=_daily_chart_response(4.54),
        usdkrw_daily_chart=_daily_chart_response(1352.0),
    )
    written: list[dict] = []
    _macro_cycle_recorder(monkeypatch, written)
    monkeypatch.setattr(
        "mahdi.main.yfinance_fallback.fetch_last_close",
        _fallback_stub(zn=_FALLBACK_PRICE, es=_FALLBACK_PRICE, move=_FALLBACK_PRICE),
    )

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert len(written) == 3
    # 1번째 사이클엔 전부 due → 값이 있고, 2·3번째는 저빈도 항목이 NULL로 남는다.
    assert [row["zn_front"] for row in written] == [108.50, None, None]
    assert [row["us10y_yield"] for row in written] == [4.54, None, None]
    assert [row["usdkrw"] for row in written] == [1352.0, None, None]
    assert [row["move_index"] for row in written] == [_FALLBACK_PRICE, None, None]
    # 반면 신호에 실제로 쓰이는 값들은 매 사이클 유지돼야 한다.
    assert all(row["vix_front"] == 17.50 for row in written)
    assert all(row["usdcnh"] == 6.7803 for row in written)
    assert all(row["es_front"] is not None for row in written)


def test_poll_macro_snapshot_keeps_es_value_every_cycle_via_fallback(monkeypatch):
    # ES는 compute_macro_score_proxy의 실제 입력이라 값 자체는 매 사이클 있어야 한다 —
    # 저빈도로 돌리는 건 100% 실패가 확정된 KIS 시도(es_kis_probe)뿐이다.
    master = _FakeOverseasFutureMaster(
        {"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26"), "ES": ("ESU26", "ESZ26")}
    )
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },  # ESU26 없음 → KIS 실패 → yfinance 폴백
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []
    _macro_cycle_recorder(monkeypatch, written)
    monkeypatch.setattr(
        "mahdi.main.yfinance_fallback.fetch_last_close",
        _fallback_stub(zn=_FALLBACK_PRICE, es=_FALLBACK_PRICE, move=_FALLBACK_PRICE),
    )

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert all(row["es_front"] == _FALLBACK_PRICE for row in written)
    assert all(row["es_front_source"] == "yfinance_fallback" for row in written)


def test_poll_macro_snapshot_does_not_count_unfetched_zn_cycles_as_failures(monkeypatch):
    # ZN 미조회 사이클의 NULL은 "실패"가 아니다 — 그걸 세면 조회 주기(1시간)와 무관하게
    # 매 5분 스트릭이 올라 ZN_DUAL_FAILURE_ALERT_STREAK(=2)를 곧바로 넘겨 오경보가 된다.
    master = _FakeOverseasFutureMaster({"VX": ("VXN26", "VXQ26"), "CNH": ("CNHN26", "CNHU26")})
    rest_client = _FakeOverseasRestClient(
        future_prices={
            "VXN26": _future_price_response(17.50),
            "VXQ26": _future_price_response(17.80),
            "CNHN26": _future_price_response(6.7803),
        },
        daily_chart=_daily_chart_response(4.54),
    )
    written: list[dict] = []
    _macro_cycle_recorder(monkeypatch, written)
    # ZN은 첫 사이클에 성공 → 스트릭 0. 이후 사이클은 아예 조회 안 함(NULL).
    monkeypatch.setattr(
        "mahdi.main.yfinance_fallback.fetch_last_close",
        _fallback_stub(zn=_FALLBACK_PRICE, es=_FALLBACK_PRICE, move=_FALLBACK_PRICE),
    )

    notify_calls: list[tuple[str, str]] = []
    monkeypatch.setattr("mahdi.main.notify.notify", lambda message, level="INFO": notify_calls.append((message, level)))

    call_count = {"n": 0}

    async def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] >= 5:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_macro_snapshot(rest_client, master, interval_seconds=1))

    assert [row["zn_front"] for row in written] == [_FALLBACK_PRICE, None, None, None, None]
    assert notify_calls == []  # 미조회 NULL 4건이 이중실패로 오인되지 않아야 한다


def test_run_observation_loop_rerolls_atm_when_futures_bar_completes(monkeypatch):
    """2026-08-03 §2-2 후속 회귀 — ATM은 스팟을 따라 **계속** 이동해야 한다.

    수정 전에는 `roll_to_spot()`이 WS 연결당 단 한 번(진입부)만 호출됐다. 그 결과 2026-08-03
    실측에서 07:31 장전 호가 1046.81로 잡힌 행사가 1042.50~1052.50 5개가 하루 종일 고정됐고,
    정작 시장은 983~1015에서 움직여 **하루치 옵션 체인 전체가 약 5.5% 외가격에서 수집**됐다.
    §2-1(NaN 오염)을 걷어낸 뒤에도 감마플립 산출률이 0%로 남은 이유가 이것이다.
    """
    futures_symbol = "101S03"
    incoming = [
        _make_h0ifcnt0("090000", 350.0, 10, 350.05, 349.95, 100, 100, symbol=futures_symbol),
        # 스팟이 5포인트 올라 ATM이 350.0 → 355.0으로 두 칸 이동하는 봉
        _make_h0ifcnt0("090030", 355.0, 10, 355.05, 354.95, 100, 100, symbol=futures_symbol),
        _make_h0ifcnt0("090100", 355.0, 5, 355.05, 354.95, 100, 100, symbol=futures_symbol),  # 09:00봉 flush
    ]
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection(incoming))
    subscription_manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    rest_client = FakeRestClient(spot=350.0)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_regime_state", lambda conn, **kwargs: None)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda conn, underlying, symbol, updated_at: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol=futures_symbol,
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )

    # 진입부 초기 롤링은 REST 스팟 350.0 → {347.5, 350.0, 352.5}.
    # 09:00봉 종가 355.0으로 재롤링되면 {352.5, 355.0, 357.5}이어야 한다.
    assert subscription_manager.desired_strikes == frozenset({352.5, 355.0, 357.5})
    # 슬롯 수는 그대로 — 롤링은 범위를 벗어난 구독을 먼저 해제한 뒤 새 행사가를 구독한다
    # (선물/장운영정보 구독은 옵션 슬롯과 무관하므로 옵션 TR만 센다).
    option_subs = {key for key in ws_client.active_subscriptions if key[0] == "H0IOCNT0"}
    assert len(option_subs) == 6


def test_reroll_books_to_spot_logs_only_when_window_actually_moves(monkeypatch, caplog):
    # roll_to_spot()은 diff 기반이라 변화가 없으면 무동작이지만, "언제 어디로 옮겼는가"는
    # 로그에 남아야 한다 — 매분 호출되므로 변화 없을 때 조용한 것도 똑같이 중요하다.
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([]))
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    with caplog.at_level("INFO", logger="mahdi.main"):
        _run(mahdi_main._reroll_books_to_spot([manager], 350.0))
        first = [r for r in caplog.records if "ATM 롤링" in r.message]
        _run(mahdi_main._reroll_books_to_spot([manager], 350.4))  # 같은 ATM 격자 → 변화 없음
        second = [r for r in caplog.records if "ATM 롤링" in r.message]
        _run(mahdi_main._reroll_books_to_spot([manager], 356.0))  # ATM 355.0 → 이동
        third = [r for r in caplog.records if "ATM 롤링" in r.message]

    assert len(first) == 1
    assert len(second) == 1, "ATM이 그대로면 로그를 남기지 않는다"
    assert len(third) == 2


def test_reroll_warns_when_the_window_stays_stuck_two_cycles(caplog):
    """2026-08-18(SERIES_ROTATION_RULE_v1 §6-3) — 창 고착 상시 진단.

    08-04 이전 사고(롤링이 안 돌아 하루치 체인이 5.5% OTM 방치)가 재발하면, 롤 직후에도
    임계를 넘긴 거리가 남고 다음 사이클에도 안 줄어든다 — 그때 WARNING 1줄. 1사이클짜리
    초과(다음 롤이 잡는 과도 상태)에는 울리지 않는다.
    """
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([]))
    manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1, label="regular"
    )
    liveness = mahdi_main.WsLiveness()

    with caplog.at_level("WARNING", logger="mahdi.main"):
        _run(mahdi_main._reroll_books_to_spot([manager], 350.0, ws_liveness=liveness))
        assert liveness.window_stuck_warnings == 0

        # 롤링 로직이 죽은 상태를 재현 — roll_to_spot이 불려도 창이 안 움직인다.
        async def frozen_roll(spot):
            return None

        manager.roll_to_spot = frozen_roll
        _run(mahdi_main._reroll_books_to_spot([manager], 356.0, ws_liveness=liveness))
        assert liveness.window_stuck_warnings == 0, "첫 관측은 과도 상태일 수 있어 기다린다"
        _run(mahdi_main._reroll_books_to_spot([manager], 356.0, ws_liveness=liveness))

    assert liveness.window_stuck_warnings == 1
    stuck = [r for r in caplog.records if "창 고착 의심" in r.message]
    assert len(stuck) == 1
    assert "regular" in stuck[0].getMessage()


def test_reroll_stuck_state_clears_once_the_window_moves_again(caplog):
    """고착이 풀리면(창이 다시 스팟을 따라가면) 직전 거리 상태도 지워져 다시 울리지 않는다."""
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([]))
    manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1, label="regular"
    )
    liveness = mahdi_main.WsLiveness()
    real_roll = manager.roll_to_spot

    with caplog.at_level("WARNING", logger="mahdi.main"):
        _run(mahdi_main._reroll_books_to_spot([manager], 350.0, ws_liveness=liveness))

        async def frozen_roll(spot):
            return None

        manager.roll_to_spot = frozen_roll
        _run(mahdi_main._reroll_books_to_spot([manager], 356.0, ws_liveness=liveness))
        del manager.roll_to_spot  # 인스턴스 가림막 제거 → 원래 롤링 복구
        assert manager.roll_to_spot == real_roll
        _run(mahdi_main._reroll_books_to_spot([manager], 356.0, ws_liveness=liveness))

    assert liveness.window_stuck_warnings == 0
    assert liveness.window_stuck_prev == {}


# ===== 2026-08-03 §2-8 / §4 우선순위 3: 로그 위생 =====


def test_kis_call_failure_keeps_response_body_but_drops_traceback(caplog):
    """HTTPStatusError는 원인이 응답 바디에 전부 있고 스택은 항상 같은 세 프레임이라 정보가 0이다.

    08-03 실측: 이 트레이스백만 하루 약 1,700줄이었고, 그중 306줄이 "CBOT/CME SUB거래소 신청
    계좌가 아닙니다"(계정 권한 문제 — 코드로는 절대 해결 안 되는 항상 실패) 18건이었다.
    """
    request = httpx.Request("GET", "https://openapivts.koreainvestment.com/uapi/x?SRS_CD=ZNU26")
    response = httpx.Response(
        500, request=request,
        json={"rt_cd": "1", "msg_cd": "EGW00552", "msg1": "CBOT SUB거래소 신청 계좌가 아닙니다."},
    )
    exc = httpx.HTTPStatusError("Server error '500'", request=request, response=response)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        mahdi_main._log_kis_call_failure("ZN(10년 국채선물) 근월물 조회 실패: ZNU26", exc)

    record = caplog.records[-1]
    assert "EGW00552" in record.getMessage(), "KIS 원인 코드는 반드시 남아야 한다"
    assert not record.exc_info, "HTTPStatusError에는 트레이스백을 붙이지 않는다"


def test_kis_call_failure_keeps_traceback_for_unexpected_errors(caplog):
    # 반대로 HTTPStatusError가 아닌 예외는 "어디서 났는가"가 곧 원인이므로 스택을 남긴다.
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        mahdi_main._log_kis_call_failure("응답 파싱 실패", ValueError("bad payload"))

    assert caplog.records[-1].exc_info is not None


# ===== 2026-08-03 §2-4 / §4 우선순위 4: 구독 성립 신호 =====


def test_observation_loop_records_market_op_subscription_ack(monkeypatch):
    """08-03에 장운영정보 데이터가 하루 0건이었는데 하트비트는 정상이라 배지가 초록이었다.

    `last_message_at`에는 임계를 걸 수 없으므로(정상일에도 0~2건) **구독이 성립했는가**를
    따로 기록해 "구독이 안 걸린 것"과 "이벤트가 없었던 것"을 구분한다.
    """
    ack = json.dumps(
        {
            "header": {"tr_id": tr_codes.WS_TR_MARKET_OPERATION_INFO, "tr_key": "005930"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
        }
    )
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([ack]))
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)
    liveness = mahdi_main.WsLiveness()

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 4, 7, 31, 4))

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [manager], FakeRestClient(spot=350.0), futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                ws_liveness=liveness,
            )
        )

    assert liveness.market_op_subscribed_at == datetime(2026, 8, 4, 7, 31, 4)
    # 구독 응답은 데이터가 아니다 — last_message_at을 건드리면 "감지기가 뭔가를 봤다"가 거짓이 된다.
    assert liveness.last_message_at is None


def test_observation_loop_ignores_ack_for_other_tr_ids(monkeypatch):
    ack = json.dumps(
        {
            "header": {"tr_id": "H0IFCNT0", "tr_key": "101S03"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
        }
    )
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([ack]))
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)
    liveness = mahdi_main.WsLiveness()

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [manager], FakeRestClient(spot=350.0), futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                ws_liveness=liveness,
            )
        )

    assert liveness.market_op_subscribed_at is None


def test_market_op_ack_is_written_to_db_immediately_not_only_by_heartbeat(monkeypatch):
    """2026-08-04 회귀 — 하트비트에만 맡기면 매일 아침 5분간 "구독 미성립" 오경보가 뜬다.

    첫 하트비트가 구독 ACK보다 먼저 돌기 때문이다(08-04 실측: 하트비트 07:31:02.7,
    ACK 07:31:03.2). ACK은 기동/재연결 시에만 오는 드문 이벤트라 즉시 써도 부담이 없다.
    """
    ack = json.dumps(
        {
            "header": {"tr_id": tr_codes.WS_TR_MARKET_OPERATION_INFO, "tr_key": "005930"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
        }
    )
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([ack]))
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)
    liveness = mahdi_main.WsLiveness()
    written: list = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 4, 7, 31, 3))
    monkeypatch.setattr("mahdi.main.db.mark_market_op_subscribed", lambda conn, ts: written.append(ts))

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [manager], FakeRestClient(spot=350.0), futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                ws_liveness=liveness,
            )
        )

    assert written == [datetime(2026, 8, 4, 7, 31, 3)]


def test_market_op_ack_db_failure_does_not_break_observation(monkeypatch):
    # DB 기록이 실패해도 구독 자체와 관측은 계속돼야 한다 — 다음 하트비트가 메모리 값을 싣고 간다.
    ack = json.dumps(
        {
            "header": {"tr_id": tr_codes.WS_TR_MARKET_OPERATION_INFO, "tr_key": "005930"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
        }
    )
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([ack]))
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)
    liveness = mahdi_main.WsLiveness()

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 4, 7, 31, 3))

    def boom(conn, ts):
        raise RuntimeError("DB 다운")

    monkeypatch.setattr("mahdi.main.db.mark_market_op_subscribed", boom)

    with pytest.raises(ConnectionError):  # WS 수신 루프는 끝까지 돈다
        _run(
            run_observation_loop(
                ws_client, [manager], FakeRestClient(spot=350.0), futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                ws_liveness=liveness,
            )
        )

    assert liveness.market_op_subscribed_at == datetime(2026, 8, 4, 7, 31, 3)


# --- VRP 배선(2026-08-05 운영점검보고서 §2 이상점 1 / Fix#1) --------------------------------


def _vrp_chain_row(strike: float, opt: str, expiry: date, *, iv: float, rv: float | None) -> dict:
    return {"strike": strike, "option_type": opt, "oi": 100.0, "iv": iv, "gamma": 0.02,
            "gex": 0.0, "expiry": expiry, "timestamp": datetime(2026, 8, 5, 10, 0), "rv_5d": rv}


def test_build_signal_inputs_computes_vrp_from_the_monthly_book_only(monkeypatch):
    """VRP는 GEX와 **같은 북(먼슬리)** 에서 나와야 한다(Fix#5와 같은 이유, v6 §11.4).

    08-05 실측에서 위클리 두 북은 `rv_5d`가 전 행 0이었다 — 섞이면 VRP가 곧 IV가 돼
    그 분은 항상 극단적 고평가로 판정된다.
    """
    chain = [
        _vrp_chain_row(350.0, "C", _MONTHLY, iv=0.86, rv=0.78),
        _vrp_chain_row(350.0, "P", _MONTHLY, iv=0.90, rv=0.78),
        # 위클리는 rv=0(08-05 실측) — 이 값이 새어 들어오면 vrp가 IV 그대로가 된다.
        _vrp_chain_row(350.0, "C", _WEEKLY, iv=1.18, rv=0.0),
        _vrp_chain_row(350.0, "P", _WEEKLY, iv=1.18, rv=0.0),
    ]
    _patch_chain(monkeypatch, chain, spot=350.5)

    _inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert chain_inputs["gex_expiry"] == _MONTHLY
    assert chain_inputs["vrp"] == pytest.approx((0.86 + 0.90) / 2 - 0.78)  # = 0.10, 먼슬리 기준


def test_build_signal_inputs_leaves_vrp_none_when_it_cannot_be_computed(monkeypatch):
    """산출 불가는 None으로 남는다 — 0.0으로 채우면 "쟀는데 적정"과 구분되지 않는다.
    호출측이 팔레트에 넘길 때만 안전한 쪽(fair=관망)으로 폴백한다."""
    _patch_chain(monkeypatch, [
        _vrp_chain_row(350.0, "C", _MONTHLY, iv=0.86, rv=None),
        _vrp_chain_row(350.0, "P", _MONTHLY, iv=0.90, rv=None),
    ], spot=350.5)

    _inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert chain_inputs["vrp"] is None


def test_build_signal_inputs_keeps_vrp_none_when_chain_is_empty(monkeypatch):
    _patch_chain(monkeypatch, [], spot=350.5)

    _inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert chain_inputs["vrp"] is None
    assert chain_inputs["gex_expiry"] is None


def test_empty_chain_kills_options_flow_and_is_recorded_as_such(monkeypatch):
    """2026-08-14 §2-1 회귀 방지 — **체인 전멸 분의 판단은 감마 지형을 못 봤다고 말해야 한다.**

    그날 14:00~15:23 84분 연속으로 옵션체인이 0행이었다. 5분 신선도 창
    (`db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES`, SQL 경계는 `test_chain_snapshot_bounds_freshness_and_expiry`가
    검사한다)이 그 구간의 스냅샷을 비우므로 이 함수에는 **빈 목록**이 들어온다 — 즉 08-14
    보고서 Fix#1이 걱정한 *"창 밖에서도 계속 쓴다"*는 실제로 일어나지 않는다. 이 테스트가
    그 사실을 못박는다(그 전제가 다시 흔들리면 여기가 깨진다).

    **그러나 이 테스트가 함께 기록하는 것이 더 중요하다**: 체인이 비어도 판단 자체는 계속
    나온다(다른 멤버가 살아 있으므로). 08-14에 84분 내내 `signal_decisions`가 적재되고 ENTER가
    297건 나온 이유가 이것이다. 주문이 배선되는 날 이 성질은 **사람 확인(CONFIRM) 없이는
    안전하지 않다** — 확신도 분모가 가용 멤버 수라서 정보가 줄면 확신도가 오히려 오를 수 있다.
    """
    _patch_chain(monkeypatch, [], spot=350.5)

    inputs, chain_inputs = _build_signal_inputs(
        conn=object(), regime_state=None, underlying="KOSPI200"
    )

    # ① 체인에서 오는 것 전부가 None이고, 그 사실이 판단 행에 남는다.
    assert chain_inputs["chain_input_source"] == "none"
    assert chain_inputs["chain_leg_count"] == 0
    assert chain_inputs["chain_oldest_leg_age_seconds"] is None
    assert (inputs.gex, inputs.gamma_flip, inputs.gamma_wall) == (None, None, None)

    # ② `options_flow`가 죽고, **왜** 죽었는지가 이름으로 남는다(장전/현물마감 사유가 아니다).
    assert build_member_scores(inputs).options_flow is None
    from mahdi.main import _member_unavailable_reasons

    reason = _member_unavailable_reasons(inputs, _CHAIN_FIXTURE_NOW)["options_flow"]
    assert "gex" in reason and "기준선" in reason

    # ③ 그런데 다른 멤버는 살아 있다 — 판단은 계속 나온다. 이것이 08-14의 위험 지점이다.
    assert build_member_scores(inputs).flow_position is not None


def test_poll_signal_fusion_cycle_passes_vrp_to_the_strategy_palette(monkeypatch):
    """회귀 방지 §2 이상점 1(Fix#1): `evaluate()`에 vrp를 안 넘기면 기본값 0.0이 쓰이고,
    그것은 `_vrp_state(0.0, 0.02)` = **항상 "fair"** 를 뜻한다.

    v6 §11.4 매트릭스 3열 중 2열이 전 이력 도달 불가였고, 레짐까지 RANGE_BALANCED 고정이라
    9칸 중 도달 가능한 칸이 `wait_and_see` 하나뿐이었다 — 08-05에 방향 ±0.692 / 동조 3 /
    확신도 0.75짜리 HIGH_CONVICTION 6건이 전부 이 셀에서 버려졌다.
    """
    from mahdi.fusion.engine import SignalFusionEngine

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    _patch_chain(monkeypatch, [
        _vrp_chain_row(350.0, "C", _MONTHLY, iv=0.86, rv=0.78),
        _vrp_chain_row(350.0, "P", _MONTHLY, iv=0.90, rv=0.78),
    ], spot=350.5)

    captured: list[float] = []
    real_evaluate = SignalFusionEngine.evaluate

    # 2026-08-11: **`**kwargs`로 받는다.** 고정 시그니처 스파이는 `evaluate()`에 인자가
    # 하나 늘 때 TypeError를 내고, 그 예외를 폴러가 사이클마다 삼켜 **테스트가 실패하는
    # 대신 무한 루프**가 된다(고도화 D 구현 중 실제로 겪었다).
    def spy(self, signal_inputs, meta_context, vrp=0.0, already_used_strategies_today=frozenset(), **kwargs):
        captured.append(vrp)
        return real_evaluate(self, signal_inputs, meta_context, vrp, already_used_strategies_today, **kwargs)

    monkeypatch.setattr(SignalFusionEngine, "evaluate", spy)

    recorded: list[dict] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(chain_inputs or {}),
    )
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: _FakeLoop([1000.0, 1200.0]))

    async def fake_sleep(seconds):
        if len(recorded) >= 1:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))

    assert captured and captured[0] == pytest.approx(0.10)  # 기본값 0.0이 아니다
    assert recorded[0]["vrp"] == pytest.approx(0.10)  # 마이그레이션 024 — 판단 행에도 남는다


def test_poll_signal_fusion_cycle_falls_back_to_fair_when_vrp_is_unavailable(monkeypatch):
    """산출 불가일 때 0.0(=fair=관망)으로 떨어지는 것은 **의도된 안전 폴백**이다 —
    없는 값을 지어내 진입을 만들지 않는다. 구분은 `signal_decisions.vrp`(NULL)가 보존한다."""
    from mahdi.fusion.engine import SignalFusionEngine

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    _patch_chain(monkeypatch, [], spot=None)

    captured: list[float] = []
    real_evaluate = SignalFusionEngine.evaluate

    # 2026-08-11: **`**kwargs`로 받는다.** 고정 시그니처 스파이는 `evaluate()`에 인자가
    # 하나 늘 때 TypeError를 내고, 그 예외를 폴러가 사이클마다 삼켜 **테스트가 실패하는
    # 대신 무한 루프**가 된다(고도화 D 구현 중 실제로 겪었다).
    def spy(self, signal_inputs, meta_context, vrp=0.0, already_used_strategies_today=frozenset(), **kwargs):
        captured.append(vrp)
        return real_evaluate(self, signal_inputs, meta_context, vrp, already_used_strategies_today, **kwargs)

    monkeypatch.setattr(SignalFusionEngine, "evaluate", spy)

    recorded: list[dict] = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(chain_inputs or {}),
    )
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: _FakeLoop([1000.0, 1200.0]))

    async def fake_sleep(seconds):
        if len(recorded) >= 1:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))

    assert captured == [0.0]  # 팔레트에는 안전한 쪽
    assert recorded[0]["vrp"] is None  # 기록에는 "못 쟀다"가 그대로


# --- MetaLabelContext 실입력 배선(2026-08-05 §2 이상점 2 / Fix#3) ---------------------------


def _signal_inputs_with(gex, stability_flag):
    from mahdi.fusion.signal_layer import SignalInputs

    regime_state = (
        RegimeState(regime=RegimeLabel.RANGE_BALANCED, prob_vector=(1.0,) + (0.0,) * 7,
                    stability_flag=stability_flag)
        if stability_flag is not None else None
    )
    return SignalInputs(regime_state=regime_state, gex=gex)


def test_meta_label_context_marks_gamma_regime_unstable_on_negative_gex():
    """`MetaLabelInputs` 주석이 정의를 이미 적어두었다 — `GEX>=0 & stability_flag`.
    같은 두 값을 `engine.py`가 팔레트 게이트로 쓰고 있었는데 메타 라벨에는 안 연결돼 있었다."""
    from mahdi.main import _build_meta_label_context

    assert _build_meta_label_context(_signal_inputs_with(-9.9e9, True)).gamma_regime_stable is False
    assert _build_meta_label_context(_signal_inputs_with(1.0e9, False)).gamma_regime_stable is False
    assert _build_meta_label_context(_signal_inputs_with(1.0e9, True)).gamma_regime_stable is True


def test_meta_label_context_is_conservative_when_inputs_are_missing():
    """모르면 '안정'으로 치지 않는다 — 페널티를 거는 쪽이 보수적이다."""
    from mahdi.main import _build_meta_label_context

    assert _build_meta_label_context(_signal_inputs_with(None, True)).gamma_regime_stable is False
    assert _build_meta_label_context(_signal_inputs_with(1.0e9, None)).gamma_regime_stable is False


def test_meta_label_context_does_not_invent_slippage_or_win_rate():
    """`trade_history`/`execution_logs`가 둘 다 0행이다. 체결이 없으면 슬리피지도 없으므로
    False는 낙관이 아니라 사실이고, 승률은 None(=이력 없음, 중립 1.0배)이 정확한 표현이다.
    `event_proximity_minutes`만은 **사실이 아니라 미지**다 — 캘린더 소스가 없어 못 채운다."""
    from mahdi.main import _build_meta_label_context

    ctx = _build_meta_label_context(_signal_inputs_with(1.0e9, True))
    assert ctx.recent_slippage_elevated is False
    assert ctx.recent_same_setup_win_rate is None
    assert ctx.event_proximity_minutes is None


def test_optimistic_gamma_default_was_inflating_conviction_by_one_grade():
    """08-05 실측 재현 — HIGH_CONVICTION 6건의 `conviction_score`는 정확히 0.75였다
    (= regime_confidence 1.0 x 동조비 3/4, 페널티 0회). 먼슬리 GEX는 −9.9bn이고
    `stability_flag`는 하루 종일 False였으므로 감마 레짐 페널티(x0.85)가 걸렸어야 하고,
    그랬다면 0.6375 < standard_max(0.65) → STANDARD로 내려갔어야 한다.
    """
    from mahdi.fusion.meta_label import MetaLabelInputs, TradePermission, classify

    thresholds = {"standard_max": 0.65, "gamma_regime_penalty_factor": 0.85}
    base = dict(regime_confidence=1.0, signal_agreement_count=3, available_member_count=4)

    optimistic = classify(MetaLabelInputs(**base, gamma_regime_stable=True), thresholds)
    truthful = classify(MetaLabelInputs(**base, gamma_regime_stable=False), thresholds)

    assert optimistic.conviction_score == pytest.approx(0.75)
    assert optimistic.trade_permission is TradePermission.HIGH_CONVICTION
    assert truthful.conviction_score == pytest.approx(0.6375)
    assert truthful.trade_permission is TradePermission.STANDARD


def test_poll_signal_fusion_cycle_passes_real_meta_label_context(monkeypatch):
    """회귀 방지 §2 이상점 2(Fix#3): `MetaLabelContext()`를 인자 없이 만들면 곱셈 페널티 4종이
    하나도 안 걸린다. 2026-07-10 `warmup_fallback(RANGE_BALANCED, 0.0, 0.0)` 하드코딩과
    같은 형태의 결함 — 함수는 입력을 받도록 설계돼 있는데 호출측이 상수를 넣고 있었다."""
    from mahdi.fusion.engine import SignalFusionEngine

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    # 먼슬리 풋 편중 → GEX 음수(08-05 실측과 같은 배치) → 감마 레짐 불안정이어야 한다.
    _patch_chain(monkeypatch, [
        _vrp_chain_row(345.0, "C", _MONTHLY, iv=0.86, rv=0.78),
        _vrp_chain_row(345.0, "P", _MONTHLY, iv=0.90, rv=0.78),
        _vrp_chain_row(350.0, "C", _MONTHLY, iv=0.86, rv=0.78),
        _vrp_chain_row(350.0, "P", _MONTHLY, iv=0.90, rv=0.78),
    ], spot=350.5)

    captured: list = []
    real_evaluate = SignalFusionEngine.evaluate

    # 2026-08-11: **`**kwargs`로 받는다.** 고정 시그니처 스파이는 `evaluate()`에 인자가
    # 하나 늘 때 TypeError를 내고, 그 예외를 폴러가 사이클마다 삼켜 **테스트가 실패하는
    # 대신 무한 루프**가 된다(고도화 D 구현 중 실제로 겪었다).
    def spy(self, signal_inputs, meta_context, vrp=0.0, already_used_strategies_today=frozenset(), **kwargs):
        captured.append(meta_context)
        return real_evaluate(self, signal_inputs, meta_context, vrp, already_used_strategies_today, **kwargs)

    monkeypatch.setattr(SignalFusionEngine, "evaluate", spy)

    recorded: list = []
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(risk_gate_state),
    )
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: _FakeLoop([1000.0, 1200.0]))

    async def fake_sleep(seconds):
        if len(recorded) >= 1:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))

    # regime_state가 None이므로 stability_flag를 모른다 → 안정으로 치지 않는다.
    assert captured and captured[0].gamma_regime_stable is False


# --- 하루 전략 상한 배선(2026-08-05 §2 이상점 6 / Fix#5) -------------------------------------


def _spy_evaluate(monkeypatch, captured: list):
    from mahdi.fusion.engine import SignalFusionEngine

    real_evaluate = SignalFusionEngine.evaluate

    # 2026-08-11: **`**kwargs`로 받는다.** 고정 시그니처 스파이는 `evaluate()`에 인자가
    # 하나 늘 때 TypeError를 내고, 그 예외를 폴러가 사이클마다 삼켜 **테스트가 실패하는
    # 대신 무한 루프**가 된다(고도화 D 구현 중 실제로 겪었다).
    def spy(self, signal_inputs, meta_context, vrp=0.0, already_used_strategies_today=frozenset(), **kwargs):
        captured.append(already_used_strategies_today)
        return real_evaluate(self, signal_inputs, meta_context, vrp, already_used_strategies_today, **kwargs)

    monkeypatch.setattr(SignalFusionEngine, "evaluate", spy)


def _run_one_fusion_cycle(monkeypatch, recorded: list):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(risk_gate_state),
    )
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: _FakeLoop([1000.0, 1200.0]))

    async def fake_sleep(seconds):
        if len(recorded) >= 1:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))


def test_poll_signal_fusion_cycle_passes_todays_used_strategies(monkeypatch):
    """회귀 방지 §2 이상점 6(Fix#5): 이 인자가 안 넘어가면 v6 §11.4의 하루 전략 상한이
    전 이력 무력이다. ADVISORY라 실손실이 없을 뿐, "안전장치는 죽었는지 알 수 있어야 한다"는
    §5-4 원칙에 어긋난다."""
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    _patch_chain(monkeypatch, [], spot=None)
    monkeypatch.setattr(
        "mahdi.main.db.entry_strategies_used_today",
        lambda conn, on_date: frozenset({"atm_long"}),
    )
    captured: list = []
    _spy_evaluate(monkeypatch, captured)

    _run_one_fusion_cycle(monkeypatch, [])

    assert captured == [frozenset({"atm_long"})]


def test_poll_signal_fusion_cycle_survives_used_strategy_lookup_failure(monkeypatch):
    """상한은 안전장치이지 판단의 전제가 아니다 — 조회가 깨져도 판단은 계속돼야 한다."""
    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    _patch_chain(monkeypatch, [], spot=None)

    def boom(conn, on_date):
        raise RuntimeError("db down")

    monkeypatch.setattr("mahdi.main.db.entry_strategies_used_today", boom)
    captured: list = []
    _spy_evaluate(monkeypatch, captured)

    recorded: list = []
    _run_one_fusion_cycle(monkeypatch, recorded)

    assert captured == [frozenset()]  # 상한 없이 진행
    assert len(recorded) == 1  # 판단 자체는 기록됐다


# --- WS 등록/해제 응답 구분(2026-08-05 §2 이상점 7 / Fix#6) ----------------------------------


def _run_loop_with_ack(monkeypatch, ack_json: str, liveness):
    ws_client = KISWebSocketClient(approval_key="APV", connection=FakeConnection([ack_json]))
    manager = RollingSubscriptionManager(ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 9, 20, 1))

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [manager], FakeRestClient(spot=350.0), futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
                ws_liveness=liveness,
            )
        )


def test_unsubscribe_ack_is_not_logged_as_a_subscription(monkeypatch, caplog):
    """08-05 하루 1,218줄이 해제 응답인데 "WS 구독 확립"으로 적혀 있었다 —
    ATM 롤링마다 나오므로 로그의 상당 부분이 사실과 반대였다."""
    ack = json.dumps(
        {
            "header": {"tr_id": "H0IOCNT0", "tr_key": "C01608A24"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "UNSUBSCRIBE SUCCESS"},
        }
    )
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        _run_loop_with_ack(monkeypatch, ack, mahdi_main.WsLiveness())

    messages = [r.getMessage() for r in caplog.records]
    assert any("WS 구독 해제 확인" in m for m in messages)
    assert not any("WS 구독 확립" in m for m in messages)


def test_subscribe_ack_still_logs_as_a_subscription(monkeypatch, caplog):
    ack = json.dumps(
        {
            "header": {"tr_id": "H0IOCNT0", "tr_key": "C01608A24"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
        }
    )
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        _run_loop_with_ack(monkeypatch, ack, mahdi_main.WsLiveness())

    messages = [r.getMessage() for r in caplog.records]
    assert any("WS 구독 확립" in m for m in messages)
    assert not any("WS 구독 해제 확인" in m for m in messages)


def test_market_op_unsubscribe_does_not_forge_a_liveness_signal(monkeypatch):
    """`market_op_subscribed_at`은 CB 감지의 **생존 신호**다 — 해제 응답으로 갱신되면
    "구독이 살아 있다"는 증거가 거꾸로 만들어진다. 지금은 H0UNMKO0을 해제하는 경로가 없어
    잠복 상태지만(08-05 실측 SUBSCRIBE 1건뿐), 생기는 날 조용히 틀린다."""
    ack = json.dumps(
        {
            "header": {"tr_id": tr_codes.WS_TR_MARKET_OPERATION_INFO, "tr_key": "005930"},
            "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "UNSUBSCRIBE SUCCESS"},
        }
    )
    liveness = mahdi_main.WsLiveness()
    _run_loop_with_ack(monkeypatch, ack, liveness)

    assert liveness.market_op_subscribed_at is None


# --- 이벤트 근접도 배선(2026-08-05, 수기 캘린더 (a)안) ---------------------------------------


def test_meta_label_context_carries_event_proximity_through():
    from mahdi.main import _build_meta_label_context

    ctx = _build_meta_label_context(_signal_inputs_with(1.0e9, True), event_proximity_minutes=10.0)
    assert ctx.event_proximity_minutes == 10.0


def test_meta_label_context_keeps_none_when_the_calendar_cannot_answer():
    """미기입/이벤트없음 둘 다 None이 들어온다 — 페널티를 지어내지 않는다.
    그 둘의 구분은 페널티가 아니라 **경고**로 처리한다(아래 테스트)."""
    from mahdi.main import _build_meta_label_context

    ctx = _build_meta_label_context(_signal_inputs_with(1.0e9, True), event_proximity_minutes=None)
    assert ctx.event_proximity_minutes is None


def _run_fusion_cycle_with_calendar(monkeypatch, calendar, recorded, now):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    _patch_signal_fusion_cycle_db_defaults(monkeypatch)
    _patch_chain(monkeypatch, [], spot=None)
    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.entry_strategies_used_today", lambda conn, on_date: frozenset())
    monkeypatch.setattr("mahdi.main.get_event_calendar", lambda: calendar)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: now)
    monkeypatch.setattr(
        "mahdi.main.db.insert_signal_decision",
        lambda conn, ts, conviction, decision, reject_reason, risk_gate_state, exec_mode,
        chain_inputs=None, selected_instruments=None: recorded.append(risk_gate_state),
    )
    monkeypatch.setattr("mahdi.main.asyncio.get_running_loop", lambda: _FakeLoop([1000.0, 1200.0]))

    async def fake_sleep(seconds):
        if len(recorded) >= 1:
            raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_signal_fusion_cycle(_FakeRegimeStateMachineWithLastState(None), interval_seconds=60))


def test_fusion_cycle_warns_when_the_event_calendar_is_not_covered(monkeypatch, caplog):
    """**이 경고가 수기 방식(a)의 유일한 방어선이다.** 캘린더를 안 채우면 시스템은
    2026-08-05 이전 상태로 조용히 되돌아가고, 그 사실 자체가 안 보인다."""
    calendar = {"covered_through": "2026-08-13", "events": []}

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        _run_fusion_cycle_with_calendar(monkeypatch, calendar, [], datetime(2026, 8, 20, 10, 0))

    assert [r for r in caplog.records if "이벤트 캘린더 미기입" in r.getMessage()]


def test_fusion_cycle_stays_quiet_when_the_calendar_says_no_upcoming_event(monkeypatch, caplog):
    """"확인했고 이벤트가 없다"는 사실이다 — 경고하면 상시 오경보가 된다
    (07-30 CB 하트비트에서 겪은 "정상을 이상으로 표시"와 같은 실수)."""
    calendar = {"covered_through": "2026-08-13", "events": []}

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        _run_fusion_cycle_with_calendar(monkeypatch, calendar, [], datetime(2026, 8, 12, 10, 0))

    assert not [r for r in caplog.records if "이벤트 캘린더 미기입" in r.getMessage()]


def test_fusion_cycle_applies_the_event_penalty_near_an_event(monkeypatch):
    """만기 15분 전이면 확신도에 x0.5가 걸려야 한다 — 08-05까지 한 번도 안 걸리던 페널티다."""
    calendar = {
        "covered_through": "2026-08-13",
        "events": [{"when": "2026-08-06 15:35", "name": "위클리(목) 만기"}],
    }
    near: list = []
    _run_fusion_cycle_with_calendar(monkeypatch, calendar, near, datetime(2026, 8, 6, 15, 25))

    far: list = []
    _run_fusion_cycle_with_calendar(monkeypatch, calendar, far, datetime(2026, 8, 6, 10, 0))

    assert near[0]["conviction_score"] == pytest.approx(far[0]["conviction_score"] * 0.5)


# ===== 2026-08-05 §2-6 / §2-7 — 0행 사이클과 북 수집 순서 =====


def test_collect_option_chain_cycle_visits_the_monthly_book_first():
    """수집 순서 불변식 — **먼슬리가 먼저다.**

    이 순서는 Fix#8(사이클 예산)의 설계 전제다: 예산이 끊으면 잘려나가는 쪽은 뒤쪽이므로,
    판단의 주입력(먼슬리 = GEX/감마플립의 유일한 입력, v6 §11.4)이 먼저 채워져야 한다.
    `_collect_option_chain_cycle` docstring이 이 순서를 **단언**하고 있는데 지금까지 아무도
    검증하지 않았다 — 08-04 §2-1이 "검증되지 않은 단언은 주석이 아니라 오보"라고 적은 그 형태다.

    (08-05 §2-7 조사 결과 순서 자체는 옳았다. 14:31/14:46에 먼슬리가 0레그였던 것은 순서가
     아니라 **먼슬리 레그가 전부 타임아웃**했기 때문이다 — 그래서 이 테스트는 회귀 방지용이다.)
    """
    from mahdi.main import _collect_option_chain_cycle

    rest_client = _FakeRestClientCountingQuotes([1000.0], 0.0, _OPTION_QUOTE_FIXTURE)
    books = [
        (_FakeManagerManyStrikes(frozenset({1000.0})), "regular"),
        (_FakeManagerManyStrikes(frozenset({1000.0})), "weekly_mon"),
        (_FakeManagerManyStrikes(frozenset({1000.0})), "weekly_thu"),
    ]
    _run(
        _collect_option_chain_cycle(
            rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 5, 10, 0),
            WarningThrottle(60.0),
        )
    )

    # _FakeMaster는 series를 심볼에 싣지 않으므로 호출 순서가 곧 북 순서다 —
    # 첫 두 건(콜/풋)이 첫 번째 북(regular)의 것이어야 한다.
    assert len(rest_client.calls) == 6
    assert books[0][1] == "regular", "books[0]은 먼슬리여야 한다 — 이 전제가 깨지면 예산 절단이 주입력을 자른다"


def test_option_chain_cycle_that_collects_nothing_is_louder_than_a_truncation(monkeypatch, caplog):
    """08-05 14:31 회귀 — `rows=0`은 "조금 잘렸다"와 **같은 줄로 보고되면 안 된다.**

    그날 KIS가 53초간 전 레그를 타임아웃시켜 그 분의 체인이 통째로 사라졌는데, 로그에 남은 것은
    예산 초과 WARNING(`...0레그로 이번 분을 마감합니다`) 한 줄뿐이었다 — 17레그로 마감한 분과
    구분되지 않았고, 결손 지표는 사이클이 돌았으므로 세지 않았다. DB에는 0행인데 리포트 §4는
    결손 1분만 보고했다(실제 0행 분은 4분).
    """
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])

    class _AlwaysFailing:
        rate_limit_total_calls = 0

        def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
            clock[0] += 4.0  # read 타임아웃과 같은 시간을 쓰고 실패한다
            raise RuntimeError("read timeout")

    books = [(_FakeManagerManyStrikes(frozenset({1000.0, 1002.5, 1005.0})), "regular")]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rows, _spot, any_strikes, _missing = _run(
            _collect_option_chain_cycle(
                _AlwaysFailing(), books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 5, 14, 31),
                # 이 테스트는 실패 경로를 실제로 태우므로 진짜 로거를 준다
                # (다른 테스트들이 넘기는 `WarningThrottle(60.0)`은 성공 경로 전용이다).
                WarningThrottle(logging.getLogger("mahdi.main"), 60.0), deadline=clock[0] + 50.0,
            )
        )

    assert any_strikes and rows == [], "전 레그 실패는 rows=0으로 끝난다(구독은 있었다)"


# ===== 2026-08-05 §2-8 — 종가 단일가 구간 인지 =====


def _ofi_less_inputs():
    from mahdi.fusion.signal_layer import SignalInputs

    return SignalInputs(ofi=None, queue_imbalance=None)


def test_missing_ofi_during_the_closing_auction_is_reported_as_market_structure():
    """15:35~15:45에 OFI가 없는 것은 **결함이 아니라 시장 구조**다 — 사유가 그렇게 남아야 한다.

    08-05 15:36~15:44: `orderflow_ofi_vpin`이 죽어 앙상블이 4 → 3으로 얇아졌는데 사유가
    `ofi/queue_imbalance 없음`으로만 남아 장애와 구분되지 않았다. 08-04 §2-10이
    *"종가 형성 구간이라 가치가 높다"* 고 판정한 바로 그 구간이다.
    """
    from mahdi.main import MEMBER_UNAVAILABLE_CLOSING_AUCTION, _member_unavailable_reasons

    reasons = _member_unavailable_reasons(_ofi_less_inputs(), now=datetime(2026, 8, 5, 15, 36))
    assert reasons["orderflow_ofi_vpin"] == MEMBER_UNAVAILABLE_CLOSING_AUCTION


def test_missing_ofi_during_continuous_trading_is_still_reported_as_missing_data():
    """연속거래 중의 OFI 부재는 여전히 **문제**다 — 단일가 예외가 그것까지 덮으면 안 된다."""
    from mahdi.main import _member_unavailable_reasons

    reasons = _member_unavailable_reasons(_ofi_less_inputs(), now=datetime(2026, 8, 5, 14, 0))
    assert reasons["orderflow_ofi_vpin"] == "ofi/queue_imbalance 없음"


def test_the_closing_auction_exception_does_not_resurrect_the_member():
    """사유만 가른다 — **멤버를 살리지는 않는다.**

    없는 값을 지어내는 것과 왜 없는지 아는 것은 다르다. 지어내면 08-03의 허수 감마플립과
    같은 종류의 결함이 된다.
    """
    from mahdi.main import _member_unavailable_reasons

    reasons = _member_unavailable_reasons(_ofi_less_inputs(), now=datetime(2026, 8, 5, 15, 40))
    assert "orderflow_ofi_vpin" in reasons, "사유가 남았다는 것은 곧 미가용이라는 뜻이다"


# ===== 2026-08-05 고도화#4 — 멤버별 점수 적재 =====


def _SignalInputsWithEverything():
    """구현된 4멤버가 **전부** 점수를 내는 원재료 한 벌.

    (미학습 2멤버 xgboost/lstm은 Phase 3까지 항상 None이라 반드시 사유 쪽에 남는다.)
    """
    from mahdi.engines.regime import RegimeLabel, RegimeState
    from mahdi.fusion.signal_layer import SignalInputs

    return SignalInputs(
        regime_state=RegimeState(
            regime=RegimeLabel.TREND_UP_STRONG,
            prob_vector=(0.6, 0.1, 0.1, 0.05, 0.05, 0.05, 0.03, 0.02),
            stability_flag=True,
        ),
        gex=1.0e9,
        gamma_wall=1000.0,
        spot=1010.0,
        ofi=0.4,
        foreign_net_flow=1.2e9,
    )


def test_member_scores_and_unavailable_reasons_partition_the_six_members():
    """두 dict는 **배타적이고 합치면 항상 6멤버**다 — 하나가 비면 다른 하나가 설명한다.

    08-05까지는 가용성만 남기고 점수를 버렸다. 그래서 판단이 살아난 그날(가용 멤버 4가 409분,
    전이 83회) **방향 ±0.692가 어느 멤버에서 왔는지 DB로 역산할 수 없었다.**
    """
    from mahdi.fusion.signal_layer import MEMBER_FIELDS, build_member_scores
    from mahdi.main import _member_scores_for_record, _member_unavailable_reasons

    inputs = _SignalInputsWithEverything()
    scores = build_member_scores(inputs)
    recorded = _member_scores_for_record(scores)
    reasons = _member_unavailable_reasons(inputs, datetime(2026, 8, 5, 14, 0), scores)

    assert set(recorded) & set(reasons) == set(), "한 멤버가 양쪽에 다 있으면 해석이 불가능하다"
    assert set(recorded) | set(reasons) == set(MEMBER_FIELDS)


def test_member_scores_record_is_empty_when_the_engine_gave_none():
    """엔진이 점수를 안 넘긴 경로(구버전 호출/테스트 더블)에서도 죽지 않는다."""
    from mahdi.main import _member_scores_for_record

    assert _member_scores_for_record(None) == {}


def test_fusion_decision_carries_the_scores_it_already_computed():
    """엔진은 이 점수를 **이미 계산하고 있었고 버리고 있었다** — 이제 판단 행까지 흘려보낸다."""
    from mahdi.fusion.engine import MetaLabelContext, SignalFusionEngine

    decision = SignalFusionEngine().evaluate(_SignalInputsWithEverything(), MetaLabelContext())

    assert decision.member_scores is not None
    assert decision.available_member_count == sum(
        1 for name in ("regime_hmm", "options_flow", "orderflow_ofi_vpin", "flow_position")
        if getattr(decision.member_scores, name) is not None
    ), "적재되는 점수와 available_member_count가 같은 계산에서 나와야 한다"


# ===== 2026-08-06 §2-1 / Fix#2 — 관측 루프 생존 신호 =====


def test_poll_process_heartbeat_writes_a_fresh_file(tmp_path, monkeypatch):
    """이 파일의 나이가 곧 "이벤트 루프가 마지막으로 스케줄을 돌린 시각"이다."""
    from mahdi import liveness
    import mahdi.main as main_module

    monkeypatch.setattr(main_module, "LOG_DIR", tmp_path)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 10, 4))

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(main_module.poll_process_heartbeat(interval_seconds=30))

    beat = liveness.read_heartbeat(liveness.heartbeat_path(tmp_path))
    assert beat["at"] == datetime(2026, 8, 6, 10, 4)
    assert beat["beats"] == 1


def test_poll_process_heartbeat_survives_a_write_failure(tmp_path, monkeypatch):
    """생존 신호를 못 썼다고 관측이 멈추면 안 된다 — 못 쓰면 파일이 늙고 워치독이 그것을 본다."""
    import mahdi.main as main_module

    monkeypatch.setattr(main_module, "LOG_DIR", tmp_path)
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: datetime(2026, 8, 6, 10, 4))
    monkeypatch.setattr(
        "mahdi.main.liveness.write_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(OSError("디스크 가득")),
    )

    async def fake_sleep(seconds):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("mahdi.main.asyncio.sleep", fake_sleep)
    # write_heartbeat이 내부에서 삼키는 계약이므로, 여기서 터지면 그 계약이 깨진 것이다.
    with pytest.raises(OSError):
        _run(main_module.poll_process_heartbeat(interval_seconds=30))


def test_main_registers_the_process_heartbeat_task():
    """gather 목록에서 빠지면 이 fix 전체가 조용히 없는 것과 같다.

    소스를 읽어 확인하는 이유: `main()`은 KIS 연결부터 시작해 테스트에서 통째로 돌릴 수 없다.
    """
    import inspect

    import mahdi.main as main_module

    source = inspect.getsource(main_module.main)
    assert "poll_process_heartbeat()" in source


# ===== 2026-08-06 고도화#1 — 먼슬리 레그 재시도 =====
#
# 08-06 실측으로 원래 계획을 정정했다: 먼슬리를 얇게 만든 것은 예산 컷이 아니라 **레그 단위
# 타임아웃**이다(먼슬리 10레그 미만 128분 vs 예산 컷이 먼슬리에 닿은 분 3분, 체인 레그 실패
# 119건 중 111건이 ReadTimeout). 순서는 이미 먼슬리 우선이었다.


class _FailFirstThenSucceed:
    """첫 호출만 실패하고 두 번째부터 성공하는 클라이언트 — 재시도의 효과를 격리해서 본다."""

    rate_limit_total_calls = 0

    def __init__(self, resp: dict, fail_symbols: set[str]) -> None:
        self._resp = resp
        self._fail_once = set(fail_symbols)
        self.calls: list[str] = []

    def get_quote(self, symbol: str, market_div_code: str | None = None) -> dict:
        self.calls.append(symbol)
        self.rate_limit_total_calls += 1
        if symbol in self._fail_once:
            self._fail_once.discard(symbol)
            raise RuntimeError("read timeout")
        return self._resp


def test_collect_reports_which_monthly_legs_were_missed():
    """실패한 레그를 **돌려줘야** 호출측이 그것만 다시 부를 수 있다."""
    from mahdi.main import _collect_option_chain_cycle

    client = _FailFirstThenSucceed(_OPTION_QUOTE_FIXTURE, {"SYM1000C"})
    books = [(_FakeManagerManyStrikes(frozenset({1000.0, 1002.5})), "regular")]
    rows, _spot, _any, missing = _run(
        _collect_option_chain_cycle(
            client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 6, 10, 0),
            WarningThrottle(logging.getLogger("mahdi.main"), 60.0),
        )
    )
    assert len(rows) == 3
    assert missing == [(1000.0, "C")]


def test_weekly_leg_failures_are_not_queued_for_retry():
    """위클리는 핀 리스크 전용이다 — 전 북을 재시도하면 총 호출이 배가 되어
    방금 고친 EGW00201/백오프를 되살린다."""
    from mahdi.main import _collect_option_chain_cycle

    client = _FailFirstThenSucceed(_OPTION_QUOTE_FIXTURE, {"SYM1000C"})
    books = [(_FakeManagerManyStrikes(frozenset({1000.0})), "weekly_mon")]
    _rows, _spot, _any, missing = _run(
        _collect_option_chain_cycle(
            client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 6, 10, 0),
            WarningThrottle(logging.getLogger("mahdi.main"), 60.0),
        )
    )
    assert missing == []


def test_retry_recovers_the_missed_monthly_legs():
    from mahdi.main import _retry_priority_legs

    client = _FailFirstThenSucceed(_OPTION_QUOTE_FIXTURE, set())
    recovered, spot, attempted = _run(
        _retry_priority_legs(
            client, [(1000.0, "C"), (1002.5, "P")], _FakeMaster(), "KOSPI200",
            datetime(2026, 8, 6, 10, 0), WarningThrottle(logging.getLogger("mahdi.main"), 60.0),
            deadline=None,
        )
    )
    assert attempted == 2
    assert len(recovered) == 2
    assert spot is not None


def test_retry_stops_at_the_cycle_deadline(monkeypatch):
    """재시도가 예산을 넘기면 다음 분이 밀린다 — 08-04 Fix#8이 막으려던 바로 그 일이다."""
    from mahdi.main import _retry_priority_legs

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])

    class _Slow(_FailFirstThenSucceed):
        def get_quote(self, symbol, market_div_code=None):
            clock[0] += 4.0
            return super().get_quote(symbol, market_div_code)

    client = _Slow(_OPTION_QUOTE_FIXTURE, set())
    recovered, _spot, attempted = _run(
        _retry_priority_legs(
            client, [(1000.0, "C"), (1002.5, "C"), (1005.0, "C")], _FakeMaster(), "KOSPI200",
            datetime(2026, 8, 6, 10, 0), WarningThrottle(logging.getLogger("mahdi.main"), 60.0),
            deadline=clock[0] + 5.0,
        )
    )
    assert attempted == 2  # 1000.0(4초) → 1002.5(4초, 아직 예산 안) → 세 번째는 예산 초과
    assert len(recovered) == 2


def test_retry_is_capped_so_a_wider_window_cannot_grow_it_silently():
    from mahdi.main import _retry_priority_legs, OPTION_CHAIN_PRIORITY_RETRY_MAX_LEGS

    client = _FailFirstThenSucceed(_OPTION_QUOTE_FIXTURE, set())
    missing = [(1000.0 + i, "C") for i in range(OPTION_CHAIN_PRIORITY_RETRY_MAX_LEGS + 5)]
    _recovered, _spot, attempted = _run(
        _retry_priority_legs(
            client, missing, _FakeMaster(), "KOSPI200", datetime(2026, 8, 6, 10, 0),
            WarningThrottle(logging.getLogger("mahdi.main"), 60.0), deadline=None,
        )
    )
    assert attempted == OPTION_CHAIN_PRIORITY_RETRY_MAX_LEGS


def test_retry_failures_do_not_log_a_second_time():
    """같은 레그로 두 줄을 남기면 08-04 §2-2의 로그 폭증을 우리 손으로 되살린다."""
    from mahdi.main import _retry_priority_legs

    class _AlwaysFails:
        rate_limit_total_calls = 0

        def get_quote(self, symbol, market_div_code=None):
            raise RuntimeError("read timeout")

    import logging as _logging
    caplog_logger = _logging.getLogger("mahdi.main")
    records: list = []
    handler = _logging.Handler()
    handler.emit = records.append
    caplog_logger.addHandler(handler)
    try:
        _recovered, _spot, attempted = _run(
            _retry_priority_legs(
                _AlwaysFails(), [(1000.0, "C")], _FakeMaster(), "KOSPI200",
                datetime(2026, 8, 6, 10, 0), WarningThrottle(caplog_logger, 60.0), deadline=None,
            )
        )
    finally:
        caplog_logger.removeHandler(handler)
    assert attempted == 1
    assert records == []


def test_priority_retry_calls_are_counted_as_our_own_not_another_pollers():
    """07-28에 밀림 원인을 특정한 계측이다 — 재시도 콜을 남의 몫으로 세면 그 계측이 오염된다."""
    import inspect

    import mahdi.main as main_module

    source = inspect.getsource(main_module.poll_option_chain)
    assert "+ priority_retry_calls" in source


# ===== 2026-08-06 고도화#4 — 혼잡 시간대 위클리 감축 레버(기본 OFF) =====


def _due_series(poll_time: datetime) -> list[str]:
    from mahdi.main import _books_due_this_cycle

    books = [
        (_FakeManagerManyStrikes(frozenset({1000.0})), "regular"),
        (_FakeManagerManyStrikes(frozenset({1000.0})), "weekly_mon"),
        (_FakeManagerManyStrikes(frozenset({1000.0})), "weekly_thu"),
    ]
    return [series for _m, series in _books_due_this_cycle(books, poll_time)]


def test_the_congestion_lever_is_down_by_default():
    """**레버는 있고, 내려져 있다.** 기본 동작이 08-06 이전과 1비트도 달라지면 안 된다."""
    from mahdi.main import OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS

    assert OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS == {}
    assert _due_series(datetime(2026, 8, 7, 10, 0)) == ["regular", "weekly_mon"]
    assert _due_series(datetime(2026, 8, 7, 10, 1)) == ["regular", "weekly_thu"]


def test_pulling_the_lever_halves_weekly_polling_in_that_hour_only(monkeypatch):
    monkeypatch.setattr("mahdi.main.OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS", {10: 4})
    # 10시: 4분 주기 — mod4가 위상과 같은 분에만 위클리가 붙는다.
    assert _due_series(datetime(2026, 8, 7, 10, 0)) == ["regular", "weekly_mon"]
    assert _due_series(datetime(2026, 8, 7, 10, 1)) == ["regular", "weekly_thu"]
    assert _due_series(datetime(2026, 8, 7, 10, 2)) == ["regular"]
    assert _due_series(datetime(2026, 8, 7, 10, 3)) == ["regular"]
    # 11시는 규칙 밖 — 종전 2분 주기 그대로다.
    assert _due_series(datetime(2026, 8, 7, 11, 2)) == ["regular", "weekly_mon"]


def test_the_lever_never_touches_the_monthly_book(monkeypatch):
    """먼슬리는 GEX/감마플립의 유일한 입력이다 — 어느 시간대에도 매 분 돈다."""
    monkeypatch.setattr("mahdi.main.OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS", {h: 10 for h in range(24)})
    for minute in range(10):
        assert "regular" in _due_series(datetime(2026, 8, 7, 13, minute))


# ---------------------------------------------------------------------------
# 2026-08-10 — ATM IV 구형파. `_update_atm_iv`가 북을 섞어 평균 내는 바람에 위클리 격분 폴링이
# `iv_chg`를 분 단위 진동으로 만들었고, 그것을 학습한 HMM이 "짝수분/홀수분"을 레짐으로 재현했다.
# ---------------------------------------------------------------------------


class _IvRecorder:
    def __init__(self):
        self.ivs = []

    def update_iv(self, atm_iv):
        self.ivs.append(atm_iv)


def _iv_chain_row(expiry, strike, option_type, iv):
    return {"expiry": expiry, "strike": strike, "option_type": option_type, "iv": iv}


def test_update_atm_iv_uses_only_the_monthly_book():
    from mahdi.main import _update_atm_iv

    monthly, weekly = date(2026, 8, 13), date(2026, 8, 10)
    rows = [
        _iv_chain_row(monthly, 980.0, "C", 0.74), _iv_chain_row(monthly, 980.0, "P", 0.74),
        _iv_chain_row(weekly, 980.0, "C", 0.32), _iv_chain_row(weekly, 980.0, "P", 0.32),
    ]
    recorder = _IvRecorder()
    _update_atm_iv(recorder, rows, latest_spot=980.0)

    assert recorder.ivs == [0.74], "위클리 IV가 섞이면 격분마다 값이 달라진다"


def test_update_atm_iv_is_stable_across_the_weekly_polling_cadence():
    """격분 폴링을 그대로 재현 — 짝수분엔 위클리가 있고 홀수분엔 없다.

    08-10 실측: 짝수분 평균 IV 0.5285 / 홀수분 0.7387. 그 격차가 iv_chg 구형파의 원인이었다.
    """
    from mahdi.main import _update_atm_iv

    monthly, weekly = date(2026, 8, 13), date(2026, 8, 10)
    recorder = _IvRecorder()
    for minute in range(8):
        rows = [_iv_chain_row(monthly, 980.0, "C", 0.74), _iv_chain_row(monthly, 980.0, "P", 0.74)]
        if minute % 2 == 0:  # 위클리는 격분에만 조회된다(_books_due_this_cycle)
            rows += [_iv_chain_row(weekly, 980.0, "C", 0.32), _iv_chain_row(weekly, 980.0, "P", 0.32)]
        _update_atm_iv(recorder, rows, latest_spot=980.0)

    assert len(set(recorder.ivs)) == 1, f"ATM IV가 폴링 격자를 따라 진동한다: {recorder.ivs}"


def test_update_atm_iv_skips_rows_without_expiry():
    from mahdi.main import _update_atm_iv

    recorder = _IvRecorder()
    _update_atm_iv(recorder, [{"strike": 980.0, "iv": 0.5}], latest_spot=980.0)
    assert recorder.ivs == []


# =====================================================================================
# 2026-08-12 Fix#2/#3/#4/#5 — 08-12 사고의 사슬을 끊는 넷
# =====================================================================================
#
# 그날의 인과는 하나로 이어져 있었다:
#   WS가 31회 끊겼다 → 재연결마다 `run_observation_loop`가 처음부터 돌았다
#   → 그 첫 줄의 무방비 `get_quote`가 31번 노출됐다 → 31번째에 KIS 500을 만나 프로세스가 죽었다
# 그리고 같은 재연결이 조용히 레짐 30분을 지웠다(봉 핸들러의 순서 때문에).


class _FakeRestClientThatFailsTheOpeningQuote:
    """기동/재연결 진입부의 스팟 조회만 실패하는 KIS — 08-12 10:10:06의 재현."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def get_quote(self, symbol: str, market_div_code: str) -> dict:
        self.calls += 1
        raise self._exc


def _run_observation_loop_with(monkeypatch, rest_client, incoming=None):
    """`run_observation_loop`을 한 번 돌린다. WS 픽스처가 소진되면 ConnectionError로 끝난다."""
    conn = FakeConnection(incoming or [])
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)
    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )
    return manager


def test_a_kis_500_on_the_opening_quote_no_longer_kills_the_process(monkeypatch, caplog):
    """**08-12 10:10:06 재현.** 진입부 스팟 조회의 HTTP 오류가 밖으로 새면 안 된다.

    그날 이 예외는 `run_observation_loop_forever`의 `except _WS_DISCONNECT_ERRORS`를 통과해
    `asyncio.gather`까지 올라갔고 **모든 폴러 태스크가 함께 죽었다**(결손 5분).
    되돌리면(try/except 제거) 이 테스트가 HTTPStatusError로 깨진다.
    """
    error = httpx.HTTPStatusError(
        "Server error '500'", request=httpx.Request("GET", "https://kis/inquire-price"),
        response=httpx.Response(500),
    )
    rest_client = _FakeRestClientThatFailsTheOpeningQuote(error)

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        manager = _run_observation_loop_with(monkeypatch, rest_client)

    assert rest_client.calls == 1
    # 스팟이 없으면 롤링을 건너뛴다 — 그 분기는 이 fix 이전부터 있었다(`if spot > 0`).
    assert not manager.desired_strikes
    assert [r for r in caplog.records if "기동 스팟 조회 실패" in r.getMessage()]


def test_a_read_timeout_on_the_opening_quote_is_survivable_too(monkeypatch):
    """`ReadTimeout`도 같은 취급 — 08-12에 하루 346건 났고 어느 것이든 재연결 직후일 수 있다."""
    rest_client = _FakeRestClientThatFailsTheOpeningQuote(httpx.ReadTimeout("timed out"))
    _run_observation_loop_with(monkeypatch, rest_client)  # 예외가 밖으로 새면 여기서 실패한다


def test_a_non_http_error_on_the_opening_quote_still_propagates(monkeypatch):
    """**넓게 잡지 않는다.** 07-19가 지키려던 것(코드/설정 오류는 사람이 본다)은 그대로다.

    `except Exception`으로 바꾸면 이 테스트가 깨진다 — 그것이 이 테스트의 목적이다.
    """
    rest_client = _FakeRestClientThatFailsTheOpeningQuote(ValueError("설정 오류"))
    conn = FakeConnection([])
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    with pytest.raises(ValueError, match="설정 오류"):
        _run(
            run_observation_loop(
                ws_client, [manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )


def test_regime_is_written_even_when_the_reroll_hits_a_dead_socket(monkeypatch):
    """**08-12 §2-2 재현.** 봉은 있는데 레짐이 없는 30분을 만든 순서 문제.

    그날 `market_raw_1m`의 선물봉은 09:13~09:29 17분이 전부 있었는데 `regime_state`는 0분이었다.
    원인은 `적재 → _reroll_books_to_spot(WS I/O) → 레짐` 순서였다 — 끊긴 소켓에서 재롤링이
    예외를 던지면 레짐이 **도달조차 못 한다.**

    순서를 되돌리면 이 테스트가 «레짐 0건»으로 깨진다.
    """
    import websockets

    incoming = [
        _make_h0ifcnt0("090000", 350.0, 10, 350.05, 349.95, 100, 100),
        _make_h0ifcnt0("090100", 351.0, 8, 351.05, 350.95, 100, 100),  # 다음 분 → flush 트리거
    ]
    written_regimes = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    async def dead_socket_reroll(*args, **kwargs):
        raise websockets.exceptions.ConnectionClosedError(None, None)

    monkeypatch.setattr("mahdi.main.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.main.db.insert_market_raw_1m", lambda conn, row: None)
    monkeypatch.setattr("mahdi.main.db.insert_regime_state",
                        lambda conn, **kwargs: written_regimes.append(kwargs))
    monkeypatch.setattr("mahdi.main.db.upsert_active_futures_symbol", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main.db.upsert_market_halt_state", lambda *a, **k: None)
    monkeypatch.setattr("mahdi.main._reroll_books_to_spot", dead_socket_reroll)

    conn = FakeConnection(incoming)
    ws_client = KISWebSocketClient(approval_key="APV", connection=conn)
    manager = RollingSubscriptionManager(
        ws_client, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1
    )
    # 재롤링 실패는 **여전히 밖으로 나간다** — 단절 감지를 삼키면 08-12보다 나쁜 고장이 된다.
    with pytest.raises(websockets.exceptions.ConnectionClosedError):
        _run(
            run_observation_loop(
                ws_client, [manager],
                _FakeRestClientThatFailsTheOpeningQuote(httpx.ReadTimeout("timed out")),
                futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
                market_halt_monitor=MarketHaltMonitor(),
            )
        )

    assert len(written_regimes) == 1, (
        "선물봉이 완성됐는데 레짐이 안 남았다 — 재롤링 예외 앞에서 레짐이 확정돼야 한다(Fix#3)."
    )


# --- Fix#4/#5: 조기 포기의 북 우선순위 -------------------------------------------------------


def _abort_with_books(monkeypatch, books, budget=50.0, seconds_per_call=1.0):
    from mahdi.main import _collect_option_chain_cycle

    clock = [1000.0]
    monkeypatch.setattr("mahdi.main.time.monotonic", lambda: clock[0])
    rest_client = _FakeRestClientAlwaysTimingOut(clock, seconds_per_call=seconds_per_call)
    rows, _spot, _any, missing = _run(
        _collect_option_chain_cycle(
            rest_client, books, _FakeMaster(), "KOSPI200", datetime(2026, 8, 12, 10, 15),
            WarningThrottle(logging.getLogger("mahdi.main"), 60.0), deadline=clock[0] + budget,
        )
    )
    return rest_client, rows, missing


def test_timeout_abort_drops_the_weekly_book_before_the_monthly(monkeypatch, caplog):
    """**08-12 §2-6 재현.** 조기 포기가 먼슬리부터 자르던 것을 위클리부터 자르게 바꾼다.

    그날 조기 포기 50회 **전부**가 컷당한북에 `regular`를 담았다. 시간 예산 경로에는 순서가
    있었는데(74회 중 먼슬리에 닿은 것 2회) 조기 포기에는 없었다.

    여기서는 먼슬리 5행사가(10레그) + 위클리 5행사가(10레그)가 전부 타임아웃된다:
      - 3레그째에 1단계가 서서 **위클리 10레그가 통째로 버려지고**
      - 먼슬리는 카운터가 초기화돼 계속 불린다(3레그 더 → 총 6레그 호출).
    되돌리면 호출이 3건에서 멈추고 위클리가 먼슬리와 함께 죽는다.
    """
    books = [
        (_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "regular"),
        (_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "weekly_mon"),
    ]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rest_client, rows, _missing = _abort_with_books(monkeypatch, books)

    assert rows == []
    # 1단계에서 3건, 먼슬리 재개 후 다시 3건 = 6건.
    assert len(rest_client.calls) == 2 * mahdi_main.OPTION_CHAIN_CONSECUTIVE_TIMEOUT_ABORT

    aborts = [r for r in caplog.records if "연속 타임아웃" in r.getMessage()]
    assert len(aborts) == 1  # 여전히 사이클당 1줄
    message = aborts[0].getMessage()
    # 두 북이 다 잘리긴 했다(먼슬리 잔여 + 위클리 전부). **순서가 지켜졌는지**는 컷당한북이
    # 아니라 아래 라벨이 답한다 — 그것이 Fix#5의 존재 이유다.
    assert "데드라인이먼슬리에서끝남=아니오" in message


def test_a_monthly_only_cycle_still_aborts_immediately(monkeypatch, caplog):
    """**Fix#1을 되돌리지 않는다.** 버릴 위클리가 없으면 종전대로 즉시 전부 접는다.

    08-12의 조기 포기 50회 중 26회가 이 형태(홀수분 = 먼슬리 단독)였고, **그 26회는 위반이
    아니다** — 자를 것이 먼슬리밖에 없는 사이클에서 먼슬리를 자르는 것은 순서 문제가 아니다.
    여기서 먼슬리에 두 번째 기회를 주면 Fix#1이 어제 실측으로 포기한 것을 하루 만에 되돌린다.
    """
    books = [(_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "regular")]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        rest_client, rows, missing = _abort_with_books(monkeypatch, books)

    assert len(rest_client.calls) == mahdi_main.OPTION_CHAIN_CONSECUTIVE_TIMEOUT_ABORT
    assert rows == []
    assert len(missing) == 10  # 재시도 경로는 그대로 살아 있다
    message = [r for r in caplog.records if "연속 타임아웃" in r.getMessage()][0].getMessage()
    assert "컷당한북=regular" in message
    assert "데드라인이먼슬리에서끝남=아니오" in message, (
        "먼슬리 단독 사이클의 꼬리 컷을 위반으로 세면 08-12의 오독(priority_cut_minutes=2)이 재현된다."
    )


def test_the_escalation_never_extends_the_time_budget(monkeypatch, caplog):
    """**1단계가 사이클을 늘리지 않는다.** 시간 예산은 스코프와 독립이다.

    조기 포기가 돌려주던 시간을 위클리가 아니라 먼슬리에 쓰는 것이지 예산을 늘리는 것이 아니다 —
    그 구분이 깨지면 08-04 Fix#8이 막은 «다음 분 밀림»이 돌아온다.
    """
    books = [
        (_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "regular"),
        (_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "weekly_mon"),
    ]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        # 레그당 2초 · 예산 5초 → 1단계로 살아난 먼슬리도 시계 앞에서는 즉시 멈춘다.
        rest_client, _rows, _missing = _abort_with_books(
            monkeypatch, books, budget=5.0, seconds_per_call=2.0
        )

    assert len(rest_client.calls) <= 3, "예산을 넘겨서까지 먼슬리를 부르면 안 된다"


def test_the_label_fires_when_the_deadline_ends_inside_the_monthly(monkeypatch, caplog):
    """라벨이 **데드라인 컷**에서는 켜져야 한다 — 안 켜지면 그 축을 아무도 못 본다.

    시간 예산이 먼슬리 도중에 끝나면 아직 안 부른 위클리를 두고 먼슬리가 잘린다. 그것은
    Fix#4(스코프 접기)가 못 막는 종류다 — **예산은 시계이지 스코프가 아니다.**

    ## 2026-08-19 — 이 테스트의 **이름과 해석을 고쳤다**(단언은 그대로다)

    08-18 보고서가 이 라벨의 옛 이름(`우선순위위반`)을 보고 「불변식이 처음 깨졌다」로 읽어
    P1을 잘못 진단했다. 실제로는 `books[0]`이 이미 먼슬리라 위클리는 **언제나 뒤에 있고**,
    데드라인이 먼슬리 구간에서 끝나면 이 조건은 **구조적으로 항상 참**이다. 그 분에 위클리
    레그는 한 건도 안 불렸다 — 먼슬리가 예산 100%를 썼고, 그것은 순서를 **지킨** 결과다.

    그러므로 이 라벨이 답하는 것은 「위반했는가」가 아니라 **「데드라인이 먼슬리에서 끝났는가」**
    이고, 그 사실은 여전히 재야 한다(그 분은 먼슬리가 얇게 끝난 분이다 — 08-18 15:01:52는
    6/10레그였다). 상세 근거는 `mahdi/main.py`의 `LOG_CHAIN_BUDGET_EXCEEDED` 위 주석.
    """
    books = [
        (_FakeManagerManyStrikes(frozenset({995.0, 997.5, 1000.0, 1002.5, 1005.0})), "regular"),
        (_FakeManagerManyStrikes(frozenset({995.0})), "weekly_mon"),
    ]
    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        _collect_with_budget_books(monkeypatch, books, seconds_per_call=10.0, budget=30.0)

    cut = [r for r in caplog.records if "수집 예산" in r.getMessage()]
    assert len(cut) == 1
    assert "데드라인이먼슬리에서끝남=예" in cut[0].getMessage()


# ===== 2026-08-21: 거래소 누적 체결량 필드를 읽는다 =====
#
# KIS 파생 실시간 체결에는 틱 단위 체결구분이 없지만 누적 매수/매도 수량은 있다.
# 출처: docs/efriend 공식 문서 시트 "지수선물 실시간체결가"(idx 41/42) ·
# "지수옵션 실시간체결가"(idx 48/49). 우리가 이미 쓰던 인덱스 6개가 그 문서와 전부 일치한다.


def test_parse_tick_reads_exchange_cumulative_volumes():
    fields = ["0"] * _NUM_FIELDS
    fields[0], fields[1], fields[2], fields[9] = "201S03C325", "093015", "16.25", "7"
    fields[10] = "1500"   # ACML_VOL
    fields[41], fields[42], fields[43], fields[44] = "16.30", "16.20", "40", "55"
    fields[48], fields[49] = "700", "800"  # SELN_CNTG_SMTN / SHNU_CNTG_SMTN

    _, tick = _parse_tick("^".join(fields), today=date(2026, 8, 21))

    assert tick.cum_volume == 1500
    assert tick.cum_buy_volume == 800
    assert tick.cum_sell_volume == 700


def test_parse_futures_tick_reads_exchange_cumulative_volumes():
    fields = ["0"] * _FUT_NUM_FIELDS
    fields[0], fields[1], fields[5], fields[9] = "101S03", "093015", "1080.5", "12"
    fields[10] = "21000"  # ACML_VOL
    fields[34], fields[35], fields[36], fields[37] = "1080.55", "1080.45", "30", "25"
    fields[41], fields[42] = "9000", "12000"  # SELN_CNTG_SMTN / SHNU_CNTG_SMTN

    _, tick = _parse_futures_tick("^".join(fields), today=date(2026, 8, 21))

    assert tick.cum_volume == 21000
    assert tick.cum_buy_volume == 12000
    assert tick.cum_sell_volume == 9000


def test_short_frames_still_produce_a_tick_without_cumulative_fields():
    """누적 필드가 없다고 틱을 버리지 않는다 — `_MIN_FIELDS`를 올리면 그렇게 된다.

    있으면 더 정확한 분류를 주지만, 없다고 체결 자체를 잃는 것은 얻는 것보다 손해다.
    그때는 틱 룰 추정으로 떨어지고 그 사실이 로그에 남는다.
    """
    fields = ["0"] * 45  # 누적 매수/매도(48/49)에 못 미치는 길이
    fields[0], fields[1], fields[2], fields[9] = "201S03C325", "093015", "16.25", "7"
    fields[41], fields[42], fields[43], fields[44] = "16.30", "16.20", "40", "55"

    parsed = _parse_tick("^".join(fields), today=date(2026, 8, 21))

    assert parsed is not None
    assert parsed[1].cum_buy_volume is None
    assert parsed[1].cum_sell_volume is None
