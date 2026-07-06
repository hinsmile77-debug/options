"""WS 틱 -> 1분 집계 (v6 §18.1 market_raw_1m 스키마, PART 21 Phase1 체크리스트).

품질 플래그(quality_flag): 0=정상, 1=저품질(버킷 내 틱 수 부족). 실시간 수집과 백테스트 재처리가
동일한 집계 로직을 쓰도록 features.orderflow의 OFI/Microprice 함수를 그대로 재사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mahdi.features.orderflow import BookSnapshot, calculate_ofi, microprice


@dataclass(frozen=True, slots=True)
class Tick:
    timestamp: datetime
    price: float
    volume: float
    bid_px: float
    bid_qty: float
    ask_px: float
    ask_qty: float


@dataclass(frozen=True, slots=True)
class MinuteBar:
    minute: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    ofi: float
    microprice: float
    bid_ask_spread: float
    buy_volume: float
    sell_volume: float
    quality_flag: int


def _floor_to_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class VolumeBucket:
    open_to_close_return: float
    volume: float


class VolumeBucketAggregator:
    """등거래량(equal-volume) 버킷 — VPIN(Easley-Lopez de Prado-O'Hara, BVC) 계산용 입력을 만든다.

    시간 기준으로 봉을 끊는 MinuteBarAggregator와 달리, 누적 체결량이 bucket_size에 도달할
    때마다 버킷을 닫는다. VPIN은 유동성이 충분한 단일 종목(보통 선물)을 전제로 설계된 지표라,
    이 클래스도 그런 종목 1개에 대해서만 인스턴스를 만들어 쓴다.
    """

    def __init__(self, bucket_size: float) -> None:
        """
        입력: 버킷 1개를 닫는 데 필요한 누적 거래량(계약수). 실거래 데이터로 보정 전까지는
             근사치를 쓴다 — 일평균거래량/50이 학계 관례지만, 실제 관측치가 쌓이기 전에는
             호출측이 임의 상수로 시작해도 된다.
        실패 조건: bucket_size<=0이면 ValueError.
        """
        if bucket_size <= 0:
            raise ValueError("bucket_size는 0보다 커야 합니다")
        self._bucket_size = bucket_size
        self._open_price: float | None = None
        self._last_price: float | None = None
        self._accumulated_volume: float = 0.0

    def add_tick(self, price: float, volume: float) -> VolumeBucket | None:
        """
        입력: 체결가, 체결량(틱 1건).
        계산: 누적 거래량이 bucket_size 이상이 되면 버킷을 닫아 (시가→종가 수익률, 버킷
             거래량)을 반환하고 내부 상태를 리셋한다 — 도달 전이면 None. 버킷을 넘치게 하는
             틱 1건이 다음 버킷으로 이월되지는 않는다(단순화).
        실패 조건: volume<=0인 틱은 누적에 반영하지 않고 무시(의미 없는 틱).
        """
        if volume <= 0:
            return None
        if self._open_price is None:
            self._open_price = price
        self._last_price = price
        self._accumulated_volume += volume

        if self._accumulated_volume >= self._bucket_size:
            open_price = self._open_price
            close_price = self._last_price
            bucket_volume = self._accumulated_volume
            self._open_price = None
            self._last_price = None
            self._accumulated_volume = 0.0
            ret = (close_price - open_price) / open_price if open_price else 0.0
            return VolumeBucket(open_to_close_return=ret, volume=bucket_volume)
        return None


class MinuteBarAggregator:
    """symbol 1개에 대해 틱을 누적하고, 분이 바뀌면 완성된 1분봉을 flush한다."""

    MIN_TICKS_FOR_NORMAL_QUALITY = 3

    def __init__(self) -> None:
        self._current_minute: datetime | None = None
        self._ticks: list[Tick] = []

    def add_tick(self, tick: Tick) -> MinuteBar | None:
        """
        입력: 최신 체결/호가 틱.
        계산: 틱의 분(minute)이 누적 중인 분과 다르면 기존 버킷을 flush해 MinuteBar로 반환하고
             새 버킷을 시작한다. 같은 분이면 누적만 하고 None을 반환한다.
        해석: 반환된 MinuteBar는 상위 Data Layer가 즉시 DB(market_raw_1m)에 적재해야 한다.
        실패 조건: 틱이 현재 버킷보다 과거 시각(지연 도착)이면 무시하고 None 반환.
        """
        minute = _floor_to_minute(tick.timestamp)

        if self._current_minute is None:
            self._current_minute = minute

        if minute < self._current_minute:
            return None

        if minute > self._current_minute:
            completed = self._build_bar()
            self._current_minute = minute
            self._ticks = [tick]
            return completed

        self._ticks.append(tick)
        return None

    def flush_final(self) -> MinuteBar | None:
        """세션 종료 시 마지막 누적 버킷을 강제로 flush한다."""
        completed = self._build_bar()
        self._ticks = []
        return completed

    def _build_bar(self) -> MinuteBar | None:
        if not self._ticks or self._current_minute is None:
            return None

        prices = [t.price for t in self._ticks]
        volumes = [t.volume for t in self._ticks]
        total_volume = sum(volumes)
        vwap = sum(p * v for p, v in zip(prices, volumes)) / total_volume if total_volume > 0 else prices[-1]

        snapshots = [BookSnapshot(t.bid_px, t.bid_qty, t.ask_px, t.ask_qty) for t in self._ticks]
        ofi = calculate_ofi(snapshots)
        last = self._ticks[-1]
        micro = microprice(last.bid_px, last.bid_qty, last.ask_px, last.ask_qty)
        spread = last.ask_px - last.bid_px

        buy_volume = 0.0
        sell_volume = 0.0
        prev_price = prices[0]
        for p, v in zip(prices, volumes):
            if p >= prev_price:
                buy_volume += v
            else:
                sell_volume += v
            prev_price = p

        quality_flag = 0 if len(self._ticks) >= self.MIN_TICKS_FOR_NORMAL_QUALITY else 1

        return MinuteBar(
            minute=self._current_minute,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            volume=total_volume,
            vwap=vwap,
            ofi=ofi,
            microprice=micro,
            bid_ask_spread=spread,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            quality_flag=quality_flag,
        )
