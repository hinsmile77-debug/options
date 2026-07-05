import asyncio
from contextlib import contextmanager
from datetime import date

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.main import _parse_tick, run_observation_loop

_NUM_FIELDS = 45  # _MIN_FIELDS in mahdi.main (index 0..44)


def _make_h0iocnt0(hhmmss: str, price: float, volume: float, ask: float, bid: float, ask_qty: float, bid_qty: float) -> str:
    """H0IOCNT0 실측 필드 순서에 맞춰 캐럿(^) 구분 메시지를 합성한다 (사용 안 하는 필드는 0)."""
    fields = ["0"] * _NUM_FIELDS
    fields[1] = hhmmss  # BSOP_HOUR
    fields[2] = str(price)  # OPTN_PRPR
    fields[9] = str(volume)  # LAST_CNQN
    fields[41] = str(ask)  # OPTN_ASKP1
    fields[42] = str(bid)  # OPTN_BIDP1
    fields[43] = str(ask_qty)  # ASKP_RSQN1
    fields[44] = str(bid_qty)  # BIDP_RSQN1
    return "^".join(fields)


def _run(coro):
    return asyncio.run(coro)


def test_parse_tick_valid_h0iocnt0_format():
    raw = _make_h0iocnt0("093015", price=350.5, volume=10, ask=350.6, bid=350.4, ask_qty=120, bid_qty=100)
    tick = _parse_tick(raw, today=date(2026, 7, 6))
    assert tick is not None
    assert tick.timestamp.hour == 9 and tick.timestamp.minute == 30 and tick.timestamp.second == 15
    assert tick.price == 350.5
    assert tick.volume == 10
    assert tick.bid_px == 350.4
    assert tick.bid_qty == 100
    assert tick.ask_px == 350.6
    assert tick.ask_qty == 120


def test_parse_tick_invalid_format_returns_none():
    assert _parse_tick("garbage") is None
    assert _parse_tick("1^2") is None


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

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, subscription_manager, rest_client, futures_symbol="101S03", symbol="KOSPI200_OPT"
            )
        )

    assert rest_client.calls == 1
    assert subscription_manager.desired_strikes  # 초기 ATM 구독이 수행됨
    assert len(written_bars) == 1
    bar = written_bars[0]
    assert bar["symbol"] == "KOSPI200_OPT"
    assert bar["open"] == 350.0
    assert bar["close"] == 350.2
    assert len(written_regimes) == 1
