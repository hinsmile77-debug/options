"""Execution Engine — Forced Flat 종료 시퀀스 자기검증 (v6 §13.3 L6, [[DECISION_LOG]] 2026-07-21).

15:10 강제청산은 "청산 주문을 보낸다"만으로 완성이 아니다 — 실제로 체결됐고 잔여
미종결 주문이 없는지 스스로 재확인해야 한다([[DECISION_LOG]] 2026-07-21 "종료
시퀀스 자기검증 없이는 배포 금지"). 이 모듈은 주문 구성과 그 자기검증만 담당한다
— 실제 제출/체결 폴링은 `order_manager.py`의 submit()/confirm_fill()이 수행하고,
그 반복 폴링 루프 자체는 main.py 라이브 배선 시점에 만들어진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mahdi.broker.order_state_machine import Order, OrderState


@dataclass(frozen=True, slots=True)
class OpenPosition:
    symbol: str
    side: str  # 진입 방향(BUY/SELL) — 청산 주문은 반대 방향
    qty: int
    current_price: float = 0.0


@dataclass(frozen=True, slots=True)
class ForcedFlatResult:
    all_flat: bool
    unconfirmed_order_ids: list[str] = field(default_factory=list)
    verified_at: datetime | None = None


def build_forced_flat_orders(open_positions: list[OpenPosition], now: datetime) -> list[Order]:
    """
    입력: 현재 보유 포지션 목록, 청산 시각.
    계산: 각 포지션의 반대 방향 시장가 주문을 PENDING 상태로 구성한다(강제청산은 체결
         확실성이 지정가보다 중요 — v6 §13.3 "15:10 이전 무조건 청산"과 배치되지 않도록
         시장가 고정).
    실패 조건: open_positions가 비어 있으면 빈 목록(청산할 게 없음).
    """
    orders: list[Order] = []
    for i, pos in enumerate(open_positions):
        opposite_side = "SELL" if pos.side.upper() == "BUY" else "BUY"
        orders.append(
            Order(
                order_id=f"forced_flat_{pos.symbol}_{now.strftime('%H%M%S')}_{i}",
                symbol=pos.symbol,
                side=opposite_side,
                order_type="MARKET",
                intended_px=pos.current_price,
                qty=pos.qty,
                timestamp=now,
            )
        )
    return orders


def verify_forced_flat(orders: list[Order], verified_at: datetime) -> ForcedFlatResult:
    """
    입력: 15:10 강제청산으로 제출한 주문들의 최신 상태 목록(order_manager.confirm_fill()로
         갱신된 뒤), 검증 시각.
    계산: 종결 상태 중 실제로 청산에 성공한 것은 FILLED뿐이다 — CANCELLED/REJECTED는
         "청산 시도가 실패해 포지션이 그대로 남았다"는 뜻이므로 PENDING/PARTIAL과 동일하게
         미종결(미확인)로 취급한다.
    해석: all_flat=False면 잔존 포지션이 있다는 뜻 — 호출측이 즉시 재시도 또는 Slack 알림을
         트리거해야 한다(조용히 넘어가면 [[DECISION_LOG]] 2026-07-21 배포 금지 조건 위반).
    실패 조건: orders가 비어 있으면(청산할 포지션이 애초에 없었음) all_flat=True.
    """
    unconfirmed = [o.order_id for o in orders if o.state != OrderState.FILLED]
    return ForcedFlatResult(all_flat=not unconfirmed, unconfirmed_order_ids=unconfirmed, verified_at=verified_at)
