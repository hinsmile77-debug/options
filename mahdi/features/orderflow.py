"""E2 주문흐름 미시구조 피처 — OFI·VPIN·Microprice·Queue Imbalance·Absorption.

실시간 수집 파이프라인과 백테스트 엔진이 동일한 함수를 호출한다 (피처 사전 Single Source of Truth,
v6 §8.2). 입력 시그니처를 그대로 유지하면 상위 레이어(Fusion/Backtest)에서 교체 없이 재사용 가능하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist, median
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


# 「가격이 안 움직였다」의 기준을 만드는 두 값(2026-08-21 실측으로 정함, 아래 `flat_range_limit`).
FLAT_RANGE_RELATIVE_FACTOR = 0.5
FLAT_RANGE_MIN_TICKS = 2


def flat_range_limit(
    recent_ranges: Sequence[float],
    tick_size: float,
    *,
    relative_factor: float = FLAT_RANGE_RELATIVE_FACTOR,
    min_ticks: int = FLAT_RANGE_MIN_TICKS,
) -> float:
    """
    입력: 최근 봉들의 가격 범위(고가−저가) 목록, 그 종목의 호가단위.
    계산: `max(min_ticks × tick_size, 최근 범위 중앙값 × relative_factor)`.
    해석: 「이 종목 기준으로 유난히 안 움직인 봉」의 상한이다. `absorption_score()`가 이 값
         이하인 봉만 흡수 후보로 본다.
    실패 조건: 없음 — `recent_ranges`가 비면 틱 하한만 남는다.

    **왜 고정 문턱이 아니라 상대 문턱인가**(2026-08-21, 실측으로 확정):

    종전 기준은 `|종가−시가| / 시가 ≤ 0.0005`였다. 가격 수준에 비례하는 상대값이라 상품마다
    뜻이 갈렸다 — 선물(≈1,080)에서는 ≈11틱까지 「정체」였고 옵션 프리미엄(≈16)에서는 틱 크기
    보다도 작아 사실상 «시가=종가»인 봉만 통과했다. 그래서 **틱 단위 고정 문턱**을 시험했는데,
    그것도 답이 아니었다. 08-21 하루치 실측:

        선물 1분봉 범위(고가−저가)  중앙값 52틱 · 10분위 32틱 · 「≤8틱」인 봉 145개 중 0개
        옵션 1분봉 범위             중앙값  9틱 · 14%가 0틱

        고정 4틱 게이트   ->  선물 통과율 0.0% · 옵션 17.6%   (선물에서 지표가 죽는다)
        상대 0.5 게이트   ->  선물 통과율 4.8% · 옵션 23.5%

    **두 상품은 틱 단위 변동성 자체가 두 자릿수로 다르다.** 어떤 고정 상수도 한쪽에서는 상시
    참이고 다른 쪽에서는 상시 거짓이 된다. 거래량을 이미 「평균 대비 배수」로 재고 있듯 가격도
    **그 종목 자신의 최근 변동성 대비**로 재야 두 Radar가 같은 뜻이 된다.

    틱 하한을 함께 두는 이유: 시장이 죽은 구간에서는 중앙값이 0에 수렴해 어떤 봉도 통과하지
    못한다. 「2틱도 안 움직였다」는 그 자체로 정체다. 다만 그런 구간은 거래량도 적어 흡수
    배수가 안 나오므로, 이 하한이 오탐을 만들지는 않는다.
    """
    floor = min_ticks * tick_size
    if not recent_ranges:
        return floor
    return max(floor, median(recent_ranges) * relative_factor)


def absorption_score(
    traded_volume: float,
    avg_volume: float,
    price_range: float,
    flat_limit: float,
) -> float:
    """
    대량 체결에도 가격이 거의 움직이지 않는 흡수(Absorption) 정도.

    입력: 해당 봉의 체결량, 평균 체결량(기준선), 그 봉의 **가격 범위(고가−저가)**,
         정체로 인정할 범위 상한(`flat_range_limit()`).
    계산: `traded_volume / avg_volume` — 단, `price_range > flat_limit`이면 0 반환.
    해석: 값이 클수록(예: 3배 이상) 대량 매물이 가격 변동 없이 소화됨 → 반전 또는 지속의
         핵심 단서(v6 §8.2, Kyle 1985 프레임 — 가격 충격 계수 λ가 낮은 순간).
         **0.0은 「판정했고 흡수가 아니다」이지 「모른다」가 아니다.**
    실패 조건: `avg_volume`이 0 이하면 0.0(기준선 부재). 호출측이 기준선 유무를 먼저 가려야
              「모른다」와 구분된다 — 화면은 그것을 `None`으로 따로 표시한다.

    **순변화가 아니라 범위를 보는 이유**(2026-08-21, 실측으로 발견):

    종전에는 `(종가−시가)/시가`로 「가격이 안 움직였다」를 판정했다. 그러면 봉 안에서 크게
    튀었다가 제자리로 돌아온 봉이 «완전 정체»로 판정된다. 08-21 A01609 실측에서
    **정체 판정된 28봉이 28봉 전부** 봉 안에서는 문턱보다 크게 움직였다:

        시가 1072.60  종가 1072.60 (변화 0.000%)  <- 「완전 정체」
        고가 1073.35  저가 1070.85 (폭 0.233%)    <- 실제로는 문턱의 4.7배를 왕복

    왕복한 봉은 흡수가 아니다. 가격이 밀렸다가 되돌아온 것이고, 그 사이 λ는 전혀 낮지 않았다.
    범위를 쓰면 순변화는 자동으로 포함된다 — `|종가−시가| ≤ 고가−저가`이기 때문이다.
    """
    if avg_volume <= 0 or price_range > flat_limit:
        return 0.0
    return traded_volume / avg_volume
