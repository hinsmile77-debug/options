#!/usr/bin/env python3
"""마흐디 일일 운영점검 — 증거 수집기 (stdlib only).

`scripts/daily_ops_report.py`는 **장마감 후** 하루가 완결된 뒤의 지표를 낸다.
이 스크립트는 그 앞을 메운다 — **장전·장중에는 자동 집계가 없고**, 장후에도
"지표에 안 실리는 것"(기동 시퀀스, 워치독 자신의 생사, 크래시, 커밋, 가설 도래분)은
사람이 매번 손으로 훑고 있었다.

역할 분담(겹치지 않게 유지할 것):

    scripts/daily_ops_report.py   하루치 **지표** — 사이클/REST/DB/판단. 장후 전용.
    이 스크립트                    하루의 **뼈대와 사건** — 기동·종료·워치독·에러·공백·
                                  레버·가설 도래·산출물 존재. 3국면 전부.

원본 로그는 `observation_loop.log`가 10MB×10 로테이션이다. 통째로 읽으면 컨텍스트가
증거로 가득 차 판단할 여력이 남지 않는다. **이 스크립트의 존재 이유는 "무엇을 볼지"가
아니라 "무엇을 안 볼지"를 정하는 것이다.**

사용:
    python docs/동작점검/tools/collect_evidence.py --phase intra
    python docs/동작점검/tools/collect_evidence.py --phase post --date 2026-08-12
    python docs/동작점검/tools/collect_evidence.py --phase post --out docs/동작점검/auto/2026-08-12_증거.md

Python 3.8+ / 외부 의존성 없음 / Windows·Linux 공통.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------- 하루의 뼈대
# 이 시각들에 무엇이 있어야 하는지가 점검의 척추다. 스케줄러(작업 스케줄러 등록분)와
# `scripts/*.bat`이 진실원천이고, 여기 값이 그것과 어긋나면 **이 파일이 틀린 것이다.**
ANCHORS = [
    ("07:30", "장전 기동(start_mahdi_premarket.bat) — docker·COCKPIT·관측 루프", "pre"),
    ("07:35", "마스터 파일·토큰·WS 구독 완료", "pre"),
    ("08:30", "만기유동성 버스트(북별 홀수분 슬롯)", "pre"),
    ("09:00", "정규장 개장 — 사이클이 분마다 돌기 시작", "intra"),
    ("12:00", "장중 중간점", "intra"),
    ("15:20", "정규장 마감(옵션)", "post"),
    ("15:45", "stop_mahdi_marketclose.bat — taskkill 후 daily_ops_report", "post"),
]
ANCHOR_WINDOW_MIN = 5

# ===== 점검 자신의 시간표 (2026-08-13 고도화 3 + 2026-08-14 고도화 6) =====
#
# 위 ANCHORS가 「시스템이 무엇을 할 시각」이라면 이쪽은 **「사람이 점검할 시각」**이다.
#
# ## 왜 장중이 두 회차인가
#
# 08-14의 사건은 **14:00~15:23**에 일어났다. 그날 12:34 장중 점검은 「P0 없음」으로 **옳게**
# 닫혔고 — 그 시각에는 정말 아무 일도 없었다 — 15:45 장후 집계가 나왔을 때는 이미 84분을
# 잃은 뒤였다. **12:30 1회 주기는 오후를 구조적으로 못 본다.**
# 14:30인 이유: v6 §4.2의 14:50 신규 진입 컷오프 직전이라 「오늘 남은 시간에 개입할 것인가」에
# 답할 수 있는 **마지막 시각**이다. 그보다 늦으면 알아도 할 수 있는 일이 없다.
#
# ## 왜 장전이 08:30이 아니라 08:00인가
#
# 08-14 장전 점검은 08:35에 돌았고 P0가 둘이었는데 개장까지 25분이었다. 그중 하나(레버 F)는
# **재기동을 수반**하므로 25분은 빠듯하다. 기동(07:31)과 첫 사이클 안정화(07:36 REST 응답시간
# 첫 인쇄)를 지나면 08:00에 이미 판단 재료가 다 있다 — 그날 07:36 이후 08:33까지 새로 나타난
# 사건이 **0건**이었다(사이클 로그만 반복). 30분을 앞당겨도 잃는 것이 없다.
#
# ⚠ **진실원천은 Claude 앱의 예약 작업이다 — Windows 작업 스케줄러가 아니다.**
#
# 두 스케줄러가 각각 다른 것을 돌린다. 섞으면 시각이 안 맞는다:
#
#   Windows 작업 스케줄러 (시스템 작업 3개)
#     Mahdi-PreMarket-Startup     start_mahdi_premarket.bat    07:30
#     Mahdi-MarketClose-Shutdown  stop_mahdi_marketclose.bat   15:45  ← 장후 증거·지표를 만든다
#     Mahdi-Watchdog              watchdog_mahdi_hidden.vbs    1분 주기
#
#   Claude 앱 예약 작업 (점검 세션 — `mahdi-daily-check` 스킬, 로컬 실행)
#     장전 점검 / 장중 점검 / 장후 점검(「Mahdi postmarket check」, 평일 16:00)
#
# 08-14 실측이 그 분업을 그대로 보여 준다(**증거는 국면마다 하나, 보고서는 하루에 하나**):
#     08:31 `_증거_pre.md`      → 08:37 `_마흐디_일일점검.md` **생성**   (장전 세션)
#     12:34 `_증거_intra.md`    → 12:40 같은 파일에 **append**          (장중 세션 ①)
#     14:39 `_증거_intra_1430.md` → 같은 파일에 **append**              (장중 세션 ②)
#     15:45 `_증거_post.md`·`_지표.md`                                  (**종료 배치**가 만든다)
#     16:19 같은 파일에 **append + 종합 완성**                          (장후 세션)
#
# ⚠ 2026-08-21 이전(`ONE_FILE_SINCE`)은 국면마다 별도 보고서였다 — `_점검_pre.md` /
#   `_점검_intra.md` / `_점검_intra_1430.md` / `_마흐디_운영점검보고서.md` 넷. 그 날짜를
#   재집계할 때는 §9가 그 넷을 기대한다. **옛 날의 산출물 규약은 옛 규약으로 판정한다.**
#
# 그래서 아래 `post`는 **15:45**다 — 이 스크립트가 `--phase post`로 도는 시각은 종료 배치
# 안이지 16:00 세션이 아니다. `pre`/`intra`는 세션이 이 스크립트를 직접 돌리므로 세션 시각과 같다.
#
# ⚠ 이 표를 바꿔도 예약 작업은 안 바뀐다. **Claude 앱에서 따로 고쳐야 한다** —
# 어긋나면 아래 회차 판정이 첫 줄에 「N분 밀렸다」로 적어 그 사실을 드러낸다.
PHASE_SCHEDULE = {
    "pre": ["08:00"],
    "intra": ["12:30", "14:30"],
    "post": ["15:45"],
}
# 이만큼까지는 「정시」로 본다. 점검은 사람이 시작하므로 몇 분 오차는 정상이고, 여기에 임계를
# 좁게 걸면 매일 ⚠가 떠서 진짜로 밀린 날을 못 알아본다.
# 이만큼까지는 「정시」로 본다. **예약 작업은 지터를 포함해 발화한다** — 08-14 실측으로
# 장전 슬롯 08:30 → 08:31, 장중 12:30 → 12:34였고, 14:30 회차는 등록 시점 확인으로 **14:39**다.
# 9분짜리 지터에 10분 임계를 걸면 여유가 1분뿐이라 며칠 안에 거짓 ⚠가 뜬다.
# 좁게 잡아 매일 우는 것보다 넓게 잡아 진짜로 밀린 날만 잡는 편이 낫다.
PHASE_LATE_TOLERANCE_MIN = 15

# 장중 14:30 회차(`mahdi-intraday-check-1430`)가 등록된 첫 영업일. 그 이전 날짜를 재집계할 때
# 14:30 산출물을 기대하면 거짓 누락이 뜬다 — 규약이 생기기 전의 날은 없는 게 정상이다.
INTRA_1430_SINCE = _date(2026, 8, 17)
# 점검 산출물이 **하루 한 파일**(`{날짜}_마흐디_일일점검.md`)로 바뀐 첫 영업일.
# 장전이 만들고 장중·장후가 append한다(`mahdi-daily-check` 대원칙 B).
#
# **`INTRA_1430_SINCE`와 같은 이유로 날짜를 박는다**: 이 날 이전을 재집계하면 그날의 산출물은
# 국면별 4파일이 맞고, 새 이름을 기대하면 **하루도 빠짐없이 거짓 누락이 뜬다.** 과거 20여 편을
# 개명해 옮기지 않는 이유는 그 이름들이 이미 커밋 이력과 보고서 상호참조에 박혀 있어서다.
ONE_FILE_SINCE = _date(2026, 8, 21)
# 이보다 더 늦으면 「밀린 점검」이 아니라 **사후 재집계**로 본다. 다음 회차(장중 12:30 → 14:30,
# 간격 120분)를 이미 지나쳤다면 그 회차가 대신 돌았어야 하므로, 이 실행은 그날의 정규 점검이
# 아니라 나중에 다시 돌린 것이다. 실제로 이 스크립트의 `--date`가 그 용도다.
PHASE_RERUN_MIN = 120

# 관측 루프가 살아 있어야 하는 구간과, 그 안에서 이만큼 끊기면 의심한다.
GAP_SCAN = ("07:30", "15:45")
# 3분인 이유: 사이클이 분마다 도므로 정상 간격은 1분이고, **08-12의 프로세스 사망은 4분**
# (10:10:06 사망 → 10:14:20 재기동)이었다. 5분으로 두면 그 사건이 표에서 사라진다.
GAP_THRESHOLD_MIN = 3
# 이 구간 안의 공백은 길이와 무관하게 적신호로 올린다 — 정규장에 관측이 끊긴 것이다.
MARKET_HOURS = ("09:00", "15:20")

# 워치독은 감시 창 안에서 이 주기로 OK를 남긴다. 공백은 워치독 자신의 무력화 신호다
# (2026-08-12 §2-3 — 재기동 오보로 5시간 31분 무력화된 채 아무도 몰랐다).
WATCHDOG_EXPECT_INTERVAL_MIN = 10
WATCHDOG_GAP_THRESHOLD_MIN = 25

# 로그 한 줄의 머리. `logging` 레코드는 반드시 여기서 시작한다 —
# 트레이스백 본문은 타임스탬프가 없어 이 정규식이 걸러 준다(형식이 아니라 구조가 만드는 구분).
TS = r"(\d{4}-\d\d-\d\d) (\d\d):(\d\d):(\d\d),(\d+)"
RECORD_RE = re.compile(TS + r" (INFO|WARNING|ERROR|CRITICAL|DEBUG):(\S+?):(.*)$")

# 프로세스 기동 표식 — 프로세스당 정확히 한 번 나온다(`mahdi/ops/log_metrics.py`와 같은 표식).
PROCESS_START_MARKER = "직전 정상 기동"

# 사이클 한 바퀴. 이 줄의 유무가 "그 분에 관측이 있었는가"의 1차 증거다.
CYCLE_TOKEN = "옵션체인 사이클 소요 분해"

# ===== 2026-08-14 §2-1·§2-2 / Fix#3 — **절벽의 선행 지표를 장중에 보이게 한다** =====
#
# 08-14 14:00~15:23 옵션체인이 84분 연속 전멸했다. 12:34 장중 점검은 이 사건을 **구조적으로
# 못 봤고**(아직 안 일어났다), 15:45 장후 집계는 「ERROR 86건 / 2종」만 냈다. 그 사이를 메우는
# 것이 이 절이다 — 세 값이 전부 **결과보다 앞선다**:
#
#   (a) 시간대별 REST수집 평균 ÷ 예산      12시 47.2/50 = 94.4% (12:34에 이미 걸렸다)
#   (b) `inquire-price` p50 ÷ read timeout 13:36 3.08/4.0 = 0.77 (절벽 24분 전)
#   (c) rows=0이 **연속된** 분 수           14:00부터 매분 (그날 84분)
#
# 셋 다 한 줄짜리 숫자인데 종전에는 어디에도 인쇄되지 않았다. 08-14를 찾은 것은 사람이
# 이틀치 로그를 손으로 겹쳐 읽었기 때문이다.
CYCLE_RE = re.compile(
    r"REST수집 ([\d.]+)초.*?\(rows=(\d+),.*?\)(?: 분=(\d\d:\d\d))?"
)
# `엔드포인트=N건 p50/p95/p99/max초`. `mahdi/ops/log_metrics._REST_LATENCY_ITEM_RE`와 같은 모양이다
# (이 파일은 stdlib 전용이라 임포트하지 않고 복제한다 — 그 규약은 파일 헤더 참고).
LATENCY_TOKEN = "REST 응답시간"
LATENCY_ITEM_RE = re.compile(r"([\w-]+)=(\d+)건 ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)초")

# 옵션체인 20레그를 실제로 부르는 엔드포인트. 다른 엔드포인트는 read 타임아웃이 따로(10초)
# 걸려 있어 같은 줄에 놓으면 틀린다(`rest_client._ENDPOINT_READ_TIMEOUT_SECONDS`).
CHAIN_ENDPOINT = "inquire-price"

# 아래 셋은 코드의 복제본이다. **원천은 주석에 적힌 상수이고, 값이 갈라지면 이 파일이 틀린 것이다**
# (ANCHORS와 같은 규약). 파서 쪽 복제는 `tests/test_ops_log_metrics_contract.py`가 지키지만
# 이 파일은 리포에서 독립 실행되는 스크립트라 테스트 대신 이 주석이 계약이다.
# ===== 2026-08-13 고도화 2 — **축 가용성을 추론이 아니라 인용으로 판정한다** =====
#
# 08-13 장전은 `options_flow` 축의 사망을 **감마플립 로그만 보고** 예측했고, 09:01:10 실측이
# 그것을 뒤집었다(그 멤버는 살아 있었다). 반대로 08-14에는 넉 달 만에 살아난 그 멤버가
# 오후에 다시 죽었는데, 그 사실은 사람이 「판단 형태 전이」 줄을 손으로 훑어서 알았다.
#
# 이 줄에 답이 그대로 들어 있다 — 가용 멤버 집합과 최초 편입 시각. 세면 끝나는 일이었다.
MEMBER_TOKEN = "판단 형태 전이"
# ===== 2026-08-19 §3-5 / Fix#6 — **가용 4/6이 실질 2.36이었다** =====
#
# 08-19 장중 회차가 이 줄의 `4/6`을 보고 축 가용성을 ✅로 읽었다. 장후 DB 축이 같은 날의
# 실질 멤버를 **2.36**으로 냈다 — 죽은 축 1.07(`regime_hmm` 410분 전량 중립).
# **0점은 중립이지 의견이 아니다**(`fusion/ensemble.EnsembleResult` 주석). 그런데 0점 멤버도
# 「가용멤버」 목록에는 그대로 남으므로, 이 줄만 보는 눈은 그 차이를 **구조적으로 못 본다.**
#
# 값은 이미 있었다 — `conflict.effective_member_count`가 08-06부터 계산돼 판단에 실려 있고
# `signal_decisions.risk_gate_state`에 적재된다. 로그에만 없었다.
#
# ⚠ **파서를 먼저 이중화하고 나서 문구를 바꾼다.** 08-04에 로그 레벨이 WARNING→INFO로
# 내려가며 정규식이 눈이 멀어 **362건이 0건으로 보고**됐다. 08-18의 `데드라인이먼슬리에서끝남`
# 개명이 그 순서를 지켜 성공했다(옛 라벨·새 라벨 양쪽을 같은 파서가 읽는다).
# 그래서 `(4/6)`(옛)과 `(4/6, 비영 2)`(새)를 **한 정규식이 둘 다** 받는다.
MEMBER_RE = re.compile(r"가용멤버 \[(.*?)\]\((\d+)/(\d+)(?:, 비영 (\d+))?\)(?: · (\w+))?")
MEMBER_NAME_RE = re.compile(r"'([\w]+)'")
# 마지막 관측이 로그 끝보다 이만큼 이르면 「도중에 빠졌다」로 본다.
# 30분인 이유: 판단은 분마다 나지만 **형태 전이 줄은 형태가 바뀔 때만** 찍힌다 — 안정된 구간은
# 조용한 것이 정상이다. 08-14 오후의 `options_flow` 이탈은 81분이었으므로 이 임계로 잡힌다.
MEMBER_DROPOUT_ALERT_MIN = 30

CHAIN_COLLECT_BUDGET_SECONDS = 50.0      # mahdi/main.OPTION_CHAIN_CYCLE_COLLECT_BUDGET_SECONDS
GLOBAL_READ_TIMEOUT_SECONDS = 4.0        # mahdi/broker/rest_client._HTTP_READ_TIMEOUT_SECONDS
BUDGET_WARN_RATIO = 0.90                 # 시간대 평균이 예산의 이만큼을 넘으면 적신호
P50_TIMEOUT_WARN_RATIO = 0.80            # mahdi/ops/log_metrics.REST_LATENCY_P50_TIMEOUT_RATIO_WARN
# ===== 2026-08-25 (08-25 §1-8·§1-11 / P1-1) — **p95가 장중 회차에 닿는다** =====
#
# 08-25에 12:30·14:30 두 회차가 「p95(느린 쪽 5%)를 아무도 안 본다」를 신규 P1으로 올렸는데,
# `daily_ops_report`는 그것을 전부 인쇄하고 있었다 — 그 파일이 **15:46에 생길 뿐이다.**
# `LATENCY_ITEM_RE`는 p50/p95/p99/max 네 값을 다 잡아 놓고 p50만 쓰고 있었다(§5-1).
# 임계는 리포트와 **같은 값**이어야 한다 — 두 곳이 갈리면 장중 판정과 장후 판정이 어긋난다.
# 원천: mahdi/ops/log_metrics.REST_LATENCY_P95_WARN_SECONDS (stdlib 전용이라 복제, 값이
# 갈라지면 이 파일이 틀린 것이다 — CHAIN_COLLECT_BUDGET_SECONDS와 같은 규약).
P95_WARN_THRESHOLD_SECONDS = 2.5
# 이틀 연속 성립 시 인쇄할 사전 대응 규칙. 조건·조치·⛔수동 발동 원칙은 hypotheses.yaml의
# 해당 항목이 정본이다(2026-07-08 페이서 분리 500 폭주 203분이 ⛔의 근거).
P95_TWO_DAY_RULE_ID = "2026-08-04-p5"

# ===== 2026-08-23 (08-21 §1-14 / §5 고도화#1) — **검열된 p50은 중앙값이 아니라 하한이다** =====
#
# 08-21 지연창 98개 중 상당수가 p50 4.03~4.05초를 냈고, read timeout이 4.0초이므로 그 값은
# **타임아웃 벽에 눌린 값**이다. 그런데 이 도구도 리포트도 그것을 「4.03초」로 평범하게 인쇄했고
# 네 회차가 실제 응답시간처럼 읽었다. `≥`가 붙어 있었다면 **「6초로 늘리면 몇 %가 더 들어오는가」를
# 이 데이터로는 계산할 수 없다**는 것이 한눈에 보였을 것이다.
#
# 재료는 새로 만들지 않는다: `rest_client._log_if_slow`가 **타임아웃 호출을 임계와 무관하게
# 반드시 남긴다**(08-05 Fix#4). 임계 3.0초 < timeout 4.0초이므로 검열된 호출은 전부 그 줄에 있다.
# 포맷 원본: `mahdi.broker.rest_client.LOG_SLOW_CALL`
SLOW_CALL_RE = re.compile(
    r"느린 REST 호출 ([\d.]+)초 = 페이서대기 ([\d.]+)초 \+ HTTP ([\d.]+)초 "
    r"\(배율 ([\d.]+)배, (\w+) (\S+)\)"
)
# p50이 타임아웃의 이 배수를 넘으면 「벽에 닿았다」로 본다. 1.00이 아닌 이유는 최근접 순위법
# p50이 4.00 바로 아래(3.99)로 떨어질 수 있기 때문이고, 그 창도 검열된 창이다.
# 원천: `mahdi/ops/log_metrics.P50_CENSORED_FLOOR_RATIO`
P50_CENSORED_FLOOR_RATIO = 0.98
ZERO_ROW_RUN_ALERT_MINUTES = 20          # mahdi/ops/db_metrics.ZERO_ROW_RUN_ALERT_MINUTES

# ===== 2026-08-24 (08-24 §1-8·§1-9 / Fix#6 B · Fix#4 B) — **분자와 분모를 같은 표에** =====
#
# 08-24에 이 두 인과를 하루에 여섯 번 대조했고 **결론이 세 번 뒤집혔다**:
#
#   「백오프가 확대되면 잔고 폴링이 실패하는가」   확대→실패 2/21  ·  실패→확대 2/2
#   「먼슬리 되살리기가 실패했는가」               15:10:50에 3개 중 0개 — 하루치 로그를 훑어 찾았다
#
# 세 번 다 서로 다른 파일의 줄을 밀리초로 맞춰 본 결론이다. **비율을 읽으려면 분자와 분모가
# 같은 표에 있어야 한다**(규약 E). 시간대 단위면 「같은 시간대에 둘 다 있었다」까지는 5초에
# 보이고, 그보다 잘게 봐야 할 때 §5-1의 지연창 TSV로 내려가면 된다.
#
# 문구는 원본 상수의 복제다 — 이 파일은 stdlib 전용이라 임포트할 수 없다(파일 헤더).
# **값이 갈라지면 이 파일이 틀린 것이다**(ANCHORS·CHAIN_COLLECT_BUDGET_SECONDS와 같은 규약).
BALANCE_POLL_FAILED_TOKEN = "계좌 잔고 폴링 사이클 실패"   # mahdi/main.LOG_BALANCE_POLL_FAILED
BACKOFF_EXPAND_TOKEN = "레이트리밋 백오프 확대"            # mahdi/broker/rest_client.LOG_BACKOFF_EXPAND
PRIORITY_RETRY_TOKEN = "먼슬리 레그 재시도"                # mahdi/main._LOG_CHAIN_PRIORITY_RETRY_HEAD
# 세 변종(평시 INFO · 되살리기 실패 WARNING · 선할당 바닥 INFO)이 같은 머리를 쓴다.
# **레벨로 가르지 않는다** — 08-04에 레벨 강등으로 정규식이 눈이 멀어 362건이 0건이 됐다.
PRIORITY_RETRY_RE = re.compile(r"먼슬리 레그 재시도: (\d+)개 중 (\d+)개 회복\(남은 예산 ([\d.]+)초\)")
PRIORITY_RETRY_FAILED_TOKEN = "되살리기 실패"

# 조용히 지나가면 안 되는 사건들. 태그가 없는 로그라 **문구로 식별한다** —
# 레벨은 사람이 읽는 우선순위일 뿐 계측의 정체성이 아니다(2026-08-04 §2-1: 레벨이
# WARNING→INFO로 내려가며 정규식이 통째로 눈이 멀어 362건을 0건으로 보고했다).
ALWAYS_QUOTE = {
    "ws_disconnect": ("WS 연결 끊김", "WS 재연결 후 다시 끊김"),
    "chain_total_failure": ("옵션 체인 폴링 전체 실패",),
    "budget_exceeded": ("옵션체인 수집 예산",),
    "timeout_abort": ("옵션체인 연속 타임아웃",),
    "failure_budget": ("옵션체인 실패 예산",),
    "catchup": ("옵션체인 결손 회수",),
    "gap_alert": ("옵션체인 결손 알림",),
    "priority_retry": ("먼슬리 레그 재시도",),
    "atm_roll": ("ATM 롤링",),
    "egw00201": ("EGW00201",),
    "event_calendar": ("이벤트 캘린더 미기입",),
    "market_operation": ("장운영정보",),
    "regime": ("레짐", "WARMUP"),
    "vi": ("VI ",),
}
QUOTE_SAMPLES = 6

# ===== 2026-08-14 장중 §8 / 고도화 3 — **계측 부재를 수집기가 스스로 신고한다** =====
#
# 08-14 장중 점검이 답하지 못한 항목이 셋이었다(stale 비율 · 레짐 분당 갱신 · ATM 왕복).
# 셋 다 「0건」이나 「판정 불가」로 **조용히** 지나갔다 — 규약 C가 말하는 두 가지 0 중
# 나쁜 쪽인데, 그것을 알아채려면 사람이 `phases.md`를 옆에 펴 놓고 하나씩 대조해야 했다.
#
# 여기서 `phases.md`의 체크 항목과 **그 항목에 답하는 로그 문구**를 짝지어 둔다. 그 문구가
# 하루 0줄이면 「계측 없음 ⚠」으로 인쇄한다 — 「그 일이 안 일어났다」와 구별해서.
#
# `대체축`이 있는 항목은 **적신호로 올리지 않는다.** 로그에 없다고 못 재는 것이 아니기 때문이다
# (08-14 장중 §4-3이 stale 비율을 「계측이 없다」로 적었는데, 실제로는 DB 축이 84.2%를 내고
# 있었다 — 그 부분 정정이 장후 §2-6에 남아 있다). **틀린 경보는 진짜 경보를 죽인다.**
MEASUREMENT_MAP = [
    # (phases.md 항목, 그 항목에 답하는 로그 문구, 대체 축 or None)
    ("B-2 예산 초과", "옵션체인 수집 예산", None),
    ("B-2 연속 타임아웃 조기 포기", "옵션체인 연속 타임아웃", None),
    ("B-2 실패 예산 소진", "옵션체인 실패 예산", None),
    ("B-2 먼슬리 레그 재시도", "먼슬리 레그 재시도", None),
    ("B-2 WS 단절", "WS 연결 끊김", "DB 축 `ws_status`"),
    ("B-2 ATM 롤링 왕복", "ATM 롤링", "지표 §? `atm_rolls.round_trip_pct`"),
    ("B-3 앙상블 멤버 가용성", MEMBER_TOKEN, "DB 축 §14-1 `member_availability`"),
    ("B-3 레짐 갱신", "레짐 전이", "DB 축 §11-2 `regime_vs_futures_bars`"),
    # 문구를 「신선도」로 두면 안 된다 — 전멸 줄의 *서술*("신선도 창 안의 직전 스냅샷을 쓴다")에
    # 걸려 계측이 살아 있는 것처럼 보인다. 08-14 장중 §4-3이 셌던 것이 정확히 그 서술이었다.
    # **비율을 인쇄하는 줄은 아직 없고**, 그 사실이 이 칸에 그대로 보여야 한다.
    ("B-3 체인 신선도(stale 비율)", "체인 신선도 비율", "DB 축 §14 `chain_input_source`"),
    ("B-3 이벤트 캘린더 미기입", "이벤트 캘린더 미기입", None),
    # 사이클 줄은 분마다 나온다 — **0이면 그날 관측이 없었거나 파서가 눈이 먼 것**이고,
    # 둘 다 이 표에서 가장 먼저 보여야 하는 사실이다.
    ("B-1 사이클 관측", "옵션체인 사이클 소요 분해", None),
]

# 레버 — `mahdi/ops/levers.py`의 `_SPEC`과 같은 이름을 쓴다. 값 해석은 하지 않고
# **어디에 어떤 줄이 있는가**만 보여 준다(판정은 그 레버의 가설이 할 일이다).
LEVER_KEYS = [
    ("use_effective_member_count", "mahdi/config/strategy_params.yaml"),
    ("reentry_cooldown_minutes", "mahdi/config/strategy_params.yaml"),
    ("SIGNAL_FUSION_PHASE_OFFSET_SECONDS", "mahdi/main.py"),
    ("OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS", "mahdi/main.py"),
    ("OPTION_CHAIN_READ_TIMEOUT_SECONDS", "mahdi/broker/rest_client.py"),
    ("REGIME_RESTORE_SESSION_WINDOW", "mahdi/engines/regime_pipeline.py"),
]

COMMIT_PREFIX = "[MW0601]"
MSG_TRUNCATE = 220


# ---------------------------------------------------------------- 유틸
def eprint(*a):
    print(*a, file=sys.stderr)


def find_repo_root(start: Path) -> Path:
    """`mahdi/` 와 `docs/동작점검` 을 함께 가진 첫 조상을 리포 루트로 본다."""
    cur = start.resolve()
    for cand in [cur, *cur.parents]:
        if (cand / "mahdi").is_dir() and (cand / "docs" / "동작점검").is_dir():
            return cand
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    return cur


def parse_date(s):
    if not s:
        return datetime.now(KST).date()
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d", "%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt in ("%m/%d", "%m-%d"):
            d = d.replace(year=datetime.now(KST).year)
        return d.date()
    raise SystemExit(f"날짜 형식을 못 읽었다: {s!r} (예: 2026-08-12 / 20260812 / 8/12)")


def hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def m2hhmm(m):
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n}B"


def truncate(s, n=MSG_TRUNCATE):
    s = str(s).replace("\n", " ⏎ ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def normalize(msg):
    """메시지에서 숫자를 지워 **같은 사건의 다른 인스턴스**를 한 줄로 묶는다."""
    return truncate(re.sub(r"\d+(\.\d+)?", "N", msg), 110)


# ===== 2026-08-19 — **이 수집기는 저장소를 잠글 수 없어야 한다** =====
#
# `.git/index.lock` 0바이트 잔재가 이틀 연속 저장소를 막았다(08-18 16:20 · 08-19 12:41).
# 두 락 모두 **이 수집기를 돌린 세션이 마지막 산출물을 쓴 그 분**에 생겼고, 락을 쥔 것은
# 아래 §1의 `git status --porcelain`이다 — status는 다음 실행을 빠르게 하려고 인덱스 캐시를
# 갱신하는데, 그 갱신에 락이 필요하다(실측 5~6ms 보유 · 0바이트). 그 순간 세션 teardown의
# 트리 킬을 맞으면 atexit 정리가 안 돌아 락이 남는다(원인 전말은 `mahdi/git_lock.py`).
#
# `GIT_OPTIONAL_LOCKS=0`은 **그 선택적 갱신을 포기하게** 한다. 실측(343개 touch 후 = 최악 조건):
#
#     기본                    262~343ms · index.lock 관측 5~6회(매번)
#     GIT_OPTIONAL_LOCKS=0    371~395ms · index.lock 관측 **0회** · 출력 동일
#
# **락을 못 만들면 트리 킬이 락을 남길 수 없다.** 워치독의 청소(`git_lock.sweep`)보다 한 자리
# 앞선 조치다 — 그쪽은 이미 생긴 락을 여는 것이고, 이쪽은 생기지 않게 한다.
# 환경변수로 거는 이유는 `run_git`을 거치는 **다섯 호출 전부**에 한 번에 적용되기 때문이다.
#
# ## 왜 §1을 없애지 않는가
#
# 「말미 git 명령을 빼면 되지 않나」가 첫 제안이었는데, 그 절은 `phases.md`가 요구하는 판정
# 근거다: *"커밋 시각 < 관측 루프 기동 시각이어야 한다"*. 이것으로 가설 상태를 `refuted`가
# 아니라 `untested`로 가른다(2026-08-04 p4 — 15분 차이로 하루를 잃은 그 규약).
# **없애면 그 판정 근거가 사라진다.** 고쳐야 하는 것은 호출이 아니라 잠글 수 있음이다.
_GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def run_git(root, args, timeout=25):
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            # 표준 입력을 끊는다 — git이 자격증명·pager 등으로 입력을 기다리면 `timeout`이
            # 만료될 때까지 세션이 매달린다. 이 수집기는 사람과 대화하지 않는다.
            stdin=subprocess.DEVNULL,
            env=_GIT_ENV,
        )
        return p.stdout.strip() if p.returncode == 0 else f"(git 실패 rc={p.returncode}) {p.stderr.strip()[:300]}"
    except Exception as e:  # noqa: BLE001
        return f"(git 실행 불가) {e}"


def read_text(path, limit=None):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read() if limit is None else f.read(limit)
    except Exception as e:  # noqa: BLE001
        return f"(읽기 실패) {e}"


def stat_line(p: Path):
    if not p.exists():
        return "**없음 ⚠**", "—", "—"
    st = p.stat()
    mt = datetime.fromtimestamp(st.st_mtime, KST)
    return "있음", fmt_bytes(st.st_size), f"{mt:%m-%d %H:%M}"


def iter_day_lines(log_dir: Path, target: _date, stem="observation_loop.log"):
    """로테이션(`*.log.N`)을 **오래된 것부터** 훑어 대상 날짜 줄만 흘려보낸다.

    하루치가 `.log.1`과 `.log`에 걸치는 일이 실제로 있다(2026-07-30). 타임스탬프가 없는
    트레이스백 줄은 **직전 줄의 날짜를 승계**한다 — 이 처리를 빠뜨리면 트레이스백이
    통째로 누락돼 볼륨 집계가 틀린다. (`mahdi/ops/log_metrics.iter_day_lines`와 같은 규칙.)
    """
    prefix = target.isoformat()
    backups = sorted(
        (p for p in log_dir.glob(f"{stem}.*") if p.suffix.lstrip(".").isdigit()),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,
    )
    for path in [*backups, log_dir / stem]:
        if not path.exists():
            continue
        carrying = False
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if len(line) >= 10 and line[4] == "-" and line[7] == "-" and line[:4].isdigit():
                        carrying = line.startswith(prefix)
                    if carrying:
                        yield line.rstrip("\n")
        except OSError as e:
            eprint(f"[collect_evidence] {path} 읽기 실패: {e}")


def tokens_seen_on_other_days(log_dir: Path, target: _date, tokens, stem="observation_loop.log"):
    """반환: `{문구: (다른 날 줄 수, 그 날짜)}` — **대상 날짜를 제외한** 잔여 로그 전체 기준.

    2026-08-14 고도화 3. 「그 문구가 오늘 0줄」에서 **「계측이 없다」와 「진짜 안 일어났다」**를
    가르는 유일한 기계적 방법이다. 08-14 장중 점검이 손으로 한 것이 정확히 이것이다:

        *"「실패 예산 소진 0건」은 계측 없음이 아니라 진짜 0이다. 같은 로그 파일에 08-13자로
          1건 남아 있어 이 경로가 로깅된다는 것이 확인된다(규약 C — 0은 두 가지다)."*

    로테이션이 10MB×10이라 보통 이틀치가 남는다 — **못 찾았다고 「계측 없음」이 확정되는 것은
    아니다**(그 사건이 이틀 내내 없었을 수도 있다). 그래서 결과는 「확인됨」과 「미확인」이지
    「없음」이 아니다.
    """
    found = {t: (0, None) for t in tokens}
    backups = sorted(
        (p for p in log_dir.glob(f"{stem}.*") if p.suffix.lstrip(".").isdigit()),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,
    )
    prefix = target.isoformat()
    for path in [*backups, log_dir / stem]:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if len(line) < 10 or not line[:4].isdigit() or line.startswith(prefix):
                        continue
                    for token in tokens:
                        if token in line:
                            n, _when = found[token]
                            found[token] = (n + 1, line[:10])
        except OSError as e:
            eprint(f"[collect_evidence] {path} 읽기 실패: {e}")
    return found


def bracket_day_lines(path: Path, target: _date):
    """`[2026-08-12 10:14:01] ...` 형식(워치독·장전 배치) 중 대상 날짜 줄."""
    if not path.exists():
        return []
    prefix = f"[{target.isoformat()}"
    out = []
    for line in read_text(path).splitlines():
        s = line.strip()
        if s.startswith(prefix):
            out.append(s)
    return out


# ---------------------------------------------------------------- 관측 루프 스캔
class LoopScan:
    """관측 루프 하루치를 한 번 훑고 남길 것만 남긴다."""

    def __init__(self):
        self.records = 0                       # 타임스탬프 있는 레코드 줄
        self.traceback_lines = 0               # 레코드에 딸린 줄(볼륨 팽창의 주범)
        self.levels = collections.Counter()
        self.loggers = collections.Counter()
        self.severe = []                       # ERROR 이상 전량
        self.warn_norm = collections.Counter()
        self.warn_first = {}
        self.warn_last = {}
        self.quoted = collections.defaultdict(list)
        self.process_starts = []
        self.cycle_minutes = set()
        self.minutes_seen = set()
        self.first = None
        self.last = None
        # 2026-08-14 Fix#3 — 시간대별 수집 소요·rows / 시간대별 p50. 상세 근거는 `CYCLE_RE` 주석.
        self.cycle_rest = collections.defaultdict(list)   # 시(hour) -> [REST수집 초]
        self.cycle_rows = collections.defaultdict(list)   # 시(hour) -> [rows]
        self.zero_row_minutes = set()                     # rows=0인 분(분 단위 정수)
        self.latency_p50 = collections.defaultdict(list)  # 시(hour) -> [(창시각, 건수, p50)]
        # 2026-08-25 P1-1 — 같은 줄의 p95도 보관한다(체인 엔드포인트, 창 단위).
        self.latency_p95 = collections.defaultdict(list)  # 시(hour) -> [(창시각, 건수, p95)]
        # 「이틀 연속」 판정용 — **전 엔드포인트**의 시간대별 (건수, p95) 표본. 지표 사이드카의
        # `rest_latency.p95_by_hour`(호출 수 가중 평균)와 같은 식으로 접어야 어긋나지 않는다.
        self.endpoint_p95_hourly = {}                     # 엔드포인트 -> {시: [(건수, p95)]}
        # 2026-08-23 고도화#1 — 검열(= read timeout에 잘린) 호출의 초 단위 시각.
        # 창에 붙이려면 분보다 잘게 알아야 한다(창 경계에 걸린 호출의 소속이 갈린다).
        self.censored_seconds = []
        # 2026-08-13 고도화 2 — 축 가용성. 상세 근거는 `MEMBER_TOKEN` 주석.
        self.member_first_seen = {}                       # 멤버 -> 최초 편입 시각
        self.member_last_seen = {}                        # 멤버 -> 마지막 관측 시각
        self.member_transitions = 0
        self.member_shape = collections.Counter()         # "4/6" -> 전이 건수
        # 2026-08-19 Fix#6 — 새 문구가 실은 「비영 N」들. 옛 로그에서는 **빈 목록**이고,
        # 그것은 「비영이 0이었다」가 아니라 **「안 셌다」**다.
        self.member_nonzero = []
        self.member_conviction = collections.Counter()
        self.member_total = None                          # 분모(6) — 로그가 알려 준다
        # 2026-08-14 고도화 3 — 계측 부재 신고. 상세 근거는 `MEASUREMENT_MAP` 주석.
        self.measurement_hits = collections.Counter()
        # 2026-08-24 Fix#6 B · Fix#4 B — 시간대별 **분자와 분모**. 상세 근거는
        # `BALANCE_POLL_FAILED_TOKEN` 위 절 주석.
        self.balance_poll_failures = collections.Counter()   # 시(hour) -> 건수
        self.backoff_expansions = collections.Counter()      # 시(hour) -> 건수
        self.priority_retries = collections.Counter()        # 시(hour) -> 건수
        self.priority_retry_failures = collections.Counter() # 시(hour) -> 회복 실패 건수
        self.priority_retry_budget_min = {}                  # 시(hour) -> 남은 예산 최소(초)

    def feed(self, line):
        m = RECORD_RE.match(line)
        if not m:
            if line.strip():
                self.traceback_lines += 1
            return
        self.records += 1
        hh, mm = int(m.group(2)), int(m.group(3))
        minute = hh * 60 + mm
        hhmmss = f"{m.group(2)}:{m.group(3)}:{m.group(4)}"
        level, logger, msg = m.group(6), m.group(7), m.group(8)
        self.levels[level] += 1
        self.loggers[f"{level}:{logger}"] += 1
        self.minutes_seen.add(minute)
        if self.first is None:
            self.first = (hhmmss, truncate(msg, 120))
        self.last = (hhmmss, truncate(msg, 120))

        if level in ("ERROR", "CRITICAL"):
            self.severe.append((hhmmss, level, logger, truncate(msg, 300)))
        elif level == "WARNING":
            key = normalize(msg)
            self.warn_norm[key] += 1
            self.warn_first.setdefault(key, hhmmss)
            self.warn_last[key] = hhmmss

        if PROCESS_START_MARKER in msg:
            self.process_starts.append((hhmmss, truncate(msg, 160)))
        if CYCLE_TOKEN in msg:
            self.cycle_minutes.add(minute)
            c = CYCLE_RE.search(msg)
            if c:
                rows = int(c.group(2))
                self.cycle_rest[hh].append(float(c.group(1)))
                self.cycle_rows[hh].append(rows)
                if rows == 0:
                    # 적재 분은 **`분=` 라벨 축**으로 잡는다 — 그 라벨이 그 사이클이 실제로
                    # 적재한 분이다(08-10에 파생 축이 반올림으로 허위 결손 2건을 냈다).
                    # 라벨이 없는 구 로그(08-07 이전)에서는 레코드 시각으로 폴백한다.
                    label = c.group(3)
                    self.zero_row_minutes.add(hhmm_to_min(label) if label else minute)
        if MEMBER_TOKEN in msg:
            mm = MEMBER_RE.search(msg)
            if mm:
                self.member_transitions += 1
                self.member_total = int(mm.group(3))
                self.member_shape[f"{mm.group(2)}/{mm.group(3)}"] += 1
                # 2026-08-19 Fix#6 — 옛 문구에는 없는 값이다. **없는 것을 0으로 접지 않는다**:
                # 「비영 0」과 「안 셌다」는 조치가 다르다(전자는 사고, 후자는 옛 로그다).
                if mm.group(4) is not None:
                    self.member_nonzero.append(int(mm.group(4)))
                if mm.group(5):
                    self.member_conviction[mm.group(5)] += 1
                for name in MEMBER_NAME_RE.findall(mm.group(1)):
                    self.member_first_seen.setdefault(name, hhmmss)
                    self.member_last_seen[name] = hhmmss
        sc = SLOW_CALL_RE.search(msg)
        if sc and sc.group(6) == CHAIN_ENDPOINT:
            # **HTTP 구간만 본다.** 페이서 대기는 우리 쪽이고 검열은 상대 쪽 천장이다.
            # 임계는 그날 실제 타임아웃이어야 하므로 호출 시점에 비교하지 않고 초만 모은다.
            self.censored_seconds.append((hh * 3600 + mm * 60 + int(m.group(4)), float(sc.group(3))))

        if LATENCY_TOKEN in msg:
            for it in LATENCY_ITEM_RE.finditer(msg):
                calls, p95 = int(it.group(2)), float(it.group(4))
                if it.group(1) == CHAIN_ENDPOINT:
                    # 2026-08-19 Fix#7 — **창 시각을 함께 싣는다.** 종전에는 `(건수, p50)`만
                    # 남겨 5분 창의 시계열이 시간대 안에서 사라졌다. 근거는 `window_latency_p50`.
                    self.latency_p50[hh].append((hhmmss, calls, float(it.group(3))))
                    # 2026-08-25 P1-1 — 같은 줄의 p95. 새로 재는 것이 아니라 안 쓰던 값이다.
                    self.latency_p95[hh].append((hhmmss, calls, p95))
                # 2026-08-25 P1-1 ④ — 이틀 연속 판정은 전 엔드포인트 축이다(08-25 성립 6구간에
                # `inquire-balance`가 둘 있었다). 창 목록이 아니라 시간대 표본만 접어 둔다.
                self.endpoint_p95_hourly.setdefault(it.group(1), {}).setdefault(hh, []).append(
                    (calls, p95)
                )

        # 2026-08-24 Fix#6 B · Fix#4 B — 시간대별로 센다.
        if BALANCE_POLL_FAILED_TOKEN in msg:
            self.balance_poll_failures[hh] += 1
        if BACKOFF_EXPAND_TOKEN in msg:
            self.backoff_expansions[hh] += 1
        pr = PRIORITY_RETRY_RE.search(msg)
        if pr:
            self.priority_retries[hh] += 1
            if int(pr.group(2)) < int(pr.group(1)) or PRIORITY_RETRY_FAILED_TOKEN in msg:
                # **두 축을 다 본다.** 숫자(회복 < 대상)가 정본이고 문구는 그 확인이다 —
                # 한쪽이 바뀌어도 다른 쪽이 남는다(08-04의 눈멂을 한 번 더 막는 자리다).
                self.priority_retry_failures[hh] += 1
            left = float(pr.group(3))
            if hh not in self.priority_retry_budget_min or left < self.priority_retry_budget_min[hh]:
                self.priority_retry_budget_min[hh] = left

        for key, tokens in ALWAYS_QUOTE.items():
            if any(t in msg for t in tokens):
                self.quoted[key].append((hhmmss, level, truncate(msg, 240)))

        # **`ALWAYS_QUOTE`와 따로 센다.** 저쪽은 「인용할 사건」의 목록이고 이쪽은 「이 체크
        # 항목에 답할 계측이 살아 있는가」의 목록이다 — 겹치는 문구가 많지만 목적이 달라서,
        # 한쪽을 고치다 다른 쪽이 조용히 눈머는 것이 08-04에 실제로 일어난 사고다.
        for _item, token, _alt in MEASUREMENT_MAP:
            if token in msg:
                self.measurement_hits[token] += 1

    def gaps(self):
        lo, hi = hhmm_to_min(GAP_SCAN[0]), hhmm_to_min(GAP_SCAN[1])
        pts = sorted(x for x in self.minutes_seen if lo <= x <= hi)
        return [(a, b, b - a) for a, b in zip(pts, pts[1:]) if b - a >= GAP_THRESHOLD_MIN]

    def longest_zero_row_run(self):
        """반환: rows=0이 **연속된** 최장 구간 `(길이, 시작, 끝)`. 없으면 None.

        「0행 86분」이라는 한 숫자로는 08-14와 평범한 날이 구별되지 않는다 — 흩어진 86분과
        붙어 있는 84분은 완전히 다른 사건이고, 뒤쪽만 판단 입력을 죽인다.

        ## 한 분이라도 회복되면 구간은 **끊긴다**

        08-14의 사람 보고서는 14:00~15:23을 「84분」으로 적었다(14:32 한 분만 행이 남았다).
        이 함수는 같은 날을 **32분 + 51분**으로 가른다. 관대해 보이는 쪽이 아니라 **이쪽이
        맞다**: 체인 스냅샷의 신선도 창은 5분이므로(`db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES`)
        14:32에 들어온 행은 그 뒤 5분간 판단에 실제로 실린다. 즉 그 한 분은 진짜로 회복이었다.

        이 지표가 재는 것은 「0행이 몇 분 이어졌는가」가 아니라 **「판단이 체인을 못 본 채로
        몇 분을 갔는가」**다. 임계(20분)도 그 뜻으로 정해져 있다.
        """
        pts = sorted(self.zero_row_minutes)
        if not pts:
            return None
        best = (1, pts[0])
        cur_start, cur_len = pts[0], 1
        for prev, m in zip(pts, pts[1:]):
            cur_len = cur_len + 1 if m == prev + 1 else 1
            if m != prev + 1:
                cur_start = m
            if cur_len > best[0]:
                best = (cur_len, cur_start)
        return best[0], m2hhmm(best[1]), m2hhmm(best[1] + best[0] - 1)

    def hourly_latency_p50(self, hour):
        """반환: 그 시간대 `inquire-price` p50의 `(호출 수 가중 평균, 창 최대)`. 창이 없으면 `(None, None)`.

        **둘 다 낸다. 판정은 최대로 한다.**

        가중 평균인 이유: 5분 창마다 호출 수가 다르다(08-14 14시 53건 vs 15시 103건).
        가중하지 않으면 한산한 창이 붐비는 창과 같은 무게를 갖는다.

        최대를 함께 내는 이유: **평균은 절벽을 눌러 없앤다.** 08-14 13시의 가중 평균은 2.18초
        (비율 0.55 — 조용하다)인데 그 시간대의 창 최대는 3.53초(**0.88** — 경고선 초과)였고,
        **그 20~60분 뒤 84분 전멸이 시작됐다.** 평균만 보는 눈은 선행 신호를 평탄화해 버린다.
        """
        items = self.latency_p50.get(hour) or []
        total = sum(n for _at, n, _p in items)
        if not total:
            return None, None
        return (round(sum(n * p for _at, n, p in items) / total, 2),
                round(max(p for _at, _n, p in items), 2))

    def window_latency_p50(self):
        """반환: 그날 전 창을 시각순으로 `[(창시각, 호출 수, p50)]`. 없으면 빈 목록.

        ## 시간대 표는 절벽을 눌러 없앤다 (2026-08-19 §2-5 / Fix#7)

        08-19 13시 행은 「창 최대 1.01 ⛔」 **한 줄**인데 실제로는
        `13:01` 0.84 → `13:06` **1.01** → `13:11` 0.69로 **6분 만에 오르내렸다.**
        그 시계열을 14:30 회차가 **손으로 다시 파싱해야 했다.**

        정답이 있는 검증: 08-14는 `13:51` 0.90(경고 최초)·`14:06` 1.01(위험 최초)이고
        그 두 숫자는 사람이 이미 손으로 구해 뒀다 —
        `tests/test_evidence_collector_latency.py`가 그것으로 이 함수를 잡는다.
        """
        out = []
        for hour in sorted(self.latency_p50):
            out.extend(self.latency_p50[hour])
        return sorted(out)

    def hourly_latency_p95_max(self, hour):
        """반환: 그 시간대 `inquire-price` **창 최대 p95**. 창이 없으면 None.

        「없음(None)」과 「0.00」은 다른 사실이다(규약 C) — 호출측이 문구를 가른다.
        판정을 최대로 하는 이유는 `hourly_latency_p50` docstring과 같다(평균은 절벽을 눌러 없앤다).
        """
        items = self.latency_p95.get(hour) or []
        return round(max(p for _at, _n, p in items), 2) if items else None

    def hourly_p95_weighted(self):
        """반환: `{엔드포인트: {시: 호출 수 가중 평균 p95}}`. 계측이 없으면 빈 dict.

        지표 사이드카의 `rest_latency.p95_by_hour`와 **같은 식**(창 p95의 호출 수 가중 평균)이다.
        「이틀 연속」 판정이 장후 리포트와 같은 축에 서야 하므로 최대가 아니라 가중 평균을 쓴다 —
        두 축이 갈리면 장중 판정과 장후 판정이 어긋나고 사람은 어느 쪽을 믿을지 모른다.
        """
        out = {}
        for endpoint, by_hour in self.endpoint_p95_hourly.items():
            folded = {}
            for hh, items in by_hour.items():
                total = sum(n for n, _p in items)
                if total:
                    folded[hh] = round(sum(n * p for n, p in items) / total, 3)
            if folded:
                out[endpoint] = folded
        return out

    def window_censored_counts(self, timeout):
        """반환: `{창시각(HH:MM:SS): 그 창에서 타임아웃에 잘린 호출 수}`.

        입력: 그날 실제로 걸려 있던 read timeout.
        계산: 창은 `[직전 창 끝, 이 창 끝)`이다 — `poll_rest_latency_snapshot`이 **직전 창의
             표본을 비우면서** 줄을 남기므로 시각은 창의 **끝**이다.
        해석: 상세 근거는 `P50_CENSORED_FLOOR_RATIO` 위 주석. 이 수를 창의 호출 수로 나눈 것이
             「검열 비율」이고, 그 값이 있어야 `≥4.0초`가 얼마나 심한 하한인지 읽을 수 있다.
        실패 조건: 없다 — 느린 호출 줄이 하나도 없으면 빈 dict이고, 호출측이 그것을
                  「0%」가 아니라 **「안 셌다」**로 인쇄한다(규약 C).
        """
        windows = self.window_latency_p50()
        if not windows or not self.censored_seconds:
            return {}
        ends = [hhmm_to_min(at[:5]) * 60 + int(at[6:8]) for at, _n, _p in windows]
        span = min((b - a for a, b in zip(ends, ends[1:])), default=300) or 300
        counts = collections.Counter()
        for at_seconds, http_seconds in self.censored_seconds:
            if http_seconds < timeout:
                continue
            for end, (label, _n, _p) in zip(ends, windows):
                if at_seconds < end <= at_seconds + span:
                    counts[label] += 1
                    break
        return dict(counts)

    def first_latency_breach(self, timeout, ratio):
        """반환: `p50/timeout`이 `ratio`에 **처음 닿은** 창 `(창시각, 호출 수, p50, 비율)`. 없으면 None.

        **최대가 아니라 최초다.** 최대는 「얼마나 나빴나」이고 최초는 **「언제부터 나빴나」**다.
        08-14에 위험선 최초 돌파(`14:06`)와 전멸 시작(`14:00`) 사이가 그 하루의 전부였다.
        """
        if not timeout:
            return None
        for at, n, p50 in self.window_latency_p50():
            if p50 / timeout >= ratio:
                return at, n, p50, round(p50 / timeout, 2)
        return None

    def anchor_hits(self, phases):
        out = []
        for at, label, ph in ANCHORS:
            if ph not in phases:
                continue
            t = hhmm_to_min(at)
            hits = sum(1 for x in self.minutes_seen if abs(x - t) <= ANCHOR_WINDOW_MIN)
            out.append((at, label, hits))
        return out


# ===== 2026-08-25 P1-1 ③·④ — p95 임계 판정과 「이틀 연속」 겹침 =====

def p95_breaches(grid, threshold=P95_WARN_THRESHOLD_SECONDS):
    """반환: `[(엔드포인트, 시, 가중 p95)]` — 임계를 **넘는** 구간(엔드포인트·시 정렬).

    입력은 `LoopScan.hourly_p95_weighted()`의 반환값이다. 판정 축이 지표 사이드카의
    `warnings`(`p95 > p95_warn_threshold`)와 같아야 장중·장후 판정이 어긋나지 않는다.
    """
    return sorted(
        (endpoint, hh, v)
        for endpoint, by_hour in grid.items()
        for hh, v in by_hour.items()
        if v > threshold
    )


def two_day_p95_overlap(today_breaches, prev_lat):
    """반환: 오늘 초과 구간 중 **직전 거래일에도 초과였던** 구간 목록. `prev_lat`이 None이면 None.

    입력: `p95_breaches()`의 반환값, 직전 거래일 사이드카의 `rest_latency` 절(dict) 또는 None.
    해석: **「판정 못 함(None)」과 「겹침 없음([])」은 다른 값이다**(규약 C) — 전자는 사이드카
         부재·파손이고 후자는 「오늘만 나쁨」이다. 직전 날의 임계는 그 사이드카에 적힌 값을
         쓴다(그날 실제로 걸려 있던 임계여야 한다 — `effective_read_timeout`과 같은 원칙).
    실패 조건: 없다 — 못 읽는 입력은 호출측이 None으로 넘긴다.
    """
    if prev_lat is None:
        return None
    prev_threshold = float(prev_lat.get("p95_warn_threshold") or P95_WARN_THRESHOLD_SECONDS)
    prev = {
        (endpoint, int(hour_str))
        for hour_str, row in (prev_lat.get("p95_by_hour") or {}).items()
        for endpoint, value in row.items()
        if value > prev_threshold
    }
    return [(ep, hh, v) for ep, hh, v in today_breaches if (ep, hh) in prev]


def revival_failure_cell(failures_for_hour: int, retry_axis_measured: bool) -> str:
    """반환: §5-1-1 「회복실패」 칸 문자열 — **0은 `0`으로 찍는다** (2026-08-25 P2-1).

    종전의 `or '—'`는 「한 번도 실패 안 했다」(좋은 소식)와 「실패를 세는 눈이 없다」(계측
    부재)를 같은 글자로 만들었다(08-25 §1-7). 재시도 줄이 하루 한 줄이라도 파싱됐으면 이
    축은 세어진 것이고 그날의 0은 진짜 0이다. 하루 0줄이면 실패 축은 시험된 적이 없으므로
    `—(계측없음)`으로 가른다(규약 C).
    """
    return str(failures_for_hour) if retry_axis_measured else "—(계측없음)"


def effective_read_timeout(root: Path) -> float:
    """반환: 그날 옵션체인 호출에 **실제로 걸려 있던** read 타임아웃(초).

    레버 `OPTION_CHAIN_READ_TIMEOUT_SECONDS`가 켜져 있으면 그 값, `None`(=OFF)이면 전역값이다.
    p50과 비교할 임계는 반드시 그날 실제 값이어야 한다 — 레버를 켠 날 전역값(4.0)으로 비교하면
    이 표가 통째로 거짓말을 한다.

    이 파일은 stdlib 전용이라 `rest_client`를 임포트하지 않고 **소스를 읽는다**(§7 레버 표와
    같은 방식). 읽지 못하면 전역값으로 폴백하고, 그 사실은 표 머리의 값으로 드러난다.
    """
    src = read_text(root / "mahdi" / "broker" / "rest_client.py")
    m = re.search(r"^OPTION_CHAIN_READ_TIMEOUT_SECONDS[^=]*=\s*([\d.]+)", src, re.M)
    return float(m.group(1)) if m else GLOBAL_READ_TIMEOUT_SECONDS


# ===== 2026-08-19 §2-3 / Fix#5 — **폐기된 진단이 다음 회차에서 되살아난다** =====
#
# 08-19 장중 두 회차가 08-18에 **이미 폐기된** 진단(먼슬리 컷 우선순위)을 P1으로 되살렸다.
# 그 회차들이 읽은 것은 08-18 보고서의 체크박스 **원문**이었고, 폐기 사실은 `NEXT_TODO.md의 「보고서 Fix#1을 폐기한다」`와
# 그날 장전 §2에 이미 적혀 있었다. **정보는 리포 안에 있었는데 경로가 닿지 않았다.**
#
# ## 같은 날 같은 보고서가 그 실패를 한 번 더 했다
#
# 08-19 장후 보고서의 §4 **Fix#1(P0)** 이 *"Slack 경보 토글을 오늘 안에 켠다"* 이다.
# 그 항목은 **2026-08-01 사용자 결정으로 보류 확정**됐고 `NEXT_TODO.md의 「Slack 알림 — 보류 유지」`이
# *"매 점검 보고서에서 다시 올리지 말 것"* 이라고 못 박아 뒀다. 자기 §2-3이 지적한 실패를
# 자기 §4가 저지른 것이다 — **이 결함은 체크박스에만 있는 것이 아니다.**
#
# ## 그래서 목록을 **그대로 인쇄한다.** 기계 판정은 넣지 않는다
#
# 보고서의 원안은 *"직전 보고서 체크박스의 Fix 번호·제목이 폐기 블록 안에 있으면
# `⛔폐기됨`을 단다"* 였다. **두 가지 이유로 그것을 넣지 않았다.**
#
# 1. **Fix 번호로 맞추면 틀린다.** NEXT_TODO의 「보고서 Fix#1을 **폐기한다**」는 **08-18**
#    보고서의 Fix#1이고, 08-19 보고서의 Fix#1은 완전히 다른 항목이다. 같은 이름이 날마다
#    다른 것을 가리킨다.
# 2. **낱말 겹침으로 맞추면 안 걸린다.** 구현 중에 실측했다 — 알려진 두 사례
#    (「데드라인 컷 경로 우선순위 보호 배선 (Fix#1)」 · 「Slack 경보 토글을 켠다」)를 폐기
#    항목 제목과 대조하니 **각각 낱말 1개**만 공유했다(임계 2). **두 사례를 다 놓치는
#    판정기다.** 임계를 1로 내리면 무관한 줄이 대량으로 걸린다.
#
# 못 잡는 판정기를 실으면 **「검사했는데 안 걸렸다」는 거짓 안심**이 남는다.
# **오늘의 Fix#1을 막을 수 있었던 것은 목록 인쇄 쪽이다** — 회차가 그 여섯 줄을 읽으면
# 「Slack 알림 — 보류 유지. 매 점검 보고서에서 다시 올리지 말 것」이 눈에 걸린다.
_NEXT_TODO_REL = ("docs", "dev_memory", "NEXT_TODO.md")
# 「다시 올리지 말 것」 목록의 절 제목. 이 문자열이 바뀌면 이 절은 **조용히 빈다** — 그래서
# 못 찾으면 그 사실 자체를 인쇄한다(규약 C: 0건과 「못 읽었다」를 가른다).
_DISCARD_SECTION_RE = re.compile(r"^##\s+폐기·종결된 안건")
# 절 밖에 흩어져 있는 개별 폐기 선언. 08-19의 `### ⚠ 보고서 Fix#1을 **폐기한다**`가 그 형태다.
_DISCARD_HEADING_RE = re.compile(r"^#{2,4}\s+.*(폐기|기각|종결)")
_DISCARD_ITEM_RE = re.compile(r"^- \[x\]\s+\*\*(.+?)\*\*")


def discarded_items(root: Path):
    """반환: `[(줄번호, 제목)]` — NEXT_TODO의 **「다시 올리지 말 것」** 항목들. 못 읽으면 `None`.

    입력: 리포 루트.
    계산: `## 폐기·종결된 안건` 절 안의 `- [x] **제목**` 과, 파일 전체의 폐기·기각·종결 헤딩.
    해석: 상세 근거는 위 절 주석. **판정하지 않는다** — 목록을 그대로 넘긴다.
    실패 조건: 파일을 못 읽으면 `None`. 빈 목록(`[]`)과 **다른 값**이다 —
         전자는 「못 읽었다」이고 후자는 「폐기된 것이 없다」이며, 조치가 다르다.
    """
    path = root.joinpath(*_NEXT_TODO_REL)
    # **`read_text()`의 반환값으로 판별하지 않는다** — 이 파일의 `read_text`는 실패 시
    # `"(읽기 실패) …"` 라는 **참인 문자열**을 준다(사람이 읽는 인용부를 위한 설계다).
    # 그것을 그대로 파싱하면 「못 읽었다」가 조용히 「0건」이 되고, 이 절이 매일 통과한다.
    if not path.is_file():
        return None
    text = read_text(path)
    if text.startswith("(읽기 실패)"):
        return None
    out, in_section = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if _DISCARD_SECTION_RE.match(line):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            in_section = False
        if in_section:
            m = _DISCARD_ITEM_RE.match(line)
            if m:
                out.append((n, m.group(1).strip()))
        elif _DISCARD_HEADING_RE.match(line):
            out.append((n, line.lstrip("# ").strip()))
    return out


# ===== 2026-08-24 (08-24 §1-1 / Fix#1) — **「전일」은 달력의 어제가 아니다** =====
#
# 08-24(월) §9가 `2026-08-23_지표.json`(토요일)을 기대해 「없음 ⚠」을 냈다. 그 파일은 앞으로도
# 영영 안 생긴다 — **토요일에는 장이 안 선다.** 월요일마다 뜨는 헛경보이고, 08-24 장전 회차가
# 그것을 이상점 1-1로 올렸다.
#
# ## 만들 것이 아니라 이어 붙일 것이다
#
# 장전 계획은 「하루씩 거슬러 올라가며 파일이 있는 첫 날을 찾는다」는 **새 부품**을 제안했다.
# 그런데 그 부품은 이미 있다 — 자동 지표 §1이 「⚠ 직전 거래일 2026-08-21 기준이다 — 그 사이
# 비거래일 2일을 건너뛰었다」를 정확히 인쇄한다(`scripts/daily_ops_report._delta_baseline_metric`).
#
# **그 로직을 복제하지 않는다.** 이 파일은 stdlib 전용이라(파일 헤더) `mahdi.ops.market_calendar`를
# 임포트할 수 없고, 달력을 여기 다시 적으면 두 곳이 조용히 갈라진다(규약 A). 대신 **파일이
# 진실원천**이라는 이 파일의 다른 규약을 쓴다(`latest_report_before` docstring과 같은 방식):
# 지표 사이드카가 **실재하는 가장 가까운 날**을 찾는다.
#
# ## 왜 5일에서 멈추는가
#
# 무한히 거슬러 오르면 **진짜 부재가 조용히 통과한다** — 지표가 일주일째 안 만들어진 날에도
# 「7일 전 것이 있으니 정상」이 된다. 연휴 최대치(추석·설 4~5일)를 덮으면서 그보다 긴 침묵은
# 사건으로 남기는 자리가 5다. 못 찾으면 **적신호를 낸다** — 이 fix는 경보를 끄는 것이 아니다.
PREV_SIDECAR_MAX_BACKTRACK_DAYS = 5


def previous_metric_sidecar(auto: Path, day: _date, max_back=PREV_SIDECAR_MAX_BACKTRACK_DAYS):
    """반환: `(찾은 날짜, 거슬러 오른 일수)` — `max_back`일 안에 없으면 `(None, max_back)`.

    입력: `auto/` 디렉터리, 오늘 날짜.
    계산: 어제부터 하루씩 거슬러 오르며 `{날짜}_지표.json`이 **실재하는** 첫 날을 고른다.
    해석: 상세 근거는 위 절 주석. 「거슬러 오른 일수 − 1」이 곧 건너뛴 비거래일 수다.
    실패 조건: 없다 — 못 찾으면 `None`이고 호출측이 그 사실을 적신호로 낸다.
    """
    for back in range(1, max_back + 1):
        candidate = day - timedelta(days=back)
        if (auto / f"{candidate.isoformat()}_지표.json").is_file():
            return candidate, back
    return None, max_back


# ===== 2026-08-24 (08-24 §3-2 / 고도화#4) — **§8-2의 거울상: 아직 안 정한 것** =====
#
# ## 오늘 무슨 일이 있었나
#
# 장중 두 회차가 「사고 싶다 336번 중 실행 경로는 23번」을 **신규 P1**으로 올렸다. 장후에
# 원인을 찾으니 `small_strangle_buy`의 |δ| 0.20~0.30이 구독 창 밖이라는 것이었고 — **그 결정은
# `NEXT_TODO.md`의 「⚠ 남은 결정 하나 — 사람이 골라야 한다」에 (a)/(b)/(c) 체크박스와 함께
# 08-18부터 엿새째 열려 있었다.** 검증 캠페인도 08-19부터 근거를 쌓고 있었다.
#
# ## 08-19와 **같은 문장, 반대 방향**이다
#
#     08-19  닫힌 것(폐기된 진단)을 새것으로 착각해 되살렸다   → §8-2가 그것을 막는다
#     08-24  열린 것(미결 결정)을 새 결함으로 착각해 P1을 올렸다 → 아무것도 안 막고 있었다
#
# 두 목록은 **같은 파일에서 뽑을 수 있다.** §8-2는 「다시 올리지 마라」를 말하고 이 절은
# 「아직 안 정했다」를 말한다.
#
# ## ⚠ 이 절은 판정을 하지 않는다
#
# §8-2와 같은 규약이다. 목록을 눈앞에 두는 것이 전부이고, 오늘 사고는 그것으로 막혔을 것이다.
# 「미체크 항목이 2개 이상」을 조건에 두는 이유: **선택지가 하나인 것은 결정이 아니라 할 일**이고,
# 그것은 §8-1의 체크박스 목록이 이미 보여 준다. 여기 실려야 하는 것은 **갈림길**이다.
#
# ## 문구 목록이 왜 이렇게 좁은가 — 실측으로 좁혔다
#
# 처음에는 제목에 `결정`만 있으면 담았다. 그러자 **닫힌 결정 둘**이 함께 딸려 왔다:
# `### Fix#6 — EGW00201 1건: 고치지 않는다(결정)`(이미 정한 것)과
# `## 2026-07-31 사용자 결정 필요`(항목이 전부 `- [x]`인 옛 절). 그 둘이 매일 실리면 이 절은
# **소음이 되고, 소음이 되면 진짜 갈림길이 그 안에 묻힌다** — 이 파일이 `MEASUREMENT_MAP`
# 주석에 적어 둔 「틀린 경보는 진짜 경보를 죽인다」가 그대로 적용되는 자리다.
#
# 그래서 **「아직 안 정했다」를 말하는 문구만** 담는다(`결정` 단독은 안 담는다). 그리고
# **최상위 체크박스만** 센다 — `- [x]` 항목 아래 딸린 미체크 하위 항목은 그 항목의 세부이지
# 갈림길이 아니다(07-31 절이 정확히 그 형태였다).
_DECISION_HEADING_RE = re.compile(
    r"^(#{2,4})\s+.*(남은 결정|결정 필요|결정 대기|미결|골라야|골라 주|고를 것|선택지|사용자 확인 대기)"
)
_OPEN_BOX_RE = re.compile(r"^- \[ \]\s+(.*)$")
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s+")
# 미체크 항목이 이 수 미만이면 「결정」이 아니라 할 일 목록으로 본다.
DECISION_MIN_OPEN_CHOICES = 2


def pending_decisions(root: Path):
    """반환: `[(줄번호, 제목, [미체크 항목])]` — **사람이 고르기로 하고 미뤄 둔 것.** 못 읽으면 `None`.

    입력: 리포 루트.
    계산: `NEXT_TODO.md`에서 제목에 결정·골라야·선택지가 있는 절을 찾아, 다음 헤딩 전까지의
         **미체크 체크박스**를 모은다. 그것이 `DECISION_MIN_OPEN_CHOICES`개 이상인 절만 남긴다.
    해석: 상세 근거는 위 절 주석. **판정하지 않는다** — 목록을 그대로 넘긴다.
    실패 조건: 파일을 못 읽으면 `None`. 빈 목록(`[]`)과 **다른 값**이다(규약 C) —
         전자는 「못 읽었다」, 후자는 「열린 결정이 없다」이며 조치가 다르다.
    """
    path = root.joinpath(*_NEXT_TODO_REL)
    # `read_text()`는 실패 시 **참인 문자열**을 준다 — `discarded_items`와 같은 함정이다.
    if not path.is_file():
        return None
    text = read_text(path)
    if text.startswith("(읽기 실패)"):
        return None
    out, current = [], None
    for n, line in enumerate(text.splitlines(), 1):
        heading = _ANY_HEADING_RE.match(line)
        if heading:
            if current and len(current[2]) >= DECISION_MIN_OPEN_CHOICES:
                out.append(current)
            decision = _DECISION_HEADING_RE.match(line)
            # 폐기·종결 헤딩은 **열린 결정이 아니다** — 그쪽은 §8-2가 이미 인쇄한다.
            if decision and not _DISCARD_HEADING_RE.match(line):
                current = (n, line.lstrip("# ").strip(), [])
            else:
                current = None
            continue
        if current is None:
            continue
        box = _OPEN_BOX_RE.match(line)
        if box:
            current[2].append((n, box.group(1).strip()))
    if current and len(current[2]) >= DECISION_MIN_OPEN_CHOICES:
        out.append(current)
    return out


# ---------------------------------------------------------------- 전일 지시 대조
# 2026-08-14 장전 §6 / 고도화 1. 상세 근거는 §8-1 절 말미의 인용.
#
# 2026-08-20 — 점검 산출물이 **하루 한 파일**(`{날짜}_마흐디_일일점검.md`)로 바뀌었다.
# **옛 이름을 함께 찾는다.** 새 이름만 찾으면 전환일 이후 이 절이 **영구히 빈다** —
# 2026-07-16~08-20의 20여 편이 옛 이름이고, 「전일 보고서」는 달력이 아니라 **파일**이
# 진실원천이기 때문이다(`latest_report_before` docstring). 옛 편을 개명해 옮기지 않는 이유는
# 그 파일들이 이미 커밋 이력·보고서 상호참조에 이름으로 박혀 있어서다 — **읽는 쪽을 넓히는
# 것이 쓰는 쪽을 고쳐 쓰는 것보다 싸다.**
_REPORT_GLOBS = ("*_마흐디_일일점검.md", "*_마흐디_운영점검보고서.md")
# 보고서 본문에 등장하는 가설 id. yaml의 `- id:` 규약(`YYYY-MM-DD-슬러그`)과 같은 모양이다.
HYPOTHESIS_ID_RE = re.compile(r"\b(20\d\d-\d\d-\d\d-[a-z0-9][a-z0-9-]*)\b")


def matched_phase_slot(phase: str, day: _date, now: datetime):
    """반환: `(예정 슬롯 'HH:MM', 밀린 분, 첫 슬롯인가)` — 지난 슬롯이 없으면 `(None, None, True)`.

    2026-08-14 고도화 3·6. 한 국면에 회차가 여럿이라(장중 12:30 / 14:30) **이번 실행이 어느
    회차인지**를 한 곳에서 정한다 — 본문의 「N분 밀렸다」 판정과 파일명이 **같은 답을 써야**
    12:30 산출물과 14:30 산출물이 어긋나지 않는다.

    과거 날짜 재집계(`--date`)에서는 슬롯을 안 고른다. 그때의 「지금」은 그날의 시각이 아니라
    다시 돌리는 시각이라, 회차를 고르면 엉뚱한 슬롯이 붙는다.
    """
    slots = PHASE_SCHEDULE.get(phase) or []
    if not slots or day != now.date():
        return None, None, True
    minute_now = now.hour * 60 + now.minute
    passed = [s for s in slots if minute_now >= hhmm_to_min(s)]
    if not passed:
        return None, None, True
    planned = passed[-1]
    return planned, minute_now - hhmm_to_min(planned), planned == slots[0]


def latest_report_before(root: Path, day: _date):
    """반환: `day`보다 **이전** 날짜의 점검 보고서 중 가장 최근 것(없으면 None).

    「전 영업일」을 달력으로 계산하지 않는 이유: 공휴일·미가동일이 있으면 그 날짜의 파일이
    아예 없고, 그때 달력 계산은 존재하지 않는 파일을 가리킨다. **파일이 진실원천이다.**

    신·구 두 이름을 함께 훑는다(`_REPORT_GLOBS`). **같은 날짜에 둘 다 있으면 새 이름이
    이긴다** — 전환일 하루는 두 파일이 공존하고, 그날 「전일 보고서」로 읽어야 할 것은
    종합 완성본인 새 파일이다. 파일 경로로 타이브레이크하면 정렬 순서에 답이 끌려간다.
    """
    found = []
    base = root / "docs" / "동작점검"
    for rank, pattern in enumerate(_REPORT_GLOBS):  # rank 0 = 새 이름 = 우선
        for p in base.glob(pattern):
            try:
                when = _date.fromisoformat(p.name[:10])
            except ValueError:
                continue
            if when < day:
                found.append((when, -rank, p))
    return max(found)[2] if found else None


def report_hypothesis_ids(text: str):
    """반환: 보고서 본문이 언급한 가설 id 집합."""
    return set(HYPOTHESIS_ID_RE.findall(text))


# ===== 2026-08-24 (08-24 §1-2 / Fix#2) — **이름이 바뀐 것을 「없어진 것」으로 세지 않는다** =====
#
# 08-24 장전 §8-1이 「전일 보고서가 언급했는데 yaml에 없는 id **13개**」를 적신호로 올렸다.
# 그중 실제 부재는 **0건**이었다 — 08-23 세션이 가설을 등재하면서 슬러그를 바꿨을 뿐이다:
#
#     보고서: 2026-08-23-fix1-...      yaml: 2026-08-23-fix5-...
#
# 종전 대조는 「완전일치 또는 `id + '-'` 접두」였다. 접두 규약은 **보고서가 짧게 부르는 경우**
# (`2026-08-12-g1` → `...-g1-reconnect-as-cost`)를 위한 것이라 **꼬리가 같고 머리가 다른**
# 이 형태를 못 잡는다.
#
# ## 슬러그 일치는 「후보」로만 인쇄하고 **판정하지 않는다**
#
# 서로 다른 두 가설이 같은 슬러그를 가질 수 있다(`...-fix3-parser-blind`가 두 날에 있으면
# 둘 다 `parser-blind`다). 그래서 **완전일치·접두 일치가 먼저**이고, 슬러그 일치는
# 「개명 후보」로 인쇄하되 §12 적신호에서는 뺀다 — 08-19 Fix#5가 「못 잡는 판정기를 실으면
# 거짓 안심이 남는다」고 적은 것의 반대 방향 조심이다: **틀리게 이어 붙이는 판정기**도 같다.
_HYPOTHESIS_DATE_PREFIX_RE = re.compile(r"^20\d\d-\d\d-\d\d-")
_HYPOTHESIS_FIX_PREFIX_RE = re.compile(r"^(?:fix|p|g|adv|e|c|wiring)\d*-")


def hypothesis_slug(hid: str) -> str:
    """반환: 날짜와 `fixN`류 접두를 뗀 **꼬리**. 개명을 따라가는 유일한 축이다.

    입력: 가설 id(`2026-08-23-fix1-broker-knows-what-we-hold`).
    계산: 앞의 `YYYY-MM-DD-`를 떼고, 이어지는 `fix1-`/`p2-`/`g1-`/`wiring2-` 한 마디를 뗀다.
    해석: 상세 근거는 위 절 주석. **판정용이 아니라 후보 제시용**이다.
    실패 조건: 없다 — 형태가 다르면 뗄 것을 못 떼고 원문에 가까운 값을 낸다.
    """
    rest = _HYPOTHESIS_DATE_PREFIX_RE.sub("", str(hid))
    return _HYPOTHESIS_FIX_PREFIX_RE.sub("", rest)


def rename_candidates(missing, known):
    """반환: `{보고서 id: [같은 슬러그를 가진 yaml id]}` — 없으면 그 키는 안 생긴다.

    입력: 완전일치·접두 일치에 실패한 id들, yaml에 실재하는 id 집합.
    계산: 슬러그가 같은 것을 모은다.
    실패 조건: 없다.
    """
    by_slug = {}
    for k in known:
        by_slug.setdefault(hypothesis_slug(k), []).append(k)
    out = {}
    for i in missing:
        hit = sorted(by_slug.get(hypothesis_slug(i)) or [])
        if hit:
            out[i] = hit
    return out


# 2026-08-24 Fix#2 변경 B — **「미등재」로 올리기 전에 §8-2 폐기 목록을 먼저 본다.**
#
# 08-19 Fix#5의 §8-2는 목록을 **눈앞에 놓기만** 했다. 그것으로 사람이 읽으면 막히지만,
# **자동 적신호는 여전히 그 항목을 「미등재」로 올린다** — 그리고 적신호는 사람이 읽기 전에
# 먼저 눈에 띈다. 폐기된 안건의 id가 적신호에 매일 실리면 그것이 곧 「재등재하라」는 지시로
# 읽힌다(08-19에 실제로 그렇게 읽혔다).
#
# **낱말 겹침으로 맞추지 않는다** — `discarded_items` 위 절 주석이 그 판정기를 실측으로
# 기각했다(알려진 두 사례가 각각 낱말 1개만 공유). 여기서는 **id 문자열 자체**가 폐기 블록
# 안에 있는지만 본다. 없으면 아무 말도 안 한다 — 못 잡는 것보다 **틀리게 잡는 것**이 나쁘다.
def discarded_hypothesis_ids(root: Path, ids):
    """반환: `{id: (줄번호, 그 줄)}` — 폐기·종결 블록 **안에서** 그 id가 언급된 것만.

    입력: 리포 루트, 확인할 id들.
    계산: `NEXT_TODO.md`를 훑어 폐기 절/헤딩 안에 있는 줄에서 id를 **문자열 그대로** 찾는다.
    해석: 상세 근거는 위 절 주석. **판정하지 않는다** — 인쇄하고 적신호에서만 뺀다.
    실패 조건: 파일을 못 읽으면 `None`(= 「대조하지 못했다」, 빈 dict와 다르다).
    """
    path = root.joinpath(*_NEXT_TODO_REL)
    if not path.is_file():
        return None
    text = read_text(path)
    if text.startswith("(읽기 실패)"):
        return None
    wanted = list(ids)
    out, in_section = {}, False
    for n, line in enumerate(text.splitlines(), 1):
        if _DISCARD_SECTION_RE.match(line):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            in_section = False
        if _DISCARD_HEADING_RE.match(line):
            # 헤딩 자체가 폐기 선언이면 그 아래 블록도 폐기 맥락이다 — 다음 헤딩까지 유지한다.
            in_section = True
            continue
        if not in_section:
            continue
        for i in wanted:
            if i in line and i not in out:
                out[i] = (n, truncate(line.strip(), 110))
    return out


# ===== 2026-08-23 (08-21 §1-3 / §4 Fix#6) — 크래시 판정을 **표식**으로 바꾼다 =====
#
# ## 이틀 연속 여덟 번 헛것을 가리켰다
#
# 종전 판정은 `observation_loop_crash.log`의 **mtime이 오늘인가** 하나였다. 그런데 08-19부터
# `start_mahdi_premarket.bat`이 **기동할 때마다** 이 파일에 표식 한 줄을 append한다:
#
#     echo [%date% %time%] ===== 관측 루프 기동 ===== >> logs\observation_loop_crash.log
#
# 즉 **정상 기동만으로 mtime이 오늘이 된다.** 08-20·08-21 두 날 네 회차씩 **여덟 번** 이
# 적신호가 떴고 여덟 번 다 크래시는 0건이었다. 08-20 §1-4가 이미 지적한 그대로다.
#
# ## 왜 이 파일에 파서를 또 쓰는가 — `mahdi/ops/crash_metrics.py`가 이미 있는데
#
# 이 스크립트는 **stdlib 전용**이다(파일 상단 docstring). 그 규약이 있어서 이 도구는 venv가
# 깨진 날에도 돌고, 실제로 그것이 여러 번 유일한 증거원이었다. 그래서 같은 문법을 여기에도
# 적는다 — 대신 **문구 상수를 공유하지 않는다는 사실 자체가 위험**이므로, 표식을 못 찾으면
# 옛 방식으로 물러서고 **그 사실을 한 줄 인쇄한다**(조용한 실패 금지, 규약 C).
_CRASH_START_MARKER_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})(?:[.,]\d+)?\]\s*=+\s*관측 루프 기동"
)
_CRASH_TRACEBACK_HEAD_RE = re.compile(r"^Traceback \(most recent call last\):")


def crash_since_last_start(lines, day):
    """반환: 그날 **마지막 기동 표식 이후**의 `{"at", "traceback", "count"}`. 표식이 없으면 None.

    입력: 크래시 로그의 줄들(여러 날치가 섞여 있어도 된다), 대상 날짜.
    계산: 뒤에서부터 그날의 마지막 표식을 찾고, 그 **뒤**의 줄만 본다.
         `count`는 트레이스백 머리 줄 수 — 예외 연쇄(`During handling of ...`)가 있으면
         실제 죽음보다 많이 세어지므로 **상한**이다(`crash_metrics`와 같은 규약).
    실패 조건: 없다. 그날 표식이 하나도 없으면 None을 내고 호출측이 옛 방식으로 물러선다.
    """
    last = None
    for index, line in enumerate(lines):
        m = _CRASH_START_MARKER_RE.match(line)
        if m and m.group(1) == day.isoformat():
            last = (index, f"{int(m.group(2)):02d}:{m.group(3)}:{m.group(4)}")
    if last is None:
        return None
    index, at = last
    # **다음 표식에서 끊는다.** 안 끊으면 과거 날짜를 조회할 때 그 뒤 날들의 본문이 통째로
    # 딸려 들어와 「그날 크래시」가 부풀려진다(도입 직후 08-20을 조회해 실제로 확인했다).
    body = []
    for line in lines[index + 1:]:
        if _CRASH_START_MARKER_RE.match(line):
            break
        if line.strip():
            body.append(line)
    return {
        "at": at,
        "traceback": body,
        # 콘솔이 남긴 `^C`가 줄 앞에 붙어 있을 수 있다 — 08-19 로그의 세 트레이스백이 전부 그랬다.
        "count": sum(1 for ln in body if _CRASH_TRACEBACK_HEAD_RE.match(ln.lstrip("^C"))),
    }


def lever_schedule(root: Path):
    """반환: `{레버 이름: {"유예횟수": n, "무조건발동일": "YYYY-MM-DD", "발동일": ...}}`.

    2026-08-14 고도화 5. YAML 파서 없이 필드만 긁는다(`due_hypotheses`와 같은 이유 —
    이 파일은 stdlib 전용이다). 한 레버를 여러 가설이 물고 있으면 **가장 이른 기한**이 이긴다:
    누구 하나라도 그 날짜에 켜져야 한다고 적었으면 그날이 마지노선이다.
    """
    p = root / "docs" / "동작점검" / "hypotheses.yaml"
    if not p.exists():
        return {}
    out, cur = {}, {}
    for raw in read_text(p).splitlines():
        if raw.startswith("- id:"):
            cur = {}
            continue
        m = re.match(r"^  (전제레버|발동일|무조건발동일|유예횟수):\s*(.*)$", raw)
        if not m:
            continue
        cur[m.group(1)] = m.group(2).strip().strip('"')
        lever = cur.get("전제레버")
        if not lever:
            continue
        slot = out.setdefault(lever, {})
        for field in ("발동일", "무조건발동일"):
            if cur.get(field) and (not slot.get(field) or cur[field] < slot[field]):
                slot[field] = cur[field]
        if cur.get("유예횟수"):
            slot["유예횟수"] = cur["유예횟수"]
    return out


def registered_hypothesis_ids(root: Path):
    """반환: `hypotheses.yaml`에 실제로 등재된 id 집합(상태 무관)."""
    p = root / "docs" / "동작점검" / "hypotheses.yaml"
    if not p.exists():
        return set()
    return {
        ln.split("id:", 1)[1].strip().strip('"')
        for ln in read_text(p).splitlines() if ln.startswith("- id:")
    }


# ---------------------------------------------------------------- 가설
def due_hypotheses(root: Path, day: _date):
    """`검증예정일 <= 오늘` 인 pending 항목. YAML 파서 없이 필드만 긁는다.

    자동 리포트 §0이 대조를 해 주지만 그것은 **장후에만** 나온다. 장전 점검이
    「오늘 무엇을 판정해야 하는가」를 물으려면 이 목록이 그 시점에 있어야 한다.
    """
    p = root / "docs" / "동작점검" / "hypotheses.yaml"
    if not p.exists():
        return None, []
    items, cur = [], None
    for raw in read_text(p).splitlines():
        if raw.startswith("- id:"):
            if cur:
                items.append(cur)
            cur = {"id": raw.split("id:", 1)[1].strip().strip('"')}
            continue
        if cur is None:
            continue
        m = re.match(r"^  (검증예정일|상태|가설|전제레버|구현일):\s*(.*)$", raw)
        if m:
            cur[m.group(1)] = m.group(2).strip().strip('"')
    if cur:
        items.append(cur)

    due = []
    for it in items:
        if it.get("상태") != "pending":
            continue
        d = it.get("검증예정일", "")
        try:
            when = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            due.append((it, "날짜 형식 불명"))
            continue
        if when <= day:
            due.append((it, "도래" if when == day else f"**{(day - when).days}일 지남**"))
    return p, due


# ---------------------------------------------------------------- 본문
def build(root: Path, day: _date, phase: str, cfg_phases) -> str:
    D = day.isoformat()
    logs = root / "logs"
    auto = root / "docs" / "동작점검" / "auto"
    now = datetime.now(KST)
    L, A = [], None
    A = L.append
    flags = []

    def due(hhmm):
        """그 시각이 이미 지났는가. **아직 안 온 일을 「없다」고 신고하지 않기 위한 것**이다
        — 07:19에 도는 장전 점검이 07:30 기동을 결손으로 보고하면 그 보고는 매일 틀린다."""
        if day != now.date():
            return True
        return now.hour * 60 + now.minute >= hhmm_to_min(hhmm)

    A(f"# 마흐디 증거 다이제스트 — {D} / {phase.upper()}")
    A("")
    A(f"- 생성 {now:%Y-%m-%d %H:%M:%S} KST · 리포 `{root}`")
    A(f"- 점검 범위: {', '.join(cfg_phases)} (장전=pre / 장중=intra / 장후=post)")
    # 2026-08-14 고도화 5·6 — **밀린 실행인지 기계가 답한다.** 보고서 첫 줄의
    # 「실행 08:35 · 예정 08:30 — 정시 실행」은 지금까지 사람이 손으로 적던 것이다.
    slots = PHASE_SCHEDULE.get(phase) or []
    if slots and day == now.date():
        planned, late, _first = matched_phase_slot(phase, day, now)
        if planned is not None:
            if late <= PHASE_LATE_TOLERANCE_MIN:
                verdict = "**정시 실행**"
            elif late > PHASE_RERUN_MIN:
                # **늦은 점검과 사후 재집계를 가른다.** 저녁에 그날 것을 다시 돌리는 일은
                # 흔하고(이 파일의 `--date`가 그 용도다), 그때마다 ⚠가 뜨면 진짜로 밀린 날을
                # 못 알아본다 — 거짓 경보가 진짜 경보를 죽이는 그 형태다.
                verdict = f"{late}분 뒤 실행 — **사후 재집계로 본다**(점검 지연 아님)"
            else:
                verdict = f"**{late}분 밀렸다 ⚠**"
            A(f"- 회차: 예정 {planned} · 실행 {now:%H:%M} — {verdict}"
              f" (이 국면의 예정 회차: {', '.join(slots)})")
            if PHASE_LATE_TOLERANCE_MIN < late <= PHASE_RERUN_MIN:
                flags.append(
                    f"{phase} 점검이 예정({planned})보다 **{late}분 밀렸다** — "
                    "장전이 밀리면 개장 전 조치 시간이, 장중이 밀리면 개입 창이 그만큼 준다"
                )
        else:
            A(f"- 회차: 이 국면의 첫 예정 시각({slots[0]}) 이전에 실행됐다 "
              f"(예정 회차: {', '.join(slots)})")
    elif slots:
        A(f"- 회차: 과거 날짜 재집계 (이 국면의 예정 회차: {', '.join(slots)})")
    A("- 이 파일은 **요약이지 원본이 아니다.** 걸린 지점만 원본으로 되짚을 것.")
    A("")

    # ---- 1. 코드·커밋 ----
    A("## 1. 코드·커밋 상태")
    A("")
    head = run_git(root, ["rev-parse", "--short", "HEAD"])
    branch = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    dirty = [x for x in run_git(root, ["status", "--porcelain"]).splitlines() if x.strip()]
    A(f"- HEAD `{head}` · 브랜치 `{branch}` · 미커밋 **{len(dirty)}건**")
    if dirty:
        A("```")
        L.extend(dirty[:40])
        if len(dirty) > 40:
            A(f"… 외 {len(dirty) - 40}건")
        A("```")
    nxt = (day + timedelta(days=1)).isoformat()
    todays = run_git(root, ["log", "--oneline", "--no-decorate", f"--since={D} 00:00", f"--until={nxt} 00:00"])
    A("")
    A(f"**당일({D}) 커밋**")
    A("```")
    A(todays if todays.strip() else "(당일 커밋 없음)")
    A("```")
    bad_prefix = [x for x in todays.splitlines() if x.strip() and COMMIT_PREFIX not in x]
    if bad_prefix:
        flags.append(f"당일 커밋 {len(bad_prefix)}건에 `{COMMIT_PREFIX}` 접두어가 없다")
    A("")
    A("**직전 커밋 10건**")
    A("```")
    A(run_git(root, ["log", "--oneline", "--no-decorate", "-10"]))
    A("```")
    A("")
    A("> 당일 커밋 시각이 관측 루프 기동 시각보다 **늦으면** 그 fix는 오늘 프로세스에 실려 있지 않다")
    A("> — 가설 상태는 `refuted`가 아니라 `untested`다(2026-08-04 p4: 15분 차이로 하루를 잃었다).")
    A("")

    # ---- 2. 기동·종료 시퀀스 ----
    A("## 2. 기동·종료 시퀀스")
    A("")
    pre_lines = bracket_day_lines(logs / "premarket_startup.log", day)
    A(f"### `premarket_startup.log` — 당일 {len(pre_lines)}행")
    A("")
    if pre_lines:
        A("```")
        L.extend(pre_lines[:40])
        if len(pre_lines) > 40:
            A(f"… 외 {len(pre_lines) - 40}행")
        A("```")
    else:
        A("**당일 라인 없음** — 장전 배치가 안 돌았거나, 아직 기동 시각 전이다.")
        if due("07:35"):
            flags.append("premarket_startup.log 에 당일 라인이 없다 — 장전 배치 기동 여부 확인 필요")
    A("")
    for name in (".last_successful_start.txt", ".last_cockpit_start.txt", ".last_marketclose_stop.txt"):
        p = logs / name
        A(f"- `{name}`: {truncate(read_text(p), 80) if p.exists() else '**없음**'}")
    A("")

    # ---- 3. 관측 루프 ----
    scan = LoopScan()
    for line in iter_day_lines(logs, day):
        scan.feed(line)

    A("## 3. 관측 루프 — 생사와 뼈대")
    A("")
    if not scan.records:
        A("**당일 레코드 0행** — 관측 루프가 안 돌았거나, 로그가 로테이션으로 밀려났거나, 기동 전이다.")
        if due("07:35"):
            flags.append("observation_loop 로그에 당일 레코드가 없다 — 관측 루프 생존 확인 필요")
    else:
        A(f"- 레코드 **{scan.records}행** · 트레이스백 등 딸린 줄 {scan.traceback_lines}행")
        A(f"- 최초 {scan.first[0]} `{scan.first[1]}`")
        A(f"- 최종 {scan.last[0]} `{scan.last[1]}`")
        A(f"- 사이클(`{CYCLE_TOKEN}`)이 관측된 분: **{len(scan.cycle_minutes)}분**")
        A("")
        A(f"**프로세스 기동 {len(scan.process_starts)}회** (`{PROCESS_START_MARKER}` 표식)")
        A("")
        if scan.process_starts:
            A("```")
            for t, m in scan.process_starts[:8]:
                A(f"{t} {m}")
            A("```")
        else:
            A("(기동 표식 없음 — 전일 기동이 이어지고 있거나 표식 이전 버전이다)")
        if len(scan.process_starts) > 1:
            flags.append(
                f"관측 루프 기동 {len(scan.process_starts)}회 "
                f"({', '.join(t for t, _ in scan.process_starts[:5])}) — 재기동 사유와 그 사이 공백을 추적할 것"
            )
        A("")
        A("### 앵커 (±%d분 안에 로그가 있는가)" % ANCHOR_WINDOW_MIN)
        A("")
        A("| 시각 | 있어야 할 일 | 창 내 관측 분 |")
        A("|---|---|---|")
        for at, label, hits in scan.anchor_hits(cfg_phases):
            A(f"| {at} | {label}{'' if hits else ' ⚠'} | {hits} |")
        A("")
        gaps = scan.gaps()
        A(f"**{GAP_SCAN[0]}~{GAP_SCAN[1]} 구간 {GAP_THRESHOLD_MIN}분 이상 공백: {len(gaps)}건**")
        if gaps:
            A("")
            A("| 시작 | 재개 | 공백(분) |")
            A("|---|---|---|")
            mo_lo, mo_hi = hhmm_to_min(MARKET_HOURS[0]), hhmm_to_min(MARKET_HOURS[1])
            for a, b, g in gaps[:20]:
                intra = mo_lo <= b and a <= mo_hi
                A(f"| {m2hhmm(a)} | {m2hhmm(b)} | {g}{' **정규장**' if intra else ''} |")
            for a, b, g in gaps:
                if (mo_lo <= b and a <= mo_hi) or g >= GAP_THRESHOLD_MIN * 3:
                    flags.append(f"관측 공백 {m2hhmm(a)}~{m2hhmm(b)} ({g}분)")
        A("")
        A("> 공백은 **「안 일어났다」가 아니라 「관측되지 않았다」**이다. 09:00 이전·15:20 이후 공백은")
        A("> 정상일 수 있지만, 정규장 안의 공백은 그 자체가 이상점 후보다.")
        A("")

    # ---- 4. 레벨·로거·경고 ----
    A("## 4. 레벨·로거 집계와 경고 분포")
    A("")
    if scan.records:
        A("- 레벨: " + ", ".join(f"{k}={v}" for k, v in scan.levels.most_common()))
        A("- 로거 상위: " + ", ".join(f"`{k}`×{v}" for k, v in scan.loggers.most_common(10)))
        A("")
        n_sev = len(scan.severe)
        A(f"### ERROR 이상 — **{n_sev}건**")
        A("")
        if scan.severe:
            # 같은 사건의 반복은 한 줄로 묶는다 — 같은 문장이 11번 실리면 그 아래 다른 ERROR가
            # 안 보인다. **묶되 건수와 최초/최종은 남긴다**(반복 자체가 정보다).
            groups = collections.OrderedDict()
            for t, lv, lg, msg in scan.severe:
                groups.setdefault(normalize(msg), []).append((t, lv, lg, msg))
            A("| 건수 | 최초 | 최종 | level | 대표 메시지 |")
            A("|---|---|---|---|---|")
            for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                A(f"| {len(items)} | {items[0][0]} | {items[-1][0]} | {items[0][1]} | {truncate(items[0][3], 160)} |")
            A("")
            A("```")
            for key, items in list(sorted(groups.items(), key=lambda kv: -len(kv[1])))[:5]:
                t, lv, lg, msg = items[0]
                A(f"{t} [{lv}] {lg}: {msg}")
            A("```")
            flags.append(f"ERROR 이상 {n_sev}건 / {len(groups)}종")
        else:
            A("(없음)")
        A("")
        A(f"### WARNING — 정규화 {len(scan.warn_norm)}종 / 총 {sum(scan.warn_norm.values())}건")
        A("")
        A("| 건수 | 최초 | 최종 | 메시지(숫자 → N) |")
        A("|---|---|---|---|")
        for key, cnt in scan.warn_norm.most_common(20):
            A(f"| {cnt} | {scan.warn_first[key]} | {scan.warn_last[key]} | {key} |")
        A("")

    # ---- 5. 항상 인용하는 사건 ----
    A("## 5. 항상 인용하는 사건")
    A("")
    if scan.quoted:
        for key, items in scan.quoted.items():
            times = [t for t, _, _ in items]
            hours = collections.Counter(t[:2] for t in times)
            A(f"### `{key}` ×{len(items)} — 시간대 " + ", ".join(f"{h}시:{c}" for h, c in sorted(hours.items())))
            A("")
            A("```")
            for t, lv, msg in items[:QUOTE_SAMPLES]:
                A(f"{t} [{lv}] {msg}")
            if len(items) > QUOTE_SAMPLES:
                A(f"… 외 {len(items) - QUOTE_SAMPLES}건")
            A("```")
            A("")
    else:
        A("(해당 사건 없음 — 그러나 **건수 0은 두 가지다**: 진짜 없었거나, 계측이 없거나.)")
        A("")
    ws = scan.quoted.get("ws_disconnect") or []
    if len(ws) >= 5:
        flags.append(f"WS 단절 {len(ws)}회 — 재연결 1회당 재구독·관측 공백이 따라온다(2026-08-12 §2-1 사슬)")

    # ---- 5-1. 수집 소요와 KIS 응답시간 (2026-08-14 Fix#3) ----
    A("## 5-1. 수집 소요 × KIS 응답시간 — 절벽의 선행 지표")
    A("")
    if not scan.cycle_rest:
        A("(사이클 소요 줄 없음 — 관측 루프가 안 돌았거나 로그 형식이 다르다)")
        A("")
    else:
        timeout = effective_read_timeout(root)
        A(f"수집 예산 **{CHAIN_COLLECT_BUDGET_SECONDS:.0f}초** · "
          f"`{CHAIN_ENDPOINT}` read timeout **{timeout:.1f}초**")
        A("")
        # ===== 2026-08-19 §2-5 / Fix#7 — **시간대 표는 절벽을 눌러 없앤다** =====
        #
        # 08-19 13시 행은 「창 최대 1.01 ⛔」 한 줄인데 실제로는 `13:01` 0.84 → `13:06` **1.01**
        # → `13:11` 0.69로 **6분 만에 오르내렸다.** 14:30 회차가 그 시계열을 손으로 다시
        # 파싱해야 했다. 최초 돌파 창을 한 줄로 인쇄한다 — **최대는 「얼마나 나빴나」이고
        # 최초는 「언제부터 나빴나」다.** 08-14에 위험선 최초 돌파(14:06)와 전멸 시작(14:00)
        # 사이가 그 하루의 전부였다.
        warn_at = scan.first_latency_breach(timeout, P50_TIMEOUT_WARN_RATIO)
        danger_at = scan.first_latency_breach(timeout, 1.0)
        windows = scan.window_latency_p50()
        if windows:
            def _breach(label, hit, mark):
                if hit is None:
                    return f"- {label} 최초 돌파: **없음**"
                at, n, p50, ratio = hit
                return f"- {label} 최초 돌파: **{at[:5]}** — p50 {p50:.2f}초 / {n}건 (비율 **{ratio:.2f}**) {mark}"
            A(_breach(f"경고선(비율 {P50_TIMEOUT_WARN_RATIO:.2f})", warn_at, "⚠"))
            A(_breach("위험선(비율 1.00)", danger_at, "⛔"))
            # 2026-08-23 고도화#1 — **검열 비율을 함께 낸다.** 상세 근거는
            # `P50_CENSORED_FLOOR_RATIO` 위 주석.
            censored = scan.window_censored_counts(timeout)
            floored = [
                (at, n, p50) for at, n, p50 in windows
                if timeout and p50 >= timeout * P50_CENSORED_FLOOR_RATIO
            ]
            tsv = write_latency_windows(
                root, day, windows, timeout, censored if scan.censored_seconds else None
            )
            A(f"- 전체 **{len(windows)}창**: "
              + (f"`auto/{D}_지연창.tsv`" if tsv else "⚠ 파일로 못 뺐다(본문만 유효)"))
            if floored:
                cut_total = sum(censored.get(at, 0) for at, _n, _p in floored)
                call_total = sum(n for _at, n, _p in floored)
                pct = f"{cut_total / call_total * 100:.0f}%" if call_total else "—"
                A(f"- ⛔ **p50이 타임아웃에 눌린 창 {len(floored)}개** — 그 창의 p50은 중앙값이 "
                  f"아니라 **`≥{timeout:.1f}초`**(하한)다"
                  + (f", 그 창들의 검열 비율 **{pct}**({cut_total}/{call_total}건)."
                     if scan.censored_seconds else ", 검열 건수는 **못 셌다**(느린 호출 줄 0건)."))
                A(f"  `≥`가 붙은 값으로는 **「제한시간을 {timeout:.0f}초에서 늘리면 몇 %가 더 "
                  "들어오는가」를 계산할 수 없다** — 잘린 호출의 실제 소요를 이 통로는 모른다. "
                  "**레버보다 계측이 먼저다**(08-21 사용자 조치 7).")
                flags.append(
                    f"`{CHAIN_ENDPOINT}` p50이 타임아웃에 검열된 창 {len(floored)}개 — "
                    f"그 값들을 실제 응답시간으로 읽지 말 것(하한이다)"
                )
            else:
                A(f"- ✅ p50이 타임아웃의 {P50_CENSORED_FLOOR_RATIO}배에 닿은 창 없음 — "
                  "오늘 p50은 검열되지 않았다(실제 중앙값으로 읽어도 된다).")
            # ===== 2026-08-25 (08-25 §1-8 / P1-2) — 하루 검열 건수를 **항상** 낸다 =====
            #
            # 08-25에 `window_censored_counts()`가 186건을 다 셌고 TSV에도 썼는데, 본문은
            # `floored`(p50이 눌린 창)가 비어서 「✅ 검열되지 않았다」만 인쇄했다 — p50이 멀쩡한
            # 날에도 **꼬리는 잘리고 있었다.** 위 `floored` 분기는 그대로 둔다(그것은 「p50 값
            # 자체를 하한으로 읽으라」는 다른 경고다).
            if scan.censored_seconds:
                day_censored = sum(1 for _at, http in scan.censored_seconds if http >= timeout)
                call_total_all = sum(n for _at, n, _p in windows)
                hit_windows = sum(1 for at, _n, _p in windows if censored.get(at))
                pct = f"{day_censored / call_total_all * 100:.2f}%" if call_total_all else "—"
                A(f"- 하루 검열(타임아웃에 잘린 호출) **{day_censored}건 / {call_total_all}건({pct})** · "
                  f"검열 창 {hit_windows}/{len(windows)} — **p50 상태와 무관하게 항상 인쇄한다.** "
                  "p50이 안전해도 이 값이 커지면 꼬리부터 막히는 중이다(20종목 순차 수집에서는 "
                  "「절반이 느려진다」보다 「5%가 완전히 막힌다」가 먼저 온다).")
            else:
                A("- 하루 검열 건수: **안 셌다**(느린 호출 줄 0건) — 「0%」가 아니다(규약 C).")
            A("")
        # 2026-08-25 P1-1 ② — 「p95 최대」 열. — 칸은 「그 시간대 p95 계측 없음」이지 0이 아니다(규약 C).
        A("| 시간대 | 사이클 | rows=0 | REST수집 평균(초) | 예산 대비 | p50 평균 | 창 최대 p50 | 최대/timeout | p95 최대 | |")
        A("|---|---|---|---|---|---|---|---|---|---|")
        for hh in sorted(scan.cycle_rest):
            rests = scan.cycle_rest[hh]
            rows = scan.cycle_rows[hh]
            mean = sum(rests) / len(rests)
            budget_ratio = mean / CHAIN_COLLECT_BUDGET_SECONDS
            p50, p50_max = scan.hourly_latency_p50(hh)
            p95_max = scan.hourly_latency_p95_max(hh)
            # **판정은 창 최대로 한다** — 근거는 `hourly_latency_p50` docstring.
            ratio = (p50_max / timeout) if (p50_max is not None and timeout) else None
            mark = ""
            if ratio is not None and ratio >= 1.0:
                mark = "⛔"
            elif (ratio is not None and ratio >= P50_TIMEOUT_WARN_RATIO) or budget_ratio >= BUDGET_WARN_RATIO:
                mark = "⚠"
            # 2026-08-19 §2-5 — **그 시간대에 재기동이 있었으면 숫자가 오염돼 있다.**
            # 08-19 10시가 그랬는데 표만 보면 알 수 없었다(10:36 재기동).
            restarts = [t[:5] for t, _m in scan.process_starts if t[:2] == f"{hh:02d}"]
            hour_label = f"{hh:02d}시" + ("".join(f" ⟳{t}" for t in restarts) if restarts else "")
            p95_mark = "⚠" if (p95_max is not None and p95_max > P95_WARN_THRESHOLD_SECONDS) else ""
            A(f"| {hour_label} | {len(rests)} | {sum(1 for r in rows if r == 0)} | {mean:.1f} | "
              f"{budget_ratio * 100:.0f}% | {'—' if p50 is None else f'{p50:.2f}'} | "
              f"{'—' if p50_max is None else f'{p50_max:.2f}'} | "
              f"{'—' if ratio is None else f'{ratio:.2f}'} | "
              f"{'—' if p95_max is None else f'{p95_max:.2f}{p95_mark}'} | {mark or '—'} |")
            if ratio is not None and ratio >= 1.0:
                flags.append(
                    f"{hh}시 `{CHAIN_ENDPOINT}` 창 최대 p50 {p50_max:.2f}초가 read timeout "
                    f"{timeout:.1f}초를 **넘었다**(비율 {ratio:.2f}) — 중앙값 호출이 타임아웃되면 "
                    "수집은 느려지는 것이 아니라 **비어 버린다**"
                )
            elif ratio is not None and ratio >= P50_TIMEOUT_WARN_RATIO:
                flags.append(
                    f"{hh}시 창 최대 p50/timeout {ratio:.2f} — 경고선({P50_TIMEOUT_WARN_RATIO}) 초과. "
                    "중앙값 호출이 타임아웃 한 뼘 앞이다(08-14는 0.77에서 24분 뒤 절벽이 왔다)"
                )
            if budget_ratio >= BUDGET_WARN_RATIO:
                flags.append(
                    f"{hh}시 REST수집 평균 {mean:.1f}초 = 예산의 {budget_ratio * 100:.0f}% "
                    f"(경고선 {BUDGET_WARN_RATIO * 100:.0f}%)"
                )
        A("")
        # ===== 2026-08-25 (08-25 §1-11 / P1-1 ③·④ · 고도화 1) — p95 임계와 「이틀 연속」을 장중에 =====
        #
        # 08-25에 이 판정 전부(임계 2.5초 · 초과 목록 · 이틀 연속 6구간 · 미리 정해 둔 조치)를
        # `daily_ops_report`가 인쇄하고 있었는데 그 파일은 15:46에 생긴다. 장중 회차가 읽는 것은
        # 이 절이고, 여기엔 p50만 있었다 — **없던 것은 계측이 아니라 경로다.**
        p95_grid = scan.hourly_p95_weighted()
        if not p95_grid:
            A("- p95 판정: **계측 없음** — `REST 응답시간` 줄이 없거나 형식이 다르다. "
              "**「p95=0」이 아니다**(규약 C).")
        else:
            today_breaches = p95_breaches(p95_grid)
            if today_breaches:
                A(f"- ⚠ p95(호출 수 가중) > **{P95_WARN_THRESHOLD_SECONDS}초** 구간 "
                  f"**{len(today_breaches)}개**: "
                  + " · ".join(f"{hh:02d}시 `{ep}` {v:.2f}초" for ep, hh, v in today_breaches)
                  + " — 임계는 장후 지표와 같은 값이다(`log_metrics.REST_LATENCY_P95_WARN_SECONDS`).")
            else:
                A(f"- ✅ p95(호출 수 가중) > {P95_WARN_THRESHOLD_SECONDS}초 구간 없음 — "
                  "오늘 꼬리(느린 쪽 5%)는 임계 아래다.")
            # ④ 「이틀 연속」 — 새 입력을 만들지 않는다: 직전 거래일 지표 사이드카를 재사용한다.
            prev51_day, _prev51_back = previous_metric_sidecar(auto, day)
            prev51_lat = None
            if prev51_day is not None:
                try:
                    prev51_lat = (json.loads(
                        (auto / f"{prev51_day.isoformat()}_지표.json").read_text(encoding="utf-8")
                    ) or {}).get("rest_latency") or {}
                except Exception:
                    prev51_lat = None
            if prev51_day is None or prev51_lat is None:
                A(f"- 이틀 연속 판정: **못 한다** — 직전 거래일 지표 사이드카를 "
                  f"{'최근 %d일 안에 못 찾았다' % PREV_SIDECAR_MAX_BACKTRACK_DAYS if prev51_day is None else '못 읽었다'}. "
                  "**「이틀 연속 아님」이 아니다**(규약 C).")
            else:
                both = two_day_p95_overlap(today_breaches, prev51_lat)
                if both:
                    joined = " · ".join(f"{hh:02d}시 `{ep}` {v:.2f}초" for ep, hh, v in both)
                    A(f"- 🔔 **사전 대응 규칙 발동 조건 성립 — 규칙 `{P95_TWO_DAY_RULE_ID}`** · "
                      f"이틀 연속(직전 거래일 {prev51_day}) 같은 구간 **{len(both)}개** / "
                      f"오늘 성립 {len(today_breaches)}개: {joined}. "
                      "미리 정해 둔 조치: **해당 시간대 위클리 폴링 2분 → 4분 격분(먼슬리는 안 "
                      "건드린다)**. ⛔ **자동 발동하지 않는다** — 발동은 사람이 결정한다"
                      "(2026-07-08 페이서 분리 500 폭주 203분).")
                    flags.append(
                        f"🔔 사전 대응 규칙 `{P95_TWO_DAY_RULE_ID}` 조건 성립 — "
                        f"p95>{P95_WARN_THRESHOLD_SECONDS}초 이틀 연속 {len(both)}구간"
                        f"(오늘 {len(today_breaches)}구간). 발동 여부는 사람이 정한다"
                    )
                elif today_breaches:
                    # 「오늘만 나쁨」과 「이틀째 나쁨」을 가른다 — 안 가르면 이 줄은 한 주 안에
                    # 배경 소음이 된다(08-15~16 ALERT_ONLY 94·113줄의 형태).
                    A(f"- 이틀 연속 구간 **없음**(직전 거래일 {prev51_day} 대조) — 「오늘만 나쁨」이다. "
                      "규칙은 이틀 연속에만 발동을 묻는다.")
        A("")
        A("> **판정은 「창 최대」로 한다** — 시간대 평균은 절벽을 눌러 없앤다. 08-14 13시는 평균")
        A("> 2.18초(0.55)로 조용했는데 창 최대는 3.53초(**0.88**)였고 그 20~60분 뒤 전멸이 시작됐다.")
        A("")
        A("> **창 최대 p50 ÷ read timeout이 1.0에 닿으면 그 창의 호출 절반 이상이 타임아웃**이고, 20레그 순차")
        A("> 수집의 기대 성공 수는 0에 수렴한다. 이때 수집 **소요는 예산 천장에 눌려 오히려 안 늘고**")
        A("> 적재만 0이 된다 — 08-14 14시가 그랬다(평균 49.9초로 13시보다 낮았고 rows는 0이었다).")
        A("> 소요만 보는 눈은 이 절벽을 **구조적으로 못 본다.**")
        A("")
        run = scan.longest_zero_row_run()
        if run is None:
            A("- 최장 연속 `rows=0` 구간: **없음** (0행 사이클 0개)")
        else:
            length, lo, hi = run
            A(f"- 최장 연속 `rows=0` 구간: **{length}분** ({lo}~{hi}, 임계 {ZERO_ROW_RUN_ALERT_MINUTES}분)")
            if length >= ZERO_ROW_RUN_ALERT_MINUTES:
                flags.append(
                    f"`rows=0`이 **{length}분 연속**({lo}~{hi}) — 흩어진 0행 분과 다른 사건이다. "
                    "신선도 창(5분)을 넘긴 분의 판단은 체인을 아예 못 본다"
                )
        A("")

    # ---- 5-1-1. 백오프 · 잔고폴링 · 먼슬리 되살리기 (2026-08-24 Fix#6 B · Fix#4 B) ----
    #
    # **왜 위 표에 열을 더하지 않고 표를 하나 더 두는가**: 위 표는 이미 9열이고, 거기 5열을
    # 더하면 사람이 안 읽는다(이 파일이 §5-1 주석에 적어 둔 「표가 옆으로 넘치면 안 읽는다」와
    # 같은 이유). 규약 E가 요구하는 것은 **한 표에 열을 몰아넣는 것이 아니라 분자와 분모가
    # 같은 표에 있는 것**이고, 아래 다섯 열이 정확히 그 짝들이다. 시간대 축도 같다.
    A("## 5-1-1. 백오프 × 잔고폴링 × 먼슬리 되살리기 — 분자와 분모를 같은 표에")
    A("")
    hours = sorted(
        set(scan.balance_poll_failures)
        | set(scan.backoff_expansions)
        | set(scan.priority_retries)
    )
    if not hours:
        A("(세 축 모두 당일 0줄 — **「없었다」가 아니라 「그 줄이 없는 버전일 수 있다」**다. "
          f"문구: `{BALANCE_POLL_FAILED_TOKEN}` · `{BACKOFF_EXPAND_TOKEN}` · `{PRIORITY_RETRY_TOKEN}`)")
        A("")
    else:
        A("| 시간대 | 백오프확대 | 잔고폴링실패 | 먼슬리재시도 | 회복실패 | 남은예산 창최소(초) |")
        A("|---|---|---|---|---|---|")
        # 2026-08-25 (08-25 §1-7 / P2-1) — 「회복실패」의 0은 **0으로 찍는다.** 근거는
        # `revival_failure_cell` docstring.
        retry_axis_measured = bool(scan.priority_retries)
        for hh in hours:
            budget_min = scan.priority_retry_budget_min.get(hh)
            failed_cell = revival_failure_cell(
                scan.priority_retry_failures.get(hh, 0), retry_axis_measured
            )
            A(f"| {hh:02d}시 | {scan.backoff_expansions.get(hh, 0)} | "
              f"{scan.balance_poll_failures.get(hh, 0)} | {scan.priority_retries.get(hh, 0)} | "
              f"{failed_cell} | "
              f"{'—' if budget_min is None else f'{budget_min:.1f}'} |")
        A("")
        failed_total = sum(scan.priority_retry_failures.values())
        if failed_total:
            worst = ", ".join(
                f"{hh:02d}시 {n}건" for hh, n in sorted(scan.priority_retry_failures.items())
            )
            flags.append(
                f"먼슬리 되살리기 **실패 {failed_total}건**({worst}) — 그 분의 GEX·감마플립은 "
                "핵심 6레그가 빈 채로 계산됐다. 「간신히 성공」과 다른 사건이다(2026-08-24 Fix#4)"
            )
        A("> **이 표가 있는 이유**: 08-24에 「백오프 확대가 잔고 폴링을 떨어뜨렸는가」를 하루에")
        A("> 여섯 번 대조했고 **결론이 세 번 뒤집혔다**(확대→실패 2/21 · 실패→확대 2/2). 두 값이")
        A("> 서로 다른 파일에 있었기 때문이다. 그리고 15:10:50의 「3개 중 0개 회복」은 사람이")
        A("> 하루치 로그를 훑어서 찾았다 — 그 줄은 그날 **INFO**였다(지금은 WARNING이다).")
        A("")
        A("> ⚠ **건수는 그날 KIS 상태에 비례한다**(규약 F) — 이 표로 판정하지 말고 **인과의")
        A("> 방향**을 본다: 같은 시간대에 둘 다 있으면 그때 지연창 TSV로 내려가 초 단위로 맞춘다.")
        A("")

    # ---- 5-2. 앙상블 멤버 가용성 (2026-08-13 고도화 2) ----
    A("## 5-2. 앙상블 멤버 가용성 — 「축이 죽었다」를 추론이 아니라 인용으로")
    A("")
    if not scan.member_transitions:
        A(f"(`{MEMBER_TOKEN}` 줄 없음 — 판단 경로가 안 돌았거나 로그 형식이 다르다. "
          "**「멤버가 다 살아 있었다」가 아니다.**)")
        A("")
    else:
        A(f"판단 형태 전이 **{scan.member_transitions}건** · 분모 {scan.member_total}종")
        A("")
        A("| 멤버 | 최초 편입 | 마지막 관측 |")
        A("|---|---|---|")
        for name in sorted(scan.member_first_seen):
            A(f"| `{name}` | {scan.member_first_seen[name]} | {scan.member_last_seen[name]} |")
        A("")
        shapes = ", ".join(f"{k} ×{v}" for k, v in sorted(scan.member_shape.items(), reverse=True))
        A(f"- 형태 분포: {shapes}")
        # 2026-08-19 Fix#6 — **가용과 실질을 나란히 둔다.** 상세 근거는 `MEMBER_RE` 위 절 주석.
        if scan.member_nonzero:
            lo, hi = min(scan.member_nonzero), max(scan.member_nonzero)
            A(f"- **비영 멤버**(0점이 아닌 축): 최소 **{lo}** · 최대 **{hi}** "
              f"(전이 {len(scan.member_nonzero)}건 기준, 분모 {scan.member_total}종)")
            if scan.member_total and hi * 2 <= scan.member_total:
                flags.append(
                    f"비영 멤버가 최대 **{hi}/{scan.member_total}** — 「가용」 목록은 그보다 넓다. "
                    "**0점은 중립이지 의견이 아니다**(08-19 실질 2.36 vs 가용 3.43)"
                )
        else:
            A("- 비영 멤버: **안 셌다** — 이 로그는 Fix#6(2026-08-19) 이전 문구다. "
              "**「비영이 0이었다」가 아니다.**")
        if scan.member_conviction:
            A("- 확신도: " + ", ".join(f"{k} ×{v}" for k, v in scan.member_conviction.most_common()))
        A("")
        A("> **최초 편입 시각이 09:00보다 한참 뒤면 그 멤버는 장전 내내 죽어 있었던 것이다.**")
        A("> 08-14에 `options_flow`가 넉 달 만에 09:01:10에 합류했고, 같은 날 오후 입력 고갈로")
        A("> 다시 빠졌다 — 두 사건 다 이 표 한 줄로 보인다.")
        A("")
        # **마지막 관측이 로그 끝보다 한참 이르면 그 멤버는 도중에 빠진 것이다.**
        # 08-14 오후의 `options_flow`가 정확히 그 형태였고, 그날 아무 카운터에도 안 걸렸다.
        if scan.last:
            end_min = hhmm_to_min(scan.last[0][:5])
            dropped = [
                (n, t) for n, t in scan.member_last_seen.items()
                if end_min - hhmm_to_min(t[:5]) >= MEMBER_DROPOUT_ALERT_MIN
            ]
            for name, seen in sorted(dropped):
                gap = end_min - hhmm_to_min(seen[:5])
                flags.append(
                    f"앙상블 멤버 `{name}`가 {seen} 이후 **{gap}분째 미관측** — 축이 도중에 빠졌다. "
                    "그 멤버가 죽은 이유가 자기 자신인지 **입력 고갈**인지 갈라야 한다(08-14 오후가 후자였다)"
                )

    # ---- 5-3. 계측 부재 신고 (2026-08-14 장중 §8 / 고도화 3) ----
    A("## 5-3. 계측 부재 — 「0건」인가 「재는 눈이 없는가」")
    A("")
    zero_tokens = [t for _i, t, _a in MEASUREMENT_MAP if not scan.measurement_hits.get(t)]
    # **당일 0줄인 문구만** 잔여 로그를 다시 훑는다 — 이미 오늘 나온 문구는 계측이 살아 있는
    # 것이 증명됐고, 로그 전체를 무조건 재훑는 것은 10MB×10에 대해 낭비다.
    elsewhere = tokens_seen_on_other_days(logs, day, zero_tokens) if zero_tokens else {}
    A("| phases 항목 | 로그 문구 | 당일 줄 | 판정 |")
    A("|---|---|---|---|")
    blind = []
    for item, token, alt in MEASUREMENT_MAP:
        n = scan.measurement_hits.get(token, 0)
        other_n, other_day = elsewhere.get(token, (0, None))
        if n:
            verdict = "계측 살아 있음"
        elif other_n:
            # 규약 C — 이것이 「진짜 0」이다. 다른 날 같은 문구가 남아 있으므로 경로는 살아 있다.
            verdict = f"**진짜 0** — {other_day}에 {other_n}줄(경로 살아 있음)"
        elif alt:
            verdict = f"로그 축 없음 — **{alt}**가 답한다"
        else:
            verdict = "**계측 미확인 ⚠**"
            blind.append((item, token))
        A(f"| {item} | `{token}` | {n} | {verdict} |")
    A("")
    A("> **「당일 줄 0」은 「그 일이 안 일어났다」가 아니다.** 문구가 바뀌거나 로그 레벨이")
    A("> 내려가면 파서는 조용히 눈이 먼다 — 2026-08-04에 WARNING→INFO 강등으로 362건이")
    A("> 0건으로 보고된 전례가 있다. **대체 축이 있는 항목은 경보로 올리지 않는다**(로그에")
    A("> 없다고 못 재는 것이 아니다 — 08-14 장중 §4-3이 그 오판이었고 장후 §2-6이 정정했다).")
    A("")
    for item, token in blind:
        flags.append(
            f"계측 없음 — `{item}`에 답할 문구 `{token}`가 당일 **0줄**이다. "
            "「안 일어났다」로 읽기 전에 **그 문구가 아직 존재하는지** 먼저 확인할 것"
        )

    # ---- 6. 워치독 ----
    A("## 6. 워치독 — 감시자 자신의 생사")
    A("")
    wd = bracket_day_lines(logs / "watchdog.log", day)
    ok = [x for x in wd if "OK" in x.split("]", 1)[-1][:12]]
    bad = [x for x in wd if x not in ok]
    A(f"- 당일 {len(wd)}행 (OK {len(ok)} / 비-OK **{len(bad)}**)")
    if bad:
        A("")
        A("**비-OK 전량**")
        A("```")
        L.extend(bad[:25])
        A("```")
        flags.append(f"워치독 비-OK {len(bad)}행 — 재기동/오보 여부를 원본으로 확인할 것")
    wd_min = []
    for x in wd:
        m = re.match(r"^\[\d{4}-\d\d-\d\d (\d\d):(\d\d)", x)
        if m:
            wd_min.append(int(m.group(1)) * 60 + int(m.group(2)))
    wd_gaps = [(a, b, b - a) for a, b in zip(wd_min, wd_min[1:]) if b - a >= WATCHDOG_GAP_THRESHOLD_MIN]
    A("")
    A(f"- 기록 간격 {WATCHDOG_GAP_THRESHOLD_MIN}분 이상 공백: **{len(wd_gaps)}건** "
      f"(정상 주기 {WATCHDOG_EXPECT_INTERVAL_MIN}분)")
    if wd_gaps:
        A("")
        A("| 마지막 기록 | 다음 기록 | 공백(분) |")
        A("|---|---|---|")
        for a, b, g in wd_gaps[:10]:
            A(f"| {m2hhmm(a)} | {m2hhmm(b)} | {g} |")
        for a, b, g in wd_gaps:
            flags.append(f"워치독 공백 {m2hhmm(a)}~{m2hhmm(b)} ({g}분) — 그동안 아무도 관측 루프를 되살릴 수 없었다")
    # **끝난 뒤 다시 시작하지 않은 것**은 공백으로 안 잡힌다 — 기록이 둘 있어야 사이가 생기기
    # 때문이다. 2026-08-12가 정확히 그 모양이었다: 10:14 재기동 오보 이후 15:45까지 한 줄도 없고,
    # 「공백 0건」으로 보였다. 감시 창의 끝과 마지막 기록의 거리를 따로 잰다.
    watch_end = hhmm_to_min(ANCHORS[-1][0])
    if day == now.date():
        watch_end = min(watch_end, now.hour * 60 + now.minute)
    if wd_min:
        tail = max(0, watch_end - max(wd_min))
        A(f"- 마지막 기록 {m2hhmm(max(wd_min))} → 감시 창 끝({m2hhmm(watch_end)}) 까지 **{tail}분 무기록**")
        if tail >= WATCHDOG_GAP_THRESHOLD_MIN:
            flags.append(
                f"워치독이 {m2hhmm(max(wd_min))} 이후 {tail}분간 한 줄도 안 남겼다 — "
                "감시자 자신이 멈춘 것인지, 스케줄러가 새 실행을 무시한 것인지(MultipleInstances) 확인할 것"
            )
    else:
        A("- 당일 기록 **0행** (감시 창 밖이면 정상 — `.watchdog_last_check.json` 을 볼 것)")
        if due("08:10"):
            flags.append("워치독 당일 기록이 0행 — 작업 스케줄러 등록/무장 상태를 확인할 것")
    for name in (".watchdog_state.json", ".watchdog_last_check.json"):
        p = logs / name
        A(f"- `{name}`: {truncate(read_text(p), 200) if p.exists() else '**없음**'}")
    crash = logs / "observation_loop_crash.log"
    if crash.exists():
        mt = datetime.fromtimestamp(crash.stat().st_mtime, KST)
        size = fmt_bytes(crash.stat().st_size)
        segment = crash_since_last_start(read_text(crash).splitlines(), day)
        if segment is None:
            # 표식을 못 찾았다 — 옛 방식으로 돌아가되 **그 사실을 인쇄한다**(조용한 실패 금지).
            A(f"- `observation_loop_crash.log`: {size} · 최종 {mt:%m-%d %H:%M}"
              + ("  ← **오늘 갱신됨 ⚠**" if mt.date() == day else ""))
            A("  ⚠ 기동 표식(`===== 관측 루프 기동 =====`)을 못 찾아 **옛 방식(mtime)으로 판정했다** — "
              "`start_mahdi_premarket.bat`의 문구가 바뀌었거나 2026-08-19 이전 로그다. "
              "**이 줄이 있는 동안 아래 판정은 믿을 수 없다.**")
            if mt.date() == day:
                flags.append("크래시 로그가 오늘 갱신됐다(표식 없음 — mtime 판정) — 트레이스백을 직접 볼 것")
                A("")
                A("```")
                L.extend(read_text(crash).splitlines()[-12:])
                A("```")
        elif segment["traceback"]:
            A(f"- `observation_loop_crash.log`: {size} · 당일 마지막 기동 "
              f"{segment['at'] or '(시각 불명)'} **이후 트레이스백 {segment['count']}건 ⚠**")
            flags.append(
                f"당일 기동({segment['at'] or '시각 불명'}) 이후 크래시 {segment['count']}건 — "
                "트레이스백 마지막 프레임을 반드시 인용할 것"
            )
            A("")
            A("```")
            L.extend(segment["traceback"][-12:])
            A("```")
        else:
            A(f"- `observation_loop_crash.log`: {size} · (당일 기동 이후 크래시 없음"
              + (f" — 마지막 기동 {segment['at']}" if segment["at"] else " — 당일 기동 표식 없음")
              + f", 파일 최종 수정 {mt:%m-%d %H:%M})")
    A("")

    # ---- 7. 레버 ----
    A("## 7. 레버 상태 — 오늘 그 코드가 실제로 돌았는가")
    A("")
    # 2026-08-14 고도화 5 — 레버마다 **유예 회차와 무조건발동일**을 나란히 둔다.
    # 「지금 꺼져 있다」만으로는 그것이 오늘 결정된 유예인지 열 번째 망각인지 알 수 없다.
    schedule = lever_schedule(root)
    A("| 레버 | 위치 | 현재 줄 | 유예 | 무조건발동일 |")
    A("|---|---|---|---|---|")
    for key, rel in LEVER_KEYS:
        p = root / rel
        found = "**파일 없음**"
        if p.exists():
            hits = [ln.strip() for ln in read_text(p).splitlines()
                    if key in ln and not ln.strip().startswith("#")]
            found = truncate(hits[0], 90) if hits else "**키 없음(기본값으로 동작)**"
        info = schedule.get(key, {})
        deferrals = info.get("유예횟수")
        deadline = info.get("무조건발동일")
        if deadline:
            left = (_date.fromisoformat(deadline) - day).days
            when = f"{deadline} (D{left:+d})" if left else f"{deadline} (**오늘**)"
            if left < 0:
                when = f"{deadline} (**{-left}일 지남 ⚠**)"
                flags.append(
                    f"레버 `{key}`의 무조건발동일({deadline})이 {-left}일 지났다 — "
                    "켜거나 날짜를 옮기고 사유를 적을 것(테스트가 실패 중일 것이다)"
                )
        else:
            # 규약 C — 「기한이 없다」는 「여유가 있다」가 아니라 **「강제력이 없다」**이다.
            when = "— (강제력 없음)"
        A(f"| `{key}` | `{rel}` | {found} | {deferrals + '회' if deferrals else '—'} | {when} |")
    A("")
    A("> **규약 H — 레버가 꺼진 날의 숫자로 그 레버의 가설을 판정하지 않는다.**")
    A("> 2026-08-12에 「오늘 켤 유일한 레버」가 안 켜졌는데 지표는 켜진 전제로 판정했다(§1-1).")
    A("")
    A("> **무조건발동일이 비어 있으면 그 레버의 유예는 영원히 조용히 성립한다.** 레버 F는")
    A("> 그렇게 세 번, 레버 E는 일곱 번 미뤄졌고 **열 번 중 한 번도 사유가 적히지 않았다** —")
    A("> 결정된 유예가 아니라 잊힌 유예였다는 뜻이다. 날짜를 박으면 그날부터")
    A("> `test_repo_levers_have_not_blown_their_unconditional_deadline`이 강제한다.")
    A("")

    # ---- 8. 오늘 판정해야 할 가설 ----
    A("## 8. 오늘 판정해야 할 가설 (검증예정일 도래분)")
    A("")
    hp, due = due_hypotheses(root, day)
    if hp is None:
        A("`docs/동작점검/hypotheses.yaml` 없음 ⚠")
        flags.append("hypotheses.yaml 을 못 찾았다")
    elif not due:
        A("(도래한 pending 항목 없음)")
    else:
        A("| id | 검증예정일 | 상태 | 가설 | 전제레버 |")
        A("|---|---|---|---|---|")
        for it, note in due:
            A(f"| `{it['id']}` | {it.get('검증예정일', '?')} ({note}) | {it.get('상태')} | "
              f"{truncate(it.get('가설', ''), 90)} | {it.get('전제레버', '—')} |")
        overdue = [x for x in due if "지남" in x[1]]
        if overdue:
            flags.append(f"검증예정일이 지난 pending 가설 {len(overdue)}건 — 손 판정이 밀리고 있다")
        A("")
        A("> **소급 적용 금지.** 예정일이 지난 항목에 예측을 새로 붙이는 것은 *숫자를 보고 예측을")
        A("> 고치는 것*이라 규약의 뿌리를 무너뜨린다 — `inconclusive`로 닫고 이유를 적는다.")
    A("")

    # ---- 8-1. 전일 지시 이행 대조 (2026-08-14 장전 §6 / 고도화 1) ----
    A("## 8-1. 전일 보고서의 지시가 이행됐는가 — 기계 대조")
    A("")
    prev_report = latest_report_before(root, day)
    if prev_report is None:
        A(f"(직전 점검 보고서를 못 찾았다 — 첫날이거나 파일명 규약이 다르다. "
          f"찾은 패턴: {' · '.join('`' + g + '`' for g in _REPORT_GLOBS)})")
        A("")
    else:
        prev_label = prev_report.name[:10]
        text = read_text(prev_report)
        ids = report_hypothesis_ids(text)
        boxes = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- [ ]")]
        yaml_path = root / "docs" / "동작점검" / "hypotheses.yaml"
        yaml_ids, _due = due_hypotheses(root, day)
        known = registered_hypothesis_ids(root)
        yaml_mtime = (
            datetime.fromtimestamp(yaml_path.stat().st_mtime, KST).date()
            if yaml_path.exists() else None
        )

        A(f"직전 보고서: `{prev_report.name}` · 미완료 체크박스 **{len(boxes)}개** · "
          f"거기 언급된 가설 id **{len(ids)}개**")
        A("")
        A(f"- `hypotheses.yaml` 최종 수정: **{yaml_mtime or '—'}**")
        if yaml_mtime is not None and yaml_mtime <= _date.fromisoformat(prev_label):
            flags.append(
                f"`hypotheses.yaml`이 {prev_label} 보고서 이후 **한 줄도 안 바뀌었다**(mtime {yaml_mtime}) — "
                "그 보고서의 등재·이동·확정 지시가 전부 무행동으로 성립하는 중이다"
            )
        # **접두사로 맞춘다.** 보고서는 `2026-08-12-g1`처럼 짧게 부르고 yaml의 실제 id는
        # `2026-08-12-g1-reconnect-as-cost`다. 완전 일치로 보면 멀쩡히 등재된 항목이
        # 「미등재」로 찍히고, 그런 오보가 몇 번 나면 이 절 전체가 무시된다.
        unregistered = sorted(
            i for i in ids if not any(k == i or k.startswith(i + "-") for k in known)
        )
        # ===== 2026-08-24 Fix#2 — **개명과 폐기를 「부재」에서 갈라낸다** =====
        #
        # 상세 근거는 `hypothesis_slug` / `discarded_hypothesis_ids` 위 절 주석. 세 갈래로
        # 나눠 **셋 다 인쇄하고**, 적신호에는 마지막 것만 올린다:
        #   개명 후보 — 슬러그가 같은 yaml id가 있다(08-24의 13건이 전부 이 형태였다)
        #   폐기 확정 — 그 id가 `NEXT_TODO.md`의 폐기 블록 안에 있다(재등재 대상이 아니다)
        #   진짜 부재 — 위 둘 다 아니다. **이것만 적신호다.**
        renamed = rename_candidates(unregistered, known)
        discarded_ids = discarded_hypothesis_ids(root, [i for i in unregistered if i not in renamed])
        really_missing = [
            i for i in unregistered if i not in renamed and i not in (discarded_ids or {})
        ]
        if renamed:
            A(f"- 🔁 **개명 후보 {len(renamed)}건** — 슬러그가 같은 항목이 yaml에 있다"
              "(**판정하지 않는다**: 서로 다른 두 가설이 같은 슬러그를 가질 수 있다)")
            for i in sorted(renamed):
                A(f"  - `{i}` → " + ", ".join(f"`{k}`" for k in renamed[i]))
        if discarded_ids:
            A(f"- ⛔ **폐기 확정 {len(discarded_ids)}건 — 재등재 대상이 아니다**")
            for i in sorted(discarded_ids):
                line_no, quoted_line = discarded_ids[i]
                A(f"  - `{i}` — NEXT_TODO:{line_no}  {quoted_line}")
        if discarded_ids is None and unregistered:
            # 규약 C — 대조를 **못 했다**와 「걸린 것이 없다」를 가른다.
            A("- ⚠ `NEXT_TODO.md`를 못 읽어 **폐기 목록과 대조하지 못했다** — 아래 목록에 "
              "이미 폐기된 항목이 섞여 있을 수 있다.")
        if really_missing:
            A(f"- ⚠ 직전 보고서가 언급했는데 **yaml에 없는 id {len(really_missing)}개**: "
              + ", ".join(f"`{i}`" for i in really_missing))
            flags.append(
                f"전일 보고서의 등재 제안 {len(really_missing)}건이 **미등재**"
                f"({', '.join(really_missing[:3])}{'…' if len(really_missing) > 3 else ''}) — "
                "**등재 창은 개장(09:00)에 닫힌다**(소급 금지)"
            )
        elif unregistered:
            A(f"- ✅ 완전일치에 실패한 {len(unregistered)}건은 **전부 개명이거나 폐기다** — "
              "진짜 미등재 0건(2026-08-24 Fix#2)")
        else:
            A("- 직전 보고서가 언급한 id는 전부 yaml에 있다")
        A("")
        if boxes:
            A("직전 보고서의 미완료 항목(원문 그대로):")
            A("")
            A("```")
            for ln in boxes[:14]:
                A(truncate(ln, 150))
            if len(boxes) > 14:
                A(f"… 외 {len(boxes) - 14}개")
            A("```")
            A("")
        A("> **이 절이 있는 이유**: 08-14에 「어제 지시 3종이 하나도 이행되지 않았다」를 찾은 것은")
        A("> 사람이 전일 보고서와 `hypotheses.yaml`을 손으로 겹쳐 읽었기 때문이고, 그날 자동")
        A("> 적신호는 「예정일 지난 12건」만 냈다. **체크박스 자체는 사람이 지우는 것이라 여기서")
        A("> 완료 판정을 하지 않는다** — 기계가 답할 수 있는 것(yaml이 움직였는가, 그 id가")
        A("> 실재하는가)만 답하고 나머지는 원문으로 보여 준다.")
        A("")

    # ---- 8-2. 다시 올리지 말 것 (2026-08-19 §2-3 / Fix#5) ----
    #
    # **`prev_report`가 없어도 인쇄한다** — 이 목록은 직전 보고서와 무관하고 첫날에도 유효하다.
    A("## 8-2. 「다시 올리지 말 것」 — `NEXT_TODO.md`의 폐기·종결 목록")
    A("")
    discarded = discarded_items(root)
    if discarded is None:
        # 규약 C — 여기서 조용히 비면 이 절은 **매일 통과**한다.
        A("⚠ `docs/dev_memory/NEXT_TODO.md`를 못 읽었다 — 이 절은 **검사한 것이 아니다**.")
        flags.append("`NEXT_TODO.md`를 못 읽어 「다시 올리지 말 것」 목록을 확인하지 못했다")
    elif not discarded:
        A("(폐기·종결 목록이 비어 있다 — 절 제목이 바뀌었는지 확인할 것)")
        flags.append("`NEXT_TODO.md`의 폐기·종결 목록이 **0건**으로 읽혔다 — 절 제목 규약이 깨졌을 수 있다")
    else:
        A(f"**{len(discarded)}건.** 이 회차가 무엇을 P0/P1으로 올리기 전에 여기부터 본다.")
        A("")
        A("```")
        for line_no, title in discarded:
            A(f"NEXT_TODO:{line_no}  {truncate(title, 110)}")
        A("```")
        A("")
    A("> **이 절이 있는 이유**: 08-19 장중 두 회차가 08-18에 **이미 폐기된** 진단을 P1으로")
    A("> 되살렸고(§2-3), 같은 날 장후 보고서의 §4 **Fix#1(P0)** 이 2026-08-01 사용자 결정으로")
    A("> 보류 확정된 Slack 토글을 다시 올렸다 — 위 목록의 그 줄이 *「매 점검 보고서에서 다시")
    A("> 올리지 말 것」* 이라고 적어 둔 그 항목이다. **정보는 리포 안에 있었는데 경로가 닿지")
    A("> 않았다.** 여기서 기계 판정은 하지 않는다(근거는 `discarded_items` 위 절 주석) —")
    A("> 목록을 눈앞에 두는 것이 이 절의 전부이고, 두 사고 다 그것으로 막혔을 것이다.")
    A("")

    # ---- 8-3. 사람이 고르기로 하고 미뤄 둔 것 (2026-08-24 §3-2 / 고도화#4) ----
    #
    # **§8-2의 거울상이라 바로 옆에 둔다.** 저쪽은 「다시 올리지 마라」이고 이쪽은
    # 「아직 안 정했다」다 — 08-19는 닫힌 것을 되살렸고 08-24는 열린 것을 새것으로 착각했다.
    A("## 8-3. 「사람이 고르기로 하고 미뤄 둔 것」 — 아직 안 정한 갈림길")
    A("")
    pending = pending_decisions(root)
    if pending is None:
        # 규약 C — §8-2와 같은 자리다. 여기서 조용히 비면 이 절은 **매일 통과**한다.
        A("⚠ `docs/dev_memory/NEXT_TODO.md`를 못 읽었다 — 이 절은 **검사한 것이 아니다**.")
        flags.append("`NEXT_TODO.md`를 못 읽어 「미결 결정」 목록을 확인하지 못했다")
    elif not pending:
        A("(열려 있는 결정이 없다 — 선택지가 둘 이상 달린 미체크 절을 찾지 못했다.)")
    else:
        A(f"**{len(pending)}건.** 이 회차가 무엇을 신규 결함으로 올리기 전에 여기부터 본다 — "
          "**여기 있는 것은 결함이 아니라 아직 안 고른 것이다.**")
        A("")
        A("```")
        for line_no, title, choices in pending:
            A(f"NEXT_TODO:{line_no}  {truncate(title, 110)}")
            for choice_line, choice in choices:
                A(f"    :{choice_line}  [ ] {truncate(choice, 100)}")
        A("```")
        A("")
    A("> **이 절이 있는 이유**: 08-24 장중 두 회차가 「사고 싶다 336번 중 실행까지 간 것은 23번」을")
    A("> **신규 P1**으로 올렸다. 그 답은 위 목록에 (a)/(b)/(c) 체크박스와 함께 **엿새째 열려**")
    A("> 있었다(§3-2). §8-2가 「닫힌 것을 되살리는 실수」를 막고 이 절이 **그 반대 방향의 실수**를")
    A("> 막는다 — 두 목록은 같은 파일에서 나온다. **여기서도 판정은 하지 않는다**(§8-2와 같은")
    A("> 규약): 목록을 눈앞에 두는 것이 전부이고, 08-24 사고는 그것으로 막혔을 것이다.")
    A("")

    # ---- 9. 산출물 ----
    A("## 9. 산출물 존재 점검")
    A("")
    # 2026-08-24 Fix#1 — **직전 거래일**을 찾는다. 상세 근거는 `previous_metric_sidecar` 위 절 주석.
    prev_day, prev_back = previous_metric_sidecar(auto, day)
    prev_hint = (prev_day or (day - timedelta(days=1))).isoformat()
    targets = [
        (f"docs/동작점검/auto/{D}_지표.md", "post"),
        (f"docs/동작점검/auto/{D}_지표.json", "post"),
        (f"docs/동작점검/auto/{prev_hint}_지표.json", "pre"),
    ]
    if day >= ONE_FILE_SINCE:
        # **하루 한 파일.** 장전이 만들고 장중·장후가 이어 붙인다 — 그래서 확인할 것은 하나다.
        #
        # 기대 국면을 `post`로 두는 이유: 이 열은 「없으면 적신호를 낼 국면」이지 「생겨야 할
        # 국면」이 아니다(플래그 조건이 `ph == "post"`다). **장전 회차가 이 수집기를 돌리는
        # 시점에는 아직 그 회차가 파일을 안 썼다** — 그때 적신호를 내면 매일 아침 거짓 경보가
        # 뜬다. 표에는 「없음 ⚠」으로 보이되(사실이다) 판정은 장후가 한다.
        targets.append((f"docs/동작점검/{D}_마흐디_일일점검.md", "post"))
    else:
        # 2026-08-20까지의 국면별 4파일 체제. 장후 회차가 **흡수해야 할 원본들**이라
        # 없으면 그날 보고서는 오전/오후 중 한쪽을 못 보고 쓰인다.
        targets += [
            (f"docs/동작점검/{D}_마흐디_운영점검보고서.md", "post"),
            (f"docs/동작점검/{D}_점검_pre.md", "post"),
            (f"docs/동작점검/{D}_점검_intra.md", "post"),
        ]
        # 2026-08-17부터 장중이 두 회차다(`mahdi-intraday-check-1430`). 그 이전 날짜를
        # 재집계할 때 이 파일을 기대하면 **매번 거짓 누락**이 뜬다.
        if day >= INTRA_1430_SINCE:
            targets.append((f"docs/동작점검/{D}_점검_intra_1430.md", "post"))
    # 증거 다이제스트는 **기계 산출물**이라 파일명 전환과 무관하다(수집기가 국면마다 하나씩
    # 낸다). 14:30 회차분만 규약 시작일에 걸린다.
    if day >= INTRA_1430_SINCE:
        targets.append((f"docs/동작점검/auto/{D}_증거_intra_1430.md", "post"))
    A("| 파일 | 기대 국면 | 상태 | 크기 | 최종기록 |")
    A("|---|---|---|---|---|")
    for rel, ph in targets:
        state, size, mt = stat_line(root / rel)
        A(f"| `{rel}` | {ph} | {state} | {size} | {mt} |")
        if ph == "post" and "post" in cfg_phases and state.startswith("**없음"):
            flags.append(f"장후 산출물 누락: `{rel}`")
    A("")
    # 2026-08-24 Fix#1 — **찾은 날짜를 함께 인쇄한다.** 「어제 것이 없다」와 「직전 거래일
    # 것을 쓰고 있다」는 다른 사실이고, 후자는 정상이다.
    if prev_day is None:
        A(f"- ⚠ **최근 {PREV_SIDECAR_MAX_BACKTRACK_DAYS}일 안에 지표 사이드카가 하나도 없다** — "
          "전일 델타가 통째로 빈다(위 줄은 달력상 어제를 가리킨다).")
        flags.append(
            f"직전 {PREV_SIDECAR_MAX_BACKTRACK_DAYS}일 안에 `_지표.json`이 **하나도 없다** — "
            "연휴가 아니면 지표 생성이 며칠째 안 돈 것이다(전일 델타는 나중에 복구할 수 없다)"
        )
    elif prev_back > 1:
        A(f"- 전일 사이드카는 **직전 거래일 {prev_day}** 기준이다 — 그 사이 비거래일 "
          f"**{prev_back - 1}일**을 건너뛰었다(달력상 어제는 `{(day - timedelta(days=1)).isoformat()}`).")
    A("")
    A("> 전일 사이드카(`_지표.json`)가 없으면 **전일 델타가 통째로 빈다.** 로그는 이틀치만")
    A("> 남으므로 그 델타는 나중에 복구할 수 없다. **달력의 어제가 아니라 파일이 실재하는")
    A(f"> 가장 가까운 날**을 최대 {PREV_SIDECAR_MAX_BACKTRACK_DAYS}일까지 찾는다(2026-08-24 Fix#1) —")
    A("> 월요일마다 뜨던 「없음 ⚠」이 그 헛경보였다. 그보다 오래 비면 그때는 **진짜 부재**다.")
    A("")

    # ---- 10. 자동 지표 발췌 (장후) ----
    if "post" in cfg_phases:
        auto_md = auto / f"{D}_지표.md"
        A("## 10. 자동 지표 발췌 — §0 가설 검정 · §1 한눈에")
        A("")
        if auto_md.exists():
            text = read_text(auto_md)
            picked, keep = [], False
            for ln in text.splitlines():
                if ln.startswith("## "):
                    keep = ln.startswith("## 0.") or ln.startswith("## 1.")
                if keep:
                    picked.append(ln)
            A("```markdown")
            L.extend(picked[:110])
            if len(picked) > 110:
                A(f"… (전문은 `{auto_md.relative_to(root)}`)")
            A("```")
        else:
            A(f"`{auto_md.name}` 없음 — `uv run python scripts/daily_ops_report.py --date {D}` 을 먼저 돌릴 것.")
        A("")

    # ---- 11. dev_memory ----
    A("## 11. dev_memory")
    A("")
    dm = root / "docs" / "dev_memory"
    if not dm.is_dir():
        A("`docs/dev_memory/` 없음 ⚠")
        flags.append("dev_memory 디렉터리를 못 찾았다")
    else:
        for fname in ("CURRENT_STATE.md", "DECISION_LOG.md", "NEXT_TODO.md"):
            p = dm / fname
            if not p.exists():
                A(f"- **{fname}**: 없음")
                continue
            st = p.stat()
            mt = datetime.fromtimestamp(st.st_mtime, KST)
            fresh = "오늘 갱신됨" if mt.date() == day else f"마지막 갱신 {mt:%Y-%m-%d %H:%M}"
            A(f"### {fname} — {fmt_bytes(st.st_size)} · {fresh}")
            text = read_text(p)
            heads = [ln.strip() for ln in text.splitlines() if re.match(r"^#{1,3} ", ln)]
            if heads:
                A("")
                A("최근 헤딩 10개:")
                A("```")
                L.extend(heads[-10:])
                A("```")
            if fname == "NEXT_TODO.md":
                open_items = [ln.strip() for ln in text.splitlines() if re.match(r"^\s*[-*]\s*\[ \]", ln)]
                A("")
                A(f"미완료 체크박스 **{len(open_items)}건** (끝에서 20건)")
                if open_items:
                    A("```")
                    L.extend(truncate(x, 160) for x in open_items[-20:])
                    A("```")
            A("")
            A(f"<details><summary>{fname} 꼬리 2KB</summary>")
            A("")
            A("```")
            A(text[-2000:])
            A("```")
            A("")
            A("</details>")
            A("")
        if "post" in cfg_phases:
            stale = [f for f in ("DECISION_LOG.md", "NEXT_TODO.md")
                     if (dm / f).exists()
                     and datetime.fromtimestamp((dm / f).stat().st_mtime, KST).date() != day]
            if stale:
                flags.append(f"dev_memory 미갱신: {', '.join(stale)} — 점검도 세션이다")

    # ---- 12. 자동 적신호 ----
    A("## 12. 자동 적신호")
    A("")
    A("**기계가 먼저 잡은 것 — 분석의 출발점이지 결론이 아니다.**")
    A("")
    if flags:
        for i, f in enumerate(dict.fromkeys(flags), 1):
            A(f"{i}. {f}")
    else:
        A("자동 탐지 적신호 없음. 그래도 §3~§7을 직접 읽고 판단할 것.")
    A("")
    A("> 기계가 못 잡는 것이 진짜 수확이다 — **설계와 다른 순서**, 있어야 할 로그가 **아예 없는 것**,")
    A("> INFO 레벨로 조용히 지나간 폴백. 이 셋은 어떤 카운터에도 안 걸린다.")
    A("")
    A("---")
    A("")
    A(f"*원본이 필요하면: `grep '{D}' logs/observation_loop.log*` 로 그 줄만 직접 열 것.*")
    return "\n".join(L)


def write_latency_windows(root: Path, day: _date, windows, timeout, censored=None) -> Path | None:
    """5분 창 전량을 `auto/{날짜}_지연창.tsv`로 뺀다. 반환: 쓴 경로(못 쓰면 None).

    입력: 리포 루트 · 날짜 · `LoopScan.window_latency_p50()` · 그날 실제 read timeout ·
         (선택) `LoopScan.window_censored_counts()`.
    계산: `창시각 · 호출수 · p50초 · p50/timeout · 검열건수 · 검열% · p50표기` 일곱 열.
         정렬은 시각순(입력 그대로).
    해석: 표는 **돌파 창만** 인쇄하고 전량은 여기로 뺀다 — 98창을 md에 실으면 그 표가
         §5-1을 통째로 밀어낸다. 손으로 다시 파싱하는 일(08-19 14:30 회차)이 없어지는 것이
         이 파일의 전부다.

         2026-08-23(08-21 §1-14 / §5 고도화#1) — **`p50표기` 열이 이 파일의 새 요점이다.**
         p50이 타임아웃의 `P50_CENSORED_FLOOR_RATIO`배를 넘으면 `4.03`이 아니라 **`≥4.0`**으로
         적는다. 08-21에 네 회차가 「4.03초」를 실제 응답시간으로 읽었고, 그 오독 위에
         「제한시간 6초」 처방이 이틀 연속 손익표에 올랐다.
         `censored`가 없으면 그 두 열은 `-`다 — **0이 아니라 「안 셌다」**이다(규약 C).
    실패 조건: 쓰기에 실패해도 **조용히 None** — 증거 본문이 이것 때문에 안 나오면 안 된다.
    """
    if not windows:
        return None
    try:
        out = root / "docs" / "동작점검" / "auto" / f"{day.isoformat()}_지연창.tsv"
        out.parent.mkdir(parents=True, exist_ok=True)
        # 탭·개행은 `chr()`로 적는다 — 이 파일은 소스에 탭 문자를 두지 않는다.
        tab, lf = chr(9), chr(10)
        head = tab.join(
            ("창시각", "호출수", "p50초", "p50/timeout", "검열건수", "검열%", "p50표기")
        )
        rows = [head]
        for at, n, p50 in windows:
            ratio = f"{p50 / timeout:.2f}" if timeout else "-"
            floored = timeout and p50 >= timeout * P50_CENSORED_FLOOR_RATIO
            label = f">={timeout:.1f}" if floored else f"{p50:.2f}"
            if censored is None:
                cut_n = cut_pct = "-"
            else:
                cut = censored.get(at, 0)
                cut_n = str(cut)
                cut_pct = f"{min(cut / n, 1.0) * 100:.0f}" if n else "-"
            rows.append(tab.join((at, str(n), f"{p50:.2f}", ratio, cut_n, cut_pct, label)))
        out.write_text(lf.join(rows) + lf, encoding="utf-8", newline=lf)
        return out
    except Exception:  # noqa: BLE001
        return None


# ===== 증거 다이제스트 정리 (2026-08-19 신설) =====
#
# **정리 대상은 `_증거_*.md` 하나뿐이다.** 다른 산출물은 여기서 절대 건드리지 않는다 —
# 08-19에 보관 기간을 실측으로 따져 본 결과가 그렇게 갈렸다:
#
#   `_증거_*.md`   수명 **하루**. 08-13~08-18 점검 문서 13편이 증거 파일을 30번 인용했는데
#                  **전부 당일 것이고 과거분 인용은 0건**이었다. 게다가 로그가 남아 있으면
#                  이 스크립트로 언제든 다시 만든다. → 지워도 되는 유일한 것.
#   `_지표.json`   `mahdi/ops/campaign.py`가 여러 날을 접는 **시계열 원자재**(min_days 10).
#                  08-19부터 git 추적이다(.gitignore 참고). → 절대 삭제 금지.
#   `_지표.md`     연 10MB 남짓. 지울 이유가 없다. → 대상 아님.
#   루트 보고서    git 추적이라 지워도 용량이 안 줄고 **grep 대상만 잃는다.** 소급 인용 꼬리가
#                  43일까지 간다(「고쳤다고 기록된 것이 재발」이 그 꼬리를 타고 나온다).
#                  → 대상 아님. 애초에 이 함수는 out-dir(=auto/) 밖을 보지 않는다.
#
# 기본값은 **끔**이다. 조용히 지우는 정리는 언젠가 지우면 안 되는 것을 지운다.

# `YYYY-MM-DD_증거_{국면}[_HHMM].md` 만 통과시킨다. `_지표.`는 이 패턴에 걸리지 않는다.
_EVIDENCE_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_증거_(?:pre|intra|post)(?:_\d{4})?\.md$"
)


def prune_evidence(out_dir: Path, keep_days: int, today: _date) -> list[Path]:
    """반환: 실제로 지운 파일 목록.

    **mtime이 아니라 파일명의 날짜로 판정한다** — 파일을 복사하거나 백업에서 되돌리면
    mtime은 바뀌지만 그 증거가 어느 날 것인지는 안 바뀐다.

    실패 조건: `keep_days < 1`이면 아무것도 안 지우고 빈 목록을 돌려준다(0을 「전부 지워라」로
              읽지 않는다 — 그 오독의 대가가 비대칭이다). 개별 파일 삭제 실패는 경고만 내고
              넘어간다. 정리는 점검의 곁가지이므로 여기서 예외를 올려 **점검 자체를 죽이면 안 된다.**
    """
    if keep_days < 1:
        eprint(f"[collect_evidence] --prune-days {keep_days} 는 1 미만이라 무시한다.")
        return []
    if not out_dir.is_dir():
        return []

    cutoff = today - timedelta(days=keep_days)
    removed: list[Path] = []
    for p in sorted(out_dir.iterdir()):
        m = _EVIDENCE_NAME_RE.match(p.name)
        if not m:
            continue
        try:
            fday = _date.fromisoformat(m.group(1))
        except ValueError:
            continue  # 날짜로 안 읽히면 손대지 않는다
        if fday > cutoff:
            continue
        try:
            p.unlink()
        except OSError as exc:
            eprint(f"[collect_evidence] 정리 실패(건너뜀): {p.name} — {exc!r}")
            continue
        removed.append(p)

    # **지운 것은 반드시 인쇄한다.** 조용한 정리는 사고가 나도 아무도 모른다.
    if removed:
        eprint(f"[collect_evidence] 정리: {cutoff.isoformat()} 이전 증거 {len(removed)}건 삭제 "
               f"(보관 {keep_days}일) — " + ", ".join(p.name for p in removed))
    else:
        eprint(f"[collect_evidence] 정리: 대상 없음 (보관 {keep_days}일, 기준 {cutoff.isoformat()})")
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(description="마흐디 일일 운영점검 증거 수집기")
    ap.add_argument("--phase", choices=["pre", "intra", "post", "all"], default="post",
                    help="장전=pre / 장중=intra / 장후=post (기본 post)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD 또는 YYYYMMDD (기본 오늘 KST)")
    ap.add_argument("--root", default=None, help="리포 루트 (기본 자동탐지)")
    ap.add_argument("--out", default=None, help="파일로 저장 (기본 stdout)")
    # 배치에서 부를 때 쓴다. **날짜 계산을 배치에 두지 않기 위한 것**이다 —
    # `%date%`는 OS 로캘을 따라 형식이 바뀌어서 같은 스크립트가 PC마다 다른 파일명을 만든다
    # (`daily_ops_report.py --out-dir`과 같은 이유·같은 이름).
    ap.add_argument("--out-dir", default=None,
                    help="디렉터리에 `{날짜}_증거_{국면}.md` 로 저장 (--out 보다 우선순위 낮음)")
    # 기본 None = **끔**. 인자를 줘야만 지운다.
    ap.add_argument("--prune-days", type=int, default=None, metavar="N",
                    help="--out-dir 안의 `_증거_*.md` 중 N일보다 오래된 것을 지운다"
                         " (지표.json·지표.md·보고서는 대상이 아니다. 기본: 끔)")
    args = ap.parse_args(argv)

    start = Path(args.root) if args.root else Path(__file__).resolve().parent
    root = find_repo_root(start)
    if not (root / "logs").is_dir():
        eprint(f"[collect_evidence] 경고: {root}/logs 가 없다. --root 로 리포 루트를 지정하라.")
    day = parse_date(args.date)
    phases = {"pre": ["pre"], "intra": ["pre", "intra"],
              "post": ["pre", "intra", "post"], "all": ["pre", "intra", "post"]}[args.phase]
    text = build(root, day, args.phase, phases)

    out_arg = args.out
    if not out_arg and args.out_dir:
        # 2026-08-14 — **회차가 여럿인 국면은 파일명을 갈라야 한다.**
        #
        # 장중이 12:30 / 14:30 두 회차가 되면서, 슬롯을 안 붙이면 14:30 실행이 12:30 증거를
        # 조용히 **덮어쓴다.** 그러면 12:30 보고서의 근거가 사라지고, 장후 회차가 두 보고서를
        # 흡수할 때 오전 시점의 증거를 못 본다 — 보고서 자체 검증의 「흡수한 원본을 지우지
        # 않았다」가 증거 쪽에서 깨지는 것이다.
        #
        # 첫 슬롯은 접미사 없이 둔다(기존 파일명 유지 — 지난 날짜 산출물과 규약이 갈리지 않는다).
        # 두 번째 이후만 `_HHMM`을 붙여 `{날짜}_증거_intra_1430.md`가 된다 — 점검 보고서의
        # `{날짜}_점검_intra_1430.md`와 같은 규약이다.
        planned, _late, first = matched_phase_slot(args.phase, day, datetime.now(KST))
        suffix = "" if (first or planned is None) else "_" + planned.replace(":", "")
        out_arg = str(Path(args.out_dir) / f"{day.isoformat()}_증거_{args.phase}{suffix}.md")
    if out_arg:
        outp = Path(out_arg)
        if not outp.is_absolute():
            outp = root / outp
        outp.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" — `.gitattributes`의 `eol=lf`를 파이썬 쪽에서도 지킨다
        # (`tests/test_repo_line_endings.py`. 없으면 Windows에서 CRLF로 되돌아간다).
        outp.write_text(text, encoding="utf-8", newline="\n")
        eprint(f"[collect_evidence] 저장: {outp} ({fmt_bytes(len(text.encode('utf-8')))})")
        # **오늘 것을 쓴 다음에 지운다.** 순서를 뒤집으면 정리가 실패한 날에 오늘 증거까지
        # 못 만들 수 있다 — 정리는 곁가지이므로 본업 뒤에 온다.
        if args.prune_days is not None:
            prune_evidence(outp.parent, args.prune_days, day)
        print(str(outp))
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
