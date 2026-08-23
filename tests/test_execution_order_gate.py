"""주문 제출 전제조건 — 실행 배선 ③.

`_ORDER_PATH_WIRED`가 True가 된 뒤 **주문을 막는 것은 이 함수 하나다.** 종전까지 「주문을
내면 안 되는 이유」는 여러 파일의 주석에 흩어져 있었고, 주석은 실행되지 않는다.

이 테스트가 지키는 것:

  1. ADVISORY/CONFIRM은 제출하지 않는다 — **CONFIRM에는 승인 채널이 없다.**
  2. 재진입 쿨다운이 꺼져 있으면 FULL_AUTO여도 막힌다(저장소 자신이 적은 선행 조건).
  3. 사유를 **전부** 모은다 — 하나만 돌려주면 「고쳤는데 여전히 안 나간다」가 반복된다.
  4. 0.0 지정가로 주문을 만들지 않는다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mahdi.execution import order_manager as om
from mahdi.execution.entry import EntryPlan
from mahdi.execution.hybrid_mode import GateAction

NOW = datetime(2026, 8, 24, 9, 30)


def _plan(order_type: str = "LIMIT", limit_price: float | None = 22.0) -> EntryPlan:
    return EntryPlan(
        symbol="B09FAWA37", side="BUY", qty=1,
        order_type=order_type, limit_price=limit_price, urgency=order_type == "MARKET",
    )


def _blockers(**over) -> list[str]:
    kwargs = {
        "gate_action": GateAction.AUTO_SUBMIT,
        "entry_plan": _plan(),
        "symbol_resolved": True,
        "qty": 1,
        "reentry_cooldown_minutes": 15,
    }
    kwargs.update(over)
    return om.submission_blockers(**kwargs)


# ===== 통과하는 유일한 조합 =====


def test_everything_satisfied_means_no_blockers():
    assert _blockers() == []


# ===== 불변식 1 — CONFIRM은 제출하지 않는다 =====


@pytest.mark.parametrize(
    "gate_action",
    [GateAction.ADVISORY_ONLY, GateAction.PENDING_CONFIRMATION, None],
)
def test_only_auto_submit_may_send_an_order(gate_action):
    """CONFIRM(`PENDING_CONFIRMATION`)을 제출로 취급하면 그것은 사실상 FULL_AUTO다.

    v6 §13.1은 CONFIRM을 «대시보드 원클릭 승인 후 실행»으로 정의했고 **그 버튼이 없다.**
    사람이 「승인 모드」라고 믿는 동안 자동매매가 도는 것이 이 저장소가 가장 경계하는 형태다.
    """
    assert om.BLOCKER_MODE_NOT_AUTO in _blockers(gate_action=gate_action)


# ===== 불변식 2 — 쿨다운은 전제다 =====


@pytest.mark.parametrize("cooldown", [0, 0.0, None])
def test_reentry_cooldown_off_blocks_even_in_full_auto(cooldown):
    """08-11 실측: ENTER 281건/494분(56.9%), 09:03~09:19 **16분 연속.**

    ADVISORY라 무해했지만 실주문이었다면 16분 연속 진입이다. 그리고 08-23에 켠 레버 F가
    HIGH_CONVICTION을 늘리는 방향이라 그 빈도는 더 올라간다.
    """
    assert om.BLOCKER_REENTRY_COOLDOWN_OFF in _blockers(reentry_cooldown_minutes=cooldown)


def test_a_configured_cooldown_clears_that_blocker():
    assert om.BLOCKER_REENTRY_COOLDOWN_OFF not in _blockers(reentry_cooldown_minutes=1)


# ===== 불변식 3 — 사유를 전부 모은다 =====


def test_every_blocker_is_reported_not_just_the_first():
    blockers = _blockers(
        gate_action=GateAction.ADVISORY_ONLY,
        reentry_cooldown_minutes=0,
        entry_plan=None,
        symbol_resolved=False,
        qty=0,
    )

    assert set(blockers) == {
        om.BLOCKER_MODE_NOT_AUTO,
        om.BLOCKER_REENTRY_COOLDOWN_OFF,
        om.BLOCKER_NO_ENTRY_PLAN,
        om.BLOCKER_SYMBOL_UNRESOLVED,
        om.BLOCKER_QTY_NOT_POSITIVE,
    }


# ===== 불변식 4 — 0.0 지정가를 내보내지 않는다 =====


@pytest.mark.parametrize("bad_price", [None, 0.0])
def test_a_limit_order_without_a_price_is_blocked(bad_price):
    """0.0을 지정가로 보내면 거래소가 거부하고, 그 거부는 「주문 API가 안 된다」로 오귀속된다."""
    assert om.BLOCKER_NO_LIMIT_PRICE in _blockers(entry_plan=_plan(limit_price=bad_price))


def test_a_market_order_needs_no_limit_price():
    assert _blockers(entry_plan=_plan("MARKET", None)) == []


def test_symbol_label_instead_of_a_code_is_blocked():
    """행사가 라벨(`C1090.0`)로는 주문을 못 낸다 — 단축상품번호가 있어야 한다."""
    assert om.BLOCKER_SYMBOL_UNRESOLVED in _blockers(symbol_resolved=False)


# ===== Order 만들기 =====


def test_build_order_starts_pending_and_carries_the_intended_price():
    order = om.build_order(_plan(), "mahdi_local_1", NOW)

    assert order.state.value == "PENDING"
    assert (order.symbol, order.side, order.qty) == ("B09FAWA37", "BUY", 1)
    assert order.intended_px == 22.0
    assert order.order_id == "mahdi_local_1", "제출 응답을 받기 전에는 로컬 id다"


def test_build_order_for_market_records_zero_intended_price():
    order = om.build_order(_plan("MARKET", None), "id", NOW)

    assert order.order_type == "MARKET"
    assert order.intended_px == 0.0


@pytest.mark.parametrize(
    ("order_type", "code"), [("LIMIT", "01"), ("MARKET", "02")]
)
def test_order_dvsn_cd_maps_plan_type_to_kis_code(order_type, code):
    assert om.order_dvsn_cd(_plan(order_type, 22.0)) == code
