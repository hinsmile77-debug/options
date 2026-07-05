"""E3 옵션 인텔리전스 — GEX/Gamma Flip/Gamma Wall/Vanna·Charm/VRP (v6 §9.2).

체인 레그는 `option_analysis_1m` DB 스키마와 1:1 대응하는 OptionLeg로 표현해, 실시간 수집·
백테스트·대시보드가 동일한 자료구조를 공유하도록 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Sequence

from vollib.black_scholes.greeks.analytical import gamma as _bs_gamma

_CALL_PUT_SIGN = {"c": 1.0, "p": -1.0}


@dataclass(frozen=True, slots=True)
class OptionLeg:
    """옵션 체인의 행사가 1개 레그. vanna/charm은 Greeks 엔진에서 미리 계산해 채운다."""

    strike: float
    option_type: str  # "c" | "p"
    oi: float
    iv: float          # 내재변동성 (decimal, 예: 0.18)
    t_years: float      # 잔존만기(연 단위)
    gamma: float
    vanna: float = 0.0
    charm: float = 0.0


def calculate_gex(legs: Sequence[OptionLeg], spot: float, multiplier: float = 250_000) -> float:
    """
    GEX = Sigma(Gamma x OI x multiplier x S^2/100), call(+) put(-) 관례.

    입력: 옵션 체인 레그(감마·미결제약정 포함), 기초자산 현재가, 계약승수
         (KOSPI200 옵션 = 250,000원/포인트).
    계산: 레그별 감마 익스포저를 부호(콜+/풋-) 규약으로 합산.
    해석: +GEX -> 딜러가 변동성을 억제(회귀장), -GEX -> 증폭(추세·급변장).
    실패 조건: legs가 비어있으면 0.0.
    """
    s_term = spot**2 / 100
    return sum(_CALL_PUT_SIGN[leg.option_type] * leg.gamma * leg.oi * multiplier * s_term for leg in legs)


def find_gamma_flip(
    legs: Sequence[OptionLeg],
    spot: float,
    multiplier: float = 250_000,
    risk_free_rate: float = 0.035,
    search_pct: float = 0.05,
    steps: int = 41,
) -> float | None:
    """
    GEX 부호가 바뀌는 기초자산 레벨(Gamma Flip) — 이탈 시 urgency 모드.

    입력: 옵션 체인 레그(행사가·IV·잔존만기 포함), 현재 스팟, 계약승수.
    계산: 스팟 ±search_pct 구간을 steps개 그리드로 나눠 각 지점에서 Black-Scholes 감마를
         재계산(행사가·IV·잔존만기는 고정, 스팟만 이동)해 GEX(S)를 구성한 뒤, 부호가 바뀌는
         구간을 선형보간해 flip 레벨을 추정한다.
    해석: 이 레벨을 이탈하면 딜러 헤지가 안정화<->증폭으로 전환 — 변동성 폭발 준비 신호.
    실패 조건: legs가 비어있으면 None. 그리드 전 구간에서 부호가 바뀌지 않으면 None
              (flip 레벨이 탐색 범위 밖에 있음을 의미).
    """
    if not legs:
        return None

    step_size = (spot * 2 * search_pct) / (steps - 1)
    grid = [spot * (1 - search_pct) + i * step_size for i in range(steps)]

    def gex_at(s: float) -> float:
        s_term = s**2 / 100
        total = 0.0
        for leg in legs:
            g = _bs_gamma(leg.option_type, s, leg.strike, leg.t_years, risk_free_rate, leg.iv)
            total += _CALL_PUT_SIGN[leg.option_type] * g * leg.oi * multiplier * s_term
        return total

    values = [gex_at(s) for s in grid]
    for i in range(1, len(grid)):
        if values[i - 1] == 0:
            return grid[i - 1]
        if values[i - 1] * values[i] < 0:
            frac = values[i - 1] / (values[i - 1] - values[i])
            return grid[i - 1] + frac * (grid[i] - grid[i - 1])
    return None


def gamma_walls(
    legs: Sequence[OptionLeg], spot: float, multiplier: float = 250_000, top_n: int = 3
) -> list[tuple[float, float]]:
    """
    감마 집중 상위 행사가 — Pinning 후보, 부분청산 기준선.

    계산: 행사가별 |Gamma x OI x multiplier x S^2/100| 합산 후 내림차순 top_n.
    해석: 값이 큰 행사가일수록 만기 근접 시 가격이 붙들리는 자석(Pinning) 후보.
    실패 조건: legs가 비어있으면 빈 리스트.
    """
    s_term = spot**2 / 100
    by_strike: dict[float, float] = {}
    for leg in legs:
        exposure = abs(leg.gamma * leg.oi * multiplier * s_term)
        by_strike[leg.strike] = by_strike.get(leg.strike, 0.0) + exposure
    return sorted(by_strike.items(), key=lambda kv: kv[1], reverse=True)[:top_n]


def vanna_charm_drift(legs: Sequence[OptionLeg], now: time, charm_active_after: time = time(14, 0)) -> dict:
    """
    Vanna: dDelta/dVol -> IV 변화 방향과 결합해 딜러 재헤지 방향 추정.
    Charm: dDelta/dTime -> 14:00 이후 Charm 방향 드리프트 가중치 활성화.

    입력: 레그별 vanna/charm(Greeks 엔진에서 미리 계산해 채운 값), 현재 시각.
    계산: 전체 vanna/charm 익스포저 합산, 마감 임박 여부(charm_active) 플래그.
    해석: charm_active=True일 때만 Charm 드리프트 방향을 신호에 반영해야 한다.
    실패 조건: OI 데이터 지연·이벤트 당일에는 신뢰도 하향 — 호출측(Fusion)에서 처리한다.
    """
    total_vanna = sum(leg.vanna * leg.oi for leg in legs)
    total_charm = sum(leg.charm * leg.oi for leg in legs)
    return {
        "total_vanna": total_vanna,
        "total_charm": total_charm,
        "charm_active": now >= charm_active_after,
    }


class GammaMapEngine:
    """v6 §9.2 GammaMapEngine — 체인 스냅샷을 받아 GEX/Flip/Wall/Vanna·Charm을 계산한다."""

    def __init__(self, multiplier: float = 250_000, risk_free_rate: float = 0.035) -> None:
        self.multiplier = multiplier
        self.risk_free_rate = risk_free_rate

    def calculate_gex(self, legs: Sequence[OptionLeg], spot: float) -> float:
        return calculate_gex(legs, spot, self.multiplier)

    def find_gamma_flip(self, legs: Sequence[OptionLeg], spot: float) -> float | None:
        return find_gamma_flip(legs, spot, self.multiplier, self.risk_free_rate)

    def gamma_walls(self, legs: Sequence[OptionLeg], spot: float, top_n: int = 3) -> list[tuple[float, float]]:
        return gamma_walls(legs, spot, self.multiplier, top_n)

    def vanna_charm_drift(self, legs: Sequence[OptionLeg], now: time) -> dict:
        return vanna_charm_drift(legs, now)


def calculate_vrp(iv: float, realized_vol: float) -> float:
    """
    IV-RV Spread (변동성 리스크 프리미엄, VRP).

    계산: iv - realized_vol.
    해석: VRP>0 -> 옵션이 비쌈(프리미엄 매도 후보, 안정 레짐+positive GEX 한정),
         VRP<0 -> 옵션이 저평가(이벤트 전 눌린 IV일 가능성 -> Long Vol 후보).
    실패 조건: 없음(단순 차분) — realized_vol 추정 윈도우가 짧으면 노이즈에 취약함에 유의.
    """
    return iv - realized_vol
