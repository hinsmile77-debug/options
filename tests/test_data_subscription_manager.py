import asyncio

import pytest

from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.data.subscription_manager import (
    SUBSCRIPTION_RESERVED_SLOTS,
    RollingSubscriptionManager,
    SubscriptionRetentionPool,
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


# ==========================================================================================
# 2026-08-07(운영점검 §B-1 / 고도화#1) — 구독 유지 풀
# ==========================================================================================


def _pool_manager(strikes_each_side=1, reserved_slots=0):
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    pool = SubscriptionRetentionPool(ws, reserved_slots=reserved_slots)
    manager = RollingSubscriptionManager(
        ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=strikes_each_side,
        retention_pool=pool,
    )
    return ws, pool, manager


def test_retention_pool_keeps_subscriptions_alive_when_the_window_moves():
    """창을 벗어나도 슬롯이 남으면 해제하지 않는다 — 그 종목의 1분봉이 끊기지 않는다.

    08-07 실측: BAFBRW980이 11:35:00 해제 → 11:36:59 재구독 → 11:42:59 또 해제됐고,
    COCKPIT Flow Radar의 「봉 없음 8분」이 그 구간이다.
    """
    ws, pool, manager = _pool_manager()

    _run(manager.roll_to_spot(350.0))          # 347.5 / 350.0 / 352.5
    _run(manager.roll_to_spot(355.0))          # 352.5 / 355.0 / 357.5

    assert manager.desired_strikes == frozenset({352.5, 355.0, 357.5})
    # 창 밖으로 나간 347.5/350.0은 여전히 구독 중이다.
    assert {key for _tr, key in ws.active_subscriptions} >= {"347.5C", "347.5P", "350.0C", "350.0P"}
    assert {sub.tr_key for sub in pool.retained} == {"347.5C", "347.5P", "350.0C", "350.0P"}


def test_immediate_round_trip_sends_no_websocket_traffic():
    """즉시 왕복(08-07 76회 중 24회, 31.6%)이 재구독도 봉 공백도 만들지 않아야 한다."""
    ws, pool, manager = _pool_manager()

    _run(manager.roll_to_spot(350.0))
    _run(manager.roll_to_spot(355.0))
    before = frozenset(ws.active_subscriptions)

    _run(manager.roll_to_spot(350.0))          # A -> B -> A

    assert manager.desired_strikes == frozenset({347.5, 350.0, 352.5})
    # 구독 집합이 1비트도 안 바뀐다 — 되돌아온 행사가는 끊긴 적이 없다.
    assert frozenset(ws.active_subscriptions) == before
    assert {sub.tr_key for sub in pool.retained} == {"355.0C", "355.0P", "357.5C", "357.5P"}


def test_retention_pool_evicts_oldest_first_when_slots_run_out():
    """슬롯이 모자라면 **가장 오래 창 밖에 있던 것**부터 버린다(LRU)."""
    ws, pool, manager = _pool_manager()
    ws.MAX_SUBSCRIPTIONS = 8

    _run(manager.roll_to_spot(350.0))          # 347.5/350.0/352.5 = 6슬롯
    _run(manager.roll_to_spot(355.0))          # 352.5/355.0/357.5, 신규 4 → 6+4=10 > 8이라 축출 필요

    assert len(ws.active_subscriptions) <= ws.MAX_SUBSCRIPTIONS
    live = {key for _tr, key in ws.active_subscriptions}
    assert {"352.5C", "352.5P", "355.0C", "355.0P", "357.5C", "357.5P"} <= live
    # 347.5(먼저 창을 벗어난 쪽)가 350.0보다 먼저 버려진다.
    assert "347.5C" not in live


def test_retention_pool_reserves_headroom_for_the_next_roll():
    """예약 슬롯이 있으면 그만큼은 항상 비워 둔다 — 다음 롤의 신규 구독이 ValueError를 안 낸다."""
    ws, pool, manager = _pool_manager(reserved_slots=4)
    ws.MAX_SUBSCRIPTIONS = 12

    _run(manager.roll_to_spot(350.0))
    _run(manager.roll_to_spot(355.0))

    assert ws.MAX_SUBSCRIPTIONS - len(ws.active_subscriptions) >= 4


def test_rebind_clears_the_retention_pool():
    """재연결은 서버 쪽 구독을 전부 날린다 — 유지 큐를 안 비우면 그 행사가가 영영 재등록되지 않는다."""
    ws, pool, manager = _pool_manager()
    _run(manager.roll_to_spot(350.0))
    _run(manager.roll_to_spot(355.0))
    assert pool.retained

    new_ws = KISWebSocketClient(approval_key="APV2", connection=FakeConnection())
    manager.rebind(new_ws)

    assert pool.retained == ()
    # 새 연결에 창 전체가 처음부터 다시 등록된다.
    _run(manager.roll_to_spot(355.0))
    assert {key for _tr, key in new_ws.active_subscriptions} == {
        "352.5C", "352.5P", "355.0C", "355.0P", "357.5C", "357.5P",
    }


def test_without_a_pool_the_old_unsubscribe_behaviour_is_unchanged():
    """풀을 안 주면 종전대로 즉시 해제한다 — 백테스트/기존 호출 경로가 그대로 돈다."""
    ws = KISWebSocketClient(approval_key="APV", connection=FakeConnection())
    manager = RollingSubscriptionManager(ws, tr_id="H0IOCNT0", strike_interval=2.5, strikes_each_side=1)

    _run(manager.roll_to_spot(350.0))
    _run(manager.roll_to_spot(355.0))

    assert {key for _tr, key in ws.active_subscriptions} == {
        "352.5C", "352.5P", "355.0C", "355.0P", "357.5C", "357.5P",
    }


def test_retained_subscriptions_never_widen_the_rest_polling_set():
    """유지 구독은 REST 예산을 1콜도 안 늘린다 — 체인 폴링 대상은 `desired_strikes`다."""
    ws, pool, manager = _pool_manager()

    _run(manager.roll_to_spot(350.0))
    _run(manager.roll_to_spot(355.0))

    assert manager.desired_strikes == frozenset({352.5, 355.0, 357.5})
    assert len(manager.desired_strikes) == 3          # 창 폭은 그대로
    assert len(ws.active_subscriptions) > len(manager.desired_strikes) * 2   # 구독만 넓다


def test_reserved_slots_leave_retention_usable_on_a_three_book_day():
    """예약 슬롯이 크면 3북 날(32/41 사용)에 이 고도화가 통째로 무력화된다.

    설계 구독 = (STRIKES_EACH_SIDE*2+1) x 2(C/P) x 3북 + 선물 + KOSPI.
    남는 슬롯에서 예약을 뺀 값이 **행사가 한 쌍(2슬롯) 이상**은 돼야 유지가 의미를 갖는다.
    """
    from mahdi.broker.ws_client import KISWebSocketClient as WS
    from mahdi.main import STRIKES_EACH_SIDE

    design = (STRIKES_EACH_SIDE * 2 + 1) * 2 * 3 + 2
    assert design <= WS.MAX_SUBSCRIPTIONS
    assert WS.MAX_SUBSCRIPTIONS - design - SUBSCRIPTION_RESERVED_SLOTS >= 2
