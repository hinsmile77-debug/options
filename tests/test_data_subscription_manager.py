import asyncio

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import (
    RollingSubscriptionManager,
    should_roll_atm,
    strikes_around_atm,
)


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


# ===== ATM 롤링 히스테리시스 (2026-08-04 운영점검보고서 §2-2 / Fix#6) =====


def test_should_roll_atm_always_rolls_when_no_window_yet():
    # 기동 직후/재연결 직후엔 창이 없다 — 반드시 롤링해야 한다(안 하면 구독이 하나도 안 나간다).
    assert should_roll_atm(None, spot=1001.0, strike_interval=2.5) is True


def test_should_roll_atm_holds_the_window_inside_the_hysteresis_band():
    # 08-04 실측 왕복 패턴: ATM 1000.0에서 스팟이 1001.25(격자 중점) 근처를 오갔다.
    # 중점을 넘었어도 임계(2.5 x 0.75 = 1.875) 안이면 창을 유지한다.
    assert should_roll_atm(1000.0, spot=1001.25, strike_interval=2.5) is False
    assert should_roll_atm(1000.0, spot=1001.8, strike_interval=2.5) is False
    assert should_roll_atm(1000.0, spot=998.2, strike_interval=2.5) is False


def test_should_roll_atm_moves_once_the_spot_really_leaves():
    assert should_roll_atm(1000.0, spot=1002.0, strike_interval=2.5) is True
    assert should_roll_atm(1000.0, spot=997.5, strike_interval=2.5) is True


def test_should_roll_atm_does_not_roll_when_the_rounded_grid_point_is_unchanged():
    """임계는 넘었지만 반올림 결과가 같은 칸이면 구독 변경이 없다 — 헛도는 롤링을 만들지 않는다.

    기본 비율(0.75)에서는 이 분기가 **도달 불가능**하다: `round()`는 항상 가장 가까운 격자점을
    주므로 `|spot - nearest| <= interval/2`이고, 임계 `interval x 0.75`를 넘은 스팟은 이미
    다른 칸에 더 가깝다. 비율을 0.5 이하로 낮췄을 때만 의미가 생기므로 그 조건으로 검증한다
    (가드를 지우면 낮은 비율에서 "구독은 그대로인데 롤링했다"는 로그가 다시 생긴다).
    """
    assert should_roll_atm(1000.0, spot=1001.0, strike_interval=2.5, hysteresis_ratio=0.3) is False
    assert should_roll_atm(1000.0, spot=1001.4, strike_interval=2.5, hysteresis_ratio=0.3) is True


def test_roll_to_spot_does_not_thrash_on_an_oscillating_spot():
    """회귀 방지 §2-2: 08-04에 롤링 194회 중 70회(36%)가 즉시 왕복이었다.

    격자 중점을 사이에 두고 스팟이 진동해도 구독은 한 번만 나가야 한다.
    """
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    _run(manager.roll_to_spot(1000.0))
    baseline = dict(ws.active_subscriptions)

    for spot in (1001.3, 999.1, 1001.4, 998.9, 1001.2):  # 08-04 08:48~08:53 패턴
        _run(manager.roll_to_spot(spot))

    assert manager.current_atm == 1000.0
    assert dict(ws.active_subscriptions) == baseline


def test_roll_to_spot_still_follows_a_genuine_trend():
    # 히스테리시스가 추세 이동까지 막으면 08-03 §2-2(하루 종일 5.5% 외가격)로 되돌아간다.
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    _run(manager.roll_to_spot(1000.0))
    for spot in (1002.6, 1005.1, 1007.6):
        _run(manager.roll_to_spot(spot))

    assert manager.current_atm == 1007.5
    assert manager.desired_strikes == frozenset({1005.0, 1007.5, 1010.0})


def test_rebind_clears_the_hysteresis_anchor_too():
    """회귀 방지: `_desired_strikes`만 비우고 `_current_atm`을 남기면, 재연결 직후
    `should_roll_atm()`이 "이미 그 ATM이다"로 판정해 **새 연결에 구독을 하나도 안 보낸다** —
    2026-07-19에 `rebind()`가 막으려던 상태를 히스테리시스가 다른 경로로 다시 만든다."""
    old_ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(old_ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)
    _run(manager.roll_to_spot(1000.0))

    new_ws = KISWebSocketClient(approval_key="APV2", connection=FakeConnection())
    manager.rebind(new_ws)
    assert manager.current_atm is None

    _run(manager.roll_to_spot(1000.0))  # 같은 스팟 = 같은 ATM이지만 새 연결엔 전부 나가야 한다
    assert len(new_ws.active_subscriptions) == 6


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
