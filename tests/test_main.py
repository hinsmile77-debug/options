import asyncio
from contextlib import contextmanager
from datetime import datetime

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.main import _parse_tick, run_observation_loop


def _run(coro):
    return asyncio.run(coro)


def test_parse_tick_valid_pipe_format():
    raw = "350.5^10^350.4^100^350.6^120"
    tick = _parse_tick(raw)
    assert tick is not None
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

    def get_option_chain(self, underlying_code: str) -> dict:
        self.calls += 1
        return {"output": {"stck_prpr": str(self._spot)}}


class _FakeClock:
    """_parse_tick의 datetime.now() 호출을 미리 정한 시퀀스로 대체해, 실제 벽시계 시각에
    의존하지 않고 분(minute) 롤오버를 결정론적으로 재현한다."""

    def __init__(self, sequence: list[datetime]):
        self._iter = iter(sequence)

    def now(self) -> datetime:
        return next(self._iter)


def test_run_observation_loop_writes_bar_and_regime_on_minute_rollover(monkeypatch):
    # 09:00:00~09:00:20 3틱 → 09:01:00 진입 틱으로 flush 유도
    monkeypatch.setattr(
        "mahdi.main.datetime",
        _FakeClock(
            [
                datetime(2026, 7, 5, 9, 0, 0),
                datetime(2026, 7, 5, 9, 0, 10),
                datetime(2026, 7, 5, 9, 0, 20),
                datetime(2026, 7, 5, 9, 1, 0),
            ]
        ),
    )
    incoming = [
        "350.0^10^349.95^100^350.05^100",
        "350.5^20^350.45^100^350.55^100",
        "350.2^5^350.15^100^350.25^100",
        "351.0^8^350.95^100^351.05^100",  # 다음 분 틱 (버킷 롤오버 트리거) 이후 recv() 소진 → ConnectionError
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
                ws_client, subscription_manager, rest_client, underlying_code="201", symbol="KOSPI200_OPT"
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
