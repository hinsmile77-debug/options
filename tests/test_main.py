import asyncio
from contextlib import contextmanager
from datetime import date, datetime

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.features.options_intel import OptionLeg, calculate_gex
from mahdi.features.orderflow import calculate_vpin
from mahdi.main import (
    _parse_futures_tick,
    _parse_option_quote,
    _parse_tick,
    poll_investor_flow,
    poll_option_chain,
    run_observation_loop,
)

_NUM_FIELDS = 45  # _MIN_FIELDS in mahdi.main (index 0..44)
_FUT_NUM_FIELDS = 40  # _FUT_MIN_FIELDS(38) in mahdi.main


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
        _run(run_observation_loop(ws_client, subscription_manager, rest_client, futures_symbol="101S03"))

    assert rest_client.calls == 1
    assert subscription_manager.desired_strikes  # 초기 ATM 구독이 수행됨
    assert len(written_bars) == 1
    bar = written_bars[0]
    assert bar["symbol"] == "201S03C325"
    assert bar["open"] == 350.0
    assert bar["close"] == 350.2
    assert len(written_regimes) == 1


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
        _run(run_observation_loop(ws_client, subscription_manager, rest_client, futures_symbol="101S03"))

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
    def option_symbol(self, option_type: str, strike: float, underlying: str = "KOSPI200") -> str | None:
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
                rest_client, _FakeSubscriptionManagerWithStrikes(), _FakeMaster(), interval_seconds=1
            )
        )

    assert len(rest_client.calls) == 2  # 1개 행사가 x (C, P)
    assert len(written_rows) == 2
    assert written_spots == [1333.77]  # 사이클당 한 번만 적재(레그마다 중복 적재 안 함)


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
        _run(run_observation_loop(ws_client, subscription_manager, rest_client, futures_symbol=futures_symbol))

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
        _run(run_observation_loop(ws_client, subscription_manager, rest_client, futures_symbol=futures_symbol))

    assert len(written_bars) == 1
    bar = written_bars[0]
    assert bar["symbol"] == "201S03C325"

    ret1 = (62.0 - 60.0) / 60.0
    ret2 = (58.0 - 62.0) / 62.0
    expected_vpin = calculate_vpin([ret1, ret2], [55.0, 55.0])
    assert expected_vpin > 0
    assert bar["vpin"] == pytest.approx(expected_vpin)
