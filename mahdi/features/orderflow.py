"""E2 주문흐름 미시구조 피처 — OFI·VPIN·Microprice·Queue Imbalance·Absorption.

실시간 수집 파이프라인과 백테스트 엔진이 동일한 함수를 호출한다 (피처 사전 Single Source of Truth,
v6 §8.2). 입력 시그니처를 그대로 유지하면 상위 레이어(Fusion/Backtest)에서 교체 없이 재사용 가능하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist
from typing import Sequence

_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """최우선 호가 스냅샷 1틱."""

    bid_px: float
    bid_qty: float
    ask_px: float
    ask_qty: float


def _ofi_step(prev: BookSnapshot, curr: BookSnapshot) -> float:
    if curr.bid_px > prev.bid_px:
        delta_bid = curr.bid_qty
    elif curr.bid_px == prev.bid_px:
        delta_bid = curr.bid_qty - prev.bid_qty
    else:
        delta_bid = 0.0

    if curr.ask_px > prev.ask_px:
        delta_ask = 0.0
    elif curr.ask_px == prev.ask_px:
        delta_ask = curr.ask_qty - prev.ask_qty
    else:
        delta_ask = curr.ask_qty

    return delta_bid - delta_ask


def calculate_ofi(snapshots: Sequence[BookSnapshot]) -> float:
    """
    Cont-Kukanov-Stoikov (2014) Order Flow Imbalance.

    입력: 시간순 정렬된 최우선 호가 스냅샷 시퀀스(보통 1분 윈도우 내 틱).
    계산: e_n = ΔBidQty·1{bid유지/상승} - ΔAskQty·1{ask유지/하락}; OFI = Σ e_n.
    해석: OFI 급증 + 가격 미반영 = 매수/매도 압력 축적 → 방향 진입 후보.
    실패 조건: 스냅샷 2개 미만이면 0.0 반환(정의 불가). 호가 스프레드 급확대 구간에서는
              신뢰도가 낮아지므로 상위 로직(Fusion)에서 가중치를 자동 축소해야 한다.
    """
    if len(snapshots) < 2:
        return 0.0
    return sum(_ofi_step(prev, curr) for prev, curr in zip(snapshots, snapshots[1:]))


def microprice(bid_px: float, bid_qty: float, ask_px: float, ask_qty: float) -> float:
    """
    잔량 가중 중심가격 (Stoikov 2018 Micro-Price).

    입력: 최우선 매수/매도 호가와 잔량.
    계산: (ask_px·bid_qty + bid_px·ask_qty) / (bid_qty + ask_qty).
    해석: 매수잔량이 두꺼우면 microprice가 ask 쪽으로 쏠림 — 다음 틱 상승압력 선행 신호.
    실패 조건: 양측 잔량 합이 0이면 (bid_px+ask_px)/2로 폴백.
    """
    total_qty = bid_qty + ask_qty
    if total_qty <= 0:
        return (bid_px + ask_px) / 2
    return (ask_px * bid_qty + bid_px * ask_qty) / total_qty


def queue_imbalance(bid_qty: float, ask_qty: float) -> float:
    """
    최우선 호가 잔량 비대칭.

    계산: (bid_qty - ask_qty) / (bid_qty + ask_qty), 범위 [-1, 1].
    해석: 양수(+) → 체결 압력 매수 방향, 음수(-) → 매도 방향.
    실패 조건: 양측 잔량 합이 0이면 0.0 (불균형 정의 불가).
    """
    total_qty = bid_qty + ask_qty
    if total_qty <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total_qty


def calculate_vpin(bucket_returns: Sequence[float], bucket_volumes: Sequence[float], window: int = 50) -> float:
    """
    Easley-Lopez de Prado-O'Hara (2012) VPIN — Bulk Volume Classification 기반 정보거래 확률.

    입력: 등거래량 버킷별 [시가→종가 수익률, 버킷 거래량] 시퀀스 (버킷 생성은 Data Layer 담당).
    계산: 각 버킷의 매수비율을 Z = return/σ(return)의 표준정규 CDF로 근사(BVC)한 뒤,
         |매수량-매도량|의 최근 window개 합을 총 거래량으로 나눈다.
    해석: VPIN > 0.7 → 정보거래자 활성 → mean reversion 금지, 추세추종 또는 거래 회피.
    실패 조건: 버킷이 없으면 0.0. 수익률 표준편차가 0이면(무변동) 매수비율 0.5로 처리(burn-in 구간
              등 데이터 부족 시에도 안전하게 0.5 중립값으로 수렴).
    """
    if not bucket_returns or not bucket_volumes:
        return 0.0
    n = min(window, len(bucket_returns))
    returns = list(bucket_returns[-n:])
    volumes = list(bucket_volumes[-n:])

    mean_r = sum(returns) / n
    sigma = (sum((r - mean_r) ** 2 for r in returns) / n) ** 0.5

    total_imbalance = 0.0
    total_volume = 0.0
    for r, v in zip(returns, volumes):
        buy_frac = _NORMAL.cdf(r / sigma) if sigma > 0 else 0.5
        total_imbalance += abs(v * buy_frac - v * (1 - buy_frac))
        total_volume += v

    if total_volume <= 0:
        return 0.0
    return total_imbalance / total_volume


def absorption_score(
    traded_volume: float,
    avg_volume: float,
    price_change: float,
    price_change_threshold: float = 0.0005,
) -> float:
    """
    대량 체결에도 가격이 거의 움직이지 않는 흡수(Absorption) 정도.

    입력: 해당 구간 체결량, 평균 체결량(기준선), 가격 변화율.
    계산: traded_volume / avg_volume — 단, |price_change| > threshold면 흡수로 보지 않고 0 반환.
    해석: 값이 클수록(예: 3배 이상) 대량 매물이 가격 변동 없이 소화됨 → 반전 또는 지속의 핵심 단서.
    실패 조건: avg_volume이 0이면 0.0 (기준선 부재).
    """
    if avg_volume <= 0 or abs(price_change) > price_change_threshold:
        return 0.0
    return traded_volume / avg_volume
