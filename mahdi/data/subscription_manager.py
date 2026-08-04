"""ATM 중심 옵션 체인 구독 롤링 매니저 (v6 §19.2, PART 21 Phase1 체크리스트 2번).

KIS WS는 세션당 구독 슬롯이 제한적(약 41건)이라 전체 옵션 체인을 상시 구독할 수 없다.
현재가가 바뀌어 ATM이 이동하면, 범위를 벗어난 행사가 구독을 해제하고 새로 진입한 행사가를
구독해 슬롯을 항상 ATM 근방(±strikes_each_side)으로 유지한다.
"""

from __future__ import annotations

from mahdi.broker.ws_client import KISWebSocketClient, Subscription


# 2026-08-04(운영점검보고서 §2-2 / Fix#6) — ATM 재롤링 히스테리시스.
#
# 08-03에 재롤링을 켠 것(§2-2 이전에는 WS 연결당 1회만 롤링해 하루치 체인이 5.5% 외가격이었다)은
# 방향이 옳았고 실제로 돌았다. 그런데 임계가 없어서 **스팟이 격자 중점 근처에서 진동하면 ATM이
# 매분 왕복한다.**
#
# 08-04 실측: 롤링 이벤트 **194회**(로그 582줄 = 3북 x 194), 그중 **70회(36%)가 즉시 왕복**
# (A→B 다음 틱에 B→A). 08:48:50 `1000.0~1010.0 → 997.5~1007.5` → 08:49:49 되돌아감 →
# 08:51:50 또 되돌아감(3분간 왕복 2회).
#
# 피해는 로그가 아니라 **판단 입력**이다. 체인 스냅샷은 10분 창(`db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES`)
# 안의 최신 레그를 모으는데, 그 10분 안에 서로 다른 행사가 창이 여러 개 겹쳐 쌓인다:
#   장전 27.6레그(설계값 30) → 09시 **50.0레그** / 최대 72레그, 최고령 128초 → 444초.
# 그리고 하루 동안 방문한 행사가가 **25개(952.5~1012.5, ±3%)** 로 번졌다.
#
# 0.75 근거: 왕복은 스팟이 두 격자점의 **중점(0.5)** 근처를 오갈 때 난다. 임계를 0.5로 두면
# 히스테리시스가 없는 것과 같고, 1.0 이상이면 ATM이 실제로 한 칸 옮겨간 뒤에도 안 따라간다.
# 0.75는 "중점을 넘은 뒤 추가로 격자의 1/4만큼 더 가야 옮긴다"는 뜻이라, 진동 폭이 ±0.625p
# (2.5 x 0.25)를 넘지 않는 한 왕복이 생기지 않으면서 실제 추세 이동은 한 틱 안에 따라간다.
ATM_ROLL_HYSTERESIS_RATIO = 0.75


def strikes_around_atm(spot: float, strike_interval: float, strikes_each_side: int) -> list[float]:
    """
    입력: 현재가, 행사가 간격(KOSPI200 옵션=2.5), 편측 유지 개수.
    계산: ATM(현재가에 가장 가까운 행사가 격자점) 기준 ±strikes_each_side 범위를 생성.
    실패 조건: strike_interval<=0이면 ValueError.
    """
    if strike_interval <= 0:
        raise ValueError("strike_interval은 0보다 커야 합니다")
    return [
        atm_for_spot(spot, strike_interval) + i * strike_interval
        for i in range(-strikes_each_side, strikes_each_side + 1)
    ]


def atm_for_spot(spot: float, strike_interval: float) -> float:
    """입력: 현재가, 행사가 간격. 계산: 가장 가까운 행사가 격자점. 실패 조건: interval<=0이면 ValueError."""
    if strike_interval <= 0:
        raise ValueError("strike_interval은 0보다 커야 합니다")
    return round(spot / strike_interval) * strike_interval


