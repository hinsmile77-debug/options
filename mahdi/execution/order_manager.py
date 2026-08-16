"""Execution Engine — 주문 제출 + 체결통보-REST 이중 확인 (v6 §13.2).

실제 브로커 호출은 `BrokerClient` 프로토콜을 만족하는 객체로 주입받는다 — 테스트는
목을 넣고, 실전 배선 시점엔 `broker/rest_client.py`의 `submit_order()`/시세·체결
조회를 감싸는 얇은 어댑터를 넘긴다(이 모듈 자체는 KIS API를 직접 알 필요가 없다).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mahdi.broker.order_state_machine import Order, OrderState, OrderStateMachine

logger = logging.getLogger(__name__)


class BrokerClient(Protocol):
    def submit_order(self, symbol: str, side: str, qty: int, price: float, order_dvsn_cd: str = "01") -> dict: ...

    def get_order_fill_status(self, order_id: str) -> dict: ...  # {"state": str, "filled_px": float, "filled_qty": int}


@dataclass
class OrderSubmissionResult:
    order: Order
    broker_response: dict


def submit(
    order: Order,
    broker: BrokerClient,
    extract_order_no: Callable[[dict], str | None] | None = None,
) -> OrderSubmissionResult:
    """
    입력: PENDING 상태의 Order, BrokerClient, (선택) 응답에서 **브로커 주문번호**를 뽑는 함수.
    계산: broker.submit_order()를 호출한다. KIS 응답의 `rt_cd`가 성공("0")이 아니면 즉시
         REJECTED로 전이한다 — 그 외엔 PENDING을 유지(실제 체결 확인은 confirm_fill()의 몫,
         v6 §13.2 "체결통보-REST 이중 확인").

         `extract_order_no`가 주어지면 그 결과로 **`order.order_id`를 갈아끼운다.**

    ## 왜 주문번호를 갈아끼워야 하는가 (2026-08-16 통합 리허설에서 발견)

    `confirm_fill()`은 `broker.get_order_fill_status(order.order_id)`로 조회한다. 그런데
    `order_id`는 **우리가 만든 로컬 식별자**이고, KIS가 부여하는 주문번호(`ODNO`)는 제출
    **응답에만** 들어 있다. 갈아끼우지 않으면 조회는 존재하지 않는 번호를 묻게 되고,
    `get_order_fill_status()`는 「없으면 PENDING」 규약에 따라 **영원히 PENDING을 돌려준다** —
    즉 체결된 주문이 끝까지 미체결로 보인다. 취소도 같은 이유로 불가능해진다
    (`cancel_order(ORGN_ODNO)`가 KIS 번호를 요구한다).

    이 결함은 순수 로직 테스트로는 드러나지 않는다(목 브로커가 로컬 id를 그대로 받아 주니까).
    **①~⑫를 실제로 이어 붙여 본 뒤에야 보였다** — 그래서 통합 테스트가 필요했다.

    기본값이 None인 이유: 인자를 안 주면 **종전과 바이트 단위로 같은 동작**이다. KIS 응답
    형식을 아는 것은 `broker/rest_client.extract_order_no()`이고(이 모듈은 KIS를 몰라야 한다 —
    파일 docstring), 그 함수를 호출측이 주입한다.
    실패 조건: broker.submit_order()가 예외를 던지면 그대로 전파한다 — 주문 제출 자체의
              실패를 이 함수가 조용히 삼키면 안 되고, 호출측이 REJECTED 기록/재시도/알림
              여부를 결정해야 한다. 추출이 None을 내면 로컬 id를 유지하고 **경고를 남긴다**
              (조용히 넘어가면 위 함정이 그대로 재현된다).
    """
    response = broker.submit_order(order.symbol, order.side, order.qty, order.intended_px)
    machine = OrderStateMachine(order)
    if response.get("rt_cd") not in (None, "0"):
        machine.transition(OrderState.REJECTED)
        return OrderSubmissionResult(order=machine.order, broker_response=response)

    if extract_order_no is not None:
        broker_order_no = extract_order_no(response)
        if broker_order_no:
            machine.order.order_id = broker_order_no
        else:
            logger.warning(
                "제출 응답에서 브로커 주문번호를 못 찾았다 — 로컬 id(%s)를 유지한다. "
                "이 상태로는 체결 조회도 취소도 불가능하다(응답: %r)",
                order.order_id, response,
            )
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
