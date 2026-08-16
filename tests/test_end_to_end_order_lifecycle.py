"""①~⑫ 전 경로 관통 — 목 브로커로 제출→부분체결→체결→강제청산 (2026-08-16 통합 리허설).

## 왜 이 파일이 필요한가

`docs/동작흐름과상태/2026-08-06_진입흐름과_진행상황.md`가 ①~⑧을 라이브, ⑨~⑫를 미배선으로
적었고 각 조각은 단위 테스트가 있다. **그런데 조각들을 실제로 이어 붙여 본 적이 없다.**

이어 붙이자마자 결함이 나왔다: `order_manager.submit()`이 KIS가 준 주문번호(`ODNO`)를
`order.order_id`에 반영하지 않아서 `confirm_fill()`이 **로컬 식별자로 조회**하고 있었다.
그 상태로 배선하면 체결된 주문이 끝까지 PENDING으로 보이고 취소도 불가능하다.
단위 테스트로는 안 보인다 — 목 브로커가 로컬 id를 그대로 받아 주기 때문이다.

## 이 파일이 검증하는 불변식

  * 진입 판단이 리스크 게이트를 지나 **정수 계약수**가 되고, 그것이 주문 수량이 된다.
  * CONFIRM 모드는 **진입을 사람 확인 대상으로** 만들지만 **강제청산은 모드와 무관하게 자동**이다.
  * 브로커 주문번호를 채택해야 체결 조회가 성립한다.
  * 부분체결 → 전량체결로 상태가 누적 전이된다.
  * 15:10 강제청산이 반대 방향 시장가를 내고, **FILLED가 아닌 것은 청산 성공으로 세지 않는다.**
  * 전 과정이 `execution_logs` 행으로 남을 수 있다.

**고정 계약수를 2로 둔다** — 배포 설정은 1이지만(`tests/test_risk_sizing.py`가 그것을 검사한다)
1계약으로는 부분체결이라는 상태가 존재할 수 없어 ⑫의 절반을 검증할 수 없다.
"""

from dataclasses import dataclass
from datetime import datetime

import pytest

from mahdi.broker.order_state_machine import (
    InvalidTransitionError,
    Order,
    OrderState,
    OrderStateMachine,
    order_to_execution_log_row,
)
from mahdi.broker.rest_client import extract_order_no
from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.execution import order_manager
from mahdi.execution.engine import EntryRequest, ExecutionEngine
from mahdi.execution.entry import EntryContext
from mahdi.execution.exit_stack import (
    BeliefState,
    ExitLayer,
    MarketStructureState,
    PositionState,
)
from mahdi.execution.forced_flat import (
    OpenPosition,
    build_forced_flat_orders,
    verify_forced_flat,
)
from mahdi.execution.hybrid_mode import GateAction, HybridMode
from mahdi.fusion.engine import MetaLabelContext, SignalFusionEngine
from mahdi.fusion.meta_label import TradePermission
from mahdi.fusion.signal_layer import SignalInputs
from mahdi.risk.circuit_breaker import MarketConditions
from mahdi.risk.engine import RiskEngine
from mahdi.risk.limits import AccountState
from mahdi.risk.sizing import PositionSizingInput

_SYMBOL = "201S03C325"
_NOW = datetime(2026, 8, 18, 10, 4)
_FORCED_FLAT_AT = datetime(2026, 8, 18, 15, 10)

_STRATEGY_PARAMS = {
    "ensemble": {
        "regime_hmm": {"base_w": 0.20}, "xgboost_tabular": {"base_w": 0.20},
        "lstm_temporal": {"base_w": 0.15}, "options_flow": {"base_w": 0.20},
        "orderflow_ofi_vpin": {"base_w": 0.15}, "flow_position": {"base_w": 0.10},
    },
    "meta_label_thresholds": {
        "no_trade_max": 0.15, "small_test_max": 0.35, "standard_max": 0.65,
        "slippage_penalty_factor": 0.7, "gamma_regime_penalty_factor": 0.85,
        "foreign_flow_penalty_factor": 0.8, "event_proximity_penalty_minutes": 15,
        "event_proximity_penalty_factor": 0.5,
    },
    "strategy_gates": {"max_priority_strategies_per_regime_day": 2},
    "exit_rules": {},
}

