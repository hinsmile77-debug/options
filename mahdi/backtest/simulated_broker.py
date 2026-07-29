"""Backtest — 체결 시뮬레이터 (v6 PART 21 "백테스트 엔진").

실거래와 같은 `Order`/`OrderStateMachine`(broker/order_state_machine.py)을 그대로 써서
백테스트와 실거래가 같은 자료구조를 공유한다(피처 사전 Single Source of Truth와 같은
원칙, features/orderflow.py 모듈 docstring 참고). 체결 규칙은 의도적으로 단순화한다 —
LIMIT은 봉의 저가~고가가 지정가를 스치면(터치) 그 가격에 체결, MARKET은 다음 봉의
시가에 슬리피지를 불리하게 더해 체결한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from mahdi.broker.order_state_machine import Order, OrderState, OrderStateMachine


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp_minutes: float  # 세션 시작 기준 경과 분(단순화를 위해 datetime 대신 사용)
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FillConfig:
    slippage_ticks: float = 1.0
    tick_size: float = 0.05
    commission_per_contract: float = 0.0


def try_fill_limit(order: Order, bar: Bar) -> Order:
    """
    입력: PENDING LIMIT Order, 현재 봉.
    계산: BUY는 봉의 저가가 intended_px 이하로 내려오면(터치) intended_px에 체결.
         SELL은 봉의 고가가 intended_px 이상으로 오르면 체결.
    해석: 체결 안 되면 원래 Order를 그대로 반환(PENDING 유지) — 다음 봉에서 다시 시도한다.
    실패 조건: order.order_type이 "LIMIT"이 아니면 ValueError(호출측 오용 방지).
    """
    if order.order_type != "LIMIT":
        raise ValueError("try_fill_limit()은 LIMIT 주문에만 사용한다")
    touched = bar.low <= order.intended_px if order.side.upper() == "BUY" else bar.high >= order.intended_px
    if not touched:
        return order
    machine = OrderStateMachine(order)
    return machine.transition(OrderState.FILLED, filled_px=order.intended_px, filled_qty=order.qty)


def fill_market_at_next_open(order: Order, next_bar: Bar, config: FillConfig) -> Order:
    """
    입력: PENDING MARKET Order, 다음 봉, 체결 설정(슬리피지 틱 수/틱 크기).
    계산: next_bar.open에 슬리피지(slippage_ticks x tick_size)를 항상 불리한 방향으로
         더해 체결가를 정한다(BUY는 위로, SELL은 아래로) — 시뮬레이션이 실제 체결보다
         낙관적이 되지 않도록 하는 보수적 가정.
    실패 조건: order.order_type이 "MARKET"이 아니면 ValueError.
    """
    if order.order_type != "MARKET":
        raise ValueError("fill_market_at_next_open()은 MARKET 주문에만 사용한다")
    slippage = config.slippage_ticks * config.tick_size
    fill_price = next_bar.open + slippage if order.side.upper() == "BUY" else next_bar.open - slippage
    machine = OrderStateMachine(order)
    return machine.transition(OrderState.FILLED, filled_px=fill_price, filled_qty=order.qty)
