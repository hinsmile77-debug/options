"""거래 세션 경계 — KOSPI200 선물·옵션의 하루가 어떻게 나뉘는가 (2026-08-05 §2-8).

## 이 모듈이 생긴 이유

**종가 단일가 구간(15:35~15:45)에는 연속 체결이 없다.** WS 체결 메시지가 끊기고 1분봉이
안 만들어지므로 `market_raw_1m.ofi`도 채워지지 않는다. 이것은 결함이 아니라 시장 구조다.

이 사실을 아는 코드가 2026-08-05까지 프로젝트에 **딱 하나뿐이었고, 그게 화면이었다** —
`dashboard/data_source._CLOSING_AUCTION_START`. 08-03에 그 상수가 생긴 이유도 같은 형태의
사고였다: COCKPIT 선물 배지가 **매 거래일 15:40~15:45에 오경보**를 냈다(정상을 이상으로 표시).

그런데 판단 경로에는 그 인식이 없었다. 08-05 실측:

    15:35  STANDARD    가용멤버 4
    15:36  SMALL_TEST  가용멤버 3   ← orderflow_ofi_vpin 사망("ofi/queue_imbalance 없음")
    ...
    15:44  SMALL_TEST  가용멤버 3

9분 동안 앙상블이 25% 얇아졌는데 **사유가 "데이터가 없다"로만 남아** 장애와 구분되지 않았다.
08-04 §2-10은 이 시간대를 *"종가 형성 구간이라 가치가 높다"* 고 적었다 — 가치가 높다고 판정한
구간에서 판단이 조용히 얇아지고 있었던 것이다.

## 규약 B의 적용

같은 사실을 두 곳에 적지 않는다. 화면(`dashboard`)과 판단(`main`)이 **이 모듈 하나**를 쓴다.
`tests/test_ops_metric_conventions.py`가 그 단일 출처를 강제한다.

## 이 모듈이 하지 않는 것

**멤버를 억지로 살리지 않는다.** 단일가 구간에도 OFI는 여전히 없고, 없는 값을 지어내면
08-03의 허수 감마플립과 같은 종류의 결함이 된다. 여기서 하는 일은 오직
*"왜 없는지 아는가"* 를 기록 가능하게 만드는 것이다.
"""

from __future__ import annotations

from datetime import time as dtime

# 정규장 연속거래 구간 (v6 §16.1).
TRADING_DAY_START = dtime(9, 0)
TRADING_DAY_END = dtime(15, 45)

# 종가 단일가(장 마감 동시호가) 시작 — 이 시각부터 TRADING_DAY_END까지 연속 체결이 없다.
#
# 2026-08-03 COCKPIT 육안 점검에서 확정한 값이다(그날 15:44:53 화면이 "선물 시세 12분째 결손"
# 노란불을 띄웠고, WS 마지막 수신이 15:34:55였다). 08-05 판단 로그가 같은 경계를 독립적으로
# 재확인했다 — `orderflow_ofi_vpin`이 정확히 15:36부터 미가용으로 떨어졌다(15:35 봉이 마지막).
CLOSING_AUCTION_START = dtime(15, 35)


def is_closing_auction(now) -> bool:
    """입력: datetime(또는 time). 반환: 종가 단일가 구간인가.

    상한을 `TRADING_DAY_END`로 막지 않는 이유: 15:45 이후는 장외라 어차피 관측 대상이 아니고,
    막아두면 종료 처리가 몇 초 늦어진 사이클이 "연속거래 중"으로 잘못 분류된다.
    """
    moment = now.time() if hasattr(now, "time") else now
    return moment >= CLOSING_AUCTION_START


def is_continuous_trading(now) -> bool:
    """연속 체결이 일어나는 구간인가 — WS 기반 데이터(1분봉·OFI·VPIN)의 존재 전제다."""
    moment = now.time() if hasattr(now, "time") else now
    return TRADING_DAY_START <= moment < CLOSING_AUCTION_START


def is_preopen(now) -> bool:
    """입력: datetime(또는 time). 반환: 장 개시(`TRADING_DAY_START`) 전인가.

    2026-08-06(운영점검 장전편 §2-5 / Fix#5) — 이 구간에는 **기초자산 스팟이 설계상 없다.**
    2026-08-05 `9ffcb9c`가 장전 스팟 적재를 의도적으로 끊었기 때문이다(그전에는 전일 종가가
    75분간 stale하게 실려 ATM 정합률 지표를 통과해 버렸다). `_build_signal_inputs()`는
    `db.UNDERLYING_SPOT_MAX_AGE_MINUTES` 경계로 그것을 받아 GEX/감마플립/VRP를 **틀린 값 대신
    미가용**으로 만든다 — 의도된 정상 동작이다.

    그런데 관측 쪽에 이 개념이 없어서, 08-06 장전 내내 COCKPIT이 노란불로
    *"options_flow가 한 번도 활성화되지 않았다"* 를 냈다. **설계대로 동작한 것을 매일 90분씩
    장애로 신고한 것이다.** `is_closing_auction()`이 15:36~15:44에 대해 하는 일과 정확히 같은
    일을 07:31~09:00에 대해 한다.
    """
    moment = now.time() if hasattr(now, "time") else now
    return moment < TRADING_DAY_START