# 부분체결을 만들려면 2계약이 필요하다(파일 docstring 참고).
_RISK_LIMITS = {
    "sizing": {"kelly_fraction": 0.25, "max_kelly_fraction": 0.25, "fixed_contracts": 2},
    "limits": {"max_same_direction_positions": 3, "max_drawdown_pct": -0.10},
    "circuit_breaker": {},
}


@dataclass(frozen=True)
class _Plan:
    """`EntryPlan`과 같은 모양의 최소 스텁 — ⑪의 출력을 ⑫에 넘기는 자리만 흉내낸다."""

    symbol: str
    side: str
    order_type: str
    limit_price: float | None
    qty: int


class MockBroker:
    """`order_manager.BrokerClient` 프로토콜 구현 — KIS 응답 **형태**를 흉내낸다.

    특히 제출 응답은 `output`이 **array**이고 필드가 **대문자 `ODNO`**다(공식 문서 확인분).
    조회는 순서대로 미리 정한 상태를 돌려주며 **KIS 주문번호로만** 찾는다 —
    로컬 id로 물으면 「없음」이다. 그것이 이 목의 핵심이다.
    """

    ORDER_NO = "0000001666"

    def __init__(self, fill_sequence: list[dict] | None = None):
        self.submitted: list[dict] = []
        self.queried: list[str] = []
        self._fills = list(fill_sequence or [])

    def submit_order(self, symbol: str, side: str, qty: int, price: float,
                     order_dvsn_cd: str = "01") -> dict:
        self.submitted.append(
            {"symbol": symbol, "side": side, "qty": qty, "price": price,
             "order_dvsn_cd": order_dvsn_cd}
        )
        return {
            "rt_cd": "0", "msg_cd": "APBK0029", "msg1": "주문전송이 정상적으로 처리되었습니다.",
            "output": [{"ODNO": self.ORDER_NO, "ORD_TMD": "100400", "ITEM_NAME": "테스트"}],
        }

    def get_order_fill_status(self, order_id: str) -> dict:
        self.queried.append(order_id)
        if order_id != self.ORDER_NO:
            # KIS가 모르는 번호 — `rest_client.get_order_fill_status()`의 규약과 같다.
            return {"state": OrderState.PENDING.value, "filled_px": None, "filled_qty": 0}
        if self._fills:
            return self._fills.pop(0)
        return {"state": OrderState.PENDING.value, "filled_px": None, "filled_qty": 0}


def _entering_inputs() -> SignalInputs:
    """추세 강세 + 멤버 동조 — 진입 전략이 실제로 선택되는 조합."""
    prob = [0.0] * 8
    prob[RegimeLabel.TREND_UP_STRONG] = 0.95
    return SignalInputs(
        regime_state=RegimeState(
            regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob), stability_flag=True
        ),
        gex=1.5e9, gamma_flip=350.0, gamma_wall=350.0, spot=355.0,
        total_charm=1.0, charm_active=False,
        ofi=12.5, queue_imbalance=None, foreign_net_flow=500.0,
    )


def _sizing_input() -> PositionSizingInput:
    return PositionSizingInput(
        base_size=4.0, regime_confidence=0.95, signal_quality=0.8,
        target_vol=0.01, realized_vol=0.01, liquidity_score=1.0,
        drawdown_pct=0.0, portfolio_capacity_remaining_pct=1.0,
    )


def _flat_account() -> AccountState:
    return AccountState(
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, drawdown_pct=0.0,
        same_direction_positions=0, daily_trades_by_strategy={},
    )


def _order_from_plan(plan: _Plan, local_id: str = "local-tmp-1") -> Order:
    """⑪ EntryPlan → ⑫ Order. `order_id`는 **아직 로컬 임시값**이다 —
    KIS 번호는 제출 응답에만 있고, 그것을 채택하는 것이 `submit()`의 일이다."""
    return Order(
        order_id=local_id, symbol=plan.symbol, side=plan.side, order_type=plan.order_type,
        intended_px=plan.limit_price if plan.limit_price is not None else 0.0,
        qty=plan.qty, timestamp=_NOW,
    )


