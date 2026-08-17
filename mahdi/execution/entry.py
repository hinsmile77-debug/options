"""Execution Engine — Passive-first 진입 (v6 §13.2).

기본은 지정가(Passive) — 시장가 추격을 기본값으로 두지 않는다. -GEX 팽창 국면
(Urgency Mode)에서만 공격적 체결(시장가)을 허용하고, 시초 5분·이벤트 직후에는
그 공격성마저 자동으로 하향한다. 기존 `broker/order_state_machine.py`의
`Order`/`OrderStateMachine`을 그대로 재사용하도록 이 모듈은 주문 계획(EntryPlan)만
만들고 실제 제출은 `order_manager.py`가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

_OPENING_DAMPENING_UNTIL = time(9, 5)

# ===== 2026-08-18 — 호가단위의 단일 출처 =====
#
# **0.05를 쓰는 이유는 그것이 두 격자 모두에서 유효하기 때문이다.** 지수선물의 호가단위는
# 0.05이고 옵션은 프리미엄 구간에 따라 0.01까지 내려가는데, 0.05의 배수는 언제나 0.01의
# 배수이기도 하다(0.05 격자는 0.01 격자의 부분집합이다). 즉 이 값으로 스냅한 가격은 선물에서도
# 옵션에서도 거부되지 않는다 — 대신 저가 옵션에서는 해상도가 거칠어지므로, 그 구간을 다루는
# 호출측은 더 작은 tick을 명시해야 한다.
#
# **상수를 여기 두는 이유**: 2026-08-17까지 이 값이 두 곳에 따로 있었다 — 여기 `EntryContext`의
# 기본값 0.05와 `scripts/measure_order_roundtrip._away_price()`의 `round(x, 2)`(= 0.01)다.
# 후자는 주석에 "옵션 최소 호가단위"라고 적고 있었지만 그 스크립트가 실제로 겨눈 첫 대상은
# **선물(A01609)** 이었고, 실측 결과 현재가 2,000개 중 **1,600개(80%)가 0.05 격자를 위반**하는
# 지정가를 만들어냈다. 거래소가 거부하면 포지션은 안 생기지만 «주문 API가 안 된다»로 오귀속되기
# 쉽다 — 잔고 조회가 필수 파라미터 누락으로 넉 달간 실패했던 것과 같은 급의 함정이다.
DEFAULT_TICK_SIZE = 0.05


@dataclass(frozen=True, slots=True)
class EntryContext:
    symbol: str
    side: str  # BUY/SELL
    qty: int
    reference_price: float  # 현재 체결가/중간가 등 기준가
    now: time
    negative_gex_expansion: bool = False  # -GEX 팽창 국면 -> Urgency Mode 후보
    event_proximity_minutes: float | None = None
    event_dampening_minutes: float = 15.0
    tick_size: float = DEFAULT_TICK_SIZE
    passive_offset_ticks: int = 1  # 지정가를 기준가에서 몇 틱 안쪽에 두는지


@dataclass(frozen=True, slots=True)
class EntryPlan:
    symbol: str
    side: str
    qty: int
    order_type: str  # "LIMIT" | "MARKET"
    limit_price: float | None
    urgency: bool


def build_entry_plan(ctx: EntryContext) -> EntryPlan:
    """
    입력: EntryContext(방향/수량/기준가/현재시각/-GEX 팽창 여부/이벤트 근접도).
    계산: negative_gex_expansion이면 Urgency Mode 후보로 두되, 시초 5분(09:00~09:05) 또는
         이벤트 근접(event_proximity_minutes < event_dampening_minutes)이면 공격성을 강제로
         하향해 결국 지정가로 되돌린다(v6 §13.2 "시초 5분·이벤트 직후에는 공격성 자동 하향").
         Urgency Mode가 아니면 항상 지정가 — BUY는 기준가에서 tick_size만큼 아래,
         SELL은 위에 offset을 둬 Passive 체결을 유도한다.
    해석: urgency=True인 EntryPlan만 시장가(MARKET) — 그 외엔 전부 LIMIT.
    실패 조건: 없음 — offset 계산은 항상 결정론적.
    """
    urgency = ctx.negative_gex_expansion
    dampened = ctx.now < _OPENING_DAMPENING_UNTIL or (
        ctx.event_proximity_minutes is not None and ctx.event_proximity_minutes < ctx.event_dampening_minutes
    )
    if dampened:
        urgency = False

    if urgency:
        return EntryPlan(
            symbol=ctx.symbol, side=ctx.side, qty=ctx.qty, order_type="MARKET", limit_price=None, urgency=True
        )

    offset = ctx.tick_size * ctx.passive_offset_ticks
    limit_price = (
        ctx.reference_price - offset if ctx.side.upper() == "BUY" else ctx.reference_price + offset
    )
    return EntryPlan(
        symbol=ctx.symbol, side=ctx.side, qty=ctx.qty, order_type="LIMIT", limit_price=limit_price, urgency=False
    )


def forbid_averaging_down(has_open_position_same_direction: bool, is_new_signal: bool) -> bool:
    """
    계산: 같은 방향 포지션이 이미 있는데 새 신호(Signal Fusion 재평가) 없이 추가 진입하려는
         경우 True(금지)를 반환한다(v6 §13.2 "물타기(averaging down) 기본 금지").
    해석: True면 호출측(ExecutionEngine)이 진입을 즉시 거부해야 한다.
    실패 조건: 없음.
    """
    return has_open_position_same_direction and not is_new_signal
