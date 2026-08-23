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

# 2026-08-07(§3-1 / Fix#1) — **현물(유가증권시장) 연속거래 종료.** 위 15:35와 다른 시장이다.
#
# 위 `CLOSING_AUCTION_START`는 **파생상품시장**(KOSPI200 선물·옵션, 15:45 종료)의 종가 단일가다.
# 그런데 우리가 쓰는 스팟은 파생이 아니라 **현물 지수**(옵션 시세 응답의 `bstp_nmix_prpr` =
# KOSPI200 지수 현재가)이고, 유가증권시장은 **15:20~15:30이 장 마감 동시호가**라 그 구간에
# 연속 체결이 없다. 지수는 산출이 멈추고, 15:30 이후에는 종가로 고정된다.
#
# **넉 달간 이것을 매일 장애로 신고했다.** 08-04~08-07 나흘 실측(`index_frozen_max_run`):
#
#   날짜       정지 구간      최장 연속
#   08-04     15:21~15:34       —
#   08-05     15:21~15:34       9분
#   08-06     15:21~15:34       9분
#   08-07     15:21~15:34       9분   ← 15:20에 975.03으로 멈춰 15:29까지 고정,
#                                        같은 시각 선물은 978.40~980.25로 계속 움직였다
#
# **매일 같은 시각·매일 정확히 9분**이라는 규칙성 자체가 증거다. 08-05에 이 지표를 만들 때
# "베이시스로는 설명되지 않는다(그 값이 곧 사고다)"라고 적었는데, 그 판정이 맞으려면
# **지수가 살아 있는 구간**이어야 한다는 전제가 빠져 있었다. 07-31~09:00을 `is_preopen`으로
# 가른 것과 정확히 같은 종류의 누락이다.
#
# 상한을 안 두는 이유: 15:30 이후에도 지수는 **종가에 고정**이라 여전히 실시간 값이 아니다.
# 즉 15:20부터 장 마감까지 전 구간에서 지수 스팟은 "직전 체결의 잔상"이다.
EQUITY_CONTINUOUS_TRADING_END = dtime(15, 20)

# 2026-08-23(08-21 §1-13 / §4 Fix#1) — **유가증권시장 장 마감 동시호가의 「끝」.**
#
# 위 `EQUITY_CONTINUOUS_TRADING_END`(15:20)는 그 구간의 **시작**이다. 둘을 한자리에 두는 이유는
# 08-21에 그 차이가 실제로 하루를 갈랐기 때문이다: 08-20 보고서가 적재 감시창을 「`15:20`까지」로
# 넓히라고 지시했는데, 08-21의 빈손 구간은 **15:05~15:30(26분)** 이었다. 15:20으로 옮겼으면
# **구멍의 10분이 그대로 남는다.** 시작 시각으로 끝을 막으려 한 것이 그 지시의 오류다.
#
# 15:30 이후를 안 덮는 이유는 `liveness.INGEST_WATCH_END` 주석에 있다 — 그 뒤는 폴링이
# 자연스럽게 잦아드는 구간이고, 거기 임계를 걸면 **매일** 오경보가 난다.
EQUITY_CLOSING_AUCTION_END = dtime(15, 30)