def _limit_plan() -> _Plan:
    return _Plan(symbol=_SYMBOL, side="BUY", order_type="LIMIT", limit_price=3.50, qty=2)


def _decide_entry():
    """①~⑧: 신호 → 융합 판단 → 리스크 게이트."""
    fusion = SignalFusionEngine(strategy_params=_STRATEGY_PARAMS)
    decision = fusion.evaluate(_entering_inputs(), MetaLabelContext(), vrp=0.0)
    risk = RiskEngine(risk_limits=_RISK_LIMITS).evaluate_entry(
        _sizing_input(), _flat_account(),
        strategy_id="atm_long", market_conditions=MarketConditions(),
    )
    return decision, risk


def _entry_request(qty: int) -> EntryRequest:
    return EntryRequest(
        entry_context=EntryContext(
            symbol=_SYMBOL, side="BUY", qty=qty, reference_price=3.55, now=_NOW.time(),
        ),
        sizing_input=_sizing_input(),
        account_state=_flat_account(),
        strategy_id="atm_long",
        market_conditions=MarketConditions(),
        has_open_position_same_direction=False,
        is_new_signal=True,
    )


def _engine() -> ExecutionEngine:
    return ExecutionEngine(
        risk_engine=RiskEngine(risk_limits=_RISK_LIMITS), strategy_params=_STRATEGY_PARAMS
    )


def test_fusion_produces_a_tradeable_decision_and_risk_sizes_it_to_whole_contracts():
    """①~⑧ — 판단이 진입까지 가고, 리스크 게이트가 **정수 계약수**를 낸다.

    `approved_contracts`가 없으면 ⑪이 쓸 수 있는 수량이 존재하지 않는다(Block D).
    """
    decision, risk = _decide_entry()

    assert decision.trade_permission != TradePermission.NO_TRADE
    assert decision.allowed_strategies, f"진입 전략이 없다: {decision.reject_reasons}"
    assert risk.approved is True
    assert risk.approved_contracts == 2
    assert isinstance(risk.approved_contracts, int)


def test_confirm_mode_holds_the_entry_but_still_builds_the_plan():
    """⑨~⑪ — CONFIRM은 「사람 확인 대상」으로 만들 뿐 계획은 만든다(ADVISORY와 다른 점)."""
    _, risk = _decide_entry()
    outcome = _engine().evaluate_entry(_entry_request(risk.approved_contracts), HybridMode.CONFIRM)

    assert outcome.approved is True
    assert outcome.gate_decision.action == GateAction.PENDING_CONFIRMATION
    assert outcome.gate_decision.confirmation_timeout_seconds == 60
    assert outcome.entry_plan is not None
    # Passive-first — 매수는 기준가보다 **낮은** 지정가다(시장가 추격이 기본이 아니다).
    assert outcome.entry_plan.order_type == "LIMIT"
    assert outcome.entry_plan.limit_price < 3.55
    assert outcome.entry_plan.qty == 2


def test_advisory_mode_blocks_the_order_path_entirely():
    """08-14까지의 상태 — 승인은 나지만 주문 계획이 없어 ⑫에 도달할 수 없다."""
    outcome = _engine().evaluate_entry(_entry_request(2), HybridMode.ADVISORY)

    assert outcome.approved is True
    assert outcome.gate_decision.action == GateAction.ADVISORY_ONLY
    assert outcome.entry_plan is None


def test_averaging_down_is_refused_before_the_risk_gate_even_runs():
    """물타기 금지는 **선체크**다 — 같은 방향 포지션이 있는데 새 신호가 없으면 즉시 거부된다.
    Block B가 `has_open_position_same_direction`의 입력을 만들어 주기 전까지 이 자리는
    언제나 False였다(브로커가 답을 갖고 있는데 아무도 묻지 않았다)."""
    request = EntryRequest(
        entry_context=EntryContext(symbol=_SYMBOL, side="BUY", qty=2,
                                   reference_price=3.55, now=_NOW.time()),
        sizing_input=_sizing_input(), account_state=_flat_account(),
        strategy_id="atm_long", market_conditions=MarketConditions(),
        has_open_position_same_direction=True, is_new_signal=False,
    )
    outcome = _engine().evaluate_entry(request, HybridMode.CONFIRM)

    assert outcome.approved is False
    assert outcome.reject_reasons == ["averaging_down_forbidden"]
    assert outcome.entry_plan is None


