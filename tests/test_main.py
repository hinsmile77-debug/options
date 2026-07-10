import asyncio
from contextlib import contextmanager
from datetime import date, datetime

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.features.options_intel import OptionLeg, calculate_gex
from mahdi.features.orderflow import calculate_vpin
from mahdi.main import (
    _atm_liquidity_window,
    _parse_asking_price_leg,
    _parse_futures_tick,
    _parse_option_quote,
    _parse_tick,
    poll_expiry_liquidity,
    poll_investor_flow,
    poll_option_chain,
    run_observation_loop,
)

_NUM_FIELDS = 45  # _MIN_FIELDS in mahdi.main (index 0..44)
_FUT_NUM_FIELDS = 40  # _FUT_MIN_FIELDS(38) in mahdi.main


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

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
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

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol="101S03",
                regime_state_machine=_FakeRegimeStateMachine(),
            )
        )

    assert len(written_bars) == 1  # 09:00분 봉은 콜만 flush됨(풋은 아직 진행 중인 분이라 미완성)
    call_bar = next(b for b in written_bars if b["symbol"] == "201S03C325")
    assert call_bar["open"] == 60.0
    assert call_bar["close"] == 62.0  # 40.0/41.0(풋) 값이 섞이면 안 됨


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


class _FakeConnWithRollback:
    """단순 object()와 달리 rollback()을 지원 — DB 삽입 실패 시 트랜잭션 복구 경로를 검증한다."""

    def __init__(self):
        self.rollback_calls = 0

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_poll_option_chain_skips_bad_leg_and_continues_after_db_error(monkeypatch):
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

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_option_chain(
                rest_client, [(_FakeSubscriptionManagerWithStrikes(), "regular")], _FakeMaster(), interval_seconds=1
            )
        )

    assert call_count["n"] == 2  # 1개 행사가 x (C, P) 둘 다 시도됨
    assert len(written_rows) == 1  # 첫 레그만 실패, 둘째 레그는 정상 적재됨(루프가 안 죽음)
    assert fake_conn.rollback_calls == 1


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


class _FakeLoop:
    """asyncio.get_running_loop()를 대체 — .time() 호출마다 미리 정한 값을 순서대로 반환한다."""

    def __init__(self, times: list[float]):
        self._times = iter(times)

    def time(self) -> float:
        return next(self._times)


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

    # 1번째 사이클 종료 시각=1000.0 -> next_tick=1000+60=1060(정상 60초 대기 예상).
    # 2번째 사이클 종료 시각=1200.0(가상으로 사이클이 오래 걸려 next_tick 1060을 이미 지나침)
    # -> 밀린 걸 따라잡지 않고 그 시점으로 재기준, delay=0.0이어야 한다.
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

    assert sleep_calls == [60.0, 0.0]  # 정상 사이클은 60초 대기, 밀린 사이클은 따라잡지 않고 즉시 재기준


class _FakeInvestorFlowRestClient:
    """섹터(F001/OC01/OP01)별로 다른 응답을 돌려주고, 지정한 섹터는 예외를 던진다."""

    def __init__(self, responses: dict, failing_sectors: set[str] = frozenset()):
        self._responses = responses
        self._failing_sectors = failing_sectors
        self.calls: list[tuple[str, str]] = []

    def get_investor_flow(self, market_code: str, sector_code: str) -> dict:
        self.calls.append((market_code, sector_code))
        if sector_code in self._failing_sectors:
            raise RuntimeError("KIS 500")
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

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol=futures_symbol,
                regime_state_machine=_FakeRegimeStateMachine(),
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

    with pytest.raises(ConnectionError):
        _run(
            run_observation_loop(
                ws_client, [subscription_manager], rest_client, futures_symbol=futures_symbol,
                regime_state_machine=_FakeRegimeStateMachine(),
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


def test_poll_expiry_liquidity_aggregates_one_row_per_book(monkeypatch):
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

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert call_count["n"] == 2  # 북 2개 각각 1행씩 시도됨
    assert len(written_rows) == 1  # 첫 북만 실패, 둘째 북은 정상 적재됨(루프가 안 죽음)
    assert fake_conn.rollback_calls == 1


def test_poll_expiry_liquidity_waits_startup_offset_before_first_cycle(monkeypatch):
    # 2026-07-09: poll_option_chain과 동시에 기동하면 두 폴러의 정규 사이클이 같은 순간에 겹쳐
    # 공유 레이트리미터 큐가 길어지는 것을 완화하기 위해 최초 사이클을 startup_offset_seconds만큼
    # 지연시킨다 — 지연이 정확히 한 번, 사이클 진입보다 먼저 일어나는지 검증한다.
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

    books = [(_FakeSubscriptionManagerForLiquidity(frozenset()), "regular")]

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(
            poll_expiry_liquidity(
                rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1, startup_offset_seconds=30.0
            )
        )

    # 오프셋 대기가 먼저 일어나고(30.0), 그 뒤에야 사이클 진입 -> 구독 행사가 없어 2초 재확인 경로.
    # fake_sleep이 첫 호출에서 바로 예외를 던지므로 오프셋 대기만 기록되고 루프에 도달하지 못한다.
    assert sleep_calls == [30.0]


def test_poll_expiry_liquidity_default_offset_is_zero_and_skips_wait(monkeypatch):
    # startup_offset_seconds 기본값(main.py 시그니처 기준 0.0)일 때는 기존 동작(오프셋 없이 바로
    # 사이클 진입)을 그대로 유지해야 한다 — main() 밖 호출부(테스트 등)의 하위호환 보장.
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

    books = [(_FakeSubscriptionManagerForLiquidity(frozenset()), "regular")]

    with pytest.raises(RuntimeError, match="stop-loop"):
        _run(poll_expiry_liquidity(rest_client, books, _FakeMasterForLiquidity(), interval_seconds=1))

    assert sleep_calls == [2.0]  # 오프셋 없이 바로 "구독 없음 -> 2초 재확인" 경로
