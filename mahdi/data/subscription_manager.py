"""ATM 중심 옵션 체인 구독 롤링 매니저 (v6 §19.2, PART 21 Phase1 체크리스트 2번).

KIS WS는 세션당 구독 슬롯이 제한적(약 41건)이라 전체 옵션 체인을 상시 구독할 수 없다.
현재가가 바뀌어 ATM이 이동하면, 범위를 벗어난 행사가 구독을 해제하고 새로 진입한 행사가를
구독해 슬롯을 항상 ATM 근방(±strikes_each_side)으로 유지한다.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from mahdi.broker.ws_client import KISWebSocketClient, Subscription

logger = logging.getLogger("mahdi.data.subscription_manager")


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


# 2026-08-07(운영점검 §B-1 / 고도화#1) — 유지 큐가 항상 비워 두는 슬롯 수.
#
# **작아야 한다.** 롤 경로의 슬롯 부족은 이 예약이 아니라 `ensure_free()`가 그때그때 축출해
# 해결한다 — 예약은 **풀을 거치지 않는 구독**(기동 시 선물 A01609 / KOSPI 005930, 재연결 직후
# 재등록)만을 위한 여유다. 그 경로들은 유지 큐가 비어 있을 때만 도는데(기동 시점, `clear()`
# 직후), 그래도 0으로 두지 않는 것은 순서를 코드 밖 사실에 의존하고 싶지 않아서다.
#
# 크게 잡으면 **3북 날에 이 고도화가 통째로 무력화된다**: 설계 구독은 (2*2+1)x2x3북 + 선물 +
# KOSPI = 32/41이라 여유가 9뿐이고, 예약을 8로 두면 유지 가능 슬롯이 1이 된다. 2로 두면 3북
# 날에도 7슬롯(행사가 3.5칸분)을 유지할 수 있고, 2북 날(08-07 실측 활성 21~22)에는 17슬롯이다.
SUBSCRIPTION_RESERVED_SLOTS = 2


class SubscriptionRetentionPool:
    """
    ATM 창을 벗어난 구독을 **슬롯이 남는 동안 유지**하고, 슬롯이 필요하면 오래된 것부터 버린다.

    2026-08-07(운영점검 §B-1 / 고도화#1). 종전에는 창이 움직이는 즉시 해제했다. 08-07 실측:

        ATM 롤 76회 / 287분 = 3.8분마다 1회, 그중 즉시 왕복 24회(31.6%)
        WS 구독 요청 410건 / 해제 388건 — 반나절에 798회 갈아엎었다
        BAFBRW980: 11:35:00 해제 → 11:36:59 재구독 → 11:42:59 또 해제

    피해는 슬롯이 아니라 **1분봉의 연속성**이다. 해제된 종목은 그 구간 `market_raw_1m`에
    행이 안 생기고, COCKPIT Flow Radar의 「봉 없음 N분」이 그것이다(08-07 8분). 유동성 공백이
    아니라 **우리가 끊은 것**이다.

    **히스테리시스를 넓히는 것으로는 못 고친다** — 08-07 롤 76회를 전수 검산하면 전부 임계
    (2.5 x 0.75 = 1.875p)를 정당하게 넘었다. 오늘 지수가 1004~964~982(40p)를 오갔고 격자가
    2.5p라 어떤 임계값을 써도 이 왕복은 남는다. 08-04 리플레이도 같은 결론이었다
    (`db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES` 위 주석). 그래서 **롤을 줄이는 대신 롤의 대가를 없앤다.**

    **왜 공짜에 가까운가**: WS 구독은 REST 페이서 예산을 1건도 안 쓴다. REST 폴링 대상은
    `desired_strikes`(창)에서 나오므로(main.py `_reroll_books_to_spot` 주석) 유지된 구독은
    체인 수집을 1콜도 늘리지 않는다. 비용은 슬롯뿐이고, 08-07 실측 활성은 21~22/41이었다.

    **왜 매니저마다가 아니라 공용인가**: 세 북이 `KISWebSocketClient` 하나를 공유하므로 슬롯도
    공유 자원이다. 북마다 예산을 나눠 주면 3북 날(31/41 사용)에는 북당 3슬롯이라 아무것도
    유지하지 못하고, 2북 날(22/41)에는 놀리는 슬롯이 생긴다.
    """

    def __init__(self, ws_client: KISWebSocketClient, reserved_slots: int = SUBSCRIPTION_RESERVED_SLOTS) -> None:
        self._ws = ws_client
        self._reserved = reserved_slots
        # 축출 순서 = 유지 큐에 들어온 순서(창을 벗어난 순서). OrderedDict가 곧 LRU다.
        self._retained: OrderedDict[tuple[str, str], Subscription] = OrderedDict()
        # 2026-08-07 고도화#2 — 롤의 **대가**. 마이그레이션 028 참고.
        self._dropped = 0

    @property
    def retained(self) -> tuple[Subscription, ...]:
        """유지 중(창 밖이지만 아직 구독 살아 있음)인 구독 — 오래된 것부터."""
        return tuple(self._retained.values())

    @property
    def dropped_subscriptions(self) -> int:
        """
        슬롯이 모자라 **실제로 해제한** 구독 수(누적) — 2026-08-07 고도화#2의 대가 지표.

        롤 횟수와 달리 이 값은 우리가 통제한다: 0이면 창은 따라갔는데 1분봉은 하나도 안 끊겼다는
        뜻이다. `clear()`(재연결)는 이 값을 **리셋하지 않는다** — 하루치 누적이라야 대가를 잰다.
        """
        return self._dropped

    def hold(self, sub: Subscription) -> bool:
        """
        입력: 창을 벗어난 구독.
        계산: 해제하지 않고 유지 큐 **맨 뒤**에 넣는다. 실제 구독은 그대로 살아 있다.
        해석: 반환값은 "유지했는가" — 활성 구독이 아니었으면(이미 해제됐거나 심볼 생성 실패)
             유지할 것도 없으므로 False.
        실패 조건: 없다.
        """
        key = (sub.tr_id, sub.tr_key)
        if key not in self._ws.active_subscriptions:
            return False
        self._retained.pop(key, None)
        self._retained[key] = sub
        return True

    def reclaim(self, sub: Subscription) -> bool:
        """
        입력: 창 안으로 다시 들어온 구독.
        계산: 유지 큐에서 빼기만 한다 — **구독은 끊긴 적이 없으므로 재구독 메시지가 없다.**
             즉시 왕복(08-07 31.6%)이 WS 트래픽도 1분봉 공백도 만들지 않게 되는 지점이다.
        실패 조건: 없다. 유지 큐에 없으면 False(호출측이 새로 구독해야 한다).
        """
        return self._retained.pop((sub.tr_id, sub.tr_key), None) is not None

    async def ensure_free(self, needed: int) -> None:
        """
        입력: 지금 새로 구독해야 하는 슬롯 수.
        계산: `needed + reserved`만큼 빌 때까지 유지 큐의 **오래된 것부터** 실제로 해제한다.
        해석: 유지 큐가 비면 더 버릴 게 없으므로 조용히 끝낸다 — 그때 슬롯이 모자라면
             `subscribe()`가 ValueError를 던지고, 그건 창 설계가 슬롯 한도를 넘었다는 뜻이라
             조용히 삼키면 안 되는 사고다(종전과 같은 동작).
        실패 조건: 없다.
        """
        while self._retained:
            free = self._ws.MAX_SUBSCRIPTIONS - len(self._ws.active_subscriptions)
            if free >= needed + self._reserved:
                return
            _key, sub = self._retained.popitem(last=False)
            await self._ws.unsubscribe(sub)
            self._dropped += 1

    def clear(self) -> None:
        """
        유지 큐만 비운다. **재연결 경로는 `clear()`가 아니라 `rebind()`를 부른다** — 이유는
        바로 아래 docstring(2026-08-12 §7-1). 이 메서드는 그 안에서 쓰이고, 테스트가 유지 큐만
        따로 비울 때도 쓴다.
        """
        self._retained.clear()

    def rebind(self, ws_client: KISWebSocketClient) -> None:
        """
        입력: 재연결로 새로 만들어진 WS 클라이언트.
        계산: **풀이 쥔 클라이언트를 교체하고** 유지 큐를 비운다.

        ## 왜 이것이 없어서 08-12에 재연결이 31회 났는가 (§7-1의 답)

        08-12 09:13:30에 **진짜 네트워크 리셋이 한 번** 있었다(WinError 64). 그 뒤 10:10까지
        31회가 더 끊겼는데, 그것은 KIS도 네트워크도 아니라 **우리가 만든 자기지속 루프**였다.

        종전 `RollingSubscriptionManager.rebind()`는 매니저의 `_ws`만 새 클라이언트로 바꾸고
        풀에는 `clear()`만 불렀다. 풀은 `main.py`에서 **기동 시 한 번** 생성되므로
        (`SubscriptionRetentionPool(ws_client)`), 그 뒤로 영원히 **첫 클라이언트**를 쥔다.

        유지된 구독은 슬롯을 그대로 먹으므로 장중 활성은 상한 근처(실측 39/41)까지 찬다.
        그래서 재연결 뒤 풀의 셈은 이렇게 된다:

            free = MAX(41) - len(죽은 클라의 active)  =  41 - 39  =  2      ← 얼어붙은 값
            ensure_free(needed)  →  2 < needed + reserved  →  축출 루프 진입
            await self._ws.unsubscribe(sub)   ← **죽은 커넥션에 send** → ConnectionClosedError

        그 예외가 `listen()`을 뚫고 `run_observation_loop_forever`까지 올라가면 그것은
        **WS 단절로 처리되어 또 재연결**된다. 새 연결은 멀쩡한데 풀은 여전히 첫 클라이언트를
        쥐고 있으므로 **다음 ATM 롤에서 같은 일이 반복된다.**

        관측된 사실이 전부 이 설명과 맞는다:
          · 끊김이 ATM 롤이 있는 분에만 몰렸다(창이 안 움직이면 `ensure_free`가 안 불린다) —
            09:29~09:35, 09:38~10:01의 조용한 구간이 그것이다.
          · 10:14 재기동 뒤 **0회**다. 프로세스가 새로 뜨면 풀도 클라이언트도 새것이다.
          · 08-04·08-11은 각 1회였다 — 그날은 첫 단절 뒤 이 경로를 다시 밟지 않았다.

        ## 왜 `clear()`만으로는 안 됐나

        `clear()`가 지키려던 것은 *"살아 있다고 믿는 구독이 실제로는 없는 상태를 만들지 않는다"*
        이고 그것은 옳다. 놓친 것은 **풀이 쥔 클라이언트 자체**다 — 큐를 비워도 `free` 계산과
        `unsubscribe`의 수신자는 여전히 죽은 커넥션이었다.
        """
        self._ws = ws_client
        self.clear()


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
        retention_pool: SubscriptionRetentionPool | None = None,
        label: str = "",
    ) -> None:
        self._ws = ws_client
        # 진단 로그용 이름(예: series "regular"). 동작에는 안 쓴다 — 창 고착 WARNING(아래
        # `window_stuck_distance()` 호출측)이 "어느 북의 창인가"를 사람이 읽을 수 있게만 한다.
        self.label = label
        # None이면 종전 동작(창을 벗어나는 즉시 해제) — 기존 테스트와 백테스트 경로가 그대로 돈다.
        self._retention = retention_pool
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

    @property
    def retention_pool(self) -> SubscriptionRetentionPool | None:
        """공용 구독 유지 풀(2026-08-07 고도화#1). 없으면 종전대로 즉시 해제하는 매니저다."""
        return self._retention

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

        # 창을 벗어난 것: 유지 풀이 있으면 **해제하지 않고** 넘긴다(2026-08-07 고도화#1).
        for strike in sorted(to_remove):
            for opt in self._option_types:
                symbol = self._symbol_formatter(strike, opt)
                if symbol is None:
                    continue
                sub = Subscription(self._tr_id, symbol)
                if self._retention is None or not self._retention.hold(sub):
                    await self._ws.unsubscribe(sub)

        # 창에 들어온 것: 유지 풀에 살아 있으면 회수만 하고(재구독 없음), 없는 것만 새로 구독한다.
        fresh: list[Subscription] = []
        for strike in sorted(to_add):
            for opt in self._option_types:
                symbol = self._symbol_formatter(strike, opt)
                if symbol is None:
                    continue
                sub = Subscription(self._tr_id, symbol)
                if self._retention is not None and self._retention.reclaim(sub):
                    continue
                fresh.append(sub)

        if fresh and self._retention is not None:
            await self._retention.ensure_free(len(fresh))
        for sub in fresh:
            await self._ws.subscribe(sub)

        self._desired_strikes = new_strikes
        self._current_atm = atm_for_spot(spot, self._strike_interval)

    @property
    def desired_strikes(self) -> frozenset[float]:
        return frozenset(self._desired_strikes)

    def window_stuck_distance(self, spot: float) -> float | None:
        """
        입력: 방금 `roll_to_spot()`을 시도한 뒤의 최신 스팟.
        계산: 지금도 롤링이 걸려야 하는 상태면(`should_roll_atm()` 참) 스팟↔창 중심 거리를,
             아니면 None을 돌려준다.
        해석: 2026-08-18(SERIES_ROTATION_RULE_v1 §6-3) — **롤 직후에 재는 값이라 정상이면 항상
             None이다.** 값이 나오는 것 자체가 "롤링이 걸렸어야 하는데 창이 안 움직였다"는
             뜻이고, 그것이 다음 사이클에도 줄지 않으면 08-04 이전의 창 고착 사고(하루치 체인이
             5.5% 외가격으로 방치)가 재발한 것이다 — 지속 판정과 WARNING은 호출측
             (`main._reroll_books_to_spot`)이 맡는다. `should_roll_atm()`을 그대로 쓰는 이유:
             "임계는 넘었지만 같은 격자 칸"인 경계 상태를 고착으로 오판하지 않기 위해서다.
        실패 조건: 창이 아직 없으면(기동 직전) None — 고착이 아니라 미기동이다.
        """
        if self._current_atm is None:
            return None
        if not should_roll_atm(self._current_atm, spot, self._strike_interval, self._hysteresis_ratio):
            return None
        return abs(spot - self._current_atm)

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
        # 2026-08-07(고도화#1): 유지 큐도 비운다. 재연결은 서버 쪽 구독을 전부 날리므로
        # "창 밖이지만 살아 있다"고 믿던 구독이 실제로는 없다 — 그대로 두면 `reclaim()`이
        # 없는 구독을 회수했다고 판정해 **그 행사가가 새 연결에 영영 등록되지 않는다**
        # (바로 아래 `_current_atm = None`이 막으려는 것과 같은 계열의 상태 불일치).
        #
        # 2026-08-12(§7-1) — **`clear()`가 아니라 `rebind()`다.** 종전에는 큐만 비우고 풀이 쥔
        # 클라이언트는 그대로 뒀는데, 풀은 기동 시 한 번만 만들어지므로 그 뒤로 영원히 **첫**
        # 클라이언트를 쥐었다. 그 결과 `ensure_free()`가 죽은 커넥션에 `unsubscribe`를 보내
        # 예외를 냈고, 그 예외가 다시 재연결을 태워 **자기지속 루프**가 됐다(08-12 31회).
        # 상세 근거는 `SubscriptionRetentionPool.rebind` docstring.
        if self._retention is not None:
            self._retention.rebind(ws_client)
        # 2026-08-04(Fix#6): 히스테리시스도 함께 초기화한다 — 안 그러면 재연결 직후
        # `should_roll_atm()`이 "이미 그 ATM이다"로 판정해 **새 연결에 구독을 하나도 안 보낸다**
        # (위 `_desired_strikes = set()`이 막으려던 바로 그 상태를 다른 경로로 다시 만든다).
        self._current_atm = None
