"""청산 루프 + 15:10 강제청산 — 실행 배선 ④·⑤.

이 테스트가 지키는 것은 **다섯 가지**다:

  1. 방어는 모드와 무관하게 자동이다 — ADVISORY 설정에서도 하드스톱·강제청산은 주문을 낸다.
  2. 현재가를 모르면 **하드스톱을 안 건다** — 0.0을 넣으면 매수 포지션이 −100%로 보여
     즉시 전량 청산이 나간다(지어낸 값이 주문이 되는 형태).
  3. 레이어 4(Belief Decay)는 **평가되지 않는다** — EV 입력이 없는데 중립값을 채우면
     그 숫자가 부분청산 주문이 된다.
  4. 청산 주문은 **반대 방향 시장가**다(체결 확실성 > 가격).
  5. 15:10 자기검증이 미확인 건수를 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytest

from mahdi import main, session
from mahdi.execution import position_ledger as pl
from mahdi.execution.engine import ExecutionEngine

SESSION_START = datetime(2026, 8, 24, 9, 0)


def _run(coro):
    return asyncio.run(coro)


def _entry(**over):
    kwargs = {
        "symbol": "B09FAWA37", "side": "BUY", "qty": 1, "entry_price": 22.0,
        "opened_at": datetime(2026, 8, 24, 9, 30), "exit_rules_key": "RANGE_TIGHT",
    }
    kwargs.update(over)
    return pl.LedgerEntry(**kwargs)


class _RecordingBroker:
    def __init__(self):
        self.submitted: list[dict] = []

    def submit_order(self, symbol, side, qty, price, order_dvsn_cd="01"):
        self.submitted.append(
            {"symbol": symbol, "side": side, "qty": qty, "price": price}
        )
        return {"rt_cd": "0", "output": [{"ODNO": "0000009001"}]}


@pytest.fixture
def no_db(monkeypatch):
    """`insert_execution_log`만 가로챈다 — 청산 판정 자체는 DB를 안 본다."""
    written: list[dict] = []
    monkeypatch.setattr("mahdi.main.db.insert_execution_log", lambda conn, row: written.append(row))
    return written


# ===== 불변식 1 — 방어는 모드와 무관하게 자동 =====


def test_hard_stop_submits_even_though_the_configured_mode_is_advisory(no_db):
    """v6 §13.1 불변 규칙: 수동 모드는 **공격**의 자유이지 방어의 자유가 아니다.

    진입은 `submission_blockers()`가 ADVISORY에서 막지만, 청산의 하드스톱은 막히면 안 된다.
    """
    broker = _RecordingBroker()
    entry = _entry(entry_price=22.0)
    # RANGE_TIGHT의 stop은 −0.008 — 22.0에서 21.0이면 −4.5%로 한참 넘는다.
    order = main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), entry, {"B09FAWA37": 21.0},
        datetime(2026, 8, 24, 10, 0),
    )

    assert order is not None
    assert broker.submitted == [
        {"symbol": "B09FAWA37", "side": "SELL", "qty": 1, "price": 0.0}
    ]


def test_forced_flat_submits_after_1510_regardless_of_pnl(no_db):
    """15:10 이후에는 손익과 무관하게 전량이다 — 해제 불가 레이어."""
    broker = _RecordingBroker()
    # 이익 중인 포지션(22 → 30)이라 다른 레이어는 아무것도 안 건다.
    order = main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), _entry(), {"B09FAWA37": 30.0},
        datetime(2026, 8, 24, 15, 10, 1),
    )

    assert order is not None
    assert broker.submitted[0]["side"] == "SELL"
    # 로컬 id(`exit_forced_flat_15_10_…`)가 **KIS 주문번호로 갈아끼워진다** — 안 갈아끼우면
    # 체결 조회가 존재하지 않는 번호를 묻고 영원히 PENDING을 돌려받는다(08-16 발견).
    assert order.order_id == "0000009001"


def test_a_healthy_position_before_1510_is_held(no_db):
    broker = _RecordingBroker()

    order = main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), _entry(), {"B09FAWA37": 22.1},
        datetime(2026, 8, 24, 10, 0),
    )

    assert order is None
    assert broker.submitted == []


# ===== 불변식 2 — 현재가를 모르면 하드스톱을 안 건다 =====


def test_a_missing_price_does_not_fabricate_a_100pct_loss(no_db, caplog):
    """가격이 없을 때 0.0을 넣으면 매수 포지션이 −100%가 되어 **즉시 전량 청산**이 나간다."""
    broker = _RecordingBroker()

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        order = main._evaluate_and_exit_one(
            None, broker, ExecutionEngine(), _entry(), {}, datetime(2026, 8, 24, 10, 0)
        )

    assert order is None, "가격을 모르면 하드스톱을 걸지 않는다"
    assert broker.submitted == []
    assert "현재가가 없다" in caplog.text, "조용히 넘어가면 안 된다"


def test_a_missing_price_still_lets_forced_flat_through(no_db):
    """가격을 몰라도 15:10은 걸린다 — 그 레이어는 가격을 안 본다."""
    broker = _RecordingBroker()

    order = main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), _entry(), {}, datetime(2026, 8, 24, 15, 10, 1)
    )

    assert order is not None
    assert broker.submitted[0]["side"] == "SELL"


def test_the_time_stop_only_advises_in_advisory_mode(no_db, caplog):
    """**타임스톱은 「항상 자동」 레이어가 아니다.**

    `always_automatic`은 `hard_stop`·`circuit_breaker`·`forced_flat_15_10` 셋뿐이다
    (v6 §13.1). ADVISORY에서 타임스톱은 판정만 남고 주문은 안 나간다 — 그것이 「수동 모드는
    공격의 자유이지 방어의 자유가 아니다」의 정확한 경계다.

    ⚠ 운영상 의미: 기본 설정(ADVISORY)으로 포지션을 들면 **타임스톱이 포지션을 닫지 않는다.**
    실제로 닫는 것은 하드스톱과 15:10뿐이다.
    """
    broker = _RecordingBroker()
    entry = _entry(opened_at=datetime(2026, 8, 24, 9, 30))

    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        order = main._evaluate_and_exit_one(
            None, broker, ExecutionEngine(), entry, {}, datetime(2026, 8, 24, 11, 0)
        )

    assert order is None and broker.submitted == []
    assert "청산 판정" in caplog.text and "time_stop" in caplog.text, (
        "주문은 안 내도 판정은 반드시 남아야 한다 — 조용히 넘기면 「걸릴 것이 없었다」와 구분이 안 된다"
    )


def test_the_time_stop_does_submit_once_the_mode_is_full_auto(no_db, monkeypatch):
    from mahdi.config.settings import get_strategy_params

    monkeypatch.setattr(
        "mahdi.main.get_strategy_params",
        lambda: {**get_strategy_params(), "hybrid_mode": {"default": "FULL_AUTO"}},
    )
    broker = _RecordingBroker()

    order = main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), _entry(opened_at=datetime(2026, 8, 24, 9, 30)), {},
        datetime(2026, 8, 24, 11, 0),
    )

    assert order is not None, "진입 90분 뒤 — 타임스톱(30분)을 한참 넘었다"


# ===== 불변식 3 — 레이어 4는 평가되지 않는다 =====


def test_belief_decay_layer_is_not_evaluated_and_says_so(caplog):
    """EV 입력(`trade_history`)이 0행인데 중립값을 채우면 EV가 0이 되고, 그것이 악화 1건으로
    세어져 다른 플래그 하나만 붙어도 **지어낸 부분청산**이 나간다."""
    from mahdi.execution import exit_stack

    exit_stack._warned_belief_unavailable = False
    position = exit_stack.PositionState(
        symbol="X", side="BUY", entry_price=22.0, current_price=22.1,
        entry_time_minutes=0.0, now_minutes=1.0, regime="RANGE_TIGHT",
    )

    with caplog.at_level(logging.WARNING, logger="mahdi.execution.exit_stack"):
        decision = exit_stack.evaluate_exit_stack(
            position, exit_stack.MarketStructureState(), None,
            {"RANGE_TIGHT": {"stop": -0.008, "time_stop": 30}},
        )

    assert decision.action == "HOLD"
    assert "레이어 4" in caplog.text and "평가하지 않는다" in caplog.text


def test_a_supplied_belief_state_is_still_evaluated():
    """None일 때만 건너뛴다 — 백테스트는 여전히 레이어 4를 쓴다."""
    from mahdi.execution import exit_stack

    position = exit_stack.PositionState(
        symbol="X", side="BUY", entry_price=22.0, current_price=22.1,
        entry_time_minutes=0.0, now_minutes=1.0, regime="RANGE_TIGHT",
    )
    belief = exit_stack.BeliefState(
        win_probability=0.1, avg_win=1.0, avg_loss=5.0,
        regime_degraded=True, volatility_state_mismatch=True,
    )

    decision = exit_stack.evaluate_exit_stack(
        position, exit_stack.MarketStructureState(), belief,
        {"RANGE_TIGHT": {"stop": -0.008, "time_stop": 30}},
    )

    assert decision.triggered_layer == exit_stack.ExitLayer.BELIEF_DECAY_STOP


# ===== 불변식 4 — 반대 방향 시장가 =====


def test_a_short_position_exits_by_buying(no_db):
    broker = _RecordingBroker()
    # 매도 진입 22.0 → 현재 30.0이면 −36%로 손절이다.
    main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), _entry(side="SELL"), {"B09FAWA37": 30.0},
        datetime(2026, 8, 24, 10, 0),
    )

    assert broker.submitted[0]["side"] == "BUY"


def test_exit_orders_are_market_not_limit(no_db):
    """청산은 체결 확실성이 가격보다 중요하다(v6 §13.3)."""
    broker = _RecordingBroker()

    order = main._evaluate_and_exit_one(
        None, broker, ExecutionEngine(), _entry(), {"B09FAWA37": 21.0},
        datetime(2026, 8, 24, 10, 0),
    )

    assert order.order_type == "MARKET"


# ===== 불변식 5 — 15:10 자기검증 =====


def test_forced_flat_verification_reports_unconfirmed_orders(caplog):
    """[[DECISION_LOG]] 2026-07-21: 「종료 시퀀스 자기검증 없이는 배포 금지」."""
    from mahdi.broker.order_state_machine import Order

    pending = Order(
        order_id="exit_forced_flat_15_10_X_151001", symbol="X", side="SELL",
        order_type="MARKET", intended_px=0.0, qty=1, timestamp=datetime(2026, 8, 24, 15, 10),
    )

    with caplog.at_level(logging.WARNING, logger="mahdi.main"):
        main._verify_forced_flat(None, [pending], datetime(2026, 8, 24, 15, 10, 5))

    assert "미확인 1건" in caplog.text
    assert "밤을 넘는다" in caplog.text


def test_forced_flat_with_nothing_to_close_is_not_an_alarm(caplog):
    with caplog.at_level(logging.INFO, logger="mahdi.main"):
        main._verify_forced_flat(None, [], datetime(2026, 8, 24, 15, 10, 5))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ===== 15:10 경계는 session이 소유한다 =====


def test_the_forced_flat_boundary_comes_from_session_not_a_literal():
    assert session.FORCED_FLAT_TIME.strftime("%H:%M") == "15:10"
    assert main._exit_market_state(datetime(2026, 8, 24, 15, 9, 59)).is_forced_flat_time is False
    assert main._exit_market_state(datetime(2026, 8, 24, 15, 10, 0)).is_forced_flat_time is True
