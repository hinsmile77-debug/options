"""Backtest 엔진 — 이벤트 재생 루프 (v6 PART 21 "백테스트 엔진").

실거래 이력이 아직 3주 남짓이라 의미 있는 과거 검증은 불가능하다 — 이번 증분은
`SignalFusionEngine` -> `RiskEngine`(ExecutionEngine 내부) -> `ExecutionEngine`을
그대로 재사용하는 재생 루프 프레임워크만 구현하고, 합성 픽스처로 프레임워크 자체의
정합성만 검증한다(WFO/Monte Carlo/DSR 검증 스택은 실데이터가 쌓인 뒤의 후속 증분).

실제 피처 재계산(체결틱 -> 봉 -> 신호)은 이 엔진의 책임이 아니다 — 각 스텝의 신호
입력은 호출측이 미리 준비해서 넘긴다("같은 결정 엔진, 다른 데이터 공급원" 원칙,
features/orderflow.py의 피처 사전 Single Source of Truth와 같은 정신). 한 번에
포지션 1개만 다룬다(다중 동시 포지션은 이번 증분 범위 밖).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from mahdi.backtest.simulated_broker import Bar, FillConfig, fill_market_at_next_open, try_fill_limit
from mahdi.broker.order_state_machine import Order, OrderState
from mahdi.execution.engine import EntryRequest, ExecutionEngine
from mahdi.execution.entry import EntryContext
from mahdi.execution.exit_stack import BeliefState, MarketStructureState, PositionState
from mahdi.execution.hybrid_mode import HybridMode
from mahdi.fusion.engine import FusionDecision, MetaLabelContext, SignalFusionEngine
from mahdi.fusion.meta_label import TradePermission
from mahdi.fusion.signal_layer import SignalInputs
from mahdi.risk.circuit_breaker import MarketConditions
from mahdi.risk.limits import AccountState

_SESSION_EPOCH = datetime(2000, 1, 1)
_DEFAULT_REGIME_FOR_EXIT_RULES = "TREND_STRONG"


@dataclass(frozen=True, slots=True)
class BacktestStep:
    """한 봉에 대응하는 재생 루프 1스텝 — 실시간 파이프라인의 "매 선물봉마다" 호출과 대응."""

    bar: Bar
    signal_inputs: SignalInputs
    account_state: AccountState
    market_conditions: MarketConditions
    meta_context: MetaLabelContext = field(default_factory=MetaLabelContext)
    market_structure: MarketStructureState = field(default_factory=MarketStructureState)
    belief_state_if_open: BeliefState | None = None
    vrp: float = 0.0
    strategy_id: str = "backtest_strategy"
    # 2026-08-17 — 이 봉 시점의 실제 레짐(`exit_rules` 키). None이면 종전대로
    # `_DEFAULT_REGIME_FOR_EXIT_RULES`를 쓴다(기존 픽스처 하위호환).
    #
    # **이 필드가 없어서 생긴 왜곡**: 종전 백테스트는 전 포지션에 "TREND_STRONG"을 박았고,
    # 그 키의 청산 파라미터는 `time_stop: 120` + 절대손절 −2%다. 실측 레짐 이력에서
    # TREND_STRONG 계열은 **0분**이다 — 즉 지금까지의 백테스트는 한 번도 일어난 적 없는
    # 레짐의 규칙으로 손익 분포를 재고 있었다.
    exit_rules_regime: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    entry_price: float
    exit_price: float
    side: str
    qty: int
    net_pnl: float
    exit_reason: str
    # 2026-08-17 — 청산 분석에는 "얼마나 들고 있었나"와 "그때 어느 레짐이었나"가 필요하다.
    # 종전에는 둘 다 없어서 `time_stop`이 실제로 무는지 사후에 확인할 방법이 없었다.
    holding_minutes: float = 0.0
    regime_at_exit: str = ""


def _minutes_to_time(minutes: float) -> time:
    total = max(0, min(23 * 60 + 59, int(minutes)))
    hour, minute = divmod(total, 60)
    return time(hour, minute)


def _sizing_input_from_decision(decision: FusionDecision, base_size: float):
    from mahdi.risk.sizing import PositionSizingInput

    return PositionSizingInput(
        base_size=base_size,
        regime_confidence=1.0,
        signal_quality=max(decision.conviction_score, 0.0),
        target_vol=0.01,
        realized_vol=0.01,
        liquidity_score=1.0,
        drawdown_pct=0.0,
        portfolio_capacity_remaining_pct=1.0,
    )


class BacktestEngine:
    """`SignalFusionEngine`/`ExecutionEngine`을 그대로 재사용해 합성 봉 시퀀스를 재생한다."""

    def __init__(
        self,
        fusion_engine: SignalFusionEngine,
        execution_engine: ExecutionEngine,
        fill_config: FillConfig | None = None,
        hybrid_mode: HybridMode = HybridMode.FULL_AUTO,
    ) -> None:
        self._fusion = fusion_engine
        self._execution = execution_engine
        self._fill_config = fill_config if fill_config is not None else FillConfig()
        self._hybrid_mode = hybrid_mode

    def run(self, steps: list[BacktestStep], base_size: float = 1.0) -> list[BacktestTrade]:
        """
        입력: BacktestStep 시퀀스(시간순), 신규 진입 사이징 base_size.
        계산: 매 스텝마다 (1) 대기 중인 진입 주문 체결 시도(LIMIT은 이번 봉 터치,
             MARKET은 이번 봉 시가+슬리피지) -> (2) 체결되면 포지션 오픈 -> (3) 보유
             포지션이 있으면 ExecutionEngine.evaluate_exit()로 청산 평가, FULL_EXIT/
             PARTIAL_EXIT_50이면 이번 봉 종가로 청산 처리 -> (4) 포지션도 대기 주문도
             없으면 SignalFusionEngine.evaluate()로 새 진입 후보를 만들고
             ExecutionEngine.evaluate_entry()로 승인받아 주문을 낸다.
        해석: 한 번에 포지션 1개만 다루므로, 진입 주문이 대기 중이거나 포지션이 열려
             있는 동안은 새 신호를 평가하지 않는다.
        실패 조건: 없음 — 신호가 전혀 트리거되지 않으면 빈 거래 목록을 반환한다.
        """
        trades: list[BacktestTrade] = []
        open_order: Order | None = None
        open_position: PositionState | None = None
        prev_bar: Bar | None = None

        for step in steps:
            if open_order is not None and open_order.state == OrderState.PENDING:
                if open_order.order_type == "LIMIT":
                    open_order = try_fill_limit(open_order, step.bar)
                elif prev_bar is not None:
                    open_order = fill_market_at_next_open(open_order, step.bar, self._fill_config)
                if open_order.state == OrderState.FILLED:
                    open_position = PositionState(
                        symbol=open_order.symbol,
                        side=open_order.side,
                        entry_price=open_order.filled_px,
                        current_price=step.bar.close,
                        entry_time_minutes=step.bar.timestamp_minutes,
                        now_minutes=step.bar.timestamp_minutes,
                        regime=step.exit_rules_regime or _DEFAULT_REGIME_FOR_EXIT_RULES,
                    )
                    open_order = None

            if open_position is not None:
                # 레짐은 보유 중에도 바뀐다 — 매 봉의 값으로 갱신한다(진입 시점에 고정하면
                # 압축에서 들어가 팽창으로 바뀐 포지션이 끝까지 압축 규칙으로 관리된다).
                open_position = replace(
                    open_position,
                    current_price=step.bar.close,
                    now_minutes=step.bar.timestamp_minutes,
                    regime=step.exit_rules_regime or open_position.regime,
                )
                belief = step.belief_state_if_open or BeliefState(win_probability=0.5, avg_win=1.0, avg_loss=1.0)
                decision, _gate = self._execution.evaluate_exit(
                    open_position,
                    step.market_structure,
                    belief,
                    step.account_state,
                    step.market_conditions,
                    self._hybrid_mode,
                )
                if decision.action in ("FULL_EXIT", "PARTIAL_EXIT_50"):
                    exit_price = step.bar.close
                    diff = exit_price - open_position.entry_price
                    pnl = diff if open_position.side.upper() == "BUY" else -diff
                    trades.append(
                        BacktestTrade(
                            entry_price=open_position.entry_price,
                            exit_price=exit_price,
                            side=open_position.side,
                            qty=1,
                            net_pnl=pnl,
                            exit_reason=decision.reason or "unknown",
                            holding_minutes=open_position.now_minutes - open_position.entry_time_minutes,
                            regime_at_exit=open_position.regime,
                        )
                    )
                    open_position = None

            elif open_order is None:
                fusion_decision = self._fusion.evaluate(step.signal_inputs, step.meta_context, step.vrp)
                if fusion_decision.trade_permission != TradePermission.NO_TRADE and fusion_decision.direction != 0.0:
                    side = "BUY" if fusion_decision.direction > 0 else "SELL"
                    entry_context = EntryContext(
                        symbol="BACKTEST",
                        side=side,
                        qty=1,
                        reference_price=step.bar.close,
                        now=_minutes_to_time(step.bar.timestamp_minutes),
                    )
                    entry_request = EntryRequest(
                        entry_context=entry_context,
                        sizing_input=_sizing_input_from_decision(fusion_decision, base_size),
                        account_state=step.account_state,
                        strategy_id=step.strategy_id,
                        market_conditions=step.market_conditions,
                    )
                    outcome = self._execution.evaluate_entry(entry_request, self._hybrid_mode)
                    if outcome.approved and outcome.entry_plan is not None:
                        open_order = Order(
                            order_id=f"bt_{step.bar.timestamp_minutes}",
                            symbol=outcome.entry_plan.symbol,
                            side=outcome.entry_plan.side,
                            order_type=outcome.entry_plan.order_type,
                            intended_px=outcome.entry_plan.limit_price or step.bar.close,
                            qty=outcome.entry_plan.qty,
                            timestamp=_SESSION_EPOCH + timedelta(minutes=step.bar.timestamp_minutes),
                        )

            prev_bar = step.bar

        return trades