def test_submit_adopts_the_broker_order_number_so_that_confirm_fill_can_find_it():
    """**통합 리허설이 찾은 결함.**

    `order_id`는 로컬 식별자이고 KIS 주문번호(`ODNO`)는 제출 응답에만 있다. 갈아끼우지 않으면
    조회가 존재하지 않는 번호를 묻고, 「없으면 PENDING」 규약 때문에 **체결된 주문이 끝까지
    미체결로 보인다.**
    """
    broker = MockBroker()
    result = order_manager.submit(
        _order_from_plan(_limit_plan()), broker, extract_order_no=extract_order_no
    )

    assert result.order.order_id == MockBroker.ORDER_NO
    assert result.order.state == OrderState.PENDING
    assert broker.submitted[0]["qty"] == 2


def test_without_adopting_the_order_number_the_fill_is_invisible_forever():
    """추출기를 주지 않으면 종전 동작이고, 그 종전 동작은 **체결을 못 본다.**
    이 함정을 회귀로 고정한다 — 다시 없어지면 여기가 깨진다."""
    broker = MockBroker([{"state": "FILLED", "filled_px": 3.50, "filled_qty": 2}])

    result = order_manager.submit(_order_from_plan(_limit_plan()), broker)  # 추출기 없음
    assert result.order.order_id == "local-tmp-1"

    confirmed = order_manager.confirm_fill(result.order, broker)

    assert confirmed.state == OrderState.PENDING  # 브로커는 로컬 id를 모른다
    assert broker.queried == ["local-tmp-1"]


def test_a_rejected_submission_never_adopts_an_order_number():
    """업무 실패(`rt_cd != "0"`)면 REJECTED로 끝난다 — 주문번호를 채택할 일도 없다.
    HTTP 200 + 업무 실패가 가능하므로 이 분기가 필요하다."""

    class _RejectingBroker(MockBroker):
        def submit_order(self, symbol, side, qty, price, order_dvsn_cd="01"):
            return {"rt_cd": "1", "msg1": "주문가능수량을 초과하였습니다.", "output": []}

    result = order_manager.submit(
        _order_from_plan(_limit_plan()), _RejectingBroker(), extract_order_no=extract_order_no
    )

    assert result.order.state == OrderState.REJECTED
    assert result.order.order_id == "local-tmp-1"


def test_partial_then_full_fill_accumulates_through_the_state_machine():
    """⑫ — 부분체결 → 전량체결. `filled_qty`는 **누적 가산**이다."""
    broker = MockBroker([
        {"state": "PARTIAL", "filled_px": 3.50, "filled_qty": 1},
        {"state": "FILLED", "filled_px": 3.52, "filled_qty": 1},
    ])
    order = order_manager.submit(
        _order_from_plan(_limit_plan()), broker, extract_order_no=extract_order_no
    ).order

    order = order_manager.confirm_fill(order, broker)
    assert order.state == OrderState.PARTIAL and order.filled_qty == 1

    order = order_manager.confirm_fill(order, broker)
    assert order.state == OrderState.FILLED and order.filled_qty == 2
    assert order.filled_px == 3.52
    assert broker.queried == [MockBroker.ORDER_NO, MockBroker.ORDER_NO]

    # ⚠ **종결된 주문의 재조회는 두 갈래이고, 조용한 쪽이 함정이다.**
    #
    # 브로커가 PENDING을 주면(조회 결과가 소진된 경우 등) `confirm_fill()`은 상태머신을
    # 건드리지 않고 **그대로 반환한다** — 예외가 아니다. 이 성질을 모르고 재조회를 반복하면
    # 「아무 일도 안 일어남」과 「종결됨」이 구분되지 않는다.
    quiet = order_manager.confirm_fill(order, broker)
    assert quiet.state == OrderState.FILLED  # 조용히 그대로다

    # 반면 브로커가 종결 상태를 **다시** 보고하면 상태머신이 막는다 —
    # 멱등성은 호출측 책임이라는 계약이 여기서 강제된다.
    broker._fills.append({"state": "FILLED", "filled_px": 3.52, "filled_qty": 1})
    with pytest.raises(InvalidTransitionError):
        order_manager.confirm_fill(order, broker)


