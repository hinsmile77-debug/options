"""E4 Volume Structure & Fair Value — Session/Anchored VWAP, Volume Profile(POC/VAH/VAL),
Volume Spike (v6 §10.1). 실시간·백테스트 공용 단일 소스."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def session_vwap(prices: Sequence[float], volumes: Sequence[float]) -> float:
    """
    세션 VWAP.

    입력: 세션 시작부터의 가격·거래량 시퀀스(동일 길이).
    계산: Sigma(P x V) / Sigma(V).
    해석: 가격이 VWAP 위 + 외국인 우위 + 감마 우호 + 미시체결 강세 동시 충족 → 추세 확률 상향.
    실패 조건: 총 거래량이 0이면 마지막 가격을 반환(공정가치 추정 불가 시 최선 근사).
    """
    total_volume = sum(volumes)
    if total_volume <= 0:
        return prices[-1] if prices else 0.0
    return sum(p * v for p, v in zip(prices, volumes)) / total_volume


def anchored_vwap(prices: Sequence[float], volumes: Sequence[float], anchor_index: int) -> float:
    """
    앵커드 VWAP (시가/고거래 이벤트/돌파점 기준).

    입력: 전체 가격·거래량 시퀀스, 앵커 시작 인덱스.
    계산: anchor_index 이후 구간만 사용한 session_vwap.
    실패 조건: anchor_index가 범위를 벗어나면 빈 구간으로 간주해 session_vwap의 폴백 규칙을 따른다.
    """
    return session_vwap(prices[anchor_index:], volumes[anchor_index:])


@dataclass(frozen=True, slots=True)
class VolumeProfile:
    poc: float          # Point of Control — 최대 거래량 가격대
    vah: float          # Value Area High
    val: float           # Value Area Low
    histogram: dict[float, float]   # 가격 bin → 누적 거래량


def volume_profile(
    prices: Sequence[float],
    volumes: Sequence[float],
    bin_size: float,
    value_area_pct: float = 0.70,
) -> VolumeProfile | None:
    """
    Volume Profile: POC(공정가치)·VAH/VAL(가치영역 70%)·HVN/LVN 판별용 히스토그램.

    입력: 가격·거래량 시퀀스, 가격 bin 크기, 가치영역 비율(기본 70%).
    계산: 가격을 bin_size 단위로 양자화해 bin별 거래량 합산 → 최대 bin이 POC → POC에서
         인접 bin을 거래량 큰 순으로 확장하며 누적 거래량이 value_area_pct에 도달할 때까지
         포함한 bin의 최고/최저가가 VAH/VAL.
    해석: LVN 돌파 = 속도 구간 / HVN 복귀 = 회귀 가능성 / POC 붕괴 = 균형점 상실(단순 이탈 아님).
    실패 조건: 데이터가 없거나 총 거래량이 0이면 None.
    """
    if not prices or not volumes or sum(volumes) <= 0:
        return None

    histogram: dict[float, float] = {}
    for p, v in zip(prices, volumes):
        b = round(p / bin_size) * bin_size
        histogram[b] = histogram.get(b, 0.0) + v

    total_volume = sum(histogram.values())
    poc = max(histogram, key=histogram.get)

    included = {poc}
    included_volume = histogram[poc]
    sorted_bins = sorted(histogram.keys())
    poc_idx = sorted_bins.index(poc)
    lo, hi = poc_idx, poc_idx

    while included_volume < value_area_pct * total_volume and (lo > 0 or hi < len(sorted_bins) - 1):
        expand_down_vol = histogram[sorted_bins[lo - 1]] if lo > 0 else -1.0
        expand_up_vol = histogram[sorted_bins[hi + 1]] if hi < len(sorted_bins) - 1 else -1.0

        if expand_up_vol >= expand_down_vol:
            hi += 1
            included_volume += histogram[sorted_bins[hi]]
        else:
            lo -= 1
            included_volume += histogram[sorted_bins[lo]]
        included.update({sorted_bins[lo], sorted_bins[hi]})

    return VolumeProfile(poc=poc, vah=sorted_bins[hi], val=sorted_bins[lo], histogram=histogram)


def volume_spike(current_volume: float, avg_volume: float) -> float:
    """
    Volume Spike / Exhaustion 배율.

    계산: current_volume / avg_volume.
    해석: 3배 이상 + 가격 정체(orderflow.absorption_score와 함께 판단) = Absorption 의심.
    실패 조건: avg_volume이 0이면 0.0.
    """
    if avg_volume <= 0:
        return 0.0
    return current_volume / avg_volume