# 신규 진입 컷오프 / 강제 평탄화 — v6 §4.2 "운영 헌법" (2026-08-06 §2-2 / Fix#1).
#
# ## 왜 이 두 값이 같은 모듈에 있는가
#
# v6 §4.2는 이 둘을 **한 표에 나란히** 적어뒀다:
#
#     | 신규 진입 컷오프 | 14:50 이후 신규 진입 금지 |
#     | 강제 평탄화       | 15:10 이전 완료 (운영 헌법) |
#
# 두 값은 독립적이지 않다. 컷오프가 평탄화보다 **뒤에** 있으면 청산할 수 없는 포지션이
# 생긴다 — 15:30에 진입한 것을 15:10이 청산할 방법은 없다. 그래서 두 값을 한자리에 두고
# `tests/test_session.py`가 `NEW_ENTRY_CUTOFF < FORCED_FLAT_TIME`을 기계적으로 강제한다.
#
# ## 2026-08-06에 실제로 벌어진 일
#
# 청산 쪽 15:10은 `execution/exit_stack.py`에 구현돼 있었다(`is_forced_flat_time`).
# **진입 쪽 14:50은 설계 문서에만 있고 코드 어디에도 없었다** — `RiskEngine.evaluate_entry()`에
# 시각 인자 자체가 없었다. 그날 실측:
#
#     14:50 초과 ENTER  21건
#     15:10 초과 ENTER  18건 (마지막 15:30)
#
# 08-05까지 팔레트가 전량 `wait_only`라 ENTER가 0건이었고, 08-05 `p1`(VRP 배선)이 팔레트를
# 연 **첫날 바로** 드러났다. 막힌 경로 뒤의 게이트는 검정된 적이 없다 — 08-03 §2-1의
# 감마플립 사건(넉 달간 존재하지 않는 것을 "개선"했다)과 같은 형태다.
#
# ## 확신도 페널티로는 대신할 수 없다
#
# 같은 날 이벤트 캘린더(08-05 `p7`)가 15:21~15:35 확신도를 0.638 → 0.213(0.33배)으로 깎았는데도
# 그 구간에서 SMALL_TEST ENTER가 10분 연속 나왔다. **가중치를 낮추는 것과 금지하는 것은 다르다.**
NEW_ENTRY_CUTOFF = dtime(14, 50)
FORCED_FLAT_TIME = dtime(15, 10)


def is_after_entry_cutoff(now) -> bool:
    """입력: datetime(또는 time). 반환: 신규 진입이 금지된 시각인가(v6 §4.2).

    경계는 **초과**(`>`)가 아니라 **이상**(`>=`)이다 — "14:50 이후 신규 진입 금지"의 자연스러운
    독해이고, 14:50:00 정각 판단을 통과시키면 그 한 건만 규칙 밖에 놓인다.

    상한을 두지 않는 이유는 `is_closing_auction()`과 같다 — 장 마감 이후는 어차피 관측 대상이
    아니고, 막아두면 종료가 늦어진 사이클이 "진입 가능"으로 잘못 분류된다.
    """
    moment = now.time() if hasattr(now, "time") else now
    return moment >= NEW_ENTRY_CUTOFF


def is_forced_flat_time(now) -> bool:
    """입력: datetime(또는 time). 반환: 강제 평탄화 시각을 지났는가(v6 §13.3, 해제 불가).

    `execution/exit_stack.MarketStructureState.is_forced_flat_time`에 넣을 값을 여기서 만든다 —
    라이브 루프가 실행 엔진에 배선되는 시점(Phase 2)에 그 필드를 손으로 채우게 두면 15:10이
    두 번째 장소에 적히고, 그 순간 이 모듈의 존재 이유가 사라진다.
    """
    moment = now.time() if hasattr(now, "time") else now
    return moment >= FORCED_FLAT_TIME


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


def is_equity_spot_live(now) -> bool:
    """
    입력: datetime(또는 time). 반환: **지수(현물) 스팟이 실시간 값인가.**

    2026-08-07(§3-1 / Fix#1·#2). 근거는 `EQUITY_CONTINUOUS_TRADING_END` 주석.
    `is_continuous_trading()`과 경계가 다른 이유는 **시장이 다르기** 때문이다 —
    그쪽은 파생(15:35까지), 이쪽은 현물(15:20까지)이다. 하나로 합치면 15:20~15:35의
    25분이 어느 쪽으로든 틀리게 분류된다.

    이 함수가 False인 구간에서 `underlying_spot_1m`은 **적재되지 않고**(main.py),
    그래서 `options_flow`는 스팟 없음으로 자연히 미가용이 된다 — 장전(`is_preopen`)에
    2026-08-05 `9ffcb9c`가 한 것과 **정확히 같은 처리**다. 멤버를 억지로 살리지도,
    죽이지도 않는다: 없는 입력은 없다고 쓴다.
    """
    moment = now.time() if hasattr(now, "time") else now
    return TRADING_DAY_START <= moment < EQUITY_CONTINUOUS_TRADING_END


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
