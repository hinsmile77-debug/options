"""ATM 중심 옵션 체인 구독 롤링 매니저 (v6 §19.2, PART 21 Phase1 체크리스트 2번).

KIS WS는 세션당 구독 슬롯이 제한적(약 41건)이라 전체 옵션 체인을 상시 구독할 수 없다.
현재가가 바뀌어 ATM이 이동하면, 범위를 벗어난 행사가 구독을 해제하고 새로 진입한 행사가를
구독해 슬롯을 항상 ATM 근방(±strikes_each_side)으로 유지한다.
"""

from __future__ import annotations

from mahdi.broker.ws_client import KISWebSocketClient, Subscription


def strikes_around_atm(spot: float, strike_interval: float, strikes_each_side: int) -> list[float]:
    """
    입력: 현재가, 행사가 간격(KOSPI200 옵션=2.5), 편측 유지 개수.
    계산: ATM(현재가에 가장 가까운 행사가 격자점) 기준 ±strikes_each_side 범위를 생성.
    실패 조건: strike_interval<=0이면 ValueError.
    """
    if strike_interval <= 0:
        raise ValueError("strike_interval은 0보다 커야 합니다")
    atm = round(spot / strike_interval) * strike_interval
    return [atm + i * strike_interval for i in range(-strikes_each_side, strikes_each_side + 1)]


class RollingSubscriptionManager:
    """ATM 이동에 따라 WS 구독을 자동으로 롤링한다."""

    def __init__(
        self,
        ws_client: KISWebSocketClient,
        tr_id: str,
        strike_interval: float,
        strikes_each_side: int,
        option_types: tuple[str, ...] = ("C", "P"),
        symbol_formatter=None,
    ) -> None:
        self._ws = ws_client
        self._tr_id = tr_id
        self._strike_interval = strike_interval
        self._strikes_each_side = strikes_each_side
        self._option_types = option_types
        self._symbol_formatter = symbol_formatter or (lambda strike, opt: f"{strike}{opt}")
        self._desired_strikes: set[float] = set()

    async def roll_to_spot(self, spot: float) -> None:
        """
        입력: 최신 기초자산 현재가.
        계산: 새 ATM±N 범위를 계산해, 범위를 벗어난 기존 구독은 해제하고 새로 들어온 행사가는 구독.
        해석: 매 호출마다 최소한의 구독 변경만 수행(불필요한 재구독 방지). symbol_formatter가
             None을 반환하면(예: 그리드가 가정한 strike가 실제 상장 행사가와 맞지 않는 경우)
             해당 강목은 조용히 건너뛴다.
        실패 조건: 새 범위가 MAX_SUBSCRIPTIONS를 넘으면 ws_client.subscribe()가 ValueError를 던진다
                  (strikes_each_side를 슬롯 한도에 맞게 구성하는 것은 호출측 책임).
        """
        new_strikes = set(strikes_around_atm(spot, self._strike_interval, self._strikes_each_side))

        to_remove = self._desired_strikes - new_strikes
        to_add = new_strikes - self._desired_strikes

        for strike in to_remove:
            for opt in self._option_types:
                symbol = self._symbol_formatter(strike, opt)
                if symbol is not None:
                    await self._ws.unsubscribe(Subscription(self._tr_id, symbol))

        for strike in to_add:
            for opt in self._option_types:
                symbol = self._symbol_formatter(strike, opt)
                if symbol is not None:
                    await self._ws.subscribe(Subscription(self._tr_id, symbol))

        self._desired_strikes = new_strikes

    @property
    def desired_strikes(self) -> frozenset[float]:
        return frozenset(self._desired_strikes)
