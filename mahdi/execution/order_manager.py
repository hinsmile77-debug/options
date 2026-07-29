"""Execution Engine — 주문 제출 + 체결통보-REST 이중 확인 (v6 §13.2).

실제 브로커 호출은 `BrokerClient` 프로토콜을 만족하는 객체로 주입받는다 — 테스트는
목을 넣고, 실전 배선 시점엔 `broker/rest_client.py`의 `submit_order()`/시세·체결
조회를 감싸는 얇은 어댑터를 넘긴다(이 모듈 자체는 KIS API를 직접 알 필요가 없다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mahdi.broker.order_state_machine import Order, OrderState, OrderStateMachine


class BrokerClient(Protocol):
    def submit_order(self, symbol: str, side: str, qty: int, price: float, order_dvsn_cd: str = "01") -> dict: ...

    def get_order_fill_status(self, order_id: str) -> dict: ...  # {"state": str, "filled_px": float, "filled_qty": int}


@dataclass
class OrderSubmissionResult:
    order: Order
    broker_response: dict


def submit(order: Order, broker: BrokerClient) -> OrderSubmissionResult:
    """
    입력: PENDING 상태의 Order, BrokerClient.
    계산: broker.submit_order()를 호출한다. KIS 응답의 `rt_cd`가 성공("0")이 아니면 즉시
         REJECTED로 전이한다 — 그 외엔 PENDING을 유지(실제 체결 확인은 confirm_fill()의 몫,
         v6 §13.2 "체결통보-REST 이중 확인").
    실패 조건: broker.submit_order()가 예외를 던지면 그대로 전파한다 — 주문 제출 자체의
              실패를 이 함수가 조용히 삼키면 안 되고, 호출측이 REJECTED 기록/재시도/알림
              여부를 결정해야 한다.
    """
    response = broker.submit_order(order.symbol, order.side, order.qty, order.intended_px)
    machine = OrderStateMachine(order)
    if response.get("rt_cd") not in (None, "0"):
        machine.transition(OrderState.REJECTED)
    return OrderSubmissionResult(order=machine.order, broker_response=response)


def confirm_fill(order: Order, broker: BrokerClient) -> Order:
    """
    입력: 아직 종결되지 않은(PENDING/PARTIAL) Order, BrokerClient.
    계산: broker.get_order_fill_status()로 실제 체결 상태를 REST로 재확인해 상태머신을
         전이시킨다. 아직 PENDING 그대로면(체결 없음) 전이 없이 그대로 반환 —
         `OrderStateMachine`은 PENDING→PENDING 자기 전이를 허용하지 않으므로 이 경우를
         먼저 걸러낸다.
    해석: PARTIAL→PARTIAL은 상태머신이 허용하는 자기 전이라(추가 부분체결 누적)
         그대로 transition()에 맡긴다.
    실패 조건: 이미 종결된(FILLED/CANCELLED/REJECTED) Order를 넘기면
              `InvalidTransitionError` — 호출측이 종결 주문을 재확인하지 않도록
              멱등성을 보장해야 한다.
    """
    status = broker.get_order_fill_status(order.order_id)
    new_state = OrderState(status["state"])
    if new_state == OrderState.PENDING:
        return order
    machine = OrderStateMachine(order)
    return machine.transition(new_state, filled_px=status.get("filled_px"), filled_qty=status.get("filled_qty"))