def should_roll_atm(
    current_atm: float | None,
    spot: float,
    strike_interval: float,
    hysteresis_ratio: float = ATM_ROLL_HYSTERESIS_RATIO,
) -> bool:
    """
    입력: 지금 창의 중심 행사가(아직 없으면 None), 최신 스팟, 행사가 간격, 히스테리시스 비율.
    계산: 아직 창이 없으면 무조건 롤링한다. 있으면 **스팟이 현재 ATM에서 `interval x ratio`
         이상 벗어났을 때만** 롤링한다.
    해석: 상세 근거는 `ATM_ROLL_HYSTERESIS_RATIO` 주석. 판정을 별도 함수로 뺀 이유는
         "언제 옮기는가"가 `roll_to_spot()`의 구독 diff 로직과 **다른 관심사**이고, 테스트가
         WS 없이 이 규칙만 직접 검증할 수 있어야 하기 때문이다.
    실패 조건: strike_interval<=0이면 `atm_for_spot()`이 ValueError를 던진다.
    """
    if current_atm is None:
        return True
    if strike_interval <= 0:
        raise ValueError("strike_interval은 0보다 커야 합니다")
    if abs(spot - current_atm) < strike_interval * hysteresis_ratio:
        return False
    # 임계는 넘었지만 반올림 결과가 같은 칸이면(경계 바로 밖) 구독 변경이 없다 — 굳이 돌지 않는다.
    return atm_for_spot(spot, strike_interval) != current_atm


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
        hysteresis_ratio: float = ATM_ROLL_HYSTERESIS_RATIO,
    ) -> None:
        self._ws = ws_client
        self._tr_id = tr_id
        self._strike_interval = strike_interval
        self._strikes_each_side = strikes_each_side
        self._option_types = option_types
        self._symbol_formatter = symbol_formatter or (lambda strike, opt: f"{strike}{opt}")
        self._desired_strikes: set[float] = set()
        self._hysteresis_ratio = hysteresis_ratio
        # 지금 창의 중심 행사가. `_desired_strikes`에서 역산할 수도 있지만(min+max)/2), 상장
        # 행사가가 아니어서 심볼이 만들어지지 않은 칸이 섞이면 중심이 어긋난다 — 따로 들고 있는다.
        self._current_atm: float | None = None

    @property
    def current_atm(self) -> float | None:
        return self._current_atm

    async def roll_to_spot(self, spot: float) -> None:
        """
        입력: 최신 기초자산 현재가.
        계산: **히스테리시스를 통과했을 때만**(`should_roll_atm()`) 새 ATM±N 범위를 계산해,
             범위를 벗어난 기존 구독은 해제하고 새로 들어온 행사가는 구독한다.
        해석: 매 호출마다 최소한의 구독 변경만 수행(불필요한 재구독 방지). symbol_formatter가
             None을 반환하면(예: 그리드가 가정한 strike가 실제 상장 행사가와 맞지 않는 경우)
             해당 강목은 조용히 건너뛴다.
             2026-08-04(§2-2 / Fix#6): 히스테리시스 도입 — 근거는 `ATM_ROLL_HYSTERESIS_RATIO`.
             임계에 걸려 돌지 않으면 `desired_strikes`가 그대로이므로 호출측(`_reroll_books_to_spot`)
             의 diff 로그도 자연히 안 남는다.
        실패 조건: 새 범위가 MAX_SUBSCRIPTIONS를 넘으면 ws_client.subscribe()가 ValueError를 던진다
                  (strikes_each_side를 슬롯 한도에 맞게 구성하는 것은 호출측 책임).
        """
        if not should_roll_atm(self._current_atm, spot, self._strike_interval, self._hysteresis_ratio):
            return

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
        self._current_atm = atm_for_spot(spot, self._strike_interval)

    @property
    def desired_strikes(self) -> frozenset[float]:
        return frozenset(self._desired_strikes)

    def rebind(self, ws_client: KISWebSocketClient) -> None:
        """
        입력: 재연결로 새로 만들어진 WS 클라이언트(2026-07-19, WS 재연결 도입).
        계산: 새 클라이언트로 교체하고 _desired_strikes를 비운다. 재연결은 KIS 서버 쪽 구독
             상태를 전부 초기화하므로(새 세션), 다음 roll_to_spot() 호출이 "겹치는 행사가는
             그대로 두고 diff만 보낸다"로 동작하면 이미 알고 있던 _desired_strikes와 새로 계산한
             범위가 같을 때 아무것도 재구독하지 않는다 — 실제로는 새 연결에 구독이 하나도 없는데
             매니저만 "이미 구독했다"고 착각하는 상태가 된다. 비워두면 다음 roll_to_spot()이
             현재 범위 전체를 새 연결에 처음부터 다시 구독한다.
        """
        self._ws = ws_client
        self._desired_strikes = set()
        # 2026-08-04(Fix#6): 히스테리시스도 함께 초기화한다 — 안 그러면 재연결 직후
        # `should_roll_atm()`이 "이미 그 ATM이다"로 판정해 **새 연결에 구독을 하나도 안 보낸다**
        # (위 `_desired_strikes = set()`이 막으려던 바로 그 상태를 다른 경로로 다시 만든다).
        self._current_atm = None
