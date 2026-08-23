"""Execution Engine — 6-Layer Exit Stack + 확률 기반 재평가 (v6 §13.3, §13.4).

레이어 1(Hard Stop)~6(Forced Flat)을 순서대로 점검해 가장 먼저 트리거된 레이어를
반환한다. Belief Decay Stop(레이어 4)만 단순 불리언이 아니라 §13.4의 EV 공식 +
4대 악화 항목 개수로 부분청산(50%)/전량철수를 가른다 — 그 로직은 `reevaluate_position()`
으로 분리해 스펙의 pseudocode를 그대로 반영한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from mahdi.engines.regime import RegimeLabel

logger = logging.getLogger("mahdi.execution.exit_stack")


class ExitLayer(str, Enum):
    HARD_STOP = "hard_stop"
    STRUCTURE_STOP = "structure_stop"
    FLOW_STOP = "flow_stop"
    BELIEF_DECAY_STOP = "belief_decay_stop"
    TIME_STOP = "time_stop"
    FORCED_FLAT = "forced_flat_15_10"


# ===== 2026-08-17 — RegimeLabel 8종 -> exit_rules 키 매핑 =====
#
# **이 매핑이 없어서 생긴 일**: `PositionState.regime`은 `exit_rules`의 키 문자열인데,
# 8종 `RegimeLabel`을 그 문자열로 옮기는 함수가 **어디에도 없었다.** 유일한 호출부인
# 백테스트는 `_DEFAULT_REGIME_FOR_EXIT_RULES = "TREND_STRONG"`을 전 포지션에 박아 넣었고,
# 라이브는 `ExecutionEngine` 자체가 미배선이라 아무도 이 빈칸을 밟지 않았다.
#
# 빈칸의 대가는 `check_time_stop()`이 미정의 키에 **조용히 False**를 돌려주는 것이다 —
# 예외도 경고도 없이 "타임스톱 없음"이 된다. 실측 레짐 이력(2026-08-17 DB 전수):
#
#     RANGE_BREAK_PREP    414분 (28.3%)   -> RANGE_TIGHT     (정의됨)
#     VOL_COMPRESSION   1,047분 (71.7%)   -> VOL_COMPRESSION (**미정의**)
#     TREND_UP/DOWN         0분           -> 백테스트가 하드코딩하던 그 키다
#
# 즉 학습된 레짐이 나온 분의 **71.7%가 청산 시간 규칙이 없는 레짐**이고, 백테스트가 쓰던
# 키는 **한 번도 나온 적이 없다**. 배선 후 그 구간에서 열린 포지션은 15:10 강제청산까지
# 시간 방어선이 없다.
#
# **여기서 미정의 레짐에 값을 지어내지 않는 이유**: v6 §13.5가 정의한 청산 파라미터는 4종
# 뿐이다. 없는 값을 만들면 그 숫자가 곧 결론이 된다(08-05 스팟 괴리율에서 한 실수).
# 대신 **조용한 False를 시끄러운 경고로 바꾼다** — 값을 정하는 것은 사람의 일이고,
# 그 전까지 이 경고가 매번 남는 것이 설계 의도다.
EXIT_RULES_KEY_BY_REGIME: dict[RegimeLabel, str] = {
    RegimeLabel.TREND_UP_STRONG: "TREND_STRONG",
    RegimeLabel.TREND_DOWN_STRONG: "TREND_STRONG",
    RegimeLabel.RANGE_BALANCED: "RANGE_TIGHT",
    RegimeLabel.RANGE_BREAK_PREP: "RANGE_TIGHT",
    RegimeLabel.VOL_EXPANSION: "VOL_EXPANSION",
    RegimeLabel.VOL_COMPRESSION: "VOL_COMPRESSION",
    RegimeLabel.LIQUIDITY_THIN: "LIQUIDITY_THIN",
    RegimeLabel.CRISIS_DEFENSE: "CRISIS_DEFENSE",
}

_EXPIRY_DAY_KEY = "EXPIRY_DAY_0DTE"


def exit_rules_key(regime: RegimeLabel | int, *, is_expiry_day: bool = False) -> str:
    """
    입력: `RegimeLabel`(또는 그 정수값), 오늘이 만기 당일인지 여부.
    계산: 만기 당일이면 레짐과 무관하게 `EXPIRY_DAY_0DTE`를 돌려준다 — v6 §11.4가 0DTE를
         "만기 당일 전용 **별도 파라미터 세트**"로 정의했기 때문이다(감마 폭발 구간에서
         평시 레짐 파라미터를 쓰는 것이 위험의 본체다). 그 외에는 8종 매핑을 따른다.
    해석: 반환값이 `exit_rules`에 없을 수 있다 — 그 판정은 `resolve_exit_params()`가 한다.
    실패 조건: RegimeLabel로 변환되지 않는 값이면 ValueError(모르는 레짐을 조용히
         TREND_STRONG으로 흘려보내면 이 함수를 만든 이유가 사라진다).
    """
    if is_expiry_day:
        return _EXPIRY_DAY_KEY
    return EXIT_RULES_KEY_BY_REGIME[RegimeLabel(int(regime))]


@dataclass(frozen=True, slots=True)
class ExitParams:
    """`exit_rules`의 한 레짐 행 — `defined=False`면 그 레짐 행이 설정에 아예 없다."""

    key: str
    time_stop: float | None = None
    stop: float | None = None
    defined: bool = True


# 미정의 키 경고는 키당 1회만 — 매분 호출되는 경로라 로그가 잠긴다(`RegimeEngine.predict`의
# 길이 1 경고와 같은 이유).
_warned_missing_keys: set[str] = set()


def resolve_exit_params(regime_key: str, exit_rules_cfg: dict) -> ExitParams:
    """
    입력: `exit_rules` 키 문자열, 설정 dict.
    계산: 해당 행을 찾아 `time_stop`/`stop`을 꺼낸다. 행이 없으면 `defined=False`로 표시하고
         **키당 1회 경고**를 남긴다(종전에는 아무 신호 없이 타임스톱만 사라졌다).
    해석: `defined=False`는 "그 레짐의 청산 규칙을 우리가 아직 정하지 않았다"는 뜻이지
         "규칙이 없어도 된다"가 아니다. 호출측은 이 값을 화면/리포트에 노출해야 한다.
    실패 조건: 없음 — 예외를 던지지 않는다. 라이브 청산 경로에서 예외는 포지션을 방치한다.
    """
    row = exit_rules_cfg.get(regime_key)
    if row is None:
        if regime_key not in _warned_missing_keys:
            _warned_missing_keys.add(regime_key)
            logger.warning(
                "exit_rules에 '%s' 행이 없다 — 이 레짐의 포지션은 타임스톱·레짐 손절이 "
                "걸리지 않고 15:10 강제청산까지 간다. 값을 정하거나 이 레짐의 진입을 막을 것.",
                regime_key,
            )
        return ExitParams(key=regime_key, defined=False)
    return ExitParams(key=regime_key, time_stop=row.get("time_stop"), stop=row.get("stop"))


@dataclass(frozen=True, slots=True)
class PositionState:
    symbol: str
    side: str  # BUY/SELL(진입 방향)
    entry_price: float
    current_price: float
    entry_time_minutes: float  # 세션 시작 기준 경과 분 — 호출측이 계산해 전달
    now_minutes: float
    regime: str  # exit_rules 키 — `exit_rules_key()`로 RegimeLabel에서 변환해 넣을 것


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


def effective_stop_pct(params: ExitParams, hard_stop_pct: float) -> float:
    """
    입력: 해당 레짐의 `ExitParams`, 절대 손실 한도(`hard_stop_pct`, 음수).
    계산: 레짐 손절이 정의돼 있으면 **둘 중 더 타이트한 쪽**(둘 다 음수이므로 max)을 쓴다.
    해석: v6 §13.3 레이어 1은 "절대 허용 손실 한도(−1~2%)"이고, §13.5의 레짐별 `stop`은
         그보다 **먼저 걸리는 전술적 손절**이다(레인지 −0.8% < 절대한도 −2%). 둘을 별개
         레이어로 늘리지 않고 한 지점에서 합치는 이유: 레이어를 늘리면 §13.3 표가 6-Layer가
         아니게 되고 `gate_exit()`의 자동/승인 분류에 이름을 하나 더 정해야 한다 — 그것은
         스펙 변경이라 이 증분의 범위 밖이다. **더 타이트한 쪽을 쓰면 절대 한도는 결코
         느슨해지지 않는다**(안전 방향으로만 움직인다).
    실패 조건: 없음 — 레짐 손절이 없으면 `hard_stop_pct`를 그대로 돌려준다(종전 동작).
    """
    if params.stop is None:
        return hard_stop_pct
    return max(hard_stop_pct, float(params.stop))


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
    """
    계산: 레짐별 exit_rules.time_stop(분)을 경과 보유 시간과 비교.
    해석: 미설정 레짐은 여전히 False지만, 이제 `resolve_exit_params()`가 **경고를 남긴다** —
         종전에는 이 경로가 완전히 조용해서 "타임스톱이 안 걸린다"는 사실이 아무 데도
         나타나지 않았다.
    """
    params = resolve_exit_params(position.regime, exit_rules_cfg)
    if params.time_stop is None:
        return False
    return (position.now_minutes - position.entry_time_minutes) >= params.time_stop


# ===== 2026-08-23 (실행 배선 ④) — 레이어 4의 입력이 없으면 **평가하지 않는다** =====
#
# §13.4의 EV 공식은 `P(win)`·`AvgWin`·`AvgLoss`를 요구하고, 그 셋은 `trade_history`에서 온다.
# 그 표는 오늘 0행이다(08-23에 쓰기 함수가 처음 생겼다). 즉 **우리는 그 값을 모른다.**
#
# ## 왜 중립값으로 채우면 안 되는가
#
# `win_probability=0.5, avg_win=avg_loss`를 넣으면 EV가 정확히 0이 되고, `reevaluate_position()`
# 은 `ev <= 0`을 **악화 1건으로 센다.** 거기에 레짐 악화 같은 플래그가 하나만 더 붙으면 2건이
# 되어 **부분청산(50%)이 나간다** — 지어낸 숫자가 실제 청산 주문이 되는 것이다.
#
# 반대로 EV가 크게 양수가 되도록 채우면 레이어 4가 영원히 안 걸린다. 그것도 거짓이지만
# 방향이 다르다: 없는 신호를 만들어내는 대신 **없는 신호를 없다고 두는 것**이다.
#
# ## 그래서 세 번째 선택 — 평가하지 않고, 그 사실을 남긴다
#
# `belief=None`이면 레이어 4를 건너뛰고 키당 1회 경고를 남긴다. `resolve_exit_params()`가
# 미정의 레짐에 대해 하는 것과 **정확히 같은 처리**다: 조용한 False가 아니라 시끄러운 경고.
# 나머지 다섯 레이어(하드스톱·구조·플로우·타임스톱·강제청산)는 그대로 돈다.
_warned_belief_unavailable = False


def _warn_belief_unavailable() -> None:
    global _warned_belief_unavailable
    if _warned_belief_unavailable:
        return
    _warned_belief_unavailable = True
    logger.warning(
        "청산 레이어 4(Belief Decay)를 평가하지 않는다 — EV 입력(P(win)/AvgWin/AvgLoss)이 "
        "없다. `trade_history`가 쌓이기 전까지 이 레이어는 꺼져 있고, 그동안 확신도 붕괴에 "
        "의한 청산은 일어나지 않는다. 나머지 다섯 레이어는 정상 동작한다."
    )


def evaluate_exit_stack(
    position: PositionState,
    market: MarketStructureState,
    belief: BeliefState | None,
    exit_rules_cfg: dict,
    hard_stop_pct: float = -0.02,
) -> ExitDecision:
    """
    입력: 포지션/시장구조/확신 상태 + 레짐별 exit_rules 설정.
    계산: 레이어 6(Forced Flat, 해제 불가) -> 1(Hard Stop) -> 2(Structure Stop) ->
         3(Flow Stop) -> 4(Belief Decay, **`belief=None`이면 건너뛴다** — 위 주석) ->
         5(Time Stop) 순서로
         점검해 가장 먼저 트리거된 레이어를 반환한다(§13.3 표 순서, 단 Forced Flat은 항상
         자동·해제 불가라 최우선 점검).
    해석: action="HOLD"면 어떤 레이어도 트리거되지 않은 것.
    실패 조건: 없음 — 입력 부재는 각 check 함수의 문서화된 실패 조건대로 안전한 기본값으로 처리.
    """
    if market.is_forced_flat_time:
        return ExitDecision(triggered_layer=ExitLayer.FORCED_FLAT, action="FULL_EXIT", reason="15:10 강제청산")

    # 2026-08-17 — 레짐별 손절(§13.5)을 레이어 1에 합류시킨다. 자세한 근거는 `effective_stop_pct()`.
    params = resolve_exit_params(position.regime, exit_rules_cfg)
    stop_pct = effective_stop_pct(params, hard_stop_pct)
    if check_hard_stop(position, stop_pct):
        reason = f"레짐 손절 {stop_pct:.3%} 이탈({params.key})" if params.stop is not None else "hard_stop_pct 이탈"
        return ExitDecision(triggered_layer=ExitLayer.HARD_STOP, action="FULL_EXIT", reason=reason)
    if check_structure_stop(position, market):
        return ExitDecision(triggered_layer=ExitLayer.STRUCTURE_STOP, action="FULL_EXIT", reason="구조적 붕괴")
    if check_flow_stop(market):
        return ExitDecision(triggered_layer=ExitLayer.FLOW_STOP, action="FULL_EXIT", reason="수급/주문흐름 역행")

    # 2026-08-23 — `belief`가 None이면 레이어 4는 **평가되지 않는다**(위 주석). 지어낸
    # 중립값으로 EV를 계산하면 그 숫자가 그대로 청산 주문이 된다.
    if belief is None:
        _warn_belief_unavailable()
    else:
        belief_decision = reevaluate_position(belief)
        if belief_decision.action != "HOLD":
            return belief_decision

    if check_time_stop(position, exit_rules_cfg):
        return ExitDecision(triggered_layer=ExitLayer.TIME_STOP, action="FULL_EXIT", reason="레짐별 time_stop 도달")

    return ExitDecision(triggered_layer=None, action="HOLD")
