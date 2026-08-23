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

from datetime import datetime

from mahdi.broker.order_state_machine import Order, OrderState, OrderStateMachine
from mahdi.execution.hybrid_mode import GateAction

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


# ===== 2026-08-23 (실행 배선 ③) — 주문이 나가기 위해 **전부** 참이어야 하는 것들 =====
#
# 이 함수가 이 증분의 안전장치다. 종전까지 「주문을 내면 안 되는 이유」는 여러 파일의 **주석**에
# 흩어져 있었고, 주석은 실행되지 않는다. 여기서 값으로 만든다.
#
# ## 왜 CONFIRM이 제출하지 않는가 — 승인 채널이 없다
#
# `gate_entry()`는 CONFIRM에서 `PENDING_CONFIRMATION`(60초 타임아웃)을 낸다. v6 §13.1은 그것을
# *"대시보드 원클릭 승인 후 실행"*으로 정의했는데, **그 대시보드 버튼이 없다.** 승인 채널이
# 없는 상태에서 CONFIRM을 제출로 취급하면 그것은 FULL_AUTO이고, 사람이 「승인 모드」라고 믿는
# 동안 자동매매가 도는 것이 된다 — 이 저장소가 가장 경계하는 형태의 거짓(설정과 사실의 분리)이다.
# 그래서 **AUTO_SUBMIT만 제출한다.**
#
# ## 왜 재진입 쿨다운이 전제인가 — 저장소 자신이 그렇게 적었다
#
# `strategy_params.yaml`의 `reentry_cooldown_minutes` 주석: *"켤 조건 … **실주문 배선 전에**
# 켜고, 그 전에 며칠치 ENTER 빈도 분포를 본다."* 그 값이 아직 0이다.
#
# 08-11 실측이 그 이유를 말한다: ENTER **281건/494분(56.9%)**, 09:03~09:19 **16분 연속**.
# ADVISORY라 무해했지만 실주문이었다면 16분 연속 진입이다. 그리고 08-23에 켠 레버 F가
# HIGH_CONVICTION을 **늘리는** 방향이다 — 즉 그 빈도는 더 올라간다.
#
# 여기서 쿨다운을 대신 켜지 않는 이유: 그것은 판단 출력을 움직이는 결정이고, 레버 F와 같은
# 날 켜면 **귀속이 안 갈린다**(08-23에 레버 E를 09-01로 미룬 것과 같은 이유). 대신 **켜지지
# 않은 동안 주문을 막는다** — 사람이 값을 정하는 것이 이 항목의 해소이고, 그 전까지는
# 배선이 완성돼도 주문이 안 나간다.
BLOCKER_MODE_NOT_AUTO = "mode_not_auto_submit"
BLOCKER_REENTRY_COOLDOWN_OFF = "reentry_cooldown_not_configured"
BLOCKER_NO_ENTRY_PLAN = "no_entry_plan"
BLOCKER_NO_LIMIT_PRICE = "limit_price_missing"
BLOCKER_SYMBOL_UNRESOLVED = "symbol_unresolved"
BLOCKER_QTY_NOT_POSITIVE = "qty_not_positive"


def submission_blockers(
    *,
    gate_action,
    entry_plan,
    symbol_resolved: bool,
    qty: int,
    reentry_cooldown_minutes: float,
) -> list[str]:
    """
    입력: 하이브리드 게이트 판정, 만들어진 `EntryPlan`(없으면 None), 종목코드를 실제로
         찾았는지, 승인 계약수, 설정된 재진입 쿨다운(분).
    계산: 주문을 막는 사유를 **전부** 모은다 — 첫 번째에서 멈추지 않는다.
    반환: 빈 목록이면 제출해도 된다.
    해석: **전부 모으는 것이 요점이다.** 하나만 돌려주면 그것을 고친 뒤에야 다음이 보이고,
         「고쳤는데 여전히 안 나간다」가 반복된다. 이 목록이 그대로
         `signal_decisions.risk_gate_state`에 실려 리포트가 인쇄한다.
    실패 조건: 없다.

    ⚠ **이 함수는 리스크 게이트가 아니다.** 한도·서킷브레이커·14:50 컷오프는 이미
      `RiskEngine.evaluate_entry()`가 봤고(§12 독립 거부권), 여기 오는 것은 그것을 통과한
      후보다. 이 함수가 보는 것은 **「실행 경로가 준비됐는가」** 하나다. 둘을 섞으면 어느
      층이 거부했는지 사후에 못 가린다.
    """
    blockers: list[str] = []
    if gate_action != GateAction.AUTO_SUBMIT:
        blockers.append(BLOCKER_MODE_NOT_AUTO)
    if not reentry_cooldown_minutes or reentry_cooldown_minutes <= 0:
        blockers.append(BLOCKER_REENTRY_COOLDOWN_OFF)
    if entry_plan is None:
        blockers.append(BLOCKER_NO_ENTRY_PLAN)
    elif entry_plan.order_type == "LIMIT" and not entry_plan.limit_price:
        # 0.0을 지정가로 보내면 거래소가 거부한다 — 그런데 그 거부는 「주문 API가 안 된다」로
        # 오귀속되기 쉽다(`entry.DEFAULT_TICK_SIZE` 주석의 08-18 사례와 같은 함정).
        blockers.append(BLOCKER_NO_LIMIT_PRICE)
    if not symbol_resolved:
        # 행사가 라벨(`C1090.0`)로는 주문을 못 낸다 — 단축상품번호가 있어야 한다.
        blockers.append(BLOCKER_SYMBOL_UNRESOLVED)
    if qty <= 0:
        blockers.append(BLOCKER_QTY_NOT_POSITIVE)
    return blockers


def build_order(plan, order_id: str, now: datetime) -> Order:
    """
    입력: 승인된 `EntryPlan`, 로컬 주문 식별자, 주문 시각.
    계산: PENDING 상태의 `Order`를 만든다. 시장가면 `intended_px`에 0.0이 아니라
         `limit_price`(없으면 0.0)를 넣는다 — 시장가 주문의 의도 가격은 기록용이다.
    해석: `order_id`는 **로컬 식별자**다. 제출 응답의 KIS 주문번호(`ODNO`)로 갈아끼우는 것은
         `submit()`의 `extract_order_no`가 한다 — 안 갈아끼우면 체결 조회가 존재하지 않는
         번호를 묻고 **영원히 PENDING**을 돌려받는다(`submit()` docstring의 08-16 발견).
    실패 조건: 없다.
    """
    return Order(
        order_id=order_id,
        symbol=plan.symbol,
        side=plan.side,
        order_type=plan.order_type,
        intended_px=float(plan.limit_price or 0.0),
        qty=int(plan.qty),
        timestamp=now,
    )


def order_dvsn_cd(plan) -> str:
    """`EntryPlan.order_type` → KIS 주문구분코드. 01=지정가 / 02=시장가(`rest_client` 참고)."""
    return "02" if plan.order_type == "MARKET" else "01"
