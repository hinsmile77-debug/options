import asyncio

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import RollingSubscriptionManager, strikes_around_atm


class FakeConnection:
    async def send(self, message: str) -> None:
        pass

    async def recv(self) -> str:
        raise ConnectionError("사용되지 않음")

    async def close(self) -> None:
        pass


def _run(coro):
    return asyncio.run(coro)


def test_strikes_around_atm_rounds_to_grid_and_centers():
    strikes = strikes_around_atm(spot=352.3, strike_interval=2.5, strikes_each_side=2)
    assert strikes == [347.5, 350.0, 352.5, 355.0, 357.5]


def test_strikes_around_atm_invalid_interval_raises():
    with pytest.raises(ValueError):
        strikes_around_atm(spot=350, strike_interval=0, strikes_each_side=1)


def test_roll_to_spot_subscribes_initial_range():
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    _run(manager.roll_to_spot(350.0))

    assert manager.desired_strikes == frozenset({347.5, 350.0, 352.5})
    assert len(ws.active_subscriptions) == 6  # 3 strikes x (C,P)


def test_roll_to_spot_moves_window_and_unsubscribes_out_of_range():
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    _run(manager.roll_to_spot(350.0))  # [347.5, 350.0, 352.5]
    _run(manager.roll_to_spot(354.0))  # atm=355.0 → [352.5, 355.0, 357.5]

    assert manager.desired_strikes == frozenset({352.5, 355.0, 357.5})
    active_strikes = {float(key[1][:-1]) for key in ws.active_subscriptions}
    assert active_strikes == {352.5, 355.0, 357.5}
    assert len(ws.active_subscriptions) == 6  # 겹치는 352.5는 유지, 나머지는 롤링


def test_roll_to_spot_is_idempotent_for_unchanged_range():
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    _run(manager.roll_to_spot(350.0))
    sent_after_first = len(ws.active_subscriptions)
    _run(manager.roll_to_spot(350.4))  # 같은 ATM 격자 안에서의 소폭 변동

    assert len(ws.active_subscriptions) == sent_after_first


def test_rebind_resets_desired_strikes_so_next_roll_resubscribes_everything():
    # 2026-07-19 WS 재연결 도입: 재연결로 서버 쪽 구독 상태가 전부 사라졌는데 매니저의
    # desired_strikes만 남아있으면, roll_to_spot()의 diff 로직(위 idempotent 테스트가 보여주듯
    # 겹치는 범위는 아무것도 재전송 안 함)이 새 연결에 아무것도 재구독하지 않는 버그가 생긴다.
    old_ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(old_ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)
    _run(manager.roll_to_spot(350.0))
    assert manager.desired_strikes == frozenset({347.5, 350.0, 352.5})

    new_ws = KISWebSocketClient(approval_key="APV2", connection=FakeConnection())
    manager.rebind(new_ws)
    assert manager.desired_strikes == frozenset()  # rebind 직후엔 "아직 아무것도 구독 안 한 상태"

    _run(manager.roll_to_spot(350.0))  # 같은 스팟이라도 새 연결엔 전부 새로 구독돼야 함

    assert manager.desired_strikes == frozenset({347.5, 350.0, 352.5})
    assert len(new_ws.active_subscriptions) == 6  # 3 strikes x (C,P) 전부 새 연결에 재전송됨
    assert len(old_ws.active_subscriptions) == 6  # 옛 연결 쪽 기록은 그대로(더 이상 안 씀)


def test_roll_to_spot_skips_strikes_with_no_symbol():
    # symbol_formatter가 None을 반환하면(실제 상장 행사가와 그리드가 어긋난 경우) 조용히 건너뛴다.
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())

    def formatter(strike: float, opt: str) -> str | None:
        return None if strike == 350.0 else f"{strike}{opt}"

    manager = RollingSubscriptionManager(
        ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1, symbol_formatter=formatter
    )

    _run(manager.roll_to_spot(350.0))  # [347.5, 350.0, 352.5] → 350.0은 심볼 없음

    assert len(ws.active_subscriptions) == 4  # 347.5(C,P) + 352.5(C,P)만 구독됨
