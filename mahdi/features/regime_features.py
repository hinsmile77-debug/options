"""E1 Regime 입력 피처 계산 — §7.3 6개 변수 (v6 PART 7).

실시간 수집 파이프라인(mahdi.engines.regime_pipeline)과 오프라인 fit 배치가 동일한 함수를
호출한다(피처 사전 Single Source of Truth, v6 §8.2 원칙을 §7.3에도 동일 적용).
"""

from __future__ import annotations

import math
from typing import Sequence

_MIN_HURST_WINDOW = 20
_MIN_ADX_WINDOW = 15  # period(14) + 1개 이상의 -DM/+DM 계산용
_NEUTRAL_HURST = 0.5
_NEUTRAL_ADX = 20.0
_NEUTRAL_RV_RATIO = 1.0
_NEUTRAL_IV_CHG = 0.0
_NEUTRAL_BOOK_THINNING = 0.0
_NEUTRAL_CROSS_ASSET_STRESS = 0.0


def hurst_exponent(closes: Sequence[float]) -> float:
    """
    R/S(Rescaled Range) 방법 Hurst Exponent.

    입력: 시간순 정렬된 종가 시퀀스(보통 최근 60~120개 1분봉).
    계산: 로그수익률을 여러 구간 크기(chunk size)로 나눠 각 구간의 R/S(범위/표준편차)를 구하고,
         log(chunk size) vs log(R/S) 선형회귀 기울기를 Hurst로 사용한다.
    해석: H>0.6 추세 지속, H<0.4 평균회귀, H≈0.5 랜덤워크(§7.3).
    실패 조건: 데이터가 _MIN_HURST_WINDOW 미만이거나 수익률 표준편차가 0(무변동)이면
              중립값 0.5 반환 — 판단 불가 상태를 랜덤워크로 취급.
    """
    n = len(closes)
    if n < _MIN_HURST_WINDOW:
        return _NEUTRAL_HURST

    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, n) if closes[i - 1] > 0 and closes[i] > 0]
    if len(log_returns) < _MIN_HURST_WINDOW - 1:
        return _NEUTRAL_HURST

    max_chunk_size = len(log_returns) // 2
    chunk_sizes = sorted(set(int(s) for s in np_logspace(2, max_chunk_size) if 2 <= s <= max_chunk_size))
    if len(chunk_sizes) < 2:
        return _NEUTRAL_HURST

    log_sizes: list[float] = []
    log_rs: list[float] = []
    for size in chunk_sizes:
        rs_values = []
        for start in range(0, len(log_returns) - size + 1, size):
            chunk = log_returns[start : start + size]
            mean = sum(chunk) / size
            deviations = [x - mean for x in chunk]
            cumulative = _cumsum(deviations)
            range_ = max(cumulative) - min(cumulative)
            std = (sum(d * d for d in deviations) / size) ** 0.5
            if std > 0:
                rs_values.append(range_ / std)
        if rs_values:
            avg_rs = sum(rs_values) / len(rs_values)
            if avg_rs > 0:
                log_sizes.append(math.log(size))
                log_rs.append(math.log(avg_rs))

    if len(log_sizes) < 2:
        return _NEUTRAL_HURST

    slope = _linreg_slope(log_sizes, log_rs)
    return max(0.0, min(1.0, slope))


def _cumsum(values: Sequence[float]) -> list[float]:
    total = 0.0
    out = []
    for v in values:
        total += v
        out.append(total)
    return out


def np_logspace(start_pow: int, max_size: int) -> list[float]:
    """2^start_pow부터 max_size까지 대략 균등 로그 간격 chunk size 후보 생성 (numpy 의존 없이)."""
    if max_size < 2:
        return []
    sizes = []
    size = float(2**start_pow) if start_pow > 1 else 4.0
    while size <= max_size:
        sizes.append(size)
        size *= 1.5
    sizes.append(float(max_size))
    return sizes


