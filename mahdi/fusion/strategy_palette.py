"""Signal Fusion — Strategy Palette (레짐 x IV 매트릭스, v6 §11.4).

레짐과 VRP(IV 고/저평가) 조합으로 허용 전략 목록을 정하는 순수 룩업 + 두 가지 구조적
제약: (1) 프리미엄 매도 계열은 `strategy_gates.short_gamma_requires`(최고신뢰+positive
GEX+안정 레짐)를 전부 만족해야만 허용, (2) 하루 우선 전략군은
`strategy_gates.max_priority_strategies_per_regime_day`개로 제한한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from mahdi.engines.regime import RegimeLabel

# §11.4 매트릭스의 "레짐" 행 — RegimeLabel 8종을 매트릭스 4행에 매핑한다. LIQUIDITY_THIN/
# CRISIS_DEFENSE는 매트릭스에 없는 방어 전용 레짐이라 별도 처리(항상 빈 목록).
_TREND_STRONG = frozenset({RegimeLabel.TREND_UP_STRONG, RegimeLabel.TREND_DOWN_STRONG})
_RANGE_TIGHT = frozenset({RegimeLabel.RANGE_BALANCED, RegimeLabel.RANGE_BREAK_PREP})
_VOL_EXPANSION = frozenset({RegimeLabel.VOL_EXPANSION})
_VOL_COMPRESSION = frozenset({RegimeLabel.VOL_COMPRESSION})
_DEFENSIVE_ONLY = frozenset({RegimeLabel.LIQUIDITY_THIN, RegimeLabel.CRISIS_DEFENSE})

_PREMIUM_SELL_STRATEGIES = frozenset({"limited_premium_sell", "highly_limited_premium_sell"})

# 2026-07-30(운영점검보고서 §2-2): §11.4 매트릭스의 "fair" 열에는 실제 진입 전략이 아니라
# **관망 지시**가 들어있는 셀이 둘 있다 — RANGE_TIGHT×fair의 `wait_and_see`(관망)와
# VOL_COMPRESSION×fair의 `breakout_wait`(돌파 대기). 둘 다 "지금은 들어가지 말고 기다려라"라는
# 뜻인데, 호출측이 `bool(allowed_strategies)`만 보고 진입 후보 여부를 판정하면 **비어있지 않은
# 리스트라는 이유만으로 진입으로 계수**된다. 07-30 실측에서 정확히 이 일이 벌어져 08:45~15:44
# 419분 내내 `decision="ENTER"`가 기록됐다(전부 allowed_strategies=["wait_and_see"]).
#
# 매트릭스 자체에서 빼지 않고 별도 집합으로 분리하는 이유: `wait_and_see`는 v6 §11.4가 정의한
# 정당한 셀 값이고, COCKPIT "판단 현황" 패널이 "지금 왜 안 들어가는지"를 보여주려면 그 값이
# 그대로 남아있어야 한다. 팔레트는 사실 그대로 반환하고, **진입 여부 판정만** 이 집합을 걸러낸다.
NON_ENTRY_STRATEGIES = frozenset({"wait_and_see", "breakout_wait"})


@dataclass(frozen=True, slots=True)
class StrategyPaletteResult:
    allowed_strategies: list[str]
    reason: str | None = None  # 빈 목록일 때(방어 레짐/게이트 미충족) 사유


def _vrp_state(vrp: float, neutral_band: float) -> str:
    if vrp < -neutral_band:
        return "underpriced"
    if vrp > neutral_band:
        return "overpriced"
    return "fair"


def select_strategies(
    regime: RegimeLabel,
    vrp: float,
    *,
    highest_confidence: bool = False,
    positive_gex: bool = False,
    stable_regime: bool = True,
    vrp_neutral_band: float = 0.02,
) -> StrategyPaletteResult:
    """
    입력: 현재 레짐, VRP(iv-realized_vol), 프리미엄 매도 게이트 3개 플래그
         (`strategy_gates.short_gamma_requires`와 1:1 대응).
    계산: 레짐을 매트릭스 4행(TREND_STRONG/RANGE_TIGHT/VOL_EXPANSION/VOL_COMPRESSION) 중
         하나로 매핑하고, VRP를 저평가/적정/고평가 3열로 분류해 v6 §11.4 매트릭스의 해당
         셀을 반환한다. 프리미엄 매도 계열 전략은 3개 게이트를 전부 만족할 때만 포함한다.
    해석: LIQUIDITY_THIN/CRISIS_DEFENSE는 매트릭스에 없는 방어 전용 레짐이라 항상 빈 목록
         + reason="defensive_regime_no_new_entries"(v6 §12.4 방어 시나리오 우선).
    실패 조건: 없음 — 매핑 안 되는 레짐은 없다(8종 전부 4행 또는 방어 그룹으로 분류됨).
    """
    if regime in _DEFENSIVE_ONLY:
        return StrategyPaletteResult(allowed_strategies=[], reason="defensive_regime_no_new_entries")

    vrp_state = _vrp_state(vrp, vrp_neutral_band)

    if regime in _TREND_STRONG:
        matrix_row = {"underpriced": ["atm_long"], "fair": ["itm_debit"], "overpriced": ["debit_spread"]}
    elif regime in _RANGE_TIGHT:
        matrix_row = {
            "underpriced": ["small_strangle_buy"],
            "fair": ["wait_and_see"],
            "overpriced": ["limited_premium_sell"],
        }
    elif regime in _VOL_EXPANSION:
        matrix_row = {"underpriced": ["atm_straddle"], "fair": ["long_gamma"], "overpriced": []}
    elif regime in _VOL_COMPRESSION:
        matrix_row = {
            "underpriced": ["straddle_accumulate"],
            "fair": ["breakout_wait"],
            "overpriced": ["highly_limited_premium_sell"],
        }
    else:  # pragma: no cover - 8종 RegimeLabel 전부 위 그룹에 속함
        matrix_row = {"underpriced": [], "fair": [], "overpriced": []}

    allowed = list(matrix_row[vrp_state])

    gate_ok = highest_confidence and positive_gex and stable_regime
    if not gate_ok:
        removed_gated = [s for s in allowed if s in _PREMIUM_SELL_STRATEGIES]
        allowed = [s for s in allowed if s not in _PREMIUM_SELL_STRATEGIES]
        if removed_gated and not allowed:
            return StrategyPaletteResult(allowed_strategies=[], reason="short_gamma_requires_not_met")

    if not allowed:
        return StrategyPaletteResult(allowed_strategies=[], reason="no_strategy_for_this_cell")
    return StrategyPaletteResult(allowed_strategies=allowed)


def entry_strategies(allowed_strategies: list[str]) -> list[str]:
    """
    입력: `select_strategies()`(또는 `FusionDecision.allowed_strategies`)가 반환한 허용 전략 목록.
    계산: `NON_ENTRY_STRATEGIES`(관망/대기 지시)를 제거하고 남은 실제 진입 전략만 순서대로 반환.
    해석: 반환값이 비어 있으면 "팔레트가 관망만 지시했다" — 진입 후보가 아니다. 호출측은
         `bool(allowed_strategies)`가 아니라 **이 함수의 결과**로 진입 여부를 판정해야 한다
         (2026-07-30 운영점검 §2-2, 419건 오계수의 재발 방지 지점).
    실패 조건: 없음 — 입력이 비어 있으면 빈 목록.
    """
    return [s for s in allowed_strategies if s not in NON_ENTRY_STRATEGIES]


def enforce_daily_strategy_cap(
    allowed_strategies: list[str],
    already_used_today: frozenset[str],
    cap: int,
) -> list[str]:
    """
    입력: select_strategies()가 반환한 허용 목록, 오늘 이미 사용한 전략 집합, 하루 상한
         (`strategy_gates.max_priority_strategies_per_regime_day`).
    계산: 오늘 이미 쓴 전략(연속 사용)을 우선 포함하고, 남는 슬롯을 새 전략으로 채운다
         (v6 §11.4 "하루 레짐당 우선 전략군 2개 이하로 제한 — 다각화는 전략 수가 아니라
         알파 원천의 다각화").
    실패 조건: cap이 0 이하면 빈 목록(신규/기존 전략 모두 차단).
    """
    if cap <= 0:
        return []
    continuing = [s for s in allowed_strategies if s in already_used_today]
    fresh = [s for s in allowed_strategies if s not in already_used_today]
    return (continuing + fresh)[:cap]
