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
    # 2026-08-21 — **거래소가 분류한 누적 체결량**(H0IFCNT0 idx 41/42, H0IOCNT0 idx 48/49의
    # SELN_CNTG_SMTN / SHNU_CNTG_SMTN, 그리고 ACML_VOL). 봉의 매수량은 이 누적값의 차분이다 —
    # 틱 룰 추정이 아니라 거래소 판정이다. 프레임이 짧거나 파싱이 안 되면 None이고, 그때만
    # 틱 룰로 떨어진다(`MinuteBarAggregator._classify_volumes`).
    cum_volume: float | None = None
    cum_buy_volume: float | None = None
    cum_sell_volume: float | None = None


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
    # 매수/매도를 무엇으로 갈랐는지. "exchange"면 거래소 누적값의 차분, "tick_rule"이면 추정이다.
    # DB에는 안 실린다(마이그레이션 없음) — 상위 레이어가 폴백 발생을 **로그로 알리기 위한** 값이다.
    # 분류가 조용히 바뀌면 CVD가 통째로 다른 것을 그린다(2026-08-21 오전 사고).
    volume_source: str = "tick_rule"


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


def _midpoint_direction(tick: Tick) -> int:
    """
    입력: 비교할 직전 체결이 아직 없는 틱(세션 최초 1건).
    계산: 호가 중간값보다 비싸게 체결됐으면 매수(+1), 싸게면 매도(-1).
    해석: Lee-Ready(1991)가 tick test를 쓸 수 없을 때 쓰는 quote test와 같은 판정이다.
    실패 조건: 중간값과 정확히 같거나 호가가 비정상(bid >= ask)이면 **0(모름)**. 이때 상위
              호출자는 그 틱을 매수·매도 어느 쪽으로도 세지 않는다 — 모르는 것을 한쪽으로
              적는 것이 편향의 시작이다.
    """
    if not (tick.bid_px < tick.ask_px):
        return 0
    mid = (tick.bid_px + tick.ask_px) / 2
    if tick.price > mid:
        return 1
    if tick.price < mid:
        return -1
    return 0