def _linreg_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return _NEUTRAL_HURST
    return numerator / denominator


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    """
    Wilder(1978) Average Directional Index.

    입력: 시간순 정렬된 고가/저가/종가 시퀀스(동일 길이).
    계산: +DM/-DM과 True Range를 Wilder 평활(지수이동평균 근사, alpha=1/period)로 누적해
         +DI/-DI를 구하고, DX=|+DI−−DI|/(+DI+−DI)*100의 period 구간 평균을 ADX로 사용한다.
    해석: ADX 상승 = 추세 강도 증가(방향 무관), §7.3 레짐 판별 입력.
    실패 조건: 길이가 _MIN_ADX_WINDOW 미만이거나 세 시퀀스 길이가 다르면 중립값 20.0 반환.
    """
    n = len(closes)
    if n < _MIN_ADX_WINDOW or len(highs) != n or len(lows) != n:
        return _NEUTRAL_ADX

    plus_dm = [0.0]
    minus_dm = [0.0]
    tr = [0.0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    smoothed_tr = _wilder_smooth(tr, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)

    dx_values = []
    for atr, pdm, mdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if atr <= 0:
            continue
        plus_di = 100 * pdm / atr
        minus_di = 100 * mdm / atr
        denom = plus_di + minus_di
        if denom > 0:
            dx_values.append(100 * abs(plus_di - minus_di) / denom)

    if not dx_values:
        return _NEUTRAL_ADX
    tail = dx_values[-period:] if len(dx_values) >= period else dx_values
    return sum(tail) / len(tail)


def _wilder_smooth(values: Sequence[float], period: int) -> list[float]:
    if len(values) <= period:
        return values
    smoothed = [sum(values[1 : period + 1])]
    for v in values[period + 1 :]:
        smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
    return smoothed


def rv_ratio(daily_closes: Sequence[float]) -> float:
    """
    RV5d/RV20d — 최근 5거래일 실현변동성 대비 최근 20거래일 실현변동성 비율.

    입력: 시간순(오래된 순) 일별 종가. 최소 21개(20일 수익률 + 5일 수익률 계산용) 필요.
    계산: 일별 로그수익률의 최근 5개/20개 표준편차 비율.
    해석: RV5d/RV20d > 1.3 → 변동성 팽창(§7.3).
    실패 조건: 21개 미만이면(아직 세션 데이터 축적 전) 중립값 1.0 반환 — 시간이 지나 데이터가
              쌓이면 자연히 의미있는 값이 된다. RV20d가 0이면(무변동 20일) 중립값 1.0.
    """
    if len(daily_closes) < 21:
        return _NEUTRAL_RV_RATIO

    returns = [
        math.log(daily_closes[i] / daily_closes[i - 1])
        for i in range(1, len(daily_closes))
        if daily_closes[i - 1] > 0 and daily_closes[i] > 0
    ]
    if len(returns) < 20:
        return _NEUTRAL_RV_RATIO

    rv5 = _stdev(returns[-5:])
    rv20 = _stdev(returns[-20:])
    if rv20 <= 0:
        return _NEUTRAL_RV_RATIO
    return rv5 / rv20


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / n) ** 0.5


def iv_change_rate(iv_series: Sequence[float]) -> float:
    """
    ATM IV 변화율.

    입력: 시간순 정렬된 ATM(또는 ATM 근사) IV 시퀀스(예: 최근 30분).
    계산: (최신값 - 최초값) / 최초값.
    해석: 급등 시 VOL_EXPANSION/CRISIS_DEFENSE 신호(§7.3).
    실패 조건: 2개 미만이거나 최초값이 0이면 중립값 0.0.
    """
    if len(iv_series) < 2 or iv_series[0] == 0:
        return _NEUTRAL_IV_CHG
    return (iv_series[-1] - iv_series[0]) / iv_series[0]


def book_thinning(spread_series: Sequence[float]) -> float:
    """
    호가 잔량 급감 대리 지표 — 최우선 매도/매수 스프레드 확대 z-score.

    입력: 시간순 정렬된 bid_ask_spread 시퀀스(market_raw_1m에 이미 적재됨). 원 스펙(§7.3)은
         호가 잔량 절대치 급감이지만, 잔량 절대치는 현재 스키마에 없다 — 스프레드가 급격히
         벌어지는 것은 유동성 공급자가 호가를 거둬들이는 것과 사실상 동일한 신호라 대리로 쓴다.
    계산: 최신 스프레드가 직전 구간 평균 대비 몇 표준편차 위에 있는지(z-score).
    해석: 값이 클수록(예: 2 이상) 호가 유동성이 급격히 얇아짐 → LIQUIDITY_THIN 신호.
    실패 조건: 데이터 3개 미만이거나 표준편차가 0(스프레드 불변)이면 중립값 0.0.
    """
    if len(spread_series) < 3:
        return _NEUTRAL_BOOK_THINNING
    baseline = spread_series[:-1]
    mean = sum(baseline) / len(baseline)
    std = _stdev(baseline)
    if std <= 0:
        return _NEUTRAL_BOOK_THINNING
    return (spread_series[-1] - mean) / std


def cross_asset_stress() -> float:
    """
    Cross-asset stress(USDKRW·USDCNH·US10Y 급변) — §7.3.

    TODO(2026-07-10): 이 코드베이스는 아직 USDKRW/USDCNH/US10Y를 수집하지 않는다(KIS API 범위
    밖 — 별도 데이터 소스 연동 필요). 그 연동이 붙기 전까지는 항상 중립값 0.0을 반환해 HMM 피처
    차원은 유지하되 허위 신호를 만들지 않는다.
    """
    return _NEUTRAL_CROSS_ASSET_STRESS
