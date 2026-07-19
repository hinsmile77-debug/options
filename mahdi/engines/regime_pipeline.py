"""E1 Regime 실시간 오케스트레이션 — §7.3 피처 축적 + §7.4/§16.1 워밍업 실데이터화 + HMM 전환.

main.py는 매 선물봉마다 RegimeStateMachine.step()만 호출하면 된다. 내부적으로:
  1) 세션 내 인메모리 롤링 윈도(고/저/종가, ATM IV, 스프레드)로 6개 피처를 계산해 feature_store에
     매분 적재한다(오프라인 fit 배치의 원료 축적 — scripts/fit_regime_engine.py).
  2) data/models/regime_engine.pkl에 캘리브레이션된 모델이 있고 세션 내 워밍업(burn-in)이 끝났으면
     RegimeEngine.predict()를, 아니면 실거래 데이터로 계산한 gap_zscore/macro_score/전일 마감
     레짐을 넣은 warmup_fallback()을 반환한다.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, time as dtime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from mahdi.data import db
from mahdi.engines.regime import FEATURE_NAMES, RegimeEngine, RegimeLabel, RegimeState, warmup_fallback
from mahdi.features.regime_features import adx, book_thinning, cross_asset_stress, hurst_exponent, iv_change_rate, rv_ratio

if TYPE_CHECKING:
    from mahdi.data.collector import MinuteBar

FEATURE_VERSION = "v1"
DEFAULT_MODEL_PATH = Path("data/models/regime_engine.pkl")

_ROLLING_WINDOW_MINUTES = 120  # Hurst/ADX 입력 — 약 2시간, R/S 방법이 안정적으로 수렴하는 최소 길이
_IV_WINDOW_MINUTES = 30
_SPREAD_WINDOW_MINUTES = 30
_MIN_WARMUP_BARS = 30  # 이 정도 봉이 쌓이기 전에는 모델이 있어도 predict() 대신 warmup_fallback 유지(burn-in)
_DAILY_CLOSES_LOOKBACK_DAYS = 30  # rv_ratio가 21개를 요구 — 롤오버 등을 감안해 여유 있게 조회


class RegimeFeatureBuilder:
    """선물 1분봉 롤링 윈도로 §7.3 6개 피처를 계산한다."""

    def __init__(
        self,
        window: int = _ROLLING_WINDOW_MINUTES,
        iv_window: int = _IV_WINDOW_MINUTES,
        spread_window: int = _SPREAD_WINDOW_MINUTES,
    ) -> None:
        self._closes: deque[float] = deque(maxlen=window)
        self._highs: deque[float] = deque(maxlen=window)
        self._lows: deque[float] = deque(maxlen=window)
        self._spreads: deque[float] = deque(maxlen=spread_window)
        self._ivs: deque[float] = deque(maxlen=iv_window)

    def update_bar(self, bar: "MinuteBar") -> None:
        """입력: 완성된 선물 1분봉. 계산: 고/저/종가/스프레드 롤링 윈도에 추가."""
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._spreads.append(bar.bid_ask_spread)

    def update_iv(self, atm_iv: float) -> None:
        """입력: 옵션체인 폴링 사이클에서 뽑은 ATM 근사 IV(콜/풋 평균). 계산: IV 롤링 윈도에 추가."""
        self._ivs.append(atm_iv)

    def build(self, daily_closes: list[float]) -> list[float]:
        """
        입력: 일별 종가 이력(rv_ratio용, 호출측이 DB에서 매번 조회해 전달).
        계산: FEATURE_NAMES 순서(hurst, adx, rv_ratio, iv_chg, cross_asset_stress, book_thinning)로
             피처 벡터를 구성한다.
        """
        return [
            hurst_exponent(list(self._closes)),
            adx(list(self._highs), list(self._lows), list(self._closes)),
            rv_ratio(daily_closes),
            iv_change_rate(list(self._ivs)),
            cross_asset_stress(),
            book_thinning(list(self._spreads)),
        ]


def compute_gap_zscore(conn, underlying: str) -> float:
    """
    §16.1 WARMUP ② — 갭 z-score = (오늘 첫 스팟 − 전일 마지막 스팟) / 전일 ATM 스트래들 IV 기반
    오버나이트 기대변동폭.

    입력: DB 커넥션, underlying 라벨(예: "KOSPI200").
    계산: underlying_spot_1m에서 전일 마지막 스팟·오늘 첫 스팟을 조회하고, option_analysis_1m에서
         전일 마지막 시점 기준 그 스팟에 가장 가까운 행사가의 IV(콜/풋 평균)로 1캘린더데이 기대
         변동폭(spot·iv·sqrt(1/365))을 근사한다.
    실패 조건: 전일 데이터가 없거나(첫 실행일) IV를 못 찾으면 0.0(갭 없음으로 간주 — 안전한 중립값).
    """
    today = db.local_now().date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, spot FROM underlying_spot_1m WHERE underlying=%s AND timestamp::date < %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (underlying, today),
        )
        prev_row = cur.fetchone()
        cur.execute(
            "SELECT spot FROM underlying_spot_1m WHERE underlying=%s AND timestamp::date = %s "
            "ORDER BY timestamp ASC LIMIT 1",
            (underlying, today),
        )
        today_row = cur.fetchone()

    if prev_row is None or today_row is None:
        return 0.0

    prev_ts, prev_close = prev_row
    prev_close = float(prev_close)
    today_open = float(today_row[0])
    if prev_close <= 0:
        return 0.0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT iv FROM option_analysis_1m WHERE underlying=%s AND timestamp::date=%s AND iv IS NOT NULL "
            "ORDER BY ABS(strike - %s) ASC, timestamp DESC LIMIT 2",
            (underlying, prev_ts.date(), prev_close),
        )
        iv_rows = cur.fetchall()

    if not iv_rows:
        return 0.0
    atm_iv = sum(float(r[0]) for r in iv_rows) / len(iv_rows)
    expected_move = prev_close * atm_iv * math.sqrt(1 / 365)
    if expected_move <= 0:
        return 0.0
    return (today_open - prev_close) / expected_move


def compute_macro_score_proxy(conn, underlying: str) -> float:
    """
    §16.1 WARMUP ①의 "장전 매크로 스코어" 근사치.

    실제 스펙(§8 나침반: VIX 기간구조·S&P선물·USDKRW·USDCNH·US10Y)은 이 코드베이스에 연동되어
    있지 않다(TODO — 별도 데이터 소스 필요). 이미 수집 중인 investor_flow_1m의 외국인 순매수
    방향을 위험선호/회피의 대리 신호로 쓴다: 외국인 순매수(foreign_net) 부호 그대로.
    실패 조건: 수급 데이터가 아직 없으면 0.0(중립).
    """
    flow = db.latest_investor_flow(conn, underlying)
    if flow is None:
        return 0.0
    foreign_net, _institution_net, _individual_net = flow
    if foreign_net > 0:
        return 1.0
    if foreign_net < 0:
        return -1.0
    return 0.0


def latest_prior_close_regime(conn) -> RegimeLabel:
    """전일 마감 레짐 조회 — 없으면(첫 실행일) RANGE_BALANCED로 폴백."""
    today_midnight = datetime.combine(db.local_now().date(), dtime.min)
    regime_int = db.latest_regime_before(conn, today_midnight)
    if regime_int is None:
        return RegimeLabel.RANGE_BALANCED
    return RegimeLabel(regime_int)


class RegimeStateMachine:
    """세션 하나(프로세스 하나)당 1개 — main.py가 선물봉마다 step()을 호출한다."""

    def __init__(self, underlying: str, futures_symbol: str, model_path: str | Path = DEFAULT_MODEL_PATH) -> None:
        self.underlying = underlying
        self.futures_symbol = futures_symbol
        self.feature_builder = RegimeFeatureBuilder()
        self._bar_count = 0
        self._gap_zscore: float | None = None  # 세션 첫 계산값을 캐싱(갭은 장중 재계산 대상이 아님)
        try:
            self.engine: RegimeEngine | None = RegimeEngine.load(model_path)
        except FileNotFoundError:
            self.engine = None

    def update_bar(self, bar: "MinuteBar") -> None:
        self.feature_builder.update_bar(bar)
        self._bar_count += 1

    def update_iv(self, atm_iv: float) -> None:
        self.feature_builder.update_iv(atm_iv)

    def step(self, conn, timestamp: datetime) -> RegimeState:
        """
        입력: DB 커넥션, 이번 선물봉의 타임스탬프.
        계산: 피처 벡터를 계산해 feature_store에 적재한 뒤, 캘리브레이션된 모델이 있고 세션 내
             워밍업(_MIN_WARMUP_BARS)이 끝났으면 predict(), 아니면 실데이터 기반 warmup_fallback()을
             반환한다.
        """
        daily_closes = db.daily_closes(conn, self.futures_symbol, days=_DAILY_CLOSES_LOOKBACK_DAYS)
        features = self.feature_builder.build(daily_closes)
        db.insert_feature_store(conn, timestamp, self.underlying, dict(zip(FEATURE_NAMES, features)), FEATURE_VERSION)

        if self.engine is not None and self._bar_count >= _MIN_WARMUP_BARS:
            return self.engine.predict(np.array([features]))

        if self._gap_zscore is None:
            self._gap_zscore = compute_gap_zscore(conn, self.underlying)
        macro_score = compute_macro_score_proxy(conn, self.underlying)
        prior_regime = latest_prior_close_regime(conn)
        return warmup_fallback(prior_regime, macro_score=macro_score, gap_zscore=self._gap_zscore)
