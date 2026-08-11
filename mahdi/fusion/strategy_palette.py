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
    계산: 오늘 이미 쓴 전략(연속 사용)은 상한과 무관하게 통과시키고, **새 전략은 남은 슬롯
         (`cap - len(already_used_today)`)만큼만** 통과시킨다
         (v6 §11.4 "하루 레짐당 우선 전략군 2개 이하로 제한 — 다각화는 전략 수가 아니라
         알파 원천의 다각화").

    2026-08-05(운영점검보고서 2026-08-05 §2 이상점 6 / Fix#5) — **상한이 하루 누적으로
    걸리지 않고 있었다.** 종전 구현은 `(continuing + fresh)[:cap]`이라 **이번 호출의 목록
    길이**만 잘랐다. 그런데 `select_strategies()`가 반환하는 §11.4 매트릭스 셀은 전부
    원소 1개짜리다 — 즉 `[:2]`가 1개짜리 리스트를 자르는 일은 **한 번도 없었고**, 상한은
    호출 결과에 아무 영향을 주지 못했다.

    구체적으로: 오늘 이미 A·B 두 전략을 썼고(상한 도달) 이번 분에 C가 허용되면,
    종전 구현은 `continuing=[] / fresh=[C]` → `[C][:2] = [C]`로 **세 번째 전략을 통과**시켰다.
    이제는 남은 슬롯이 0이라 `[]`가 되어 실제로 막힌다.

    연속 사용(`continuing`)을 상한에서 제외하는 이유: 이미 쓴 전략을 계속하는 것은 **새로운
    알파 원천을 추가하는 행위가 아니다.** §11.4가 제한하려는 것은 "하루에 벌여놓는 전략군의
    가짓수"이지 기존 포지션의 유지가 아니다.
    실패 조건: cap이 0 이하면 빈 목록(신규/기존 전략 모두 차단).
    """
    if cap <= 0:
        return []
    continuing = [s for s in allowed_strategies if s in already_used_today]
    remaining_slots = max(cap - len(already_used_today), 0)
    fresh = [s for s in allowed_strategies if s not in already_used_today][:remaining_slots]
    return continuing + fresh


# ===== 2026-08-11 고도화 D — 동일 전략 재진입 쿨다운 (레버, 기본 OFF) =====
#
# ## 무엇이 이 항목의 착수 조건을 만들었는가
#
# 08-11에 ENTER가 **281건 / 494분**(56.9%)이었고, 형태가 분 단위 연속 스트림이었다
# (09:03~09:19에 09:05만 빼고 전부). ADVISORY라 무해했지만, `CURRENT_STATE`가
# *"`ExecutionEngine` 재진입 방지 로직 부재 — 배선 전 선행 해결 필요"* 라고 적어 둔 항목이
# 그날 **구체적 형태**를 얻었다: 실주문이었다면 16분 연속 진입이다.
#
# ## 하루 상한(`enforce_daily_strategy_cap`)이 이것을 못 막는 이유
#
# 그 상한은 **가짓수**를 제한한다 — "하루에 벌여놓는 전략군 2개 이하"(v6 §11.4). 같은 전략을
# 계속 쓰는 것은 `continuing`으로 **의도적으로 면제**돼 있고, 그 면제는 옳다(기존 포지션 유지는
# 새 알파 원천이 아니다). 즉 두 규칙은 **다른 것을 막는다**:
#
#     하루 상한   서로 다른 전략을 몇 개까지 벌일 것인가   (가짓수)
#     쿨다운      같은 전략에 얼마나 자주 들어갈 것인가     (빈도)
#
# ## 왜 기본 OFF인가
#
# 켜면 그날 ENTER 계열이 통째로 바뀐다. 08-11은 **레짐 엔진이 처음 라이브로 돈 날**이라
# 그 기준선이 아직 하루치뿐이고, 정상 진입 빈도가 얼마인지 모른다. **모르는 채 임계를 정하면
# 그 임계가 곧 결론이 된다** — 08-05 스팟 괴리율에서 한 실수다.
#
# 그리고 지금은 ADVISORY라 **막아서 얻는 것이 없다.** 이 레버의 값은 실주문 배선일에
# "그날 처음 짜는 코드"가 되지 않게 하는 데 있다(워치독이 08-06에 그렇게 만들어졌다가
# 08-11까지 한 번도 안 돌았다 — 만들어 두는 것과 도는 것은 다르다).
#
# ## 켤 조건과 예측치 (숫자를 보기 전에 적는다)
#
#   조건  `ExecutionEngine` 실주문 배선 **전에** 켠다. 그 전에 며칠치 ENTER 빈도 분포를 본다.
#   값    `strategy_gates.reentry_cooldown_minutes` (권고 시작값 15 — v6 §13.3 청산 레이어의
#         최단 시간 스케일과 같다. 그보다 짧으면 직전 진입의 청산 판단이 아직 안 끝난다)
#   주장  하루 ENTER 건수 감소 (08-11 기준선 **281건 / 494분 = 56.9%**)
#   주장  ENTER 사이 최소 간격 >= 쿨다운 (불변식 — 0건이어야 위반)
#   대가  `decision_outcomes.entries` 감소 → 사후 평가 표본이 준다. 며칠 더 쌓아야 한다.
#   대가  진입 기회 상실 — **이 값은 재지 않는다.** 놓친 진입의 성과는 정의상 관측 불가이고,
#         그것을 추정하려 들면 팔레트가 아니라 우리의 상상을 재게 된다.
REENTRY_COOLDOWN_DISABLED = 0


def enforce_reentry_cooldown(
    allowed_strategies: list[str],
    last_entry_minutes_ago: dict[str, float],
    cooldown_minutes: float,
) -> list[str]:
    """
    입력: 상한을 통과한 전략 목록, 전략별 **직전 진입 이후 경과 분**, 쿨다운(분).
    계산: 경과가 쿨다운보다 짧은 전략을 뺀다. 기록이 없는 전략(오늘 첫 진입)은 통과시킨다.
    해석: 상세 근거는 위 주석. `cooldown_minutes <= 0`이면 **아무것도 안 한다**(레버 OFF) —
         종전과 바이트 단위로 같은 동작이어야 한다.
    실패 조건: 없다(순수 함수).
    """
    if cooldown_minutes <= REENTRY_COOLDOWN_DISABLED:
        return list(allowed_strategies)
    return [
        s for s in allowed_strategies
        if last_entry_minutes_ago.get(s) is None or last_entry_minutes_ago[s] >= cooldown_minutes
    ]
