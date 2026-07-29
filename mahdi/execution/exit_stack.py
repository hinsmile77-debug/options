"""Execution Engine — 6-Layer Exit Stack + 확률 기반 재평가 (v6 §13.3, §13.4).

레이어 1(Hard Stop)~6(Forced Flat)을 순서대로 점검해 가장 먼저 트리거된 레이어를
반환한다. Belief Decay Stop(레이어 4)만 단순 불리언이 아니라 §13.4의 EV 공식 +
4대 악화 항목 개수로 부분청산(50%)/전량철수를 가른다 — 그 로직은 `reevaluate_position()`
으로 분리해 스펙의 pseudocode를 그대로 반영한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExitLayer(str, Enum):
    HARD_STOP = "hard_stop"
    STRUCTURE_STOP = "structure_stop"
    FLOW_STOP = "flow_stop"
    BELIEF_DECAY_STOP = "belief_decay_stop"
    TIME_STOP = "time_stop"
    FORCED_FLAT = "forced_flat_15_10"


@dataclass(frozen=True, slots=True)
class PositionState:
    symbol: str
    side: str  # BUY/SELL(진입 방향)
    entry_price: float
    current_price: float
    entry_time_minutes: float  # 세션 시작 기준 경과 분 — 호출측이 계산해 전달
    now_minutes: float
    regime: str  # exit_rules 키(TREND_STRONG/RANGE_TIGHT/VOL_EXPANSION/EXPIRY_DAY_0DTE)


@dataclass(frozen=True, slots=True)
class MarketStructureState:
    vwap: float | None = None
    poc: float | None = None  # Point of Control
    gamma_wall: float | None = None
    foreign_flow_reversed: bool = False
    ofi_reversed: bool = False
    is_forced_flat_time: bool = False


@dataclass(frozen=True, slots=True)
class BeliefState:
    """§13.4 EV = P(win)*AvgWin - P(loss)*AvgLoss - theta_decay - slippage 입력."""

    win_probability: float
    avg_win: float
    avg_loss: float
    theta_decay: float = 0.0
    expected_slippage: float = 0.0
    regime_degraded: bool = False
    volatility_state_mismatch: bool = False
    slippage_worsened: bool = False


@dataclass(frozen=True, slots=True)
class ExitDecision:
    triggered_layer: ExitLayer | None
    action: str  # "HOLD" | "PARTIAL_EXIT_50" | "FULL_EXIT"
    reason: str | None = None


def _pnl_pct(side: str, entry_price: float, current_price: float) -> float:
    if entry_price == 0:
        return 0.0
    diff = current_price - entry_price
    return diff / entry_price if side.upper() == "BUY" else -diff / entry_price


def check_hard_stop(position: PositionState, hard_stop_pct: float) -> bool:
    """계산: 손익률(_pnl_pct)이 hard_stop_pct(음수, 예: -0.02) 이하면 True."""
    return _pnl_pct(position.side, position.entry_price, position.current_price) <= hard_stop_pct


def check_structure_stop(position: PositionState, market: MarketStructureState) -> bool:
    """계산: VWAP 이탈/POC 붕괴/Gamma Wall 돌파 중 방향에 불리한 쪽으로 하나라도 있으면 True."""
    price = position.current_price
    is_long = position.side.upper() == "BUY"
    for level in (market.vwap, market.poc, market.gamma_wall):
        if level is None:
            continue
        if is_long and price < level:
            return True
        if not is_long and price > level:
            return True
    return False


def check_flow_stop(market: MarketStructureState) -> bool:
    """계산: 외국인 수급 반전 또는 OFI 역행 중 하나라도 있으면 True."""
    return market.foreign_flow_reversed or market.ofi_reversed


def reevaluate_position(belief: BeliefState) -> ExitDecision:
    """
    입력: BeliefState(승률/평균익절/평균손절/theta/예상슬리피지 + 3개 악화 플래그).
    계산: EV = win_probability*avg_win - (1-win_probability)*avg_loss - theta_decay -
         expected_slippage. 4대 악화 항목(① EV<=0 ② regime_degraded ③
         volatility_state_mismatch ④ slippage_worsened) 중 동시 해당 개수를 센다.
    해석: 2개 악화 -> 부분 축소(50%) 제안, 3개 이상 -> 손익 무관 전량 철수(v6 §13.4).
    실패 조건: 없음 — 악화 0~1개는 HOLD.
    """
    ev = (
        belief.win_probability * belief.avg_win
        - (1 - belief.win_probability) * belief.avg_loss
        - belief.theta_decay
        - belief.expected_slippage
    )
    degraded_count = sum(
        [
            ev <= 0,
            belief.regime_degraded,
            belief.volatility_state_mismatch,
            belief.slippage_worsened,
        ]
    )
    if degraded_count >= 3:
        return ExitDecision(
            triggered_layer=ExitLayer.BELIEF_DECAY_STOP,
            action="FULL_EXIT",
            reason=f"{degraded_count}개 악화 항목 동시 발생",
        )
    if degraded_count == 2:
        return ExitDecision(
            triggered_layer=ExitLayer.BELIEF_DECAY_STOP,
            action="PARTIAL_EXIT_50",
            reason="2개 악화 항목 동시 발생",
        )
    return ExitDecision(triggered_layer=None, action="HOLD")


def check_time_stop(position: PositionState, exit_rules_cfg: dict) -> bool:
    """계산: 레짐별 exit_rules.time_stop(분)을 경과 보유 시간과 비교. 미설정 레짐은 항상 False."""
    time_stop = exit_rules_cfg.get(position.regime, {}).get("time_stop")
    if time_stop is None:
        return False
    return (position.now_minutes - position.entry_time_minutes) >= time_stop


def evaluate_exit_stack(
    position: PositionState,
    market: MarketStructureState,
    belief: BeliefState,
    exit_rules_cfg: dict,
    hard_stop_pct: float = -0.02,
) -> ExitDecision:
    """
    입력: 포지션/시장구조/확신 상태 + 레짐별 exit_rules 설정.
    계산: 레이어 6(Forced Flat, 해제 불가) -> 1(Hard Stop) -> 2(Structure Stop) ->
         3(Flow Stop) -> 4(Belief Decay, reevaluate_position 위임) -> 5(Time Stop) 순서로
         점검해 가장 먼저 트리거된 레이어를 반환한다(§13.3 표 순서, 단 Forced Flat은 항상
         자동·해제 불가라 최우선 점검).
    해석: action="HOLD"면 어떤 레이어도 트리거되지 않은 것.
    실패 조건: 없음 — 입력 부재는 각 check 함수의 문서화된 실패 조건대로 안전한 기본값으로 처리.
    """
    if market.is_forced_flat_time:
        return ExitDecision(triggered_layer=ExitLayer.FORCED_FLAT, action="FULL_EXIT", reason="15:10 강제청산")
    if check_hard_stop(position, hard_stop_pct):
        return ExitDecision(triggered_layer=ExitLayer.HARD_STOP, action="FULL_EXIT", reason="hard_stop_pct 이탈")
    if check_structure_stop(position, market):
        return ExitDecision(triggered_layer=ExitLayer.STRUCTURE_STOP, action="FULL_EXIT", reason="구조적 붕괴")
    if check_flow_stop(market):
        return ExitDecision(triggered_layer=ExitLayer.FLOW_STOP, action="FULL_EXIT", reason="수급/주문흐름 역행")

    belief_decision = reevaluate_position(belief)
    if belief_decision.action != "HOLD":
        return belief_decision

    if check_time_stop(position, exit_rules_cfg):
        return ExitDecision(triggered_layer=ExitLayer.TIME_STOP, action="FULL_EXIT", reason="레짐별 time_stop 도달")

    return ExitDecision(triggered_layer=None, action="HOLD")