def test_forced_flat_is_automatic_even_in_confirm_mode():
    """**모드 무관 불변식** — "수동 모드는 공격의 자유이지 방어의 자유가 아니다"(v6 §13.1).

    진입은 CONFIRM으로 사람을 기다리지만 15:10 강제청산은 기다리지 않는다.
    """
    decision, gate = _engine().evaluate_exit(
        position=PositionState(
            symbol=_SYMBOL, side="BUY", entry_price=3.50, current_price=3.55,
            entry_time_minutes=64.0, now_minutes=370.0, regime="TREND_STRONG",
        ),
        market=MarketStructureState(is_forced_flat_time=True),
        belief=BeliefState(win_probability=0.6, avg_win=1.0, avg_loss=1.0),
        account_state=AccountState(
            daily_pnl_pct=0.0, weekly_pnl_pct=0.0, drawdown_pct=0.0,
            same_direction_positions=1, daily_trades_by_strategy={},
        ),
        market_conditions=MarketConditions(),
        mode=HybridMode.CONFIRM,
    )

    assert decision.action == "FULL_EXIT"
    assert decision.triggered_layer == ExitLayer.FORCED_FLAT
    assert gate.action == GateAction.AUTO_SUBMIT  # ← 사람을 기다리지 않는다


def test_forced_flat_submits_the_opposite_side_market_order_and_verifies_it():
    """15:10 청산: 반대 방향 **시장가**(체결 확실성이 지정가보다 중요) → 확인까지."""
    orders = build_forced_flat_orders(
        [OpenPosition(symbol=_SYMBOL, side="BUY", qty=2, current_price=3.55)], _FORCED_FLAT_AT
    )
    (flat_order,) = orders
    assert flat_order.side == "SELL"
    assert flat_order.order_type == "MARKET"
    assert flat_order.qty == 2

    broker = MockBroker([{"state": "FILLED", "filled_px": 3.55, "filled_qty": 2}])
    submitted = order_manager.submit(flat_order, broker, extract_order_no=extract_order_no).order
    confirmed = order_manager.confirm_fill(submitted, broker)

    assert confirmed.state == OrderState.FILLED
    assert verify_forced_flat([confirmed], _FORCED_FLAT_AT).all_flat is True


def test_a_cancelled_flatten_order_is_not_counted_as_flat():
    """**청산 실패를 성공으로 읽지 않는다.** CANCELLED/REJECTED는 포지션이 그대로 남았다는
    뜻이다 — 조용히 넘어가면 잔존 포지션을 들고 장을 넘긴다."""
    order = Order(order_id="ff-1", symbol=_SYMBOL, side="SELL", order_type="MARKET",
                  intended_px=3.55, qty=2, timestamp=_FORCED_FLAT_AT)
    cancelled = OrderStateMachine(order).transition(OrderState.CANCELLED)

    result = verify_forced_flat([cancelled], _FORCED_FLAT_AT)

    assert result.all_flat is False
    assert result.unconfirmed_order_ids == ["ff-1"]


def test_every_stage_of_the_lifecycle_can_be_written_to_execution_logs():
    """전 과정이 `execution_logs` 행으로 남을 수 있어야 한다 —
    남지 않으면 8/18 실측의 증거가 없다."""
    broker = MockBroker([
        {"state": "PARTIAL", "filled_px": 3.50, "filled_qty": 1},
        {"state": "FILLED", "filled_px": 3.52, "filled_qty": 1},
    ])
    order = order_manager.submit(
        _order_from_plan(_limit_plan()), broker, extract_order_no=extract_order_no
    ).order

    rows = [order_to_execution_log_row(order)]
    for _ in range(2):
        order = order_manager.confirm_fill(order, broker)
        rows.append(order_to_execution_log_row(order))

    assert [r["state"] for r in rows] == ["PENDING", "PARTIAL", "FILLED"]
    # PK가 order_id라 세 행이 **같은 행을 갱신**한다 — 주문번호를 채택했으므로 KIS 번호다.
    assert {r["order_id"] for r in rows} == {MockBroker.ORDER_NO}
    assert rows[-1]["filled_px"] == 3.52
