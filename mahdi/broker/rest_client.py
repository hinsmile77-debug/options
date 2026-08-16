"""KIS REST 클라이언트 — 옵션 체인 조회, 잔고 조회, 주문 제출 (모의/실전 겸용).

TR ID/경로 상수는 tr_codes.py 단일 소스를 사용한다.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from datetime import date

import httpx

from mahdi.broker import tr_codes
from mahdi.broker.order_state_machine import OrderState
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.config.settings import KISSettings

logger = logging.getLogger("mahdi.broker.rest_client")

# 2026-07-08 실측: main.py의 옵션체인/수급/유동성 폴링 루프 3개가 동시에(asyncio.gather) 60초
# 주기로 REST를 호출하는데, 각 루프 내부는 순차 호출이라도 서로 다른 asyncio.to_thread 스레드가
# 겹치는 순간 KIS 앱키의 초당 호출 한도를 넘겨 500 Internal Server Error가 대량 발생함(정규장
# 405분 중 203분치 옵션체인 데이터가 통째로 유실됨을 DB로 확인). 당시 문서화된 모의투자 TPS
# 한도가 없어 보수적으로 2건/초(0.5초 간격)로 제한.
#
# 2026-07-20 재실측: 2건/초로도 부족함을 확인 — _collect_option_chain_cycle이 행사가마다
# 콜→풋 순서로 호출하는데, DB로 확인한 결과 콜은 거의 항상 성공(행사가당 18~19건/8분)하고
# 풋만 계속 500(행사가당 3건/8분)이 되는 정확한 교대 패턴이 5개 행사가 전부에서 동일하게
# 나타났다. 매 쌍의 두 번째 호출(0.5초 뒤)만 계속 걸리는 이 패턴은 KIS 모의투자의 실제 한도가
# 2건/초가 아니라 1건/초에 더 가깝다는 강한 정황이다 — 1건/초(1.0초 간격)로 상향한다.
# 사이클당 필요한 최대 호출(옵션체인 ~30 + 수급 3 = 33)도 33초면 끝나 60초 주기 안에 들어간다.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.0

# 2026-07-31(운영점검보고서 2026-07-31 §2-1 원인 b / §4 우선순위 3) — 느린 호출 계측 임계.
#
# 07-31 하루치 로그에서 "호출 1건당 8~9초"가 연속되는 정체 구간 7건이 관측됐다(전부 10분 창의
# 0분대, 계좌잔고 호출 직후 시작). 그런데 원인을 좁힐 수가 없었다:
#   - 공유 _RateLimiter의 당시 배율은 1.22~1.82배(= 최대 1.82초)라 9초를 설명하지 못한다.
#   - 같은 시각 투자자수급(1.2초 간격)·만기유동성 호가(3.8초)는 정상 속도로 응답했으므로
#     KIS 서버 전체 지연도 아니다.
# httpx가 남기는 로그는 **응답 완료 시점 한 줄**뿐이라 "페이서에서 기다린 시간"과 "서버가 응답을
# 준 시간"이 합쳐져 구분되지 않는 것이 문제였다. 두 구간을 따로 재서 남기면 세 가설이 갈린다:
#   페이서대기가 크다 → 예약 큐 경합(_next_allowed 누적, 즉 다른 폴러와의 충돌)
#   HTTP가 크다      → KIS 서버 또는 커넥션 풀(httpx.Client 단일 인스턴스 공유)
#   둘 다 작다       → 이벤트 루프/스레드풀 블로킹(asyncio.to_thread 기본 풀 포화 등)
#
# 임계를 둔 이유: 하루 12,947건을 전부 남기면 07-31에 어렵게 되찾은 로그 가독성
# (사람이 읽는 줄 6,161 → 2,963줄)을 다시 잃는다. 정상 호출은 페이서대기 포함 ~1.2초라
# 3.0초면 정상 구간은 거의 걸리지 않고 §2-1(b)의 5~9초 구간만 남는다.
#
# 2026-08-03(운영점검보고서 §2-0 p3 / §2-8 / §4 우선순위 3) — **진단 목적이 달성됐다.**
# 08-03 실측 933건으로 원인이 갈렸다: 페이서대기 450건 / HTTP 483건으로 우세 분류는 반반이지만
# **극단값은 전부 HTTP 쪽**이고(상위 5건 중 4건이 페이서대기 0.00~0.78초에 HTTP 8.4~10.0초),
# 최대값이 정확히 10.00초인 것은 `httpx.Client(timeout=10.0)` 천장에 닿은 것이다. 같은 날
# `RemoteProtocolError` 8건(07-31도 8건)이 함께 나온 것까지 합치면 결론은 **커넥션 풀 재사용
# 실패 + KIS 응답 지연**이다.
#
# 임계를 3.0 → 5.0초로 올리고 레벨을 WARNING → INFO로 내린다. 하루 933건의 WARNING은 진짜
# 경고를 파묻는다(08-03 사람이 읽는 줄 4,629줄의 20%가 이 한 줄이었다). 계측을 없애지 않는 이유는
# 커넥션 풀 조치(아래 `_HTTP_LIMITS`)의 효과를 같은 지표로 재야 하기 때문이다 — **고치기 위해
# 만든 계측을 고친 뒤에 끄면 회귀를 못 잡는다.**
#
# 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 4 / Fix#4) — 5.0 → **3.0초로 되돌린다.**
# 바로 위 문단이 "고치기 위해 만든 계측을 고친 뒤에 끄면 회귀를 못 잡는다"고 적어놓고,
# **정확히 그 일이 벌어졌다.**
#
# 08-04 Fix#8이 read 타임아웃을 4.0초로 낮췄다. 그 순간부터 HTTP 소요는 구조적으로 4.0초를
# 넘을 수 없는데 이 임계는 5.0초였다 — 즉 **임계가 물리적 상한 위에 놓였다.** 결과는 08-05
# 실측이 그대로 보여준다: `httpx.ReadTimeout`이 21건 실재하는데 자동 리포트 §9는
# "임계(5초) 초과 호출 없음"이었고, §0의 가설 검정은 `slow_calls 0건`을 **반증**으로 찍었다.
# 그 0건은 파서 결함이 아니라(계측 감사는 정상 통과했다) **Fix#8이 자기를 정당화한 계측을
# 침묵시킨 것**이다 — §2-6이 "밀림의 90%는 KIS 지연"이라고 귀속시킨 바로 그 표다.
#
# 3.0 근거: 등록된 read 타임아웃 중 **최솟값(4.0초)보다 낮아야** 타임아웃 직전 구간이 보인다.
# 이 부등식은 `tests/test_broker_rest_client.py`의 불변식 테스트가 기계적으로 지킨다 —
# 둘 중 하나만 바뀌어도 테스트가 깨진다(2026-08-05 고도화#3 "임계-물리한계 정합성"의 국소 적용).
#
# 로그 볼륨 재검토: 08-03에 3.0초가 933건을 만든 것은 read가 10초여서 꼬리가 길었기 때문이다.
# 지금은 4.0초에서 잘리고 페이서도 08-04에 정리됐다(밀림 0건) — 08-05 §9-1 기준 3초를 넘는
# 호출은 혼잡 시간대에 집중된 소수다. 예측치는 `hypotheses.yaml` 2026-08-05-p4에 적어둔다.
SLOW_CALL_LOG_THRESHOLD_SECONDS = 3.0

# 2026-08-03(§4 우선순위 3) — 커넥션 풀/타임아웃.
#
# 종전에는 `httpx.Client(timeout=10.0)` 하나로 기본 풀(max_connections=100,
# max_keepalive_connections=20, keepalive_expiry=5.0)을 그대로 썼다. 그런데 이 클라이언트의
# 호출은 전부 공유 `_RateLimiter`가 1건/초로 **직렬화**하므로 커넥션이 동시에 여러 개 필요한
# 상황 자체가 없다 — 풀을 좁히면 같은 커넥션을 계속 재사용해 TLS 핸드셰이크가 줄고, 무엇보다
# keep-alive 유효기간을 우리가 통제할 수 있게 된다.
#
# `keepalive_expiry`를 5.0 → 15.0초로 늘리는 이유: 우리 호출 간격(1초)에서는 5초 만료가 오히려
# 잦은 재연결을 만들고, 재연결 순간과 KIS 쪽이 커넥션을 닫는 타이밍이 겹치면 `RemoteProtocolError`
# ("server disconnected without sending a response")가 난다 — 08-03/07-31 각 8건.
#
# `connect=3.0`: 종전 timeout=10.0은 연결·읽기·쓰기 전부에 10초였다. 연결 자체가 3초를 넘으면
# 재시도가 더 빠르고, 읽기 10초는 그대로 둔다(KIS 응답이 실제로 느린 경우가 있다).
_HTTP_LIMITS = httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=15.0)

# 2026-08-04(운영점검보고서 §2-6 / Fix#8) — read 타임아웃을 10.0 → 4.0초로 낮춘다.
#
# 08-04 실측이 08-03 §4-3의 판정표에 답을 줬다: 느린 호출 362건의 총 2,278초 중 **HTTP가
# 2,050초(90%)** 이고 페이서 대기는 229초(10%)뿐이다(08-03은 54% / 46%였다). 즉 우리가 통제하는
# 스케줄링 쪽 여유분은 사실상 소진됐고, 남은 밀림은 KIS 응답 지연이다.
#
# 그런데 read 10초는 **한 레그가 사이클 전체를 잡아먹게 둔다**: 옵션체인 사이클은 60초 주기에
# 20레그(먼슬리 10 + 위클리 격분 10)를 도는데, 10초짜리 호출 2건이면 그 사이클은 이미 밀린다.
# 08-04 미회수 결손 5분(14:31 / 15:11 / 15:15 / 15:17 / 15:19)이 전부 이 패턴이다.
#
# 4.0초 근거: 08-04 느린 호출의 HTTP 시간 중앙이 약 5.6초이고 정상 호출은 페이서 포함 ~1.2초다.
# 4초를 넘긴 호출은 그 사이클 안에서 회수 가치가 이미 낮다 — 포기하고 다음 레그로 가는 편이
# 남은 레그를 살린다(레그별 부분 실패 허용은 `_collect_option_chain_cycle`에 이미 있다).
# `connect=3.0`은 그대로 둔다(08-03 근거 유지).
#
# **주의**: 이 값을 낮추면 타임아웃 예외가 늘어난다. 그것은 회귀가 아니라 **의도된 교환**이다 —
# `qualitative.read_timeout` 증가와 `overrun.count` 감소를 반드시 나란히 읽을 것.
_HTTP_READ_TIMEOUT_SECONDS = 4.0
_HTTP_CONNECT_TIMEOUT_SECONDS = 3.0
_HTTP_WRITE_POOL_TIMEOUT_SECONDS = 10.0
_HTTP_TIMEOUT = httpx.Timeout(
    _HTTP_WRITE_POOL_TIMEOUT_SECONDS,
    connect=_HTTP_CONNECT_TIMEOUT_SECONDS,
    read=_HTTP_READ_TIMEOUT_SECONDS,
)

# 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 3 / Fix#2) — read 타임아웃은 **엔드포인트마다
# 다르다.** 위 4.0초는 `httpx.Client` 하나에 걸리는 전역값이라, 08-04에 옵션체인을 근거로 정한
# 값이 그날로 **계좌 잔고 폴러를 깨뜨렸다.**
#
# 08-05 실측(자동 리포트 §9-1, 고도화#5로 신설된 전수 백분위):
#   inquire-price   2,825건  p50 0.02초   9시 p95 2.80초
#   inquire-balance    27건  p50 **0.70초**(35배)  9시 p95 1.77초  최대 4.34초
# 잔고 조회는 원래부터 느린 엔드포인트다 — 계좌·증거금·보유종목을 한 번에 집계하는 API라
# 시세 단건 조회와 응답시간 자릿수가 다르다. 4초 천장에 닿아 09:19/09:24/09:34 세 사이클이
# `httpx.ReadTimeout`으로 통째로 날아갔다(개장 후 8사이클 중 3건 = 37.5%). 08-04 이전 실패 0건.
#
# Fix#8의 근거는 *"60초 주기에 20레그를 도는 옵션체인 사이클에서 한 레그가 10초를 잡아먹으면
# 그 사이클이 이미 밀린다"* 였다. 그 근거는 **호출 1건이 곧 사이클 전체인 폴러에는 성립하지
# 않는다** — 잔고는 300초 주기 단발 호출이라 10초를 기다려도 다음 사이클을 밀지 않는다.
# 그래서 Fix#8을 되돌리는 게 아니라 **적용 범위를 원래 근거가 성립하는 곳으로 좁힌다.**
#
# 키를 마지막 경로 조각(`inquire-price`)이 아니라 **전체 경로**로 잡는 이유: 국내 선물옵션
# 시세(PATH_FUTUREOPTION_QUOTE)와 해외선물 시세(PATH_OVERSEAS_FUTUREOPTION_PRICE)는 마지막
# 조각이 둘 다 `inquire-price`다. 조각으로 키를 잡으면 서로 다른 두 엔드포인트에 같은 타임아웃이
# 걸린다(같은 이유로 §9-1의 `inquire-price` 행에는 지금 해외선물 호출이 섞여 있다 — 별건).
#
# 주문(ORDER/ORDER_MODIFY_CANCEL)에도 10초를 준다. 주문 POST가 타임아웃되면 **주문이 접수됐는지
# 아닌지를 알 수 없는 상태**가 되고, 그 모호함은 4초를 아껴서 얻을 이득보다 훨씬 비싸다.
# 지금은 ExecutionEngine이 배선 전이라 실제 호출이 없지만, 배선되는 날 이 값이 4초면 곤란하다.
_SLOW_ENDPOINT_READ_TIMEOUT_SECONDS = 10.0

# ===== 2026-08-11 Fix#3 — 옵션체인 read 타임아웃 레버 (오늘은 **안 켠다**) =====
#
# ## 왜 레버로 두는가
#
# 08-11 15:01~15:22에 22분 연속으로 옵션체인 적재가 0행이었다. 원인은 KIS 지연이 4초를 넘긴
# 것이고, **우리 타임아웃 4.0초가 그것을 100% 실패로 변환**했다. 느린 호출 줄의 HTTP 성분이
# 4.03~4.06초에 못 박혀 있는 것이 그 증거다(페이서 배율은 1.00배 — 우리 쪽 압력이 아니다).
#
# ## 08-11 실측 (자동 리포트 §9-1, 전수 백분위)
#
#     inquire-price   7,165건   p50 0.62초   p95 2.99초   p99 3.29초   최대 7.01초
#
# **4.0초는 p99(3.29)와 최대(7.01) 사이에 있다.** 정상 구간에서는 넉넉하고 지연 구간에서는
# 전부 자른다 — 즉 이 값은 *평시에는 아무 일도 안 하다가 사고 때만 사고를 키우는* 자리에 있다.
#
# ## 그런데 왜 오늘 안 올리는가
#
# **타임아웃을 늘리면 레그당 비용이 늘어 예산이 더 빨리 마른다.** 지금 레그당 비용은
# 페이서 1초 + 타임아웃 4초 = 5초이고, 6초로 올리면 7초가 되어 50초 예산에 7레그밖에 안 들어간다
# (지금 10레그). 즉 **이 레버 단독으로는 상황을 악화시킬 수 있다.**
#
# 그래서 순서가 정해져 있다: **오늘 들어간 Fix#1(연속 타임아웃 조기 포기)이 먼저 실측되어야
# 한다.** 조기 포기가 있으면 "느린 레그를 더 기다린다"의 비용 상한이 3레그로 묶이므로 그때
# 타임아웃을 올리는 것이 안전해진다. 하루에 변수 하나 — 08-04 p4의 교훈이다.
#
# ## 켤 조건과 예측치 (숫자를 보기 전에 적는다)
#
#   조건  08-12 이후 `timeout_abort.count > 0`인 날이 있고, 그날 `budget_exceeded.count`가
#         08-11(87건)보다 줄어 있을 것 — 즉 Fix#1이 실제로 조기에 접었다는 증거가 먼저 있어야 한다.
#   값    6.0초 (p99 3.29의 약 1.8배 — 최대 7.01보다 아래로 둬 "진짜 죽은 호출"은 계속 자른다)
#   주장  `db.chain_minute_coverage.zero_row_by_cause.수집전멸` 감소 (08-11 기준선 **27분**)
#   대가  `qualitative.read_timeout` 감소 · `옵션체인 REST수집 평균` 증가 (08-11 28.6초)
#   대가  `timeout_abort.count` 증가 — 레그당 비용이 늘어 조기 포기가 더 자주 걸린다(의도된 것)
#
# **`rest_latency` p95는 예측하지 않는다** — KIS 귀속이므로 우리가 콜을 줄여도 그들의 p95는
# 안 바뀐다(08-07 NEXT_TODO의 정정과 같은 이유).
#
# ===== 2026-08-16 — 이 레버를 **켜지 않는다**. 유예 사유를 여기 적는다 =====
#
# 08-14 보고서 §6-C는 이 레버(B안, 6.0초)를 *"오늘 실측이 직접 겨누는 레버"* 로 적었다.
# **그 판단은 검열된 데이터를 읽은 것이었고, 미검열 통로로 다시 재니 뒤집혔다.**
#
# ## 왜 4.05초를 근거로 쓸 수 없는가 — 우측 검열(right-censoring)
#
# 14:36 창의 `inquire-price` p50 = 4.05초는 **타임아웃 4.0초에 눌린 값**이다. 4.0초를 넘는
# 호출은 전부 4.0~4.06으로 기록되므로, 그 분포에서 읽을 수 있는 것은 「절반 이상이 4.0을
# 넘었다」뿐이고 **진짜 지연이 4.1초인지 20초인지는 이 통로로 알 수 없다.**
# 「p50 4.05 > 타임아웃 4.0이니 6.0으로 올리면 흡수된다」는 그 미지를 4.1초라고 가정한 것이다.
#
# ## 미검열 통로가 답을 갖고 있었다 (`inquire-balance` — 타임아웃 10.0초, 300초당 1콜)
#
# 같은 로그의 300초 창 전수(2026-08-14, 창당 1콜이므로 p50 = 그 호출의 실측 지연):
#
#     12:31  4.19    12:56  6.95    13:31  8.16    13:56  5.02
#     14:06 10.02    14:21 12.72    14:26 12.03    14:36 10.03
#     14:41 10.39    14:51 10.53    15:01 10.89    15:21 10.05
#     15:26  8.39    15:31  2.11    15:36  0.08   ← 정규장 마감 후 회복
#
# **전멸 84분 동안 KIS 실제 지연은 10초 이상이었다**(자기 통로의 10.0초 천장에 눌리고 일부는
# 12초를 넘겼다). 6.0초로 올려도 그 호출들은 **여전히 전부 실패**한다. 바뀌는 것은 실패 1건의
# 비용이 4초에서 6초가 되는 것뿐이고, 그러면 50초 예산에 들어가는 레그가 10개에서 7개로 준다 —
# **나쁜 날에 이득이 없고 정상일에 커버리지를 30% 잃는다.** 위 08-11 주석이 경고한
# *"이 레버 단독으로는 상황을 악화시킬 수 있다"* 가 실측으로 확인된 것이다.
#
# ## 사전 등록된 발동 조건도 충족되지 않았다
#
# 위 「켤 조건」은 `timeout_abort.count > 0`인 날에 **`budget_exceeded.count`가 08-11(87건)보다
# 줄어 있을 것**을 함께 요구한다. 08-14는 timeout_abort 16건이지만 budget_exceeded **138건**
# (증가)이다. 조건의 뒷절이 깨져 있다 — **숫자를 보고 조건을 고치지 않는다.**
#
# ## 그래서 무엇을 하는가
#
# 이 레버 대신 **미검열 통로를 선행지표로 읽는다.** 위 시계열은 12:31 4.19 → 13:31 8.16 →
# 14:06 10.02로 **절벽을 95분 앞서** 올라갔다. 검열된 `inquire-price` p95는 09시부터 종일
# 붉어서 예고력이 없었다(08-14 고도화 2가 겨눈 지표의 한계). 즉 이 사건의 조기경보는
# 「p50/타임아웃 비율」이 아니라 **「천장이 없는 통로의 절대 지연」** 이다.
#
# 재검토 조건: `inquire-balance` 기준 오후 지연이 **6.0초 미만**인 전멸일이 나오면 그날
# 데이터로 이 레버를 다시 검토한다(그때는 6.0초가 실제로 흡수한다).
OPTION_CHAIN_READ_TIMEOUT_SECONDS: float | None = None  # None = 전역값(4.0초) 사용 = 레버 OFF

_ENDPOINT_READ_TIMEOUT_SECONDS: dict[str, float] = {
    # 300초 주기 단발 호출 — 느려도 다음 사이클을 밀지 않는다.
    tr_codes.PATH_FUTUREOPTION_BALANCE: _SLOW_ENDPOINT_READ_TIMEOUT_SECONDS,
    # 60초 주기지만 북별 1개(사이클당 11콜, 점유 중앙 9.0초/최대 24.4초 — 리포트 §7)라 여유가 있다.
    # 08-05에 이 엔드포인트도 4.00초 천장에 닿아 만기유동성 폴링이 2건 실패했다.
    tr_codes.PATH_FUTUREOPTION_ASKING_PRICE: _SLOW_ENDPOINT_READ_TIMEOUT_SECONDS,
    # 주문 — 타임아웃 시 접수 여부가 불명확해진다(위 주석).
    tr_codes.PATH_FUTUREOPTION_ORDER: _SLOW_ENDPOINT_READ_TIMEOUT_SECONDS,
    tr_codes.PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL: _SLOW_ENDPOINT_READ_TIMEOUT_SECONDS,
}

# Fix#3 레버가 켜져 있을 때만 옵션체인 경로를 등록한다. `None`이면 항목 자체가 안 생기므로
# `timeout_for_url()`이 전역값으로 떨어진다 — **레버 OFF가 종전과 바이트 단위로 같은 동작**이다.
#
# 경로 충돌 확인(위 `_ENDPOINT_LABEL_OVERRIDES` 주석이 경고한 그것): 국내
# `/uapi/domestic-futureoption/.../inquire-price`와 해외
# `/uapi/overseas-futureoption/.../inquire-price`는 **서로의 suffix가 아니다**
# (`domestic-`/`overseas-`가 앞에서 갈린다). `inquire-asking-price`와도 겹치지 않는다.
# 그래서 `timeout_for_url()`의 "등록 경로끼리는 서로의 suffix가 아니다" 전제가 유지된다 —
# 이 전제는 `tests/test_broker_rest_client.py`가 기계적으로 지킨다.
if OPTION_CHAIN_READ_TIMEOUT_SECONDS is not None:
    _ENDPOINT_READ_TIMEOUT_SECONDS[tr_codes.PATH_FUTUREOPTION_QUOTE] = (
        OPTION_CHAIN_READ_TIMEOUT_SECONDS
    )


# 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 4 후속) — 계측용 엔드포인트 라벨.
#
# 종전에는 `url.rsplit("/", 1)[-1]`로 마지막 경로 조각을 그대로 썼는데, 국내 선물옵션 시세
# (`PATH_FUTUREOPTION_QUOTE`)와 해외선물 시세(`PATH_OVERSEAS_FUTUREOPTION_PRICE`)는 **마지막
# 조각이 둘 다 `inquire-price`** 다. 그래서 자동 리포트 §9-1의 `inquire-price` 행에는 옵션체인
# 2,825건과 매크로 폴러의 해외선물 호출(~88건/일)이 **한 행에 섞여** 있었다.
#
# 섞이면 무엇이 문제인가: §9-1은 "이 엔드포인트가 오늘 느렸는가"를 재는 표이고, 그 위에
# `hypotheses.yaml` 2026-08-04-p5의 **자동 대응 규칙**(p95가 2.5초를 이틀 연속 넘으면 위클리
# 폴링을 격분으로 늘린다)이 얹혀 있다. 해외선물은 CBOT/CME 미신청이라 **항상 실패**하는데,
# 그 실패 응답의 지연이 옵션체인 p95를 흔들면 **엉뚱한 폴러를 줄이게 된다.**
#
# 라벨은 `[\w-]+`만 쓴다 — `log_metrics._REST_LATENCY_ITEM_RE`가 그 문자 집합으로 파싱한다.
# 전체 경로를 쓰면 슬래시 때문에 파서가 눈이 먼다(08-04 §2-1과 같은 사고).
#
# 참고: `log_metrics.classify_endpoint()`(폴러 그룹 역산)는 같은 충돌을 **검사 순서**로 이미
# 피하고 있다(`overseas-futureoption`을 `inquire-price`보다 먼저 본다) — 그쪽은 정상이다.
_ENDPOINT_LABEL_OVERRIDES: dict[str, str] = {
    tr_codes.PATH_OVERSEAS_FUTUREOPTION_PRICE: "overseas-inquire-price",
}


def endpoint_label(url: str) -> str:
    """
    입력: 요청 URL(도메인·쿼리스트링 포함 가능).
    계산: 계측 표에 쓸 짧은 엔드포인트 라벨. 충돌하는 경로만 `_ENDPOINT_LABEL_OVERRIDES`로
         구분하고, 나머지는 마지막 경로 조각을 그대로 쓴다.
    해석: 상세 근거는 `_ENDPOINT_LABEL_OVERRIDES` 주석. 새 엔드포인트를 추가할 때 마지막 조각이
         기존과 겹치면 여기에 항목을 더해야 한다 — 잊었을 때 알려주는 것이
         `tests/test_broker_rest_client.py`의 라벨 충돌 테스트다(값이 아니라 **관계**를 지킨다).
    실패 조건: 없음.
    """
    path = url.split("?", 1)[0]
    for endpoint_path, label in _ENDPOINT_LABEL_OVERRIDES.items():
        if path.endswith(endpoint_path):
            return label
    return path.rsplit("/", 1)[-1]


def timeout_for_url(url: str) -> httpx.Timeout:
    """
    입력: 요청 URL(도메인 포함, 쿼리스트링은 있어도 되고 없어도 된다).
    계산: `_ENDPOINT_READ_TIMEOUT_SECONDS`에 등록된 경로면 그 read 타임아웃을, 아니면 기본값
         (`_HTTP_READ_TIMEOUT_SECONDS`)을 담은 `httpx.Timeout`을 돌려준다. connect/write/pool은
         엔드포인트와 무관하게 동일하다 — 느린 것은 **KIS의 응답 생성**이지 연결 수립이 아니다.
    해석: 상세 근거는 `_ENDPOINT_READ_TIMEOUT_SECONDS` 주석. 매칭은 경로 **suffix**로 한다
         (URL 앞에 실전/모의 도메인이 붙기 때문). 등록 경로끼리는 서로의 suffix가 아니므로
         (`/order`와 `/order-rvsecncl`은 끝이 다르다) 순회 순서에 결과가 좌우되지 않는다.
    실패 조건: 없음 — 모르는 경로는 기본값으로 떨어진다.
    """
    path = url.split("?", 1)[0]
    for endpoint_path, read_seconds in _ENDPOINT_READ_TIMEOUT_SECONDS.items():
        if path.endswith(endpoint_path):
            return httpx.Timeout(
                _HTTP_WRITE_POOL_TIMEOUT_SECONDS,
                connect=_HTTP_CONNECT_TIMEOUT_SECONDS,
                read=read_seconds,
            )
    return _HTTP_TIMEOUT

# ===== 2026-08-04(운영점검보고서 §2-1 / Fix#1) — 로그 포맷 계약 =====
#
# 08-03에 이 파일의 로그 세 곳을 바꿨는데(느린 호출 WARNING→INFO, RemoteProtocolError 재시도
# 도입, HTTPStatusError 트레이스백 제거) **`mahdi/ops/log_metrics.py`의 파서가 전부 눈이 멀었다.**
# 그 결과 08-04 자동 리포트는 `slow_calls 0건`(실제 362건), `remote_protocol_error 실측 없음`
# (실제 25건), `http_status_error 0건`(08-03은 105건)을 보고했다. 심지어 아래 `_log_if_slow`의
# 주석은 *"지표 집계는 계속 이 줄을 읽는다"* 고 **단언**하고 있었다 — 검증되지 않은 단언이었다.
#
# 그래서 포맷 문자열을 모듈 상수로 끌어올린다. `log_metrics`는 순수 파서로 남기기 위해 이 모듈을
# **import하지 않는다**(그 설계 결정은 유지한다) — 대신 `tests/test_ops_log_metrics_contract.py`가
# 양쪽을 동시에 import해 "이 상수로 만든 줄을 저 파서가 세는가"를 검증한다. 포맷을 바꾸면
# 테스트가 깨지므로, 로그만 바꾸고 파서를 안 고치는 일이 다시는 조용히 지나갈 수 없다.
LOG_SLOW_CALL = "느린 REST 호출 %.2f초 = 페이서대기 %.2f초 + HTTP %.2f초 (배율 %.2f배, %s %s)"
LOG_REMOTE_PROTOCOL_RETRY = "커넥션 재사용 실패(RemoteProtocolError) — 1회 재시도: %s"
LOG_BACKOFF_EXPAND = "레이트리밋 백오프 확대: %.2fs -> %.2fs (기준 대비 %.2f배)"
LOG_BACKOFF_RECOVER = "레이트리밋 백오프 회복: %.2fs -> %.2fs (기준 대비 %.2f배)"


class _RateLimiter:
    """여러 스레드(asyncio.to_thread)가 공유하는 최소 호출 간격 페이서.

    2026-07-20(고도화): 고정 간격 대신 적응형으로 개선했다 — KIS 모의투자의 실제 초당 호출
    한도는 문서화돼 있지 않고, 이미 2026-07-08(2건/초로 추정) → 2026-07-20(실측 결과 1건/초에
    더 가까움)로 한 번 틀렸던 적이 있다. 앞으로도 계좌/시간대별로 실제 한도가 달라질 가능성을
    고려해, 레이트리밋(500 + KIS 에러코드 EGW00201)이 감지되면 다음 호출부터 간격을 즉시
    넓히고(record_rate_limit_hit), 그 넓어진 간격에서 성공이 충분히 이어지면 서서히 기준
    간격(min_interval)까지만 되돌린다(record_success) — 기준 간격 밑으로는 절대 안 내려가고,
    무한정 넓어지지도 않도록 상한(_MAX_INTERVAL_MULTIPLIER배)을 둔다.

    락은 "다음 호출 가능 시각" 예약에만 쓰고 실제 대기(time.sleep)는 락 밖에서 하므로,
    대기 중인 스레드가 다른 스레드의 예약을 막지 않는다.
    """

    _BACKOFF_MULTIPLIER = 1.5  # 레이트리밋 감지될 때마다 현재 간격에 곱하는 값
    _MAX_INTERVAL_MULTIPLIER = 4.0  # 기준 간격(min_interval) 대비 최대 몇 배까지 늘어날 수 있는지
    # 2026-07-22 재조정 시도(운영점검보고서 §2-1): 임계값 20일 때는 4배(최대치)에서 1배로
    # 완전히 되돌아오는 데 성공 약 260건이 필요해(0.9배씩 13단계 × 20건) 20으로는 회복이 느려
    # 보였고, 그날 하루치(EGW00201 83건/14,852건, 스케줄 밀림 57건)를 근거로 8로 낮췄다.
    # 2026-07-23 재검토(운영점검보고서 §2-1): 8로 바꾼 첫 전체 거래일(임계값 20 그대로였던
    # 07-22와 동일 방법론으로 나란히 집계) 결과 EGW00201 비율(0.38%→0.48%)·스케줄 밀림
    # (57→83건)·평균 지연(10.7초→18.7초)·최대 지연(45.5초→76.2초)이 전부 악화됐다 — 임계값을
    # 너무 낮추면 백오프에서 너무 빨리 벗어나 다시 레이트리밋에 바로 부딪히는 "플래핑"이
    # 실제로 일어났을 가능성이 높다(당시 커밋 메시지에도 이 위험이 언급돼 있었음). 후속
    # 프로젝트 messiah(fuoption)도 같은 계약의 RateLimiter를 독립적으로 튜닝하며 기본값 20을
    # 그대로 유지하고 있어(src/messiah/broker/kis/rest_client.py) 원래 값으로 되돌린다. 이번엔
    # 아래 record_rate_limit_hit/record_success 로깅을 함께 추가해, 다음에 파라미터를 다시
    # 바꿀 때는 간접 증상(EGW00201 횟수)이 아니라 실제 배율 전이 로그로 검증할 수 있게 한다.
    _RECOVERY_SUCCESS_THRESHOLD = 20  # 이만큼 연속 성공하면 간격을 한 단계 되돌림
    _RECOVERY_FACTOR = 0.9  # 되돌릴 때 곱하는 축소 비율(급하게 되돌리지 않고 서서히)

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._current_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._consecutive_successes = 0
        # 2026-07-28(운영점검보고서 2026-07-27 후속 재수사): 이 페이서는 poll_option_chain/
        # poll_investor_flow/poll_expiry_liquidity/poll_macro_snapshot 네 폴러가 asyncio.gather로
        # 동시에 공유하는 단일 인스턴스다(main.py에서 KISRestClient 한 개를 전부에 넘김) — 옵션체인
        # 사이클의 REST수집 소요시간이 자기 몫(30콜×1.0초=~30초)만으로 설명 안 되고 32~49초로
        # 들쭉날쭉한 게 다른 폴러의 동시 호출이 같은 슬롯을 나눠 쓰기 때문인지 확인하려면 "이번
        # 옵션체인 사이클 동안 실제로 몇 건이 이 페이서를 통과했는지"를 알아야 한다 — 자기 예상
        # 호출 수(행사가×2×북)를 넘는 초과분이 있으면 다른 폴러가 끼어든 직접 증거가 된다.
        self._total_calls = 0

    @property
    def current_multiplier(self) -> float:
        """계산: 현재 페이싱 간격이 기준 간격(min_interval)의 몇 배인지 — 1.0이면 백오프 없음,
        _MAX_INTERVAL_MULTIPLIER(4.0)에 가까울수록 레이트리밋에 강하게 걸려 있는 상태다.
        COCKPIT 헬스체크(§2-1 고도화 방안, "레이트리밋 근접도 배지")가 읽는 값."""
        if self._min_interval <= 0:
            return 1.0
        return self._current_interval / self._min_interval

    @property
    def total_calls(self) -> int:
        """계산: 이 페이서가 기동 이래 통과시킨 전체 호출 건수(모든 폴러 합산) — 어느 한 폴러의
        사이클 전후 값 차이를 구하면 그 구간 동안 다른 폴러가 몇 건이나 끼어들었는지 역산할 수
        있다(2026-07-28, 스케줄 밀림 재수사)."""
        return self._total_calls

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self._current_interval
            self._total_calls += 1
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def record_completion(self) -> None:
        """호출이 끝난 직후 호출 — 다음 슬롯을 **완료 시각 + 간격**으로 다시 민다(늦추기만 한다).

        2026-08-06(운영점검 장전편 §2-1 / Fix#1) — `wait()`는 다음 슬롯을 **호출 시작 시각**
        기준으로 예약한다. 그런데 KIS는 자기가 처리를 끝낸 시점 기준으로 초당 건수를 세는 것으로
        보인다: 앞 호출이 느리면 다음 호출이 정확히 +min_interval에 나가도 KIS의 창 안으로 들어간다.

        08-05 전량(12,561콜) + 08-06 장전(752콜) 대조에서 **예외가 하나도 없었다**:

          직전 호출과의 완료 간격 < 1.00초 : 2,500건 중 EGW00201 **65건**(2.6%)
          직전 호출과의 완료 간격 >= 1.00초: **10,811건 중 0건(0.00%)**

        그리고 08-05 전체 호출의 **19.6%가 간격 1.00초 미만**이었다 — 페이서가 1.0초를 지킨다고
        믿고 있었지만 다섯 중 하나가 한도 밑으로 나가고 있었다. 대가는 EGW00201 자체가 아니라
        그것이 유발한 백오프다: 08-05 확대 84건, **시간가중 평균 배율 1.214배** = 하루 종일
        REST를 21% 느리게 썼다. 그 느려짐이 사이클 지연 → 예산 절단 → 먼슬리 북 두께로 흘러간다
        (08-06 07:48 실측: EGW00201 1건이 먼슬리 북을 10 → 9레그로 깎았다).

        **비용보다 이득이 크다**: 호출당 평균 rtt(~0.06초)만큼 실효 간격이 늘지만(1.00 → ~1.06초),
        백오프가 사라지면 배율이 1.214 → 1.00으로 돌아온다. +0.06초 손해 vs -0.21초 이득이다.

        `max()`인 이유: `wait()`가 이미 더 먼 미래를 예약해 뒀으면(백오프 확대 직후 등) 그것을
        당기면 안 된다. 이 함수는 **늦추기만** 한다 — 그래야 두 경로가 서로의 보수성을 깎지 않는다.
        """
        if self._min_interval <= 0:
            return
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + self._current_interval)

    def record_rate_limit_hit(self) -> None:
        """레이트리밋 실패가 감지되면 호출 — 다음 wait()부터 넓어진 간격이 바로 적용된다.

        2026-07-23(운영점검보고서 §2-1 Fix#1): 배율 확대 시점마다 이전/이후 배율을 로깅한다 —
        지금까지는 EGW00201 발생 횟수·스케줄 밀림 같은 간접 증상으로만 백오프 상태를 추정할 수
        있었고, 그래서 07-22의 임계값 조정이 역효과였다는 것도 다음날 로그를 정밀분석해야만
        알 수 있었다. 이제는 이 로그 한 줄로 "지금 몇 배 백오프 중인지"를 바로 알 수 있다."""
        if self._min_interval <= 0:
            return
        with self._lock:
            self._consecutive_successes = 0
            before = self._current_interval
            max_interval = self._min_interval * self._MAX_INTERVAL_MULTIPLIER
            self._current_interval = min(
                max(self._current_interval, self._min_interval) * self._BACKOFF_MULTIPLIER, max_interval
            )
            after = self._current_interval
        if after != before:
            logger.info(LOG_BACKOFF_EXPAND, before, after, after / self._min_interval)

    def record_success(self) -> None:
        """호출 성공마다 호출 — 넓어진 간격이 있을 때만 연속 성공을 세어 서서히 되돌린다.

        2026-07-23: 실제로 한 단계 되돌린 시점만 로깅한다(성공마다 찍으면 정상 상태에서도
        매 호출 로그가 남아 record_rate_limit_hit 로그가 파묻힌다)."""
        if self._current_interval <= self._min_interval:
            return
        with self._lock:
            if self._current_interval <= self._min_interval:
                return
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._RECOVERY_SUCCESS_THRESHOLD:
                self._consecutive_successes = 0
                before = self._current_interval
                self._current_interval = max(self._current_interval * self._RECOVERY_FACTOR, self._min_interval)
                after = self._current_interval
            else:
                return
        logger.info(LOG_BACKOFF_RECOVER, before, after, after / self._min_interval)


def _percentile(ordered: list[float], q: float) -> float:
    """
    입력: **이미 정렬된** 표본, 분위수(0~1).
    계산: 최근접 순위법(nearest-rank). 표본이 적을 때 보간법은 관측되지 않은 값을 만들어내는데,
         응답시간처럼 "실제로 이만큼 걸린 호출이 있었다"가 중요한 지표에서는 그게 해가 된다.
    실패 조건: 빈 목록이면 0.0.
    """
    if not ordered:
        return 0.0
    index = min(int(math.ceil(q * len(ordered))) - 1, len(ordered) - 1)
    return ordered[max(index, 0)]


def extract_order_no(response: dict) -> str | None:
    """
    입력: `submit_order()` / `cancel_order()`의 응답.
    반환: KIS가 부여한 **주문번호**(`ODNO`). 못 찾으면 None.
    해석: 2026-08-16 (통합 리허설) — 제출 응답의 `output`은 **array**이고 필드는 **대문자**다
         (`tr_codes.ORDER_SUBMIT_ORDER_NO_FIELD`). 조회 응답은 소문자 `odno`라 둘을 섞으면
         조용히 못 찾는다. dict로 오는 경우까지 받는 이유는 KIS가 단건에서 array를 벗기는
         사례가 있고, 그것을 여기서 흡수하는 편이 호출측마다 분기하는 것보다 안전하기 때문이다.
    실패 조건: 없음 — 못 찾으면 None이고 호출측이 경고한다(주문번호 없이는 조회도 취소도 못 한다).
    """
    output = response.get("output")
    rows = output if isinstance(output, list) else [output] if isinstance(output, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get(tr_codes.ORDER_SUBMIT_ORDER_NO_FIELD, "") or "").strip()
        if value:
            return value
    return None


def format_order_price(price: float) -> str:
    """
    입력: 주문가격(float).
    반환: KIS가 받는 문자열. **정수는 소수점을 붙이지 않는다.**
    해석: 2026-08-16 (Block C) — `str(0.0)`은 `"0.0"`이다. 그런데 "선물옵션 정정취소주문" 문서는
         취소 시 `UNIT_PRICE`에 **"0"** 을 넣으라고 적었고, `"0.0"`을 받아줄지는 알 수 없다.
         이 한 글자짜리 차이가 8/18 실측을 통째로 실패시킬 수 있는 자리다 —
         잔고 조회가 필수 파라미터 누락으로 넉 달간 항상 실패했던 것과 같은 급의 함정이다.

         정수는 `"350"`, 소수는 `"3.55"`로 낸다. 지수 표기(`1e-05`)가 새는 것을 막으려고
         `repr`이 아니라 포맷 문자열을 쓰고, 옵션 최소 호가(0.01)보다 작은 자리는 버린다.
    실패 조건: 없음.
    """
    if price == int(price):
        return str(int(price))
    return f"{price:.4f}".rstrip("0")


def _ccnl_int(row: dict, key: str) -> int:
    """조회 응답의 수치는 전부 문자열이고 공란이 올 수 있다 — 공란/None은 0으로 읽는다.
    앞자리 0으로 패딩된 값(`"0000000002"`)도 int()가 그대로 처리한다."""
    raw = str(row.get(key, "") or "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("주문체결내역 수치 필드 파싱 실패: %s=%r — 0으로 읽는다", key, raw)
        return 0


def _ccnl_float(row: dict, key: str) -> float | None:
    raw = str(row.get(key, "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("주문체결내역 수치 필드 파싱 실패: %s=%r — None으로 둔다", key, raw)
        return None


def parse_fill_status(row: dict) -> dict:
    """
    입력: "선물옵션 주문체결내역조회" `output1` 한 행(필드는 **소문자**).
    계산: `OrderState`로 매핑하고 평균 체결가·체결수량을 붙인다. 판정 순서가 규칙이다:

        rjct_qty > 0                        -> REJECTED   (거부가 최우선 — 부분거부도 거부다)
        tot_ccld_qty >= ord_qty (>0)        -> FILLED
        tot_ccld_qty > 0                    -> PARTIAL    (잔량이 남아 있다)
        tot_ccld_qty == 0 and qty == 0      -> CANCELLED  (체결도 잔량도 없다 = 사라졌다)
        그 외                                -> PENDING

    해석: **`qty`는 「잔량」이다**(주문수량이 아니다 — 문서: "주문 체결되지 않고 남은 수량").
         이름이 짧아서 주문수량으로 착각하기 쉽고, 그러면 CANCELLED와 PENDING이 뒤집힌다.
         주문수량은 `ord_qty`, 총체결수량은 `tot_ccld_qty`, 평균 체결가는 `avg_idx`(=평균지수)다.

         `CANCELLED` 판정에 주의: KIS는 취소된 주문을 별도 상태 필드로 주지 않고 **잔량 0 +
         체결 0**으로 나타낸다. 접수 직후 아직 아무 일도 안 일어난 주문은 잔량이 주문수량과
         같으므로 PENDING으로 갈린다.
    실패 조건: 없음 — 못 읽은 수치는 0/None으로 흡수하고 로그를 남긴다(계명 12: 조용히 넘기지 않는다).
    """
    ord_qty = _ccnl_int(row, "ord_qty")
    filled_qty = _ccnl_int(row, "tot_ccld_qty")
    remaining = _ccnl_int(row, "qty")
    rejected = _ccnl_int(row, "rjct_qty")
    avg_px = _ccnl_float(row, "avg_idx")

    if rejected > 0:
        state = OrderState.REJECTED
    elif filled_qty > 0 and ord_qty > 0 and filled_qty >= ord_qty:
        state = OrderState.FILLED
    elif filled_qty > 0:
        state = OrderState.PARTIAL
    elif remaining == 0:
        state = OrderState.CANCELLED
    else:
        state = OrderState.PENDING

    return {
        "state": state.value,
        "filled_px": avg_px if filled_qty > 0 else None,
        "filled_qty": filled_qty,
    }


def _is_kis_rate_limit_error(exc: httpx.HTTPStatusError) -> bool:
    """
    계산: KIS가 초당 거래건수 초과 시 돌려주는 특정 에러코드(EGW00201)인지 확인한다(2026-07-20
         US10Y 조회 500 응답 바디에서 {"msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다"}
         실측). 이 코드일 때만 백오프를 키운다 — 그 외 500(계좌 미승인, 존재하지 않는 종목 등)은
         페이싱과 무관한 원인이라 무분별하게 전체 호출을 느리게 만들면 안 된다.
    실패 조건: 응답 바디가 JSON이 아니거나 msg_cd가 없으면 False(레이트리밋 아님으로 취급).
    """
    try:
        return exc.response.json().get("msg_cd") == "EGW00201"
    except Exception:
        return False


class KISRestClient:
    def __init__(
        self,
        settings: KISSettings,
        token_daemon: TokenDaemon,
        client: httpx.Client | None = None,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._token_daemon = token_daemon
        self._client = client or httpx.Client(timeout=_HTTP_TIMEOUT, limits=_HTTP_LIMITS)
        self._rate_limiter = _RateLimiter(min_request_interval)
        # 2026-08-04(고도화#5) — 엔드포인트별 HTTP 소요시간 표본. `_log_if_slow`가 이미 매 호출마다
        # 재고 있는 값이라 추가 계측 비용이 없다(임계 이상만 로깅할 뿐 측정은 전부 하고 있었다).
        self._http_samples: dict[str, list[float]] = {}
        self._http_samples_lock = threading.Lock()

    @property
    def rate_limit_backoff_multiplier(self) -> float:
        """현재 공유 레이트리미터의 배율(1.0=백오프 없음) — COCKPIT 헬스체크가 읽는 값."""
        return self._rate_limiter.current_multiplier

    @property
    def rate_limit_total_calls(self) -> int:
        """공유 레이트리미터를 통과한 전체 누적 호출 건수(2026-07-28, 스케줄 밀림 재수사용)."""
        return self._rate_limiter.total_calls

    @property
    def _domain(self) -> str:
        return tr_codes.VPS_REST_DOMAIN if self._settings.is_mock else tr_codes.REAL_REST_DOMAIN

    @property
    def _env_key(self) -> str:
        return "vps" if self._settings.is_mock else "real"

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token_daemon.get_token()}",
            "appkey": self._settings.kis_app_key,
            "appsecret": self._settings.kis_app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def drain_http_latency(self) -> dict[str, dict]:
        """
        계산: 마지막 호출 이후 쌓인 엔드포인트별 HTTP 소요시간을 p50/p95/p99로 요약하고 **비운다**.
        해석: 2026-08-04 고도화#5 — §2-6이 밀림의 90%를 KIS 응답 지연으로 귀속시켰는데, 지금
             리포트는 그 지연을 **우리 지표(밀림 건수)로만** 본다. `slow_calls`는 임계(5초)
             위쪽 꼬리만 보므로 p50을 알 수 없고, "오늘 KIS가 평소보다 느렸는가"에 답하지 못한다.
             엔드포인트별 분포를 시간대와 함께 쌓으면 KIS 쪽 혼잡 패턴이 보이고, 그때 비로소
             "그 시간대에만 폴링 폭을 줄이는" 선택지가 근거를 갖는다(총량 축소보다 손실이 작다).

             **비우는(drain) 이유**: 하루치를 메모리에 들고 있으면 12,852개 표본이 쌓인다.
             주기적으로 요약해 로그 한 줄로 내보내고 버리면 메모리가 상수로 유지되고, 시간대별
             추이도 자연히 남는다(로그 줄에 시각이 있다).
        실패 조건: 없음 — 표본이 없으면 빈 dict.
        """
        with self._http_samples_lock:
            samples, self._http_samples = self._http_samples, {}
        out: dict[str, dict] = {}
        for endpoint, values in samples.items():
            ordered = sorted(values)
            out[endpoint] = {
                "n": len(ordered),
                "p50": _percentile(ordered, 0.50),
                "p95": _percentile(ordered, 0.95),
                "p99": _percentile(ordered, 0.99),
                "max": ordered[-1],
            }
        return out

    def _record_http_latency(self, url: str, http_seconds: float) -> None:
        endpoint = endpoint_label(url)
        with self._http_samples_lock:
            self._http_samples.setdefault(endpoint, []).append(http_seconds)

    def _log_if_slow(
        self, method: str, url: str, pacer_seconds: float, http_seconds: float,
        *, timed_out: bool = False,
    ) -> None:
        """계산: 페이서 대기 + HTTP 응답이 임계를 넘으면 두 구간을 **나눠서** 남긴다
        (2026-07-31 §4 우선순위 3 — 상세 근거는 SLOW_CALL_LOG_THRESHOLD_SECONDS 주석).
        2026-08-03(§2-8): 진단이 끝나 WARNING → INFO.
        2026-08-04(§2-1): 그 레벨 변경이 `log_metrics._SLOW_CALL_RE`를 침묵시켜 08-04 리포트가
        362건을 **0건**으로 보고했다. 포맷은 이제 `LOG_SLOW_CALL` 상수이고 계약 테스트가 지킨다.
        2026-08-04(고도화#5): 임계와 무관하게 **모든 호출의 HTTP 소요를 표본에 넣는다** —
        꼬리(느린 호출)만 보면 "오늘 KIS가 평소보다 느렸는가"에 답할 수 없다.
        2026-08-05(§2 이상점 4 / Fix#4): `timed_out`이면 **임계를 무시하고 반드시 남긴다.**
        타임아웃은 정의상 가장 극단적인 느린 호출인데, 그 총 소요가 타임아웃 값에서 잘리는
        탓에 임계 아래로 떨어질 수 있다 — 임계를 3.0초로 내린 지금도 read가 10초인
        엔드포인트에서는 같은 역전이 다시 생길 수 있으므로(예: 잔고 read 10초 vs 임계 3초는
        안전하지만, 앞으로 임계를 다시 올리면 아니다) 임계에 의존하지 않고 구조적으로 보장한다."""
        self._record_http_latency(url, http_seconds)
        total = pacer_seconds + http_seconds
        if total < SLOW_CALL_LOG_THRESHOLD_SECONDS and not timed_out:
            return
        logger.info(
            LOG_SLOW_CALL,
            total, pacer_seconds, http_seconds, self._rate_limiter.current_multiplier,
            method, endpoint_label(url),
        )

    def _send_get(self, url: str, **kwargs) -> httpx.Response:
        """계산: 페이싱 → GET 1회. 소요시간을 페이서/HTTP로 나눠 재고 느리면 남긴다.
        2026-08-05(Fix#2): read 타임아웃을 엔드포인트별로 건다(`timeout_for_url`). 호출측이
        `timeout`을 직접 넘겼으면 그것을 존중한다 — 테스트가 타임아웃을 강제할 이음새."""
        pacer_started = time.monotonic()
        self._rate_limiter.wait()
        kwargs.setdefault("timeout", timeout_for_url(url))
        http_started = time.monotonic()
        timed_out = False
        try:
            return self._client.get(url, **kwargs)
        except httpx.TimeoutException:
            timed_out = True
            raise
        finally:
            # 2026-08-06 Fix#1: 다음 슬롯을 완료 시각 기준으로 다시 민다. 타임아웃으로 끝난
            # 호출도 KIS 입장에서는 처리한 호출이므로 **예외 경로에서도** 밀어야 한다.
            self._rate_limiter.record_completion()
            # 예외(타임아웃 등)로 끝난 호출이야말로 계측이 필요하다 — finally에서 재고 넘긴다.
            self._log_if_slow(
                "GET", url, http_started - pacer_started, time.monotonic() - http_started,
                timed_out=timed_out,
            )

    def _get(self, url: str, **kwargs) -> dict:
        """모든 REST GET 호출의 단일 진입점 — 실제 전송 직전에 _rate_limiter로 페이싱하고,
        결과에 따라 적응형 백오프 상태를 갱신한다(2026-07-20, _RateLimiter 참고).
        2026-07-31: 페이서 대기와 HTTP 응답 시간을 따로 재서 느릴 때만 남긴다(§4 우선순위 3).

        2026-08-03(§4 우선순위 3): `RemoteProtocolError`는 **1회만** 재시도한다. 이 예외는 KIS가
        먼저 닫은 keep-alive 커넥션을 httpx가 재사용하려 할 때 나며(08-03/07-31 각 8건), 요청이
        서버에 도달하지 않았으므로 재시도가 안전하다(중복 주문 같은 부작용이 없는 GET이기도 하다).
        재시도도 **반드시 `_send_get`을 거쳐 페이서를 통과**한다 — 여기서 페이서를 건너뛰면
        EGW00201(초당 거래건수 초과)을 우리 손으로 유발하게 된다. 두 번째도 실패하면 그대로
        전파해 호출측의 기존 부분 실패 처리(레그 건너뛰기 등)에 맡긴다.
        """
        try:
            response = self._send_get(url, **kwargs)
        except httpx.RemoteProtocolError:
            logger.info(LOG_REMOTE_PROTOCOL_RETRY, url.split("?", 1)[0])
            response = self._send_get(url, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if _is_kis_rate_limit_error(exc):
                self._rate_limiter.record_rate_limit_hit()
            raise
        self._rate_limiter.record_success()
        return response.json()

    def _post(self, url: str, **kwargs) -> dict:
        """모든 REST POST 호출의 단일 진입점 — GET과 동일한 공유 레이트리미터를 통과시킨다.
        2026-08-05(Fix#2): 주문 경로는 read 10초다 — 타임아웃되면 주문 접수 여부가 불명확해진다
        (`_ENDPOINT_READ_TIMEOUT_SECONDS` 주석)."""
        pacer_started = time.monotonic()
        self._rate_limiter.wait()
        kwargs.setdefault("timeout", timeout_for_url(url))
        http_started = time.monotonic()
        timed_out = False
        try:
            response = self._client.post(url, **kwargs)
        except httpx.TimeoutException:
            timed_out = True
            raise
        finally:
            self._rate_limiter.record_completion()  # 2026-08-06 Fix#1 — GET과 같은 규칙
            self._log_if_slow(
                "POST", url, http_started - pacer_started, time.monotonic() - http_started,
                timed_out=timed_out,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if _is_kis_rate_limit_error(exc):
                self._rate_limiter.record_rate_limit_hit()
            raise
        self._rate_limiter.record_success()
        return response.json()

    def get_quote(self, symbol: str, market_div_code: str = tr_codes.FID_MRKT_DIV_INDEX_OPTION) -> dict:
        """
        단일 종목(선물 1건 또는 옵션 1건) 시세 조회 — "선물옵션 시세"(inquire-price).

        입력: 종목코드(단축코드), FID_COND_MRKT_DIV_CODE(F=지수선물, O=지수옵션 등).
        계산: PATH_FUTUREOPTION_QUOTE GET 호출.
        해석: 이 엔드포인트는 종목 1건 시세만 반환한다 — 모의투자에는 체인 전체를 한 번에
             반환하는 REST가 없으므로(전광판류는 실전 전용), 여러 행사가를 조회하려면 종목코드
             마스터파일 기준으로 이 호출을 반복해야 한다(아직 미구현 — KIS 종목코드 마스터파일
             연동 필요, github.com/koreainvestment/open-trading-api/tree/main/stocks_info).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파 — 호출측이 재시도/알림 처리.
        """
        tr_id = tr_codes.TR_OPTION_QUOTE[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_QUOTE}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": market_div_code, "FID_INPUT_ISCD": symbol},
        )

    def get_asking_price(self, symbol: str, market_div_code: str = tr_codes.FID_MRKT_DIV_INDEX_OPTION) -> dict:
        """단일 종목 시세호가(5단계 매도/매수 호가) — "선물옵션 시세호가"(inquire-asking-price)."""
        tr_id = tr_codes.TR_OPTION_ASKING_PRICE[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ASKING_PRICE}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": market_div_code, "FID_INPUT_ISCD": symbol},
        )

    def get_investor_flow(self, market_code: str, sector_code: str) -> dict:
        """
        시장별 투자자매매동향(시세) — 외국인/개인/기관계 등 순매수 수량·거래대금.

        입력: FID_INPUT_ISCD(시장구분, 파생상품은 "K2I"), FID_INPUT_ISCD_2(업종구분 — K2I일 때
             F001=선물/OC01=콜옵션/OP01=풋옵션).
        계산: "모의 TR_ID/Domain: 모의투자 미지원"으로 문서화되어 있지만, 계좌 무관 공개
             시세성 데이터라 실측 결과 모의투자 앱키로도 REAL_REST_DOMAIN 호출이 200 OK로
             성공한다(2026-07-06 확인) — 시세 WS와 같은 이유로 실전 도메인을 고정 사용한다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파.
        """
        headers = self._headers(tr_codes.TR_INVESTOR_FLOW_BY_MARKET)
        return self._get(
            f"{tr_codes.REAL_REST_DOMAIN}{tr_codes.PATH_INVESTOR_FLOW_BY_MARKET}",
            headers=headers,
            params={"FID_INPUT_ISCD": market_code, "FID_INPUT_ISCD_2": sector_code},
        )

    def get_overseas_future_price(self, srs_cd: str) -> dict:
        """
        해외선물 현재가(inquire-price) — VIX 선물(VX)·USDCNH 선물(CNH) 등 Cross-asset stress
        프록시에 쓴다(v6 §7.3).

        입력: 해외선물 단축코드(예: "VXN26" — 종목코드 마스터파일에서 최근월물/차근월물로 찾음,
             mahdi.data.overseas_future_master 참고).
        계산: PATH_OVERSEAS_FUTUREOPTION_PRICE GET 호출. 이 엔드포인트는 계좌 파라미터가 없어
             계좌 무관 공개 시세로 보이지만, 상품(거래소)에 따라 계좌에 별도 거래소 신청이 걸려
             있어야 한다(2026-07-10 실측: CBOE(VX)·HKEx(CNH)는 모의계좌로 바로 성공, CME/CBOT
             (ZN 등)는 "EGW00552: CBOT SUB거래소 신청 계좌가 아닙니다"로 거부됨 — 코드가 아니라
             계좌 설정 문제이므로 호출측이 이 에러를 구분해 재시도하지 말아야 한다).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파.
        """
        tr_id = tr_codes.TR_OVERSEAS_FUTUREOPTION_PRICE
        return self._get(
            f"{self._domain}{tr_codes.PATH_OVERSEAS_FUTUREOPTION_PRICE}",
            headers=self._headers(tr_id),
            params={"SRS_CD": srs_cd},
        )

    def get_overseas_daily_chartprice(
        self, market_div_code: str, symbol: str, date_from: str, date_to: str, period_div_code: str = "D"
    ) -> dict:
        """
        해외주식 종목_지수_환율기간별시세(일_주_월_년) — US10Y(국채구분 I, 심볼 Y0202) 등
        해외선물옵션 계좌 신청 없이도 얻을 수 있는 일봉 대체 경로(v6 §7.3).

        입력: FID_COND_MRKT_DIV_CODE(N=해외지수, X=환율, I=국채, S=금선물), 종목코드(예: "Y0202"),
             조회 시작/종료일(YYYYMMDD), 기간 구분(D=일 기본값).
        계산: PATH_OVERSEAS_INDEX_DAILY_CHARTPRICE GET 호출.
        해석: 2026-07-10 실측 — 같은 API 계열의 분봉 엔드포인트(inquire-time-indexchartprice)는
             I(국채) 구분을 "ERROR INVALID FID_COND_MRKT_DIV_CODE"로 거부해 분봉 미지원이 확정됐다
             — 이 함수(일봉)만이 US10Y를 계좌 제약 없이 얻는 유일한 경로다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파.
        """
        tr_id = tr_codes.TR_OVERSEAS_INDEX_DAILY_CHARTPRICE
        return self._get(
            f"{self._domain}{tr_codes.PATH_OVERSEAS_INDEX_DAILY_CHARTPRICE}",
            headers=self._headers(tr_id),
            params={
                "FID_COND_MRKT_DIV_CODE": market_div_code,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": date_from,
                "FID_INPUT_DATE_2": date_to,
                "FID_PERIOD_DIV_CODE": period_div_code,
            },
        )

    def get_balance(self, margin_division: str = "02", settlement_status: str = "1") -> dict:
        """
        입력: margin_division("01"=개시/"02"=유지, 기본값 유지 — 계좌 손익 추적기가 매 사이클
             조회하는 용도로는 "유지" 증거금 기준이 자연스럽다), settlement_status("1"=정산가격/
             "2"=매입가격 기준 잔고 조회).
        계산: PATH_FUTUREOPTION_BALANCE GET 호출(계좌번호는 설정에서 사용). 2026-07-28 6차
             실측(docs/efriend xlsx "선물옵션 잔고현황" 시트)으로 MGNA_DVSN/EXCC_STAT_CD/
             CTX_AREA_FK200/CTX_AREA_NK200이 전부 Required임을 확인 — 이 넷이 빠져 있으면 KIS가
             "ERROR : INPUT_FIELD_NAME MGNA_DVSN"(msg_cd=OPSQ2001)로 거부한다(과거 버전은 이
             네 필드 없이 호출해 항상 실패했었음). CTX_AREA_*200은 연속조회(페이지네이션)용이라
             최초 조회 시 빈 문자열.
        해석: 응답 output2의 `prsm_dpast`(추정예탁자산)를 일자별로 스냅샷하면 daily_pnl_pct/
             drawdown_pct 계산의 기준값이 되고, `evlu_pfls_amt_smtl`/`trad_pfls_amt_smtl`이
             평가/실현 손익 합계, output1(배열)의 `sll_buy_dvsn_name`으로 종목별 매수/매도
             방향을 셀 수 있다(Risk Engine `AccountState.same_direction_positions` 계산 재료).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파. 모의투자(VTFO6118R)는 실전과 동일한
             필드셋으로 지원되지만, 자매 API인 "선물옵션 잔고평가손익내역"(CTFO6159R)은 모의투자
             미지원이라 이 함수로 완전히 대체해야 한다.
        """
        tr_id = tr_codes.TR_BALANCE_INQUIRY[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_BALANCE}",
            headers=self._headers(tr_id),
            params={
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "MGNA_DVSN": margin_division,
                "EXCC_STAT_CD": settlement_status,
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )

    def submit_order(self, symbol: str, side: str, qty: int, price: float, order_dvsn_cd: str = "01") -> dict:
        """
        입력: 종목코드(단축상품번호 — 선물 6자리/옵션 9자리, 예: B01603955), BUY/SELL, 수량, 가격,
             주문구분코드(ORD_DVSN_CD: 01=지정가, 02=시장가, 03=조건부, 04=최유리 등).
        계산: PATH_FUTUREOPTION_ORDER POST 호출. ORD_PRCS_DVSN_CD="02"(주문전송)과 ORD_DVSN_CD는
             "선물옵션 주문" 문서 기준 필수(Required=Y) 필드 — 누락 시 KIS가 주문을 거부한다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파 — 상위 Order State Machine이 REJECTED로 기록.
        """
        tr_id = tr_codes.TR_ORDER_NEW[self._env_key]
        return self._post(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ORDER}",
            headers=self._headers(tr_id),
            json={
                "ORD_PRCS_DVSN_CD": "02",  # 02: 주문전송 (고정값)
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "SLL_BUY_DVSN_CD": "01" if side.upper() == "SELL" else "02",
                "SHTN_PDNO": symbol,
                "ORD_QTY": str(qty),
                # 2026-08-16 — 취소 경로와 **같은 포맷터**를 쓴다. 제출은 `str(price)`였는데
                # 두 경로가 다른 형식을 보내면 8/18에 한쪽만 거부당하고 원인 규명이 길어진다.
                "UNIT_PRICE": format_order_price(price),
                "ORD_DVSN_CD": order_dvsn_cd,
            },
        )

    # ===== 2026-08-16 (Block C) — 취소·정정·조회 =====
    #
    # 이 셋이 없어서 `execution/` 전체가 배선될 수 없었다:
    #   - 취소가 없으면 CONFIRM 모드의 「60초 미확인 → 자동 취소」가 성립하지 않는다.
    #   - 조회가 없으면 `order_manager.confirm_fill()`이 요구하는 `get_order_fill_status()`를
    #     만들 수 없고, v6 §13.2의 「체결통보-REST 이중 확인」이 절반만 남는다.

    def cancel_order(self, orgn_odno: str, qty: int = 0) -> dict:
        """
        입력: 원주문번호(제출 응답의 `ODNO`), 취소 수량 — **0이면 전량**이다(문서: "전량일경우
             0으로 입력").
        계산: PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL POST. "선물옵션 정정취소주문" 시트 기준
             Required 전부를 채운다. 취소에 고정되는 값이 셋 있다:
               `UNIT_PRICE`="0"           — 문서: "취소 시에도 0 입력"
               `KRX_NMPR_CNDT_CD`="0"     — 문서: "취소시 0으로 입력"
               `RMN_QTY_YN`="Y"/"N"       — 전량(qty=0)이면 Y, 일부면 N
             `NMPR_TYPE_CD`/`ORD_DVSN_CD`는 취소에서도 Required라 지정가("01")를 넣는다 —
             가격이 0이므로 유형은 의미를 갖지 않지만 **필드를 비우면 KIS가 거부한다**
             (잔고 조회에서 MGNA_DVSN 누락으로 항상 실패했던 것과 같은 종류의 함정).
        해석: 반환값의 `output.ODNO`는 **취소 주문 자체의 새 주문번호**이고 원주문번호가 아니다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파. 이미 체결된 주문의 취소는 KIS가
                  `rt_cd != "0"`으로 돌려주므로 **호출측이 rt_cd를 반드시 확인해야 한다**
                  (HTTP 200 + 업무 실패가 가능하다).
        """
        return self._modify_or_cancel(
            tr_codes.RVSE_CNCL_CANCEL, orgn_odno=orgn_odno, qty=qty, price=0.0,
        )

    def modify_order(self, orgn_odno: str, qty: int, price: float, order_dvsn_cd: str = "01") -> dict:
        """
        입력: 원주문번호, 정정 수량(전량이면 0), 새 주문가격, 주문구분코드.
        계산: `cancel_order()`와 같은 엔드포인트에 `RVSE_CNCL_DVSN_CD="01"`(정정)로 보낸다.
        해석: 8/18 실측 대상은 **취소**다. 정정은 같은 엔드포인트라 함께 열어 두지만
             실측되기 전까지 운영 경로에서 쓰지 않는다(Capability Matrix 규율).
        실패 조건: `cancel_order()`와 같다.
        """
        return self._modify_or_cancel(
            tr_codes.RVSE_CNCL_MODIFY, orgn_odno=orgn_odno, qty=qty, price=price,
            order_dvsn_cd=order_dvsn_cd,
        )

    def _modify_or_cancel(
        self, rvse_cncl_dvsn_cd: str, *, orgn_odno: str, qty: int, price: float,
        order_dvsn_cd: str = "01",
    ) -> dict:
        """정정·취소의 공통 본문 — 둘은 `RVSE_CNCL_DVSN_CD` 한 글자만 다르다(같은 TR·같은 경로).

        분리하지 않는 이유: 두 함수가 각자 본문을 만들면 Required 필드 목록이 두 곳에서
        갈릴 수 있다. 그 갈림은 「한쪽만 KIS에 거부당한다」로 나타나 원인 규명이 오래 걸린다.
        """
        tr_id = tr_codes.TR_ORDER_MODIFY_CANCEL[self._env_key]
        return self._post(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL}",
            headers=self._headers(tr_id),
            json={
                "ORD_PRCS_DVSN_CD": tr_codes.ORD_PRCS_DVSN_SEND,
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "RVSE_CNCL_DVSN_CD": rvse_cncl_dvsn_cd,
                "ORGN_ODNO": orgn_odno,
                "ORD_QTY": str(qty),
                # **`"0.0"`이 아니라 `"0"`이어야 한다** — `format_order_price()` 주석 참고.
                "UNIT_PRICE": format_order_price(price),
                "NMPR_TYPE_CD": order_dvsn_cd,
                "KRX_NMPR_CNDT_CD": "0",  # 취소=0 고정, 정정=0(없음)
                "RMN_QTY_YN": "Y" if qty == 0 else "N",
                "FUOP_ITEM_DVSN_CD": "",  # 주간은 공란(Default)
                "ORD_DVSN_CD": order_dvsn_cd,
            },
        )

    def inquire_ccnl(
        self,
        start_date: str,
        end_date: str,
        *,
        symbol: str = "",
        sll_buy_dvsn_cd: str = "00",
        ccld_nccs_dvsn: str = "00",
        start_order_no: str = "",
        sort_order: str = "DS",
    ) -> dict:
        """
        입력: 조회 시작/종료 주문일자(YYYYMMDD), (선택) 종목코드·매도매수구분("00"=전체)·
             체결미체결구분("00"=전체/"01"=체결/"02"=미체결)·시작주문번호·정렬순서("DS"=역순).
        계산: PATH_FUTUREOPTION_CCNL_INQUIRY GET. "선물옵션 주문체결내역조회" 시트 기준
             Required 11개를 전부 채운다 — `PDNO`/`MKET_ID_CD`/`CTX_AREA_*200`은 Required이면서
             **공란이 정상값**이다(문서: "공란 시 전체 조회" / "공란(Default)" / "공란: 최초 조회시").
             Required인데 공란이 정상이라는 조합은 KIS 문서에 흔하고, 이 필드를 **아예 빼면**
             거부당한다.
        해석: `output1`(array)이 주문별 상세, `output2`가 합계다. 필드는 **소문자**다
             (제출 응답은 대문자 — `tr_codes.ORDER_*_ORDER_NO_FIELD` 주석 참고).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파. 연속조회(`tr_cont`=F/M)는 이 함수가
                  처리하지 않는다 — 하루치 주문이 200건을 넘는 규모가 아니고(고정 1계약 개시),
                  넘어가면 `ctx_area_*`를 넘겨 다시 부르는 쪽을 그때 만든다.
        """
        tr_id = tr_codes.TR_ORDER_CCNL_INQUIRY[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_CCNL_INQUIRY}",
            headers=self._headers(tr_id),
            params={
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "STRT_ORD_DT": start_date,
                "END_ORD_DT": end_date,
                "SLL_BUY_DVSN_CD": sll_buy_dvsn_cd,
                "CCLD_NCCS_DVSN": ccld_nccs_dvsn,
                "SORT_SQN": sort_order,
                "STRT_ODNO": start_order_no,
                "PDNO": symbol,
                "MKET_ID_CD": "",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )

    def get_order_fill_status(self, order_id: str, *, as_of: date | None = None) -> dict:
        """
        입력: 주문번호(제출 응답 `output[0].ODNO`), (선택) 조회 기준일 — 없으면 오늘.
        계산: `inquire_ccnl()`로 그날 주문을 받아 `order_id`와 같은 `odno` 행을 찾고
             `parse_fill_status()`로 `{"state","filled_px","filled_qty"}`를 만든다.
        해석: `order_manager.BrokerClient` 프로토콜을 만족시키는 어댑터다 —
             `order_manager.confirm_fill()`이 이 형태를 요구한다.
        실패 조건: 그 주문번호가 조회 결과에 없으면 **PENDING을 돌려준다**(예외가 아니다).
                  KIS 접수 직후 조회에는 아직 안 보일 수 있고, `confirm_fill()`은 PENDING을
                  「전이 없음」으로 처리하므로 이것이 안전한 쪽이다. **다만 조용히 넘기지 않고
                  로그를 남긴다** — 영구히 안 보이면 그것은 접수 실패이고 사람이 알아야 한다.
        """
        target = as_of or date.today()
        stamp = target.strftime("%Y%m%d")
        response = self.inquire_ccnl(stamp, stamp)
        rows = response.get("output1") or []
        for row in rows:
            if str(row.get(tr_codes.ORDER_INQUIRY_ORDER_NO_FIELD, "")).strip() == str(order_id).strip():
                return parse_fill_status(row)
        logger.warning(
            "주문번호 %s가 %s 주문체결내역조회에 없다 — PENDING으로 둔다(조회 %d건). "
            "다음 사이클에도 안 보이면 접수 실패로 보고 사람이 확인해야 한다",
            order_id, stamp, len(rows),
        )
        return {"state": OrderState.PENDING.value, "filled_px": None, "filled_qty": 0}