class MinuteBarAggregator:
    """symbol 1개에 대해 틱을 누적하고, 분이 바뀌면 완성된 1분봉을 flush한다."""

    MIN_TICKS_FOR_NORMAL_QUALITY = 3
    # 거래소 누적값을 믿을지 가르는 허용 오차. `Δ매수 + Δ매도`는 `Δ누적거래량`과 같아야 한다 —
    # 둘 다 **같은 프레임의 누적 필드**라서 원리상 어긋날 수 없다. 이 가드가 겨누는 것은 시장의
    # 미세한 불일치가 아니라 **필드 인덱스가 틀린 경우**다(그러면 시각이나 가격 같은 엉뚱한
    # 숫자가 들어와 자릿수가 통째로 다르다). 그래서 느슨해도 된다. 협의대량처럼 누적거래량에만
    # 잡히는 물량이 있어 완전 일치를 요구하면 상시 오경보가 된다.
    EXCHANGE_VOLUME_TOLERANCE = 0.2

    def __init__(self) -> None:
        self._current_minute: datetime | None = None
        self._ticks: list[Tick] = []
        # 틱 룰의 기준은 **봉 경계를 넘어 이월된다**(2026-08-21). 봉마다 baseline을 새로 잡으면
        # 그 봉의 첫 틱은 자기 자신과 비교돼 언제나 매수가 된다 — 그것이 CVD를 직선으로 만든
        # 편향의 절반이었다(`_classify_volumes` docstring).
        self._prev_price: float | None = None
        self._prev_direction: int = 0  # +1 매수 · -1 매도 · 0 아직 모름
        # 거래소 누적 체결량의 기준선. 틱 룰 baseline과 같은 이유로 **봉 경계를 넘어 이월된다** —
        # 봉 안에서만 차분하면 그 봉의 첫 틱 이전 물량을 통째로 잃는다.
        self._prev_cum_volume: float | None = None
        self._prev_cum_buy: float | None = None
        self._prev_cum_sell: float | None = None

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

    def _exchange_classified_volumes(self) -> tuple[float, float] | None:
        """
        입력: 이 봉의 틱들과 **직전 봉에서 이월된** 거래소 누적값(`_prev_cum_*`).
        계산: 봉 마지막 틱의 누적값 − 직전 기준선. 세 누적 필드(거래량·매수·매도)를 함께 본다.
        해석: 성공하면 (매수, 매도)이고 이것은 **추정이 아니라 거래소 분류**다.
        실패 조건 — 아래 어느 하나라도 걸리면 `None`을 돌려 호출측이 틱 룰로 떨어지게 한다:
             ① 누적 필드가 없다(짧은 프레임·리플레이·테스트 픽스처)
             ② 기준선이 아직 없다(세션 첫 봉) 또는 누적값이 줄었다(장 넘어가며 리셋)
             ③ `Δ매수 + Δ매도`가 `Δ누적거래량`과 `EXCHANGE_VOLUME_TOLERANCE`를 넘게 어긋난다

        ③이 이 함수의 핵심이다. **필드 인덱스가 틀리면 조용히 이상한 숫자를 쓰게 되는데**,
        그것이 2026-08-21 오전에 CVD를 한 시간짜리 직선으로 만든 사고의 형태였다. 두 누적량은
        같은 프레임에서 오므로 원리상 합이 맞아야 하고, 안 맞으면 우리가 잘못 읽은 것이다.
        틀리면 **티가 나고 추정으로 되돌아간다.**
        """
        last = self._ticks[-1]
        if last.cum_volume is None or last.cum_buy_volume is None or last.cum_sell_volume is None:
            return None
        if self._prev_cum_volume is None or self._prev_cum_buy is None or self._prev_cum_sell is None:
            return None

        d_volume = last.cum_volume - self._prev_cum_volume
        d_buy = last.cum_buy_volume - self._prev_cum_buy
        d_sell = last.cum_sell_volume - self._prev_cum_sell
        if d_volume < 0 or d_buy < 0 or d_sell < 0:
            return None  # 누적이 줄었다 = 세션이 바뀌었다. 이 봉은 추정으로 가고 기준선만 새로 잡는다.
        if d_volume > 0 and abs((d_buy + d_sell) - d_volume) > self.EXCHANGE_VOLUME_TOLERANCE * d_volume:
            return None
        return d_buy, d_sell

    def _carry_cumulative_baseline(self) -> None:
        """봉 마지막 틱의 거래소 누적값을 다음 봉의 기준선으로 넘긴다.

        판정에 실패했을 때도(리셋·인덱스 불일치) **반드시 갱신한다** — 안 그러면 낡은 기준선으로
        다음 봉이 통째로 부풀어 오른다. 「이번 봉은 추정으로 갔다」와 「다음 봉의 기준선이
        틀렸다」는 별개의 문제이고, 후자를 만들면 안 된다.
        """
        last = self._ticks[-1]
        if last.cum_volume is not None:
            self._prev_cum_volume = last.cum_volume
        if last.cum_buy_volume is not None:
            self._prev_cum_buy = last.cum_buy_volume
        if last.cum_sell_volume is not None:
            self._prev_cum_sell = last.cum_sell_volume

    def _classify_volumes(self) -> tuple[float, float]:
        """
        입력: 이 봉에 쌓인 틱들(`self._ticks`)과 **직전 봉에서 이월된** 기준 체결가·직전 분류.
        계산: 틱 룰(Lee-Ready 1991의 tick test). 직전 체결보다 **오르면 매수, 내리면 매도,
             같으면 직전 분류를 승계**한다(zero-uptick / zero-downtick). 봉의 첫 틱도 직전
             봉의 마지막 체결과 비교한다 — 기준은 봉 경계에서 끊기지 않는다.
        해석: 반환값은 (매수 체결량, 매도 체결량)이고 **둘의 합이 `volume`보다 작을 수 있다.**
             가를 근거가 없는 틱을 어느 쪽으로도 세지 않기 때문이다(아래 실패 조건).
        실패 조건: 기준이 아직 없는 **세션 최초 틱**은 호가 중간값으로 가른다. 중간값과 정확히
                  같거나 호가가 비정상(bid >= ask)이면 **어느 쪽으로도 세지 않는다.**

        2026-08-21 — 종전 규칙은 `if p >= prev_price: buy else: sell`이었고 baseline이 그 봉
        자신의 첫 체결가였다. 결함이 둘 겹쳐 있었다: **동가 틱이 전부 매수**로 갔고(`>=`),
        **봉의 첫 틱도 자기 자신과 비교돼 항상 매수**였다. 이웃 틱이 같은 가격인 경우가 많은
        선물에서 편향이 가장 컸다 — 08-21 09:00~10:38 A01609 실측 매수 12,975 / 매도 8,773
        (매수 59.7%)이고, COCKPIT의 CVD가 가격이 제자리인 한 시간 동안 부호 전환 없이 85 ->
        2,454까지 곧게 올랐다. **모르는 것을 매수로 적으면 없는 매수 우위가 그려진다.**
        """
        buy_volume = 0.0
        sell_volume = 0.0
        for tick in self._ticks:
            if self._prev_price is None:
                direction = _midpoint_direction(tick)
            elif tick.price > self._prev_price:
                direction = 1
            elif tick.price < self._prev_price:
                direction = -1
            else:
                direction = self._prev_direction

            if direction > 0:
                buy_volume += tick.volume
            elif direction < 0:
                sell_volume += tick.volume

            self._prev_price = tick.price
            if direction != 0:
                self._prev_direction = direction
        return buy_volume, sell_volume

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

        # 거래소 분류가 먼저다 — 틱 룰은 그것이 없거나 못 믿을 때만 쓰는 **추정**이다.
        # `_classify_volumes()`는 어느 쪽을 쓰든 반드시 호출한다: 틱 룰 상태(직전 가격·방향)를
        # 계속 굴려야 폴백이 필요해진 순간에 baseline이 이미 준비돼 있다.
        estimated = self._classify_volumes()
        exchange = self._exchange_classified_volumes()
        buy_volume, sell_volume = exchange if exchange is not None else estimated
        volume_source = "exchange" if exchange is not None else "tick_rule"
        self._carry_cumulative_baseline()

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
            volume_source=volume_source,
        )
