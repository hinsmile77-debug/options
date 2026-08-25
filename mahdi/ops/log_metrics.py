"""`observation_loop.log` 하루치 → 지표 dict (순수 파서, 파일 I/O 없음).

2026-08-01(운영점검보고서 2026-07-31 §5-2). 여기 있는 집계는 전부 07-31 조사에서 **손으로 한 번
계산해본 것**이라 재현 가능함이 이미 확인된 항목들이다. 반환 dict은 JSON 직렬화 가능해야 한다
(`daily_ops_report.py`가 사이드카로 저장해 다음날 델타 계산에 쓴다).

파싱 대상 로그 포맷(전부 `mahdi/main.py`/`rest_client.py`의 실제 포맷 문자열과 1:1):
  옵션체인 사이클 소요 분해: REST수집 N초 + DB적재 N초 + 상태기록 N초 + 기타 N초 (rows=N, 밀림=N초, 타폴러동시호출추정=N건)
  옵션 체인 폴링 사이클이 주기(N초)를 초과해 스케줄이 N초 밀렸습니다 — 위상 격자의 다음 틱까지 N초 대기 (...)
  옵션체인 결손 회수: HH:MM 분을 먼슬리 N레그로 채움(밀린 사이클 HH:MM)      ← 2026-08-01 신규
  느린 REST 호출 N초 = 페이서대기 N초 + HTTP N초 (배율 N배, GET path)        ← 2026-08-01 신규
  레이트리밋 백오프 확대|회복: Ns -> Ns (기준 대비 N배)
  INFO:httpx:HTTP Request: GET <url> "HTTP/1.1 NNN ..."
"""

from __future__ import annotations

import bisect
import collections
import re
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

# 공유 `_RateLimiter`의 기준 간격(1.0건/초)에 대한 용량 — 수요 비율/적자 임계 계산의 분모다.
# rest_client.DEFAULT_MIN_REQUEST_INTERVAL_SECONDS와 같은 값이지만, 이 모듈을 브로커 계층에
# 의존시키지 않으려고(순수 파서 유지) 여기에 다시 둔다. 두 값이 갈라지면 테스트가 잡는다.
PACER_CAPACITY_CALLS_PER_SECOND = 1.0

# 연속 지연 에피소드 판정(§2-1 원인 b) — 이 간격을 넘는 호출이 연속으로 이만큼 이어지면 1건.
STALL_GAP_SECONDS = 5.0
STALL_MIN_RUN = 3
# 버스트(연속 호출 묶음) 경계 — 같은 그룹의 호출이 이보다 벌어지면 다른 버스트로 본다.
BURST_SPLIT_SECONDS = 60.0
# 관측 구간 대비 총 점유가 이 비율 이상이면 "버스트를 쏘는" 게 아니라 "계속 돌고 있는" 폴러로
# 보고 버스트 표에서 제외한다(옵션체인/투자자수급). 07-31 실측: 만기유동성 0.09 · 매크로 0.01
# vs 옵션체인 1.0 초과 — 두 무리가 두 자릿수 차이로 갈려 임계 선택이 민감하지 않다.
CONTINUOUS_POLLER_DUTY_RATIO = 0.5

_TS = r"(\d{4}-\d\d-\d\d) (\d\d):(\d\d):(\d\d),(\d+)"

_CYCLE_RE = re.compile(
    _TS + r" INFO:mahdi\.main:옵션체인 사이클 소요 분해: "
    r"REST수집 ([\d.]+)초 \+ DB적재 ([\d.]+)초 \+ 상태기록 ([\d.]+)초 \+ 기타 ([\d.]+)초 "
    # 2026-08-10 — `, 재시도함`은 **`밀림` 뒤**에 온다. 종전 정규식은 `rows` 뒤로 기대했고
    # (`\(rows=(\d+)(?:, 재시도함)?, 밀림=...`), 그래서 **재시도한 사이클을 한 건도 못 읽었다.**
    #
    # 출처: 포맷(`main.LOG_CHAIN_CYCLE_BREAKDOWN`)은 07-28(`e85e65d`)부터 이 모양이었고 이 정규식은
    # 08-01(`6f15e84`)에 그것을 **잘못 옮겨 적으며 태어났다.** 10일간 잠복하다 08-10 15:15에
    # 처음 발현했다 — 그날 유일한 재시도 사이클이 통째로 사라져 사이클 494→493이 됐고,
    # 그 분(15:15)이 「결손」으로 오분류됐다. **하필 그 분이 옵션체인 전멸(rows=0)이라
    # 그날 가장 중요한 사이클이었다.**
    #
    # 재시도는 **상황이 나쁠 때만** 일어난다 — 이 자리를 안 재면 파서는 가장 나쁜 사이클에서만
    # 눈이 먼다. 조건부 조각은 그래서 별도의 검사 대상이다
    # (`tests/test_ops_log_metrics_contract.py`의 변형 전수 왕복).
    r"\(rows=(\d+), 밀림=([\d.-]+)초(?:, 재시도함)?, 타폴러동시호출추정=(\S+?)\)"
    # 2026-08-07 Fix#3 — `분=HH:MM`은 **선택**이다. 08-07까지의 로그에는 없고, 그 날들을
    # 재집계할 때 이 정규식이 통째로 눈이 머는 것이 08-04 §2-1에서 겪은 사고다.
    r"(?: 분=(\d\d:\d\d))?"
)
_OVERRUN_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:옵션 체인 폴링 사이클이 주기\([\d.]+초\)를 초과해 "
    r"스케줄이 ([\d.]+)초 밀렸습니다"
)
# 2026-08-11 Fix#7 — **폴러 이름을 잡는** 밀림 정규식.
#
# 08-11 리포트 §11이 「overrun 파서 0 / 로그 실재 1」로 계측 감사 실패를 띄웠다. 그런데
# **파서는 옳았다** — 그 1건은 옵션체인이 아니라 `만기 유동성` 폴러의 밀림이었고, 위
# `_OVERRUN_RE`는 설계대로 옵션체인만 센다. 틀린 것은 **감사 토큰**(`"스케줄이"`)이었다:
# 여섯 폴러가 전부 같은 문장을 쓰므로 느슨 검사가 항상 다른 폴러를 주워 왔다.
#
# 감사 토큰을 좁히는 것만으로 ⚠는 사라지지만, 그러면 **그 1건이 어디에도 안 남는다.**
# 옵션체인 밖의 밀림도 실재하는 사건이므로(만기유동성이 밀리면 그 북의 호가가 그만큼 늙는다)
# 여기서 폴러별로 함께 센다. `overrun`(옵션체인 전용)의 의미는 그대로 둔다 — 그 위에
# 08-04부터의 가설들이 얹혀 있다.
_ANY_OVERRUN_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:(.+?) 사이클이 주기\([\d.]+초\)를 초과해 스케줄이 ([\d.]+)초 밀렸습니다"
)
_HTTPX_RE = re.compile(_TS + r' INFO:httpx:HTTP Request: (\w+) (\S+) "HTTP/1\.1 (\d+)')
_BACKOFF_RE = re.compile(
    _TS + r" INFO:mahdi\.broker\.rest_client:레이트리밋 백오프 (확대|회복): "
    r"[\d.]+s -> [\d.]+s \(기준 대비 ([\d.]+)배\)"
)
# 2026-08-04(운영점검보고서 §2-1) — **레벨을 정규식에 고정하지 않는다.**
# 08-03에 이 줄이 WARNING → INFO로 내려가면서 이 정규식이 통째로 눈이 멀었고, 08-04 리포트는
# 실제 362건을 **0건**으로 보고했다(그리고 §1의 전일 델타는 "▼933 ✅"라는 개선으로 표시됐다).
# 로그 레벨은 사람이 읽는 우선순위일 뿐 계측의 정체성이 아니다 — 문구로만 식별한다.
_SLOW_CALL_RE = re.compile(
    _TS + r" (?:INFO|WARNING):mahdi\.broker\.rest_client:느린 REST 호출 ([\d.]+)초 = "
    r"페이서대기 ([\d.]+)초 \+ HTTP ([\d.]+)초 \(배율 ([\d.]+)배, (\w+) (\S+)\)"
)
_CATCHUP_RE = re.compile(
    _TS + r" INFO:mahdi\.main:옵션체인 결손 회수: (\d\d):(\d\d) 분을 먼슬리 (\d+)레그로 채움"
)
# 2026-08-04(고도화#5) — REST 응답시간 요약(`mahdi.main.LOG_REST_LATENCY`, 5분 주기).
# 본문은 `엔드포인트=N건 p50/p95/p99/max초`가 공백으로 이어진 형태라 두 단계로 나눠 읽는다.
_REST_LATENCY_RE = re.compile(_TS + r" INFO:mahdi\.main:REST 응답시간\([\d.]+초 창\): (.+)$")
_REST_LATENCY_ITEM_RE = re.compile(
    r"([\w-]+)=(\d+)건 ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)초"
)
# 2026-08-04(고도화#3) — ATM 롤링(`mahdi.main.LOG_ATM_ROLL`). 북마다 1줄이라 같은 시각·같은
# 스팟이 여러 줄로 나온다 — 이벤트 수를 세려면 (시각, 전창→후창)으로 중복을 제거해야 한다.
_ATM_ROLL_RE = re.compile(
    _TS + r" INFO:mahdi\.main:ATM 롤링: 스팟 ([\d.]+) — 행사가 (\S+) → (\S+)"
)
# 같은 (전창→후창)이 이 시간 안에 다시 나오면 같은 이벤트의 다른 북 줄로 본다.
# 08-04 실측으로 세 줄은 0.3초 안에 붙어 나왔고, 진짜 재전이는 폴링 주기(60초) 이상 걸린다.
_ATM_ROLL_DEDUP_SECONDS = 5.0

# 2026-08-19 (08-18 보고서 §2-4) — 컷 로그 꼬리 라벨의 **이름이 바뀌었다.**
#
# `우선순위위반` → `데드라인이먼슬리에서끝남`. 옛 이름은 08-18 보고서가 P1을 잘못 진단하게
# 만들었다(상세 근거는 `mahdi/main.py`의 `LOG_CHAIN_BUDGET_EXCEEDED` 위 주석) — 데드라인
# 경로에서 그 라벨의 `예`는 **위반이 아니라 「먼슬리가 예산을 다 썼다」**이다.
#
# **옛 이름도 계속 읽는다.** 로그는 10MB×10 로테이션이라 이틀치가 남고, 이름을 바꾸느라
# 그 이틀을 「못 쟀다」로 만들면 이름을 고친 대가가 지표 손실이 된다. 정규식 하나로 둘을
# 받으면 개명 전후가 같은 축에 쌓인다 — 이 저장소가 08-04에 배운 것(포맷을 바꾸면 파서가
# 조용히 죽는다)의 반대편 적용이다.
_PRIORITY_LABEL = r"(?:우선순위위반|데드라인이먼슬리에서끝남)"
# 2026-08-04(Fix#8) — 수집 예산 초과(`mahdi.main.LOG_CHAIN_BUDGET_EXCEEDED`).
#
# 2026-08-11 Fix#2 — 꼬리에 `컷당한북=<series[,series]>`가 붙었다. **선택 그룹으로 둔다**:
# 08-10 이전 로그에는 없고, 없는 날을 `None`으로 두어야 "컷이 없었다"와 "못 쟀다"가 갈린다
# (규약 C — 라벨이 0이면 count 0은 증명이 아니다).
# 2026-08-12 Fix#5 — 그 뒤에 `· 우선순위위반=예|아니오`가 하나 더 붙었다. **역시 선택 그룹**이다
# (08-11 로그에는 없다). 없는 날은 `None`이고, 그때 「위반 0건」이라고 말하면 안 된다 — 규약 C.
_BUDGET_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:옵션체인 수집 예산\([\d.]+초\) 초과 — 남은 (\d+)레그를 포기하고 (\d+)레그로"
    r"(?:.* 컷당한북=(\S+))?(?:.* " + _PRIORITY_LABEL + r"=(\S+))?"
)
# 2026-08-11 Fix#1 — 연속 타임아웃 조기 포기(`mahdi.main.LOG_CHAIN_TIMEOUT_ABORT`).
#
# **예산 초과와 반드시 나눠 센다.** 둘 다 "레그를 포기했다"지만 원인이 다르다 — 예산 초과는
# *우리가 느렸다*, 조기 포기는 *KIS가 4초 천장에 닿았다*이다. 08-11에 22분 연속으로 후자가
# 났는데 종전 로그는 둘을 같은 줄로 냈다.
# 2026-08-25 (08-25 §1-5 / P1-3) — `%d회` 뒤에 `(리셋=예|아니오)`가 붙었다(발동 시점 값 보존과
# 함께 들어간 꼬리표). **선택 그룹**이다: 08-24 이전 로그에는 없고, 없는 날의 카운트가 죽으면
# 이름을 고친 대가가 지표 손실이 된다(`_PRIORITY_LABEL` 개명과 같은 규약).
_TIMEOUT_ABORT_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:옵션체인 연속 타임아웃 (\d+)회(?:\(리셋=[^)]*\))? — 남은 (\d+)레그를 포기하고"
    r".*적재 (\d+)행 · 컷당한북=(\S+)(?: · " + _PRIORITY_LABEL + r"=(\S+))?"
)
# 2026-08-11 고도화 A — 누적 실패 예산 소진(`mahdi.main.LOG_CHAIN_FAILURE_BUDGET`).
#
# 연속 타임아웃과 **또 다르다**: 저쪽은 KIS가 천장에 닿아 전멸하는 패턴이고, 이쪽은 성공과
# 실패가 섞여 절반이 죽는 패턴이다. 08-11 14시대가 후자였다(예산 초과 20건 / 전멸 1건).
# 셋을 한 지표로 세면 "무엇이 이 분을 얇게 만들었는가"에 답할 수 없다.
_FAILURE_BUDGET_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:옵션체인 실패 예산\((\d+)건\) 소진 — 남은 (\d+)레그를 포기하고"
    r".*적재 (\d+)행 · 컷당한북=(\S+)(?: · " + _PRIORITY_LABEL + r"=(\S+))?"
)
# 2026-08-06(고도화#1) — 먼슬리 레그 재시도(`mahdi.main.LOG_CHAIN_PRIORITY_RETRY`).
# **시도와 회복을 둘 다 센다**: 회복 0건은 "KIS가 계속 느렸다"이고, 시도 0건은 "예산이 없었다"라
# 원인이 다르다. 시도만 세면 그 둘이 같은 0으로 보인다.
#
# ===== 2026-08-24 (08-24 §1-9 / Fix#4) — **파서를 먼저 넓히고 나서 문구를 바꾼다** =====
#
# 같은 날 `mahdi.main`이 이 줄을 **회복 실패 때 WARNING으로** 올린다(15:10:50에 「3개 중
# 0개 회복」이 났다 — 되살리기가 실제로 실패한 첫 사례다). 레벨만 `INFO`로 고정돼 있으면
# 그 순간 이 파서는 **정확히 그 사건에서만 눈이 먼다.**
#
# 08-04에 같은 일이 반대 방향으로 일어났다(WARNING→INFO 강등에 정규식이 눈이 멀어 362건이
# 0건으로 보고). 그래서 **레벨을 계측의 정체성으로 쓰지 않는다** — 레벨은 사람이 읽는
# 우선순위이고, 이 파서가 세는 것은 「재시도가 돌았는가」다.
_PRIORITY_RETRY_RE = re.compile(
    _TS + r" (?:INFO|WARNING):mahdi\.main:먼슬리 레그 재시도: (\d+)개 중 (\d+)개 회복"
)
# ===== 2026-08-12 고도화 1 — 재연결을 **비용**으로 계량한다 =====
#
# 종전에 재연결은 `qualitative.ws_reconnect`(최초 끊김만)와 `failures`(재연결 후 재단절)로
# **나뉘어** 세어졌고, 시각이 없어 「언제 몰렸는가」를 물을 수 없었다. 08-12에 31회가
# 09:13~10:10에 집중됐다는 사실은 사람이 로그를 손으로 훑어서 알았다.
#
# 두 줄을 **한 축으로** 모은다 — 둘 다 「연결이 끊겼다」는 같은 사건이고, 최초인지 반복인지는
# 원인 규명에 쓰이지 **비용 계산에는 안 쓰인다**(둘 다 재구독과 관측 공백을 만든다).
#
# 「WS 재연결 시도 실패」는 **세지 않는다**: 그것은 이미 끊긴 상태에서 붙기를 시도하다 실패한
# 것이라 새 단절이 아니다(`WarningThrottle`이 60초당 1건으로 누르기까지 한다 — 세면 억제
# 정책이 곧 지표가 된다).
_WS_DISCONNECT_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:WS (연결 끊김|재연결 후 다시 끊김) — "
)
_LEVEL_RE = re.compile(_TS + r" (INFO|WARNING|ERROR|CRITICAL|DEBUG):(\S+?):")

# 2026-08-11 Fix#2 — 먼슬리(판단 주입력) 북의 series 라벨.
#
# 이 모듈은 `mahdi.main`을 임포트하지 않는다(순수 텍스트 파서라 의도적이다). 그래서 상수를
# 복제하되 **계약 테스트가 `main.OPTION_CHAIN_PRIORITY_SERIES`와의 일치를 강제**한다
# (`test_log_metrics_priority_series_matches_main`). 복제 자체가 위험한 게 아니라
# **복제가 조용히 갈라지는 것**이 위험하고, 그것은 테스트로 막는 편이 임포트보다 싸다.
PRIORITY_SERIES_LABEL = "regular"


def _priority_cut_minutes(events: list[dict]) -> int | None:
    """
    입력: `컷당한북` 라벨을 가진 이벤트 목록.
    계산: 그중 **먼슬리가 잘린 분**의 수. 라벨이 하나도 없으면 `None`(= 못 쟀다).
    해석: 규약 C — 라벨 없는 구 로그(08-10 이전)에서 0을 돌려주면 "컷이 없었다"는 거짓말이 된다.
    실패 조건: 없다.
    """
    labelled = [e for e in events if e.get("cut_books")]
    if not labelled:
        return None
    return sum(1 for e in labelled if PRIORITY_SERIES_LABEL in e["cut_books"].split(","))


# 2026-08-12 Fix#5 — 로그가 실어 보내는 순서 위반 라벨.
PRIORITY_VIOLATION_LABEL = "예"


def _ws_disconnect_metrics(seconds_of_day: list[float]) -> dict:
    """
    입력: WS 단절이 관측된 시각들(자정 기준 초).
    계산: 총 횟수 · **시간대별 분포** · 가장 몰린 시간대 · 최초/최종 시각.
    해석: 2026-08-12 고도화 1. 08-12에 31회가 09~10시에 몰렸는데 종전 지표로는 그 편중이
         안 보였다(`qualitative.ws_reconnect`는 최초 끊김 1건만 세고 나머지는 `failures`로
         흩어졌다). **편중이 곧 진단이다** — 그날 09:13의 단 한 번이 자기지속 루프를 열었고
         나머지 30회는 그 결과였다(§7-1).
    실패 조건: 없다. 빈 입력이면 count 0 / by_hour 빈 dict.

    ⚠ **임계를 걸지 않는다.** 정상 재연결 횟수의 분포를 모른다 — 표본이 08-04 1회, 08-11 1회,
      08-12 31회로 셋뿐이다. 모르는 채 임계를 정하면 그 임계가 곧 결론이 된다.
    """
    by_hour: dict[str, int] = {}
    for at in sorted(seconds_of_day):
        by_hour[f"{int(at // 3600):02d}시"] = by_hour.get(f"{int(at // 3600):02d}시", 0) + 1
    busiest = max(by_hour.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    return {
        "count": len(seconds_of_day),
        "by_hour": by_hour,
        "busiest_hour": busiest[0] if busiest else None,
        "busiest_hour_count": busiest[1] if busiest else 0,
        "first_at": _hhmm(min(seconds_of_day)) if seconds_of_day else None,
        "last_at": _hhmm(max(seconds_of_day)) if seconds_of_day else None,
    }


def _priority_before_others_minutes(events: list[dict]) -> int | None:
    """
    입력: 컷 이벤트 목록.
    계산: **아직 안 부른 위클리를 두고 먼슬리를 자른 분**의 수. 라벨이 하나도 없으면 `None`.
    해석: `_priority_cut_minutes`와 **다른 질문**이다. 저쪽은 「먼슬리가 잘렸는가」이고
         이쪽은 「먼슬리가 위클리보다 **먼저** 잘렸는가」다 — 불변식은 후자다.

         08-12에 이 구분이 없어 `priority_cut_minutes = 2`가 불변식 위반으로 보고됐다.
         실측하니 둘 다(12:49:53 / 13:51:53) **홀수분의 꼬리 컷**이었다: 그 분에는 위클리가
         애초에 due가 아니라 사이클 전체가 먼슬리였고, 50초 예산 끝에서 남은 2~3레그가 잘린
         것이다. **자를 것이 먼슬리밖에 없는 분에서 먼슬리를 자르는 것은 순서 문제가 아니다.**
         규약 G가 막는 것과 같은 형태의 오류였다 — 성립할 수 없는 상황에 임계를 걸었다.

         판정은 **로그를 내는 쪽**이 한다(`mahdi.main._collect_option_chain_cycle`). 그 시점에만
         "아직 안 부른 비우선 레그가 남아 있었는가"를 알 수 있고, 파서는 사후에 그것을 복원할
         수 없다 — 그 분에 위클리가 due였는지는 위상 설정(`OPTION_CHAIN_SLOW_SERIES_PHASE`)에
         달렸고 그 설정은 바뀐다.
    실패 조건: 없다.
    """
    labelled = [e for e in events if e.get("priority_violation")]
    if not labelled:
        return None
    return sum(1 for e in labelled if e["priority_violation"] == PRIORITY_VIOLATION_LABEL)

# 2026-08-05(§2-4) — **로그 레코드 한 줄**을 가리는 기준.
#
# 종전 `human_lines`는 httpx가 아닌 **모든 줄**을 셌다. 그래서 트레이스백 본문의
# `  File "...", line 101, in map_httpcore_exceptions` 한 줄 한 줄이 "사람이 읽는 줄"로 집계됐고,
# 08-05에 21,176줄 중 16,577줄(78%)이 그것이었다 — 그 값으로 자동 리포트 §0이 가설 `p4`를
# **반증 판정했는데 실측 4,599줄은 예측치(<=6,500)를 통과했다.** 08-04 Fix#6의 성공이
# 트레이스백에 묻혀 실패로 보고된 것이다.
#
# 판정 규칙이 이렇게 단순한 이유: 파이썬 `logging`은 레코드마다 타임스탬프를 찍고, 트레이스백
# 본문은 **레코드에 딸린 여러 줄**이라 타임스탬프가 없다. 형식이 아니라 구조가 만드는 구분이다.
_RECORD_START_RE = re.compile(_TS)
_FAILURE_RE = re.compile(_TS + r" WARNING:mahdi\.main:(.+?(?:실패|끊김))")

# 정성 카운트 — 줄의 **접두사**로 센다. 부분 문자열 포함으로 세면 트레이스백 한 사건이 여러 줄에
# 흩어져 있어 부풀려진다(2026-08-01 최초 실행에서 RemoteProtocolError가 8건 → 24건으로 부풀어
# 실제로 겪었다. 참고로 07-31 사람 보고서는 이걸 "0건"으로 잘못 적었고, 이 도구가 그 오류를
# 첫날 잡아냈다).
_QUALITATIVE_MARKERS = {
    "ws_reconnect": "WS 연결 끊김",
    "cycle_total_failure": "옵션 체인 폴링 전체 실패",
    "market_operation_message": "장운영정보(H0UNMKO0) 수신",
    "option_chain_gap_alert": "옵션체인 결손 알림",
    "egw00201": "EGW00201",
    # 2026-08-05 — 수기 이벤트 캘린더가 안 채워진 상태. **이 항목이 존재하는 이유가 곧
    # 수기 방식을 고른 대가다**: 안 채우면 `event_proximity_minutes`가 None으로 돌아가고
    # 그것은 2026-08-05 이전(페널티 한 번도 안 걸림)과 완전히 같은 상태인데, 지표상으로는
    # 아무 일도 없는 것처럼 보인다. 여기 등록해 매일 건수로 드러낸다.
    # 포맷 원본: `mahdi.main.LOG_EVENT_CALENDAR_NOT_COVERED`
    # (계약은 tests/test_ops_log_metrics_contract.py가 지킨다 — 이 모듈은 순수 파서로 남긴다.)
    "event_calendar_not_covered": "이벤트 캘린더 미기입",
    # 2026-08-13 §5 / 고도화 1 — 만기 당일 북을 감마플립 대상에서 제외한 사실.
    # **하루 1회 INFO**라(`options_intel.note_expiring_book`) 건수가 곧 「제외한 북 수」다.
    #
    # 여기 등록하는 것이 이 고도화의 핵심이다. 제외만 하고 이 줄이 없으면 08-13의 383건이
    # 그냥 **사라지고**, 사라진 것과 「그날은 만기일이 아니었다」를 다음 사람이 구분할 수 없다 —
    # 2026-08-04에 WARNING→INFO 강등으로 362건이 0건으로 보고된 사고와 같은 실패 모드다.
    # 포맷 원본: `mahdi.features.options_intel.LOG_EXPIRING_BOOK_EXCLUDED`
    "excluded_expiring_books": "만기 당일 북 제외",
    # 2026-08-05(§2-5) — 감마플립이 수집 행사가 범위 밖이라 기각된 건수.
    # **이 값이 0이라고 안심하지 말 것**: 0은 "기각할 것이 없었다"이지 "flip이 잘 나온다"가
    # 아니다. 반드시 §14의 `gamma_flip_out_of_range_count`(DB 기준 불변식)와 나란히 읽는다 —
    # 로그 마커는 기각된 것을, DB 불변식은 **기각을 통과해 적재된 것 중 범위 밖인 것**을 센다.
    # 후자가 0이 아니면 이 fix가 뚫린 것이다.
    # 포맷 원본: `mahdi.features.options_intel.LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE`
    "gamma_flip_out_of_leg_range": "감마플립 기각(레그 범위 밖)",
    # 2026-08-05(§2-6) — 사이클은 돌았는데 적재 0행인 분. **결손 지표(로그 기준)가 세지 않는
    # 유일한 손실 유형**이라 여기서 따로 센다. DB 축(`db.chain_minute_coverage`)과 나란히 읽는다.
    # 포맷 원본: `mahdi.main.LOG_CHAIN_CYCLE_EMPTY`
    "chain_cycle_empty": "옵션체인 이번 분 전멸",
    # ===== 2026-08-23 (08-21 §4 Fix#3·#5·#8) — 셋 다 「사건이 로그에 남는가」를 재는 축이다 =====
    #
    # 셋 모두 08-21에 **줄 자체가 없어서** 사람이 손으로 복원한 사실들이다:
    #   · 만기유동성 버스트가 돌았는지  → HTTP 호출 수로 역산했다(§3-3)
    #   · 판단 축이 언제 빠졌는지        → 로그 두 줄을 겹쳐 여섯 번 세었다(§1-10)
    #   · warmup이 언제 끝났는지         → 로그로는 상한 74분까지만 좁혔다(§1-12)
    #
    # **여기 등록하는 것이 그 fix들의 절반이다.** 줄만 찍고 세지 않으면 다음 사람이 그 줄의
    # 존재를 다시 손으로 확인해야 하고, 문구가 바뀌어 파서가 눈이 먼 날(08-04, 362건 → 0건)을
    # 알아챌 방법도 없다. `_PARSER_AUDIT_TOKENS`가 그 눈멂을 잡는 장치이고 이 등록이 그 입구다.
    #
    # 포맷 원본: `mahdi.main.LOG_EXPIRY_BURST_DONE`
    "expiry_burst_done": "만기 유동성 버스트 완료",
    # 포맷 원본: `mahdi.fusion.engine.LOG_MEMBER_AXIS_EXIT` / `LOG_MEMBER_AXIS_RETURN`
    "member_axis_exit": "판단 축 이탈",
    "member_axis_return": "판단 축 복귀",
    # 포맷 원본: `mahdi.engines.regime_pipeline.LOG_REGIME_WARMUP_END`
    "regime_warmup_end": "레짐 warmup 종료",
    # ===== 2026-08-23 (실행 배선 ①) — 포지션 생애주기 =====
    #
    # 이 넷이 「포지션이 언제 생겨 언제 사라졌는가」의 로그 축이다. 정본은 DB
    # (`db.ledger`)이고 이쪽은 **장중에 볼 수 있는 사본**이다 — warmup 종료 줄과 같은 관계다.
    #
    # `position_held`가 **없는 것이 의도**다: 유지는 사건이 아니고, 매 사이클 찍으면 그 줄들이
    # 진짜 사건을 덮는다(08-21 §1-11의 DEGRADED 14줄이 정확히 그 형태였다).
    #
    # 포맷 원본: `mahdi.execution.position_ledger.LOG_POSITION_OPENED` 외 3종
    "position_opened": "포지션 개시",
    "position_closed": "포지션 종료",
    "position_qty_changed": "포지션 수량 변경",
    # ⚠ 이 줄은 WARNING이다 — 브로커에는 있는데 원장에 없는 포지션이다. 0이 아닌 날은
    #   사람이 직접 냈거나 원장 기록이 실패한 것이고, 둘 다 사람이 봐야 한다.
    "orphan_position": "원장에 없는 포지션 발견",
    # ===== 2026-08-23 (실행 배선 ②) — 체결통보 =====
    #
    # `db.order_notices`(건수·필드 수)와 나란히 읽는다. **DB가 답 못 하는 것을 이쪽이 답한다:**
    # 통보 0건이 「체결이 없었다」인지 「스트림이 안 붙었다」인지는 DB만 봐서는 못 가린다
    # (둘 다 0행이다). 구독 성립 줄이 그 구분이다 — 08-03에 H0UNMKO0 수신 0건을 두고 같은
    # 질문에 답 못 했던 것이 `SubscriptionAck`을 만든 이유다.
    #
    # 포맷 원본: `mahdi.broker.order_notice.LOG_NOTICE_SUBSCRIBED` 외
    "order_notice_subscribed": "체결통보 구독 성립",
    "order_notice_received": "체결통보 수신",
    # ⚠ 아래 둘은 WARNING이다. `not_configured`는 사람이 .env를 채워야 하는 상태이고,
    #   `stream_down`은 붙어 있지 않은 상태다 — 주문이 나가는데 알림이 없는 것이 이 시스템에서
    #   가장 위험한 상태다.
    "order_notice_not_configured": "체결통보를 구독하지 않는다",
    "order_notice_stream_down": "체결통보 스트림 끊김",
    # ===== 2026-08-23 (실행 배선 ③) — 주문 =====
    #
    # `db.orders`(건수)와 나란히 읽는다. **DB가 답 못 하는 것을 이쪽이 답한다:** 주문 0건이
    # 「진입 신호가 없었다」인지 「막혔다」인지는 `execution_logs`만 봐서는 못 가린다(둘 다
    # 0행이다). `order_blocked` 줄이 그 구분이고, 사유가 그 줄 안에 있다.
    #
    # 포맷 원본: `mahdi.main.LOG_ORDER_SUBMITTED` / `LOG_ORDER_BLOCKED` / `LOG_ORDER_STATE_CHANGED`
    "order_submitted": "주문 제출",
    "order_blocked": "주문 미제출",
    "order_state_changed": "주문 상태 전이",
    # ===== 2026-08-23 (실행 배선 ④·⑤) — 청산 =====
    #
    # **`exit_price_missing`이 이 묶음에서 가장 중요하다.** 현재가를 모르면 하드스톱이 안
    # 걸리는데, 그 사실이 안 세어지면 「하드스톱이 한 번도 안 걸렸다」를 「손실이 없었다」로
    # 읽게 된다. 「안 걸렸다」와 「평가하지 못했다」는 다른 사건이다(규약 C).
    #
    # 포맷 원본: `mahdi.main.LOG_EXIT_TRIGGERED` / `LOG_EXIT_PRICE_MISSING` /
    #            `LOG_FORCED_FLAT_VERIFY`
    "exit_triggered": "청산 판정",
    "exit_price_missing": "청산 평가에 현재가가 없다",
    "forced_flat_verified": "15:10 강제청산 자기검증",
    # ===== 2026-08-24 (08-24 §1-8 / Fix#6) — 계좌 잔고 폴링이 한 사이클 통째로 빠진 것 =====
    #
    # 08-24 12:34:32에 당일 첫 사례가 났고, 그 인과를 **하루에 세 번 뒤집었다**(장중 ①이
    # 「지연 상승이 떨어뜨렸다」, 장중 ②가 「7번 중 1번짜리 우연」, 장후가 「확대→실패는
    # 2/21이지만 실패→확대는 2/2」). 세 번 다 서로 다른 파일의 줄을 밀리초로 맞춰 본 결론이다.
    #
    # 그 줄이 하루 몇 건인지조차 어느 지표에도 없었다. **분자를 먼저 센다** — 분모(백오프
    # 확대)는 `backoff` 절이 이미 세고 있고, 둘을 나란히 놓는 것이 증거 수집기 §5-1의 일이다.
    #
    # 포맷 원본: `mahdi.main.LOG_BALANCE_POLL_FAILED`
    "balance_poll_failed": "계좌 잔고 폴링 사이클 실패",
    # ===== 2026-08-25 (08-24 §1-6 / Fix#3) — 진입 판단은 섰는데 후보 종목이 0건인 분 =====
    #
    # 분 단위 정본은 DB(`selected_instruments.reason` · `strategy_rejected`)다. 이쪽은 **장중에
    # 볼 수 있는 사본**이고, 억제가 걸려 있어(사유 전환 시 + 5분 재확인) 건수는 「그 상태가
    # 몇 분이었나」가 아니라 「그 줄이 몇 번 남았나」다 — 분 수로 읽으면 안 된다.
    #
    # 포맷 원본: `mahdi.main.LOG_ENTRY_NO_CANDIDATE`
    "entry_no_candidate": "진입 판단은 섰는데 살 종목이 없다",
}

# ===== 2026-08-23 — **0을 인쇄해야 하는 마커** =====
#
# `qualitative`는 Counter라 **0건인 항목은 키 자체가 안 생긴다.** 그 설계는 옳다: 옛 로그에
# 없던 문구를 0으로 찍으면 「판정했고 0」과 「그 줄이 없던 버전」이 섞이기 때문이다(규약 C).
#
# 그런데 위 넷은 **가설의 주장 지표**다. 키가 없으면 그 가설이 「경로 없음」으로 떨어지고,
# 그러면 사건이 0건이었던 하루가 fix의 실패로 읽힌다 — 08-06 §3-1이 겪은 사고의 형태다.
#
# 여기 등록된 마커는 **파서가 돌았다는 사실 자체를 0으로 표현한다.** 「이 버전에는 그 줄이
# 있고, 오늘은 그 사건이 0번이었다」가 이 0의 뜻이다. 그 이전 날짜의 로그를 다시 파싱하면
# 같은 0이 나오지만 뜻이 다르고, 그 구분은 `levers.git_head`와 가설의 `구현일`이 한다
# (`hypotheses.measurable_on()`이 그것을 이미 강제한다).
_QUALITATIVE_ALWAYS_PRESENT = (
    "expiry_burst_done", "member_axis_exit", "member_axis_return", "regime_warmup_end",
    # 2026-08-23 (실행 배선 ①) — 08-24 예측이 「전부 0」이라 **0이 인쇄돼야 검정된다.**
    # 키가 없으면 그 가설이 「경로 없음」으로 떨어지고, 포지션이 없었던 하루가 배선 실패로
    # 읽힌다(`2026-08-23-wiring1-ledger-runs-silent-until-a-position-exists`).
    "position_opened", "position_closed", "position_qty_changed", "orphan_position",
    # 2026-08-23 (실행 배선 ②) — 08-24 예측이 「구독 0 · 수신 0 · 미설정 1」이라 **0이
    # 인쇄돼야 검정된다**(`2026-08-23-wiring2-notice-stream-says-why-it-is-silent`).
    "order_notice_subscribed", "order_notice_received",
    "order_notice_not_configured", "order_notice_stream_down",
    # 2026-08-23 (실행 배선 ③) — 08-24 예측이 「제출 0 · 미제출 N」이라 **0이 인쇄돼야
    # 검정된다**(`2026-08-23-wiring3-code-is-wired-but-config-still-blocks`).
    "order_submitted", "order_blocked", "order_state_changed",
    # 2026-08-23 (실행 배선 ④·⑤) — 포지션이 0이면 셋 다 0이어야 한다. **0이 인쇄돼야
    # 검정된다**(`2026-08-23-wiring45-defence-runs-even-in-advisory`).
    "exit_triggered", "exit_price_missing", "forced_flat_verified",
    # 2026-08-24 Fix#6 — **0이 인쇄돼야 「실패가 없던 하루」와 「그 줄이 없던 버전」이 갈린다.**
    # 이 값은 하루 2건(08-24) 수준이라 0인 날이 흔하고, 그런 날 키가 없으면 이 축이 있는지
    # 없는지 다음 사람이 매번 다시 확인해야 한다.
    "balance_poll_failed",
    # 2026-08-25 Fix#3 — 후보가 매 분 잘 골라지는 날은 이 값이 0이고, **0이 인쇄돼야
    # 「고르는 데 실패가 없던 하루」와 「그 줄이 없던 버전」이 갈린다**(규약 C).
    "entry_no_candidate",
    # 2026-08-25 P2-2 — §14의 「수집 행사가 범위 밖」 자리가 이 값을 분모로 쓰게 됐다.
    # 기각 0건인 날 키가 없으면 그 칸이 「셈 없음」으로 인쇄돼, 진짜 0(기각할 것이 없었다)과
    # 옛 로그가 섞인다.
    "gamma_flip_out_of_leg_range",
)
# 예외 유형은 트레이스백 마지막 줄(`모듈.예외명: 메시지`)만 센다 — 사건 1건 = 1줄이 보장된다.
#
# 2026-08-04(§2-1) 경고: 이 방식은 **예외가 처리되지 않고 위로 전파될 때만** 참이다. 예외를
# 잡아서 한 줄로 요약하는 순간(=대개 옳은 수정) 이 카운터는 조용히 0이 된다. 08-03에 두 곳에서
# 정확히 그 일이 일어났다:
#   - `RemoteProtocolError` → 1회 재시도 도입(§4-3). 트레이스백 소멸, 실제 25건이 "실측 없음"으로.
#   - `HTTPStatusError`     → 트레이스백 제거(§2-8). 08-03 105건이 08-04에 키 자체가 사라짐.
# 그래서 아래 `_HANDLED_EXCEPTION_MARKERS`(처리된 예외의 요약 줄)와 `_FAILURE_RE` 기반 대체
# 계측을 함께 둔다. **예외 처리 방식을 바꾸면 여기도 함께 바꿔야 한다** — 그것을 잊었을 때
# 알려주는 것이 `_PARSER_AUDIT_TOKENS`다.
_EXCEPTION_PREFIXES = {
    "remote_protocol_error": "httpx.RemoteProtocolError",
    "http_status_error": "httpx.HTTPStatusError",
    "read_timeout": "httpx.ReadTimeout",
    "connect_error": "httpx.ConnectError",
}

# 처리된(=트레이스백이 없는) 예외의 요약 줄. 위 트레이스백 카운트와 **같은 키에 합산**한다 —
# 한 사건이 어느 쪽으로 기록되든 하루 총계는 같아야 하기 때문이다.
# 포맷 원본: `mahdi.broker.rest_client.LOG_REMOTE_PROTOCOL_RETRY`
# (계약은 tests/test_ops_log_metrics_contract.py가 지킨다 — 이 모듈은 순수 파서로 남긴다).
_HANDLED_EXCEPTION_MARKERS = {
    "remote_protocol_error": "커넥션 재사용 실패(RemoteProtocolError)",
}

# 2026-08-05(§2-4) — 트레이스백 표본 소진 후의 요약 줄을 **원래 유형 키에 합산**하기 위한 마커.
# 뒤에 `_EXCEPTION_PREFIXES`의 유형명이 그대로 붙는다(`트레이스백 생략 — httpx.ReadTimeout`).
#
# 트레이스백 마지막 줄(`httpx.ReadTimeout: ...`)과 **겹치지 않는 형태**여야 한다 —
# `_EXCEPTION_PREFIXES`는 접두사로, 이쪽은 부분문자열로 세므로 겹치면 한 사건이 두 번 세어진다.
# 포맷 원본: `mahdi.main.LOG_KIS_FAILURE_TRACEBACK_OMITTED`
_TRACEBACK_OMITTED_MARKER = "트레이스백 생략 — "

# 2026-08-06(§3-2 / Fix#4) — **로그에 줄이 안 남은 예외를 세어 넣는다.**
#
# 08-06 실측: `qualitative.read_timeout` 126건, 실제 205건. **39%가 집계에서 사라졌다.**
#
# 원인은 `main._log_kis_call_failure()`의 호출 순서다:
#
#     keep, nth = _TRACEBACK_BUDGET.take(exc)   # 카운터는 여기서 무조건 올라간다
#     ...
#     throttle.warning(category, fmt, *args)    # 60초 창에 걸리면 줄이 통째로 사라진다
#
# `WarningThrottle`이 같은 카테고리를 창당 1건만 남기므로 81건이 카운터만 올리고 줄은 안 남았다.
# **완전 소실은 아니었다** — 다음 로그에 `(최근 60초간 M건 추가 억제됨)`이 붙고 08-06에 그런 줄이
# 53건 있었다. 그 M을 **아무도 안 읽고 있었을 뿐이다.**
#
# 그래서 로그 포맷을 바꾸지 않고 집계만 고친다(저위험 쪽을 골랐다). 억제된 건수는 그 요약을
# 실은 줄의 예외 유형에 합산한다 — 창이 60초라 그 안에서 유형이 바뀌는 경우는 드물고, 유형을
# 모르는 채 버리는 것보다 낫다. **다만 합산분은 `qualitative_suppressed`에 따로도 남긴다**:
# 근사가 섞였다는 사실 자체가 보이지 않으면 그 근사가 곧 사실로 굳는다.
# 포맷 원본: `mahdi.logutil.WarningThrottle.warning`
_THROTTLE_SUPPRESSED_RE = re.compile(r"최근 \d+초간 (\d+)건 추가 억제됨")

# 프로세스 기동 표식 — 이 줄은 프로세스당 정확히 한 번 나온다.
# 포맷 원본: `mahdi.main._log_startup_gap_since_last_run`
#
# 2026-08-06(§3-3 / Fix#4, §3-5 / Fix#6): 08-06에 프로세스가 **세 번** 떴고, 그래서
#   (1) `오늘 N번째` 트레이스백 카운터가 두 번 되감겨 같은 번호가 로그에 두 번 나왔고,
#   (2) 결손 21분 중 20분이 "프로세스가 아예 안 돌던 구간"인데 인프라 결손으로 집계됐다.
# 이 표식을 세면 두 문제 모두 리포트에서 보인다.
_PROCESS_START_MARKER = "직전 정상 기동"

# KIS가 rt_cd/msg_cd를 실은 에러 응답(대개 HTTP 500). 2026-08-03 §2-8이 트레이스백을 떼면서
# `http_status_error`(트레이스백 기반)가 죽었으므로, 지금 로그 모양 그대로에서 다시 센다:
#   WARNING:mahdi.main:옵션 체인 폴링 실패: C01608875 — {"rt_cd":"1","msg_cd":"EGW00201",...}
# `_FAILURE_RE`가 이미 잡는 줄이라 추가 순회가 필요 없다.
_KIS_ERROR_BODY_TOKEN = '"rt_cd"'

# ===== 0건 보고의 증명(2026-08-04 §2-1 / 고도화#1 규약 C) =====
#
# 엄격 파서가 0을 냈을 때, **그 마커의 핵심 토큰이 로그에 실제로 없는지** 느슨하게 한 번 더 센다.
# 엄격 0 · 느슨 >0 이면 그것은 "오늘 안 일어났다"가 아니라 **"파서가 눈이 멀었다"** 는 뜻이다.
# 08-04에 이 감사가 있었다면 `slow_calls` 0건(느슨 362건)이 즉시 ⚠로 떴을 것이다.
# 토큰은 포맷이 바뀌어도 살아남을 만큼 짧게 고른다(레벨·수치·엔드포인트를 포함하지 않는다).
_PARSER_AUDIT_TOKENS = {
    "cycles": "옵션체인 사이클 소요 분해",
    # 2026-08-11 Fix#7 — `"스케줄이"`에서 좁혔다. 여섯 폴러가 같은 문장을 쓰므로 느슨 검사가
    # 항상 다른 폴러를 주워 와 **매일 거짓 ⚠**를 냈다(08-11에 실제로 그랬다).
    # 옵션체인 밖의 밀림은 `overrun_by_poller`가 따로 센다.
    "overrun": "옵션 체인 폴링 사이클이",
    "backoff": "레이트리밋 백오프",
    "slow_calls": "느린 REST 호출",
    "catchups": "옵션체인 결손 회수",
    "rest_latency": "REST 응답시간",
    "atm_rolls": "ATM 롤링",
    "budget_exceeded": "수집 예산",
    "timeout_abort": "연속 타임아웃",
    "failure_budget_abort": "실패 예산",
    "remote_protocol_error": "RemoteProtocolError",
    "read_timeout": "ReadTimeout",
    "connect_error": "ConnectError",
    "kis_error_response": _KIS_ERROR_BODY_TOKEN,
    "event_calendar_not_covered": "이벤트 캘린더",
    # 엄격 마커는 "감마플립 기각(레그 범위 밖)"이다 — 괄호 안 문구를 바꾸면 엄격 파서가 0이
    # 되는데, 이 짧은 토큰이 그 침묵을 ⚠로 드러낸다.
    "gamma_flip_out_of_leg_range": "감마플립 기각",
    "chain_cycle_empty": "이번 분 전멸",
    # 2026-08-25 Fix#3 — 엄격 마커는 「진입 판단은 섰는데 살 종목이 없다」 전문이다.
    "entry_no_candidate": "살 종목이 없다",
}


# ===== 2026-08-06 §3-4 / Fix#5 — 실패의 **원인** =====
#
# 08-05 `p2`는 *"read 타임아웃을 엔드포인트별로 나누면 실패가 사라진다"* 고 주장하면서
# `failures.만기 유동성 폴링 실패 <= 1`을 주장 지표로 걸었다. 08-06 실측 7건 → **자동 판정 반증.**
#
# 그런데 그 7건의 원인은 **전부 EGW00201(레이트리밋)이었고 ReadTimeout 기인은 0건이었다.**
# 가설의 주장은 오히려 맞았는데, 지표가 원인을 안 갈라 그것을 볼 수 없었다.
#
# 규약 E("한쪽만 재지 마라")의 다음 칸이 이것이다: **주장 지표는 주장과 같은 축으로 잘려야 한다.**
# 원인을 주장하는 fix는 원인별 지표로 검정해야 한다.
#
# 분류 순서가 곧 우선순위다 — 한 줄에 여러 단서가 있으면 **더 구체적인 것**이 이긴다
# (EGW00201은 `"rt_cd"` 응답의 한 종류이므로 먼저 본다).
FAILURE_CAUSE_EGW00201 = "egw00201"          # 우리 페이서가 KIS 초당 한도를 건드렸다
FAILURE_CAUSE_KIS_ERROR = "kis_error"        # 그 외 rt_cd 에러 응답(계좌 미신청 등)
FAILURE_CAUSE_OTHER = "other"                # 위 어디에도 안 걸림 — 늘어나면 분류를 늘릴 때다

_EGW00201_TOKEN = "EGW00201"

# KIS 응답 중에는 **`rt_cd`에 따옴표가 없는 것**이 있다. 해외선물 시세 오류가 그렇다:
#   {rt_cd:"1","msg1":"CME SUB거래소 신청 계좌가 아닙니다.","msg_cd":"EGW00552"}
# `_KIS_ERROR_BODY_TOKEN`('"rt_cd"')만 보면 ES 조회 실패 9건/일이 통째로 `other`로 떨어져
# "분류를 늘릴 때"라는 신호를 거짓으로 울린다(2026-08-06 첫 실측에서 실제로 그랬다 — other 11건
# 중 9건이 이것이었다). `msg_cd`는 그 응답들이 공통으로 싣는 필드다.
_KIS_ERROR_CODE_TOKEN = "msg_cd"


def classify_failure_cause(line: str) -> str:
    """
    입력: `_FAILURE_RE`가 잡은 실패 로그 한 줄.
    반환: 원인 키(`egw00201` / `kis_error` / `read_timeout` 등 예외 유형 / `other`).
    해석: 예외 유형 키는 `_EXCEPTION_PREFIXES`에서 그대로 가져온다 — 유형이 늘어도 자동으로
         따라오고, 다른 절(`qualitative`)과 **같은 이름**을 쓰므로 두 축을 교차해 읽을 수 있다.
    한계(알고 남긴다): **트레이스백이 살아 있는 예외는 `other`로 떨어진다.** 그때 실패 줄에는
         메시지만 있고 예외 유형은 다음 줄(트레이스백 마지막 줄)에 있기 때문이다. 예산이
         유형당 프로세스당 3건이므로 하루 최대 10건 안쪽이고(08-06 실측 3건), `other`가 그보다
         크게 늘면 그것 자체가 "분류를 늘릴 때"라는 신호다 — 리포트가 그 비율을 함께 낸다.
    """
    if _EGW00201_TOKEN in line:
        return FAILURE_CAUSE_EGW00201
    for key, prefix in _EXCEPTION_PREFIXES.items():
        # 처리된 예외는 `트레이스백 생략 — httpx.ReadTimeout` 형태로 한 줄에 실린다.
        if _TRACEBACK_OMITTED_MARKER + prefix in line:
            return key
    if _KIS_ERROR_BODY_TOKEN in line or _KIS_ERROR_CODE_TOKEN in line:
        return FAILURE_CAUSE_KIS_ERROR
    return FAILURE_CAUSE_OTHER


def classify_endpoint(url: str) -> str:
    """URL → 폴러 그룹. 어느 폴러가 페이서를 쓰고 있었는지 역산하는 유일한 단서다."""
    if "inquire-asking-price" in url:
        return "만기유동성"
    if "inquire-investor-time" in url:
        return "투자자수급"
    if "inquire-balance" in url:
        return "계좌잔고"
    if "overseas-futureoption" in url or "chartprice" in url:
        return "매크로"
    if "inquire-price" in url:
        return "옵션체인"
    if "oauth2" in url or "master/" in url:
        return "기동"
    return "기타"


def iter_day_lines(log_dir: Path, target: date, stem: str = "observation_loop.log") -> Iterator[str]:
    """
    입력: 로그 디렉터리, 대상 날짜.
    계산: `observation_loop.log{,.1,..,.N}`을 **오래된 것부터** 훑어 대상 날짜 줄만 흘려보낸다.
    해석: 로그는 10MB/10개 로테이션이라 **하루치가 `.log.1`과 `.log`에 걸치는 일이 실제로 있다**
         (07-30이 그랬다). 오래된 파일부터 읽어 시간순을 보장하고, 줄 단위 스트리밍으로
         메모리에 통째로 올리지 않는다(과거 105MB 파일 전례).
    실패 조건: 트레이스백 연속 줄은 타임스탬프가 없다 — **직전 타임스탬프 줄의 날짜를 승계**한다.
         이 처리를 빠뜨리면 ZN/ES 트레이스백이 통째로 누락돼 로그 볼륨 집계가 틀린다.
    """
    prefix = target.isoformat()
    backups = sorted(
        (p for p in log_dir.glob(f"{stem}.*") if p.suffix.lstrip(".").isdigit()),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,  # .log.10 → .log.1 (오래된 것부터)
    )
    for path in [*backups, log_dir / stem]:
        if not path.exists():
            continue
        carrying = False
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if len(line) >= 10 and line[4] == "-" and line[7] == "-" and line[:4].isdigit():
                    carrying = line.startswith(prefix)
                if carrying:
                    yield line.rstrip("\n")


def _seconds_of_day(m: re.Match, group_offset: int = 0) -> float:
    """정규식 매치의 타임스탬프 그룹(1=날짜, 2~5=시/분/초/밀리) → 자정 기준 초."""
    h, mi, s, ms = (int(m.group(2 + group_offset + i)) for i in range(4))
    return h * 3600 + mi * 60 + s + ms / 1000


def _duplicate_poll_minutes(cycles: list[dict]) -> dict:
    """
    입력: 파싱된 사이클 목록.
    계산: `분=HH:MM` 라벨이 **두 번 이상** 나온 분과 그 건수. 라벨이 실린 사이클 수도 함께 낸다.
    해석: 근거는 호출측 주석(2026-08-07 Fix#3). 이 값이 0이 아니면 그 분의 데이터는
         **다음 분에 수집된 값으로 덮여 있다** — 결손보다 나쁘다(행이 정상이라 안 보인다).
    실패 조건: 없다 — 라벨이 없는 로그는 `labelled=0`으로 그 사실을 드러낸다.
    """
    labels = [c.get("poll_minute") for c in cycles if c.get("poll_minute")]
    duplicated = sorted(m for m, n in collections.Counter(labels).items() if n > 1)
    return {"count": len(duplicated), "list": duplicated, "labelled": len(labels)}


def _hhmm(seconds_of_day: float) -> str:
    total = int(seconds_of_day)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}"


def _seconds_of_label(label: str) -> int:
    """`_hhmm()`의 역함수 — "HH:MM" → 그날 0시부터의 초.

    2026-08-10 — 결손 격자의 양 끝을 라벨 축에서 뽑기 위해 필요하다(자세한 이유는 호출부 주석).
    """
    hours, minutes = label.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 2),
        "median": round(statistics.median(values), 2),
        "max": round(max(values), 2),
    }


def parse_day(lines: Iterable[str], target: date) -> dict:
    """
    입력: 하루치 로그 라인(이미 날짜로 걸러진 것), 대상 날짜.
    계산: 07-31 조사에서 쓴 집계 14종(L1~L14)을 한 번 순회로 전부 뽑는다.
    실패 조건: 파싱되는 사이클이 하나도 없으면 각 절이 빈 값/None을 담는다 — **지어내지 않는다**.
    """
    cycles: list[dict] = []
    calls: list[tuple[float, str, str]] = []  # (초, 그룹, 상태코드)
    backoff_events: list[tuple[float, str, float]] = []
    slow_calls: list[dict] = []
    catchups: list[dict] = []
    latency_windows: list[dict] = []
    atm_rolls: list[tuple[float, str, str]] = []  # (초, 이전 창, 새 창) — 북 중복 제거 후
    budget_events: list[dict] = []
    # 2026-08-12 고도화 1 — WS 단절 시각(자정 기준 초). 최초/반복을 한 축으로 모은다.
    ws_disconnects: list[float] = []
    timeout_aborts: list[dict] = []
    failure_budget_aborts: list[dict] = []
    failures: collections.Counter = collections.Counter()
    levels: collections.Counter = collections.Counter()
    qualitative: collections.Counter = collections.Counter()
    # 2026-08-06 Fix#4 — 위 카운터에 **합산된** 억제분을 따로도 남긴다(근사가 섞였다는 사실을
    # 지운 채 총계만 내면 그 근사가 곧 사실로 굳는다).
    qualitative_suppressed: collections.Counter = collections.Counter()
    process_starts: list[float] = []
    failures_by_cause: dict[str, collections.Counter] = {}
    priority_retries: list[dict] = []
    audit_loose: collections.Counter = collections.Counter()
    overrun_seconds: list[float] = []
    overrun_by_poller: dict[str, list[float]] = {}
    total_bytes = 0
    httpx_bytes = 0
    total_lines = 0
    human_lines = 0
    traceback_lines = 0

    for line in lines:
        raw = line.encode("utf-8")
        total_bytes += len(raw) + 1
        total_lines += 1
        is_httpx = "INFO:httpx:" in line
        if is_httpx:
            httpx_bytes += len(raw) + 1
        elif _RECORD_START_RE.match(line):
            human_lines += 1
        else:
            # 트레이스백 본문(및 로거가 남긴 여러 줄 레코드의 2번째 줄 이후).
            # 0으로 만들지 않고 **분리해서 함께 보고한다** — 없애면 "트레이스백이 줄었다"와
            # "세는 것을 그만뒀다"가 구분되지 않는다(§2-4의 교훈은 정확히 그 구분에 관한 것이다).
            traceback_lines += 1

        for key, marker in _QUALITATIVE_MARKERS.items():
            if marker in line:
                qualitative[key] += 1
        for key, prefix in _EXCEPTION_PREFIXES.items():
            if line.startswith(prefix):
                qualitative[key] += 1
        for key, marker in _HANDLED_EXCEPTION_MARKERS.items():
            if marker in line:
                qualitative[key] += 1
        # 2026-08-05(§2-4) — 트레이스백 표본을 다 써서 한 줄로 요약된 예외.
        # 마커를 `_EXCEPTION_PREFIXES`의 유형명에서 **생성**하므로 유형이 늘어도 자동으로 따라온다
        # (규약 A의 정신 — 같은 사실을 두 곳에 적지 않는다).
        for key, prefix in _EXCEPTION_PREFIXES.items():
            if _TRACEBACK_OMITTED_MARKER + prefix in line:
                qualitative[key] += 1
                # 2026-08-06 Fix#4 — 이 줄이 "그동안 억제된 M건"을 함께 실었으면 그 M도 센다.
                # 억제된 건들은 로그에 줄이 아예 없어, 이 숫자가 유일한 흔적이다.
                m = _THROTTLE_SUPPRESSED_RE.search(line)
                if m:
                    extra = int(m.group(1))
                    qualitative[key] += extra
                    qualitative_suppressed[key] += extra
        if _PROCESS_START_MARKER in line:
            started = _RECORD_START_RE.match(line)
            if started:
                process_starts.append(_seconds_of_day(started))
        for key, token in _PARSER_AUDIT_TOKENS.items():
            if token in line:
                audit_loose[key] += 1

        # 레벨 집계는 아래 continue들보다 **먼저** 해야 한다 — httpx/사이클/백오프 줄이 전부
        # continue로 빠져나가면 INFO가 2건으로 집계된다(2026-08-01 최초 실행에서 실제로 겪었다).
        m = _LEVEL_RE.match(line)
        if m:
            levels[m.group(6)] += 1

        m = _HTTPX_RE.match(line)
        if m:
            calls.append((_seconds_of_day(m), classify_endpoint(m.group(7)), m.group(8)))
            continue

        m = _CYCLE_RE.match(line)
        if m:
            end = _seconds_of_day(m)
            rest, db_s, state_s, other_s = (float(m.group(i)) for i in (6, 7, 8, 9))
            concurrent = m.group(12).rstrip("건)")
            cycles.append(
                {
                    "start": end - (rest + db_s + state_s + other_s),
                    # 2026-08-14 장중 §3 / 고도화 2 — **사이클이 끝난 초.** `start`는 파생값
                    # (종료 − 소요 합)이라 반올림 오차가 있지만 이쪽은 로그 타임스탬프 그 자체다.
                    # 위상은 이 값으로 재야 한다(§10 `poller_phase`와 같은 축).
                    "end": end,
                    "rest": rest,
                    "db": db_s,
                    "rows": int(m.group(10)),
                    "slip": float(m.group(11)),
                    "concurrent_reported": None if not concurrent.isdigit() else int(concurrent),
                    # 2026-08-07 Fix#3 — 이 사이클이 적재한 분 라벨(구 로그에는 없어 None).
                    "poll_minute": m.group(13),
                }
            )
            continue

        m = _ANY_OVERRUN_RE.match(line)
        if m:
            poller = m.group(6).strip()
            seconds = float(m.group(7))
            overrun_by_poller.setdefault(poller, []).append(seconds)
            # `overrun`(옵션체인 전용)의 의미는 그대로 둔다 — 08-04부터의 가설들이 그 위에 있다.
            if _OVERRUN_RE.match(line):
                overrun_seconds.append(seconds)
            continue

        m = _BACKOFF_RE.match(line)
        if m:
            backoff_events.append((_seconds_of_day(m), m.group(6), float(m.group(7))))
            continue

        m = _SLOW_CALL_RE.match(line)
        if m:
            slow_calls.append(
                {
                    "at": _hhmm(_seconds_of_day(m)),
                    # 2026-08-23 고도화#1 — **창에 붙이려면 분보다 잘게 알아야 한다.**
                    # `at`(HH:MM)만으로는 5분 창의 경계에 걸린 호출을 어느 창에 넣을지 못 정한다.
                    "at_seconds": _seconds_of_day(m),
                    "total": float(m.group(6)),
                    "pacer": float(m.group(7)),
                    "http": float(m.group(8)),
                    "multiplier": float(m.group(9)),
                    "endpoint": m.group(11),
                }
            )
            continue

        m = _CATCHUP_RE.match(line)
        if m:
            catchups.append({"minute": f"{m.group(6)}:{m.group(7)}", "legs": int(m.group(8))})
            continue

        m = _REST_LATENCY_RE.match(line)
        if m:
            at = _seconds_of_day(m)
            for item in _REST_LATENCY_ITEM_RE.finditer(m.group(6)):
                latency_windows.append(
                    {
                        "at": at, "endpoint": item.group(1), "n": int(item.group(2)),
                        "p50": float(item.group(3)), "p95": float(item.group(4)),
                        "p99": float(item.group(5)), "max": float(item.group(6)),
                    }
                )
            continue

        m = _ATM_ROLL_RE.match(line)
        if m:
            at = _seconds_of_day(m)
            before, after = m.group(7), m.group(8)
            # 롤링은 **북마다 1줄**이라 한 이벤트가 3줄로 나온다(먼슬리/위클리 월/위클리 목).
            # 세 줄의 타임스탬프는 밀리초가 다르므로 시각까지 포함해 비교하면 중복이 안 걸린다
            # (08-04 실측: 582줄 = 194이벤트 x 3). 같은 (전창→후창)이 짧은 시간 안에 이어지면
            # 한 건으로 본다 — 진짜로 같은 전이가 두 번 일어나려면 그 사이에 되돌아가야 하고,
            # 그러려면 최소 한 번의 폴링 주기(60초)가 필요하다.
            if atm_rolls and atm_rolls[-1][1] == before and atm_rolls[-1][2] == after \
                    and at - atm_rolls[-1][0] < _ATM_ROLL_DEDUP_SECONDS:
                continue
            atm_rolls.append((at, before, after))
            continue

        m = _BUDGET_RE.match(line)
        if m:
            budget_events.append(
                {"at": _hhmm(_seconds_of_day(m)), "skipped": int(m.group(6)),
                 "collected": int(m.group(7)), "cut_books": m.group(8),
                 "priority_violation": m.group(9)}
            )
            continue

        m = _TIMEOUT_ABORT_RE.match(line)
        if m:
            timeout_aborts.append(
                {"at": _hhmm(_seconds_of_day(m)), "consecutive": int(m.group(6)),
                 "skipped": int(m.group(7)), "collected": int(m.group(8)),
                 "cut_books": m.group(9), "priority_violation": m.group(10)}
            )
            continue

        m = _FAILURE_BUDGET_RE.match(line)
        if m:
            failure_budget_aborts.append(
                {"at": _hhmm(_seconds_of_day(m)), "failure_budget": int(m.group(6)),
                 "skipped": int(m.group(7)), "collected": int(m.group(8)),
                 "cut_books": m.group(9), "priority_violation": m.group(10)}
            )
            continue

        m = _WS_DISCONNECT_RE.match(line)
        if m:
            ws_disconnects.append(_seconds_of_day(m))
            # `continue` 하지 않는다 — 이 줄은 `qualitative`/`by_level` 집계에도 그대로 들어가야
            # 한다(종전 지표를 깨지 않는다). 아래 마커 매칭이 「WS 연결 끊김」을 계속 센다.

        m = _PRIORITY_RETRY_RE.match(line)
        if m:
            priority_retries.append(
                {"at": _hhmm(_seconds_of_day(m)), "attempted": int(m.group(6)),
                 "recovered": int(m.group(7))}
            )
            continue

        m = _FAILURE_RE.match(line)
        if m:
            # "옵션 체인 폴링 실패: B01608875 — {...}" → 종목/응답을 떼고 유형만 센다.
            kind = m.group(6).split(":")[0].strip()
            failures[kind] += 1
            # 2026-08-06 §3-4 / Fix#5 — **원인별로도 센다.** 상세 근거는 `classify_failure_cause`.
            failures_by_cause.setdefault(kind, collections.Counter())[
                classify_failure_cause(line)
            ] += 1
            # 2026-08-04 §2-1: `http_status_error`(트레이스백 기반)를 대체하는 계측.
            if _KIS_ERROR_BODY_TOKEN in line:
                qualitative["kis_error_response"] += 1

    cycles.sort(key=lambda c: c["start"])
    # 전용 파서가 센 값이 있으면 그것을 쓰고, 없는 항목만 `qualitative` 카운터로 채운다.
    # (순서 주의: 종전에는 dict 언팩이 뒤에 있어 **명시 키를 0으로 덮어썼다** — 08-04 구현 중
    #  실측으로 잡았다. `atm_rolls` 582건이 감사에서 "strict 0"으로 나와 오탐이 떴다.)
    strict_counts = {key: qualitative.get(key, 0) for key in _PARSER_AUDIT_TOKENS}
    strict_counts.update(
        {
            "cycles": len(cycles),
            "overrun": len(overrun_seconds),
            "backoff": len(backoff_events),
            "slow_calls": len(slow_calls),
            "catchups": len(catchups),
            "rest_latency": len(latency_windows),
            "atm_rolls": len(atm_rolls),
            "budget_exceeded": len(budget_events),
            "timeout_abort": len(timeout_aborts),
            "failure_budget_abort": len(failure_budget_aborts),
        }
    )
    return {
        "date": target.isoformat(),
        "cycles": _cycle_metrics(cycles, calls, catchups, process_starts),
        "rest": _rest_metrics(calls),
        "backoff": _backoff_metrics(backoff_events, cycles),
        "bursts": _burst_metrics(calls),
        "stalls": _stall_metrics(calls),
        "slow_calls": _slow_call_metrics(slow_calls),
        "rest_latency": _rest_latency_metrics(latency_windows, slow_calls),
        "atm_rolls": _atm_roll_metrics(atm_rolls),
        "budget_exceeded": {
            "count": len(budget_events),
            "skipped_legs_total": sum(e["skipped"] for e in budget_events),
            # 2026-08-11 Fix#2 — **예산 컷이 먼슬리에 닿은 분**. 08-06이 이 값을 손으로 세어
            # (그때 3분) "먼슬리를 얇게 만든 것은 예산 컷이 아니라 레그 단위 타임아웃"이라는
            # 결론을 냈고 그것이 고도화#1의 방향을 정했다. 그 실측이 지표로는 없었다.
            # 컷은 먼슬리 우선 순서상 뒤쪽 북부터 닿으므로 **여기 먼슬리가 들어오면 그 자체가 사건**이다.
            "priority_cut_minutes": _priority_cut_minutes(budget_events),
            # 2026-08-12 Fix#5 — **불변식은 이쪽이다**(위는 참고값). 근거는
            # `_priority_before_others_minutes` docstring.
            "priority_before_others_minutes": _priority_before_others_minutes(budget_events),
            "labelled": sum(1 for e in budget_events if e.get("cut_books")),
            "samples": budget_events[:10],
        },
        # 2026-08-11 Fix#1 — 연속 타임아웃 조기 포기. **예산 초과와 나눠 센다**(원인이 다르다).
        "timeout_abort": {
            "count": len(timeout_aborts),
            "skipped_legs_total": sum(e["skipped"] for e in timeout_aborts),
            "priority_cut_minutes": _priority_cut_minutes(timeout_aborts),
            "priority_before_others_minutes": _priority_before_others_minutes(timeout_aborts),
            "labelled": sum(1 for e in timeout_aborts if e.get("cut_books")),
            "minutes": [e["at"] for e in timeout_aborts],
            "samples": timeout_aborts[:10],
        },
        "catchups": {"count": len(catchups), "minutes": [c["minute"] for c in catchups]},
        # 2026-08-06 고도화#1 — 먼슬리 레그 재시도. 시도/회복을 나눠 두는 이유는
        # `_PRIORITY_RETRY_RE` 주석 참고(회복 0과 시도 0은 원인이 다르다).
        "priority_retry": {
            "cycles": len(priority_retries),
            "attempted": sum(r["attempted"] for r in priority_retries),
            "recovered": sum(r["recovered"] for r in priority_retries),
            # 2026-08-24 Fix#4 — **「간신히 성공」과 「실패」는 다른 사건이다.** 회복률
            # (recovery_pct)은 하루를 하나로 접어 그 구분을 지운다: 재시도 6번 중 다섯 번이
            # 전량 회복이고 한 번이 전멸이어도 평균은 83%로 평범해 보인다. 08-24가 정확히
            # 그 하루였고(전자 5건 · 후자 1건), 그 1건을 사람이 로그를 훑어 찾았다.
            "failed_cycles": sum(
                1 for r in priority_retries if r["recovered"] < r["attempted"]
            ),
            "failed_minutes": [
                r["at"] for r in priority_retries if r["recovered"] < r["attempted"]
            ],
            "recovery_pct": (
                round(
                    sum(r["recovered"] for r in priority_retries)
                    / sum(r["attempted"] for r in priority_retries) * 100, 1
                )
                if sum(r["attempted"] for r in priority_retries) else None
            ),
        },
        # 2026-08-12 고도화 1 — **재연결을 비용으로 잰다.** 근거는 `_WS_DISCONNECT_RE` 위 주석.
        "ws_disconnect": _ws_disconnect_metrics(ws_disconnects),
        "poller_phase": _phase_metrics(calls),
        "log_volume": {
            "total_bytes": total_bytes,
            "total_lines": total_lines,
            "httpx_bytes": httpx_bytes,
            "httpx_pct": round(httpx_bytes / total_bytes * 100, 1) if total_bytes else None,
            "human_lines": human_lines,
            # 2026-08-05 §2-4 — `total_lines = httpx_lines + human_lines + traceback_lines`가
            # 항등식으로 성립한다. 리포트 §11이 이 셋을 나란히 찍으므로 어느 축이 부풀었는지
            # 한 줄로 보인다(08-05에는 그 줄이 없어 트레이스백 폭증이 `human_lines` 반증으로 둔갑했다).
            "traceback_lines": traceback_lines,
            "httpx_lines": total_lines - human_lines - traceback_lines,
            "by_level": dict(levels),
        },
        # 2026-08-23 — 위 `_QUALITATIVE_ALWAYS_PRESENT`의 마커는 0이어도 키를 남긴다.
        "qualitative": {
            **{key: 0 for key in _QUALITATIVE_ALWAYS_PRESENT},
            **dict(qualitative),
        },
        # 2026-08-06 Fix#4 — 위 `qualitative`에 합산된 "줄이 안 남은" 건수. 리포트 §11이
        # `줄 N + 억제 M = 총계`로 나란히 찍는다. 08-06 실측: read_timeout 124 + 81 = 205건
        # (그날 리포트는 126건만 냈다 — 실제의 61%).
        "qualitative_suppressed": dict(qualitative_suppressed),
        # 2026-08-06 Fix#4/#6 — 그날 프로세스가 몇 번 떴는가(자정 기준 초).
        "process_starts": process_starts,
        "parser_audit": _parser_audit(strict_counts, audit_loose),
        "failures": dict(failures.most_common()),
        # 2026-08-06 §3-4 / Fix#5 — 같은 실패를 **원인 축으로** 한 번 더 접는다.
        # `failures`(총계)를 그대로 두는 이유: 기존 가설이 그 경로를 지목하고 있고, 총계와
        # 원인별 합이 갈리면 `crosscheck`가 그것을 잡는다.
        "failures_by_cause": {
            kind: dict(causes.most_common()) for kind, causes in failures_by_cause.items()
        },
        "overrun": {
            "count": len(overrun_seconds),
            "max_seconds": round(max(overrun_seconds), 1) if overrun_seconds else 0.0,
            "total_seconds": round(sum(overrun_seconds), 1),
        },
        # 2026-08-11 고도화 A — 누적 실패 예산 소진. 연속 타임아웃과 **나눠 센다**(위 주석).
        "failure_budget_abort": {
            "count": len(failure_budget_aborts),
            "skipped_legs_total": sum(e["skipped"] for e in failure_budget_aborts),
            "priority_cut_minutes": _priority_cut_minutes(failure_budget_aborts),
            "priority_before_others_minutes": _priority_before_others_minutes(failure_budget_aborts),
            "labelled": sum(1 for e in failure_budget_aborts if e.get("cut_books")),
            "minutes": [e["at"] for e in failure_budget_aborts],
        },
        # 2026-08-11 Fix#7 — 폴러별 밀림. 상세 근거는 `_ANY_OVERRUN_RE` 주석.
        # 옵션체인 밖의 밀림은 종전에 **어디에도 안 남았다**(08-11 만기유동성 1건이 그랬다).
        "overrun_by_poller": {
            poller: {
                "count": len(seconds),
                "max_seconds": round(max(seconds), 1),
                "total_seconds": round(sum(seconds), 1),
            }
            for poller, seconds in sorted(overrun_by_poller.items(), key=lambda kv: -len(kv[1]))
        },
    }


# 2026-08-04(고도화#5) — 사전 대응 규칙의 발동 임계. **숫자를 보기 전에 적는다.**
#
# 08-04 실측: 정상 호출은 페이서 포함 ~1.2초이고, 느린 호출 362건의 HTTP 중앙이 5.54초였다.
# p95가 2.5초를 넘는다는 것은 **스무 번에 한 번꼴로 정상의 두 배 넘게 걸린다**는 뜻이고, 그
# 구간에서는 20레그 사이클이 60초 예산을 못 지킨다(20 x 2.5 = 50초 = Fix#8 예산 전부).
#
# 규칙(발동은 사람이 한다 — `poll_rest_latency_snapshot` docstring의 되먹임 위험 참고):
#   `inquire-price`의 p95가 2.5초를 넘는 시간대가 **이틀 연속 같은 시간대에** 나타나면,
#   그 시간대에 한해 위클리 폴링을 2분 → 4분 격분으로 늘린다(먼슬리는 건드리지 않는다 —
#   판단 입력이다). 총량 축소보다 손실이 작은 순서로 가는 것이 2026-07-31 결정의 원칙이다.
REST_LATENCY_P95_WARN_SECONDS = 2.5

# ===== 2026-08-14 §2-2 / Fix#3 — **절벽을 만드는 것은 p95가 아니라 p50과 타임아웃의 교차다** =====
#
# 08-14 14:00~15:23 옵션체인이 84분 연속 전멸했다. 그 구간의 REST 수집 시간은 평균 49.9초로
# 12·13시보다 **오히려 낮았다** — 느려진 것이 아니라 비어 있었다. 원인은 `inquire-price`의
# **p50이 4.05초**가 되어 전역 read timeout **4.0초**를 추월한 것이다. 중앙값 호출이
# 타임아웃되면 20레그 순차 수집의 기대 성공 수는 0에 수렴하고, 재시도도 같은 벽에 부딪힌다.
#
# **p95 임계(2.5초)로는 이것을 예고하지 못했다.** 그날 p95는 09시부터 종일 붉었고, 그 상태로
# 다섯 시간 동안 아무 일도 없었다. 열화는 연속량이 아니라 **임계 현상**이다 —
# 임계를 재려면 임계값과 관측값을 **같은 줄에** 놓아야 한다.
#
# 0.8의 근거: 08-14 13:36 창의 p50이 3.08초 = 타임아웃의 **0.77배**였고, 그로부터 24분 뒤
# 절벽이 왔다. 0.8을 넘는 순간은 "중앙값 호출이 타임아웃 한 뼘 앞"이라는 뜻이고, 그 뒤로는
# 분포가 조금만 밀려도 절반이 죽는다. **하루 전 실측에 맞춘 값이 아니라 절벽 앞의 여유폭이다.**
REST_LATENCY_P50_TIMEOUT_RATIO_WARN = 0.8

# 전역 HTTP read 타임아웃의 **복제본**이다(`mahdi/broker/rest_client._HTTP_READ_TIMEOUT_SECONDS`).
#
# 이 모듈은 순수 텍스트 파서라 `mahdi.broker`를 임포트하지 않는다 — `PRIORITY_SERIES_LABEL`과
# 같은 규약이고, 같은 이유로 **계약 테스트가 두 값의 일치를 강제한다**
# (`tests/test_ops_log_metrics_contract.py`). 복제 자체가 위험한 게 아니라 복제가 조용히
# 갈라지는 것이 위험하고, 그것은 테스트로 막는 편이 임포트보다 싸다.
#
# 레버(`OPTION_CHAIN_READ_TIMEOUT_SECONDS`)가 켜진 날은 옵션체인만 값이 다르다 —
# 그때는 리포트가 레버 값을 넘겨받아 쓴다(`report._render_rest_latency`).
GLOBAL_HTTP_READ_TIMEOUT_SECONDS = 4.0

# ===== 2026-08-19 (08-18 보고서 §2-5 / Fix#6) — **우측 검열을 센다** =====
#
# ## p50이 구조적으로 못 보는 축이 있다
#
# 08-18에 `감마플립 산출 불가`가 **4건뿐이었고 시각이 전부 `:01`** 이었다
# (13:01:10 · 13:31:10 · 14:01:10 · 15:01:10). 같은 날 정규장 `느린 REST 호출` 2,850건 중
# HTTP 성분이 4.0초 이상인 것이 **333건**이었고, 그중 103건(31%)이 `:00~:03`·`:30~:33`
# **여덟 분**에 몰렸다 — 균등이면 13.3%다.
#
# 그런데 그 333건의 `4.02`·`4.05`·`4.06`은 **실제 응답시간이 아니다.** read timeout에 잘린
# 값이다. 통계에서 이것을 **우측 검열**(right censoring)이라 하고, 검열된 표본의 분위수는
# 천장에 눌려 위쪽 꼬리를 잃는다. 그래서 `p50 ÷ timeout` 게이지는 이 분들을 **원리적으로**
# 못 본다 — 14:30 회차가 「최대 0.74로 경고선 아래」라고 옳게 닫았는데, 그 게이지가 못 보는
# 축에서 넉 분의 판단이 죽었다.
#
# **검열된 값의 p50은 의미가 없고, 진짜 신호는 검열 «건수»다.** 그래서 여기서 따로 센다.
#
# ## 왜 라벨별 임계인가 — 통로마다 천장이 다르다
#
# 08-18 §3-5가 실측으로 보여 준 그대로다: `inquire-price`(천장 4.0초)는 12시부터 천장에
# 눌리기 시작했는데 `inquire-balance`(천장 10.0초)는 최대 7.62초로 **천장에 안 닿았다.**
# 두 행을 같은 임계로 세면 미검열 통로가 검열로 찍히거나 그 반대가 된다.
#
# 이 dict는 `rest_client._ENDPOINT_READ_TIMEOUT_SECONDS`의 **복제본**이다 —
# `GLOBAL_HTTP_READ_TIMEOUT_SECONDS`와 같은 규약이고(이 모듈은 브로커 계층을 import하지 않는다),
# 같은 이유로 **계약 테스트가 두 값의 일치를 강제한다**(`tests/test_ops_log_metrics_contract.py`).
# 복제 자체가 위험한 게 아니라 복제가 조용히 갈라지는 것이 위험하다.
_ENDPOINT_READ_TIMEOUT_BY_LABEL: dict[str, float] = {
    "inquire-balance": 10.0,
    "inquire-asking-price": 10.0,
    "order": 10.0,
    "order-rvsecncl": 10.0,
}

# 검열이 몰리는지 볼 위상 창 — 정각과 30분의 **앞 네 분씩**. 08-18의 103건이 정확히 이 여덟 분에
# 있었다. 창을 넓히면 어떤 분포도 「몰려 있다」로 보이고, 좁히면 초 단위 지터에 값이 튄다.
# 여덟 분이면 60분 중 13.3%라 균등선이 사람 머리에 바로 서는 것도 이 폭을 고른 이유다.
CENSORED_PHASE_MINUTES = frozenset({0, 1, 2, 3, 30, 31, 32, 33})


def read_timeout_for_label(endpoint: str) -> float:
    """반환: 그 엔드포인트의 read 타임아웃(초). 모르는 라벨은 전역값으로 떨어진다.

    **모르는 라벨을 전역값(4.0)으로 접는 것이 안전한 쪽이다** — 실제 천장이 10초인데 4초로
    보면 검열 건수가 과다 계상되어 눈에 띄고, 반대면 조용히 사라진다. 08-18의 교훈이 정확히
    「조용히 사라지는 쪽」이었다.
    """
    return _ENDPOINT_READ_TIMEOUT_BY_LABEL.get(endpoint, GLOBAL_HTTP_READ_TIMEOUT_SECONDS)


# ===== 2026-08-23 (08-21 §1-14 / §5 고도화#1) — **검열된 p50은 중앙값이 아니라 하한이다** =====
#
# ## 08-21에 세 회차가 같은 숫자를 잘못 읽었다
#
# 그날 지연창 98개 중 상당수가 p50 **4.03~4.05초**를 냈다. `inquire-price`의 read timeout은
# **4.0초**이므로 그 값은 실제 중앙값이 아니라 **타임아웃 벽에 눌린 값**이다(우측 검열).
# 4초를 넘는 호출은 전부 4.00~4.06으로 기록되므로 그 통로로는 **상한을 알 수 없다.**
#
# 그런데 리포트는 그것을 「4.03초」라고 평범하게 인쇄했고, 장전·장중①·장중②·장후 네 회차가
# 그 숫자를 실제 응답시간처럼 읽었다.
#
# ## 그 오독의 대가는 구체적이다
#
# 08-20·08-21 세 회차가 손익표에 올린 「read timeout 4.0 → 6.0초」는 *"조금만 더 기다리면 온다"*를
# 전제한다. 그런데 **6초로 늘렸을 때 몇 %가 더 들어오는지를 오늘 데이터로는 구할 수 없다** —
# 재료가 애초에 없다. 그 사실이 표기에 드러나야 「지금은 계산이 안 된다」가 한눈에 보인다.
#
# ## 재료는 이미 로그에 있었다 — 새 계측을 안 만든다
#
# `rest_client._log_if_slow`는 **타임아웃으로 끝난 호출을 임계와 무관하게 반드시 남긴다**
# (08-05 Fix#4). 슬로우 임계는 3.0초이고 read timeout은 4.0초이므로, **검열된 호출은 전부
# 그 줄에 있다.** 즉 창별 검열 비율은 `slow_calls`를 창에 붙이기만 하면 나온다 —
# `rest_client`도 `poll_rest_latency_snapshot`도 건드리지 않는다.
#
# ## 임계 0.98의 뜻
#
# p50이 타임아웃의 0.98배를 넘으면 「중앙값 호출이 벽에 닿아 있다」로 본다. 정확히 1.00을
# 쓰지 않는 이유는 최근접 순위법 p50이 4.00 바로 아래(3.99)로 떨어질 수 있기 때문이고,
# 그 창도 검열된 창이다.
P50_CENSORED_FLOOR_RATIO = 0.98


def _censored_by_window(
    windows: list[dict], slow: list[dict],
) -> dict[tuple[float, str], int]:
    """반환: `(창 시각, 엔드포인트) -> 그 창에서 타임아웃에 잘린 호출 수`.

    입력: 5분 창 요약, 느린 호출 목록(`at_seconds`가 있어야 한다).
    계산: 창은 `(직전 창 끝, 이 창 끝]`이다 — `poll_rest_latency_snapshot`이 **직전 창의 표본을
         비우면서** 줄을 남기므로 줄의 시각은 창의 **끝**이고, 창의 시작은 그 앞 줄의 시각이다.

         ⚠ **고정 길이(300초)로 가정하면 안 된다.** 재기동이 있는 날에는 그 폴러의 타이머가
         다시 시작하므로 창 하나가 짧아진다(08-21 12:19 재기동이 그랬다). 도입 당시 「가장 짧은
         간격」을 창 길이로 삼았다가 그 짧은 창 때문에 **11시대 검열이 57건 중 2건으로
         과소 계상**됐다 — 경계는 계산하는 것이 아니라 **줄에 적혀 있는 것**을 쓴다.
    실패 조건: 없다. `at_seconds`가 없는 옛 파싱 결과는 조용히 빠진다 — 그때는 검열 수가
              0이 되지만, 호출측이 그 0을 「검열 없음」으로 인쇄하지 않도록
              `censored_measured`를 함께 낸다.
    """
    if not windows or not slow:
        return {}
    ends = sorted({w["at"] for w in windows})
    counts: dict[tuple[float, str], int] = collections.defaultdict(int)
    for call in slow:
        at = call.get("at_seconds")
        if at is None:
            continue
        if call["http"] < read_timeout_for_label(call["endpoint"]):
            continue
        # 그 호출을 담는 창 = 호출 시각 **이상인 가장 이른 창 끝**. 마지막 창 끝보다 뒤의
        # 호출은 어느 창에도 안 들어간다(그 창의 요약 줄이 아직 안 나왔다).
        index = bisect.bisect_left(ends, at)
        if index < len(ends):
            counts[(ends[index], call["endpoint"])] += 1
    return dict(counts)


def _rest_latency_metrics(windows: list[dict], slow: list[dict] | None = None) -> dict:
    """
    입력: 5분 창마다 남은 엔드포인트별 응답시간 요약(`poll_rest_latency_snapshot`).
    계산: 엔드포인트별 전일 종합(호출 수 가중 p50/p95 근사, 최대)과 **시간대 x 엔드포인트**
         p95 격자를 만든다. 임계를 넘은 (시간대, 엔드포인트)는 `warnings`에 모은다.
    해석: 2026-08-04 고도화#5 — §2-6에서 밀림의 90%가 KIS 응답 지연으로 귀속됐다. 그런데 그
         지연은 지금까지 "우리 지표"(밀림 건수)로만 보였다. 이 표가 있으면 **매일 반복되는
         혼잡 시간대**가 드러나고, 그때 비로소 시간대별 폴링 조정이 근거를 갖는다.
    실패 조건: 창이 없으면(구버전 로그) 빈 dict — 리포트가 "계측 전"으로 표시한다.

    주의: p50/p95는 **창별 값의 호출 수 가중 평균**이지 하루 전체 표본의 진짜 분위수가 아니다
         (원본 표본은 창마다 버려진다 — 메모리 상수 유지가 그 대가다). 창 사이 분포가 크게
         다르면 참값과 벌어지므로, **판단은 `max`와 시간대 격자로 한다**.
    """
    if not windows:
        return {}
    # 2026-08-23 고도화#1 — 창마다 「그 창의 호출 중 몇 %가 타임아웃에 잘렸나」.
    censored = _censored_by_window(windows, slow or [])
    by_endpoint: dict[str, list[dict]] = collections.defaultdict(list)
    for w in windows:
        by_endpoint[w["endpoint"]].append(w)

    def weighted(items: list[dict], key: str) -> float:
        total = sum(i["n"] for i in items)
        return round(sum(i[key] * i["n"] for i in items) / total, 3) if total else 0.0

    endpoints = {
        endpoint: {
            "calls": sum(i["n"] for i in items),
            "p50": weighted(items, "p50"),
            "p95": weighted(items, "p95"),
            "p99": weighted(items, "p99"),
            "max": round(max(i["max"] for i in items), 3),
        }
        for endpoint, items in sorted(by_endpoint.items(), key=lambda kv: -sum(i["n"] for i in kv[1]))
    }

    grid: dict[str, dict[str, float]] = collections.defaultdict(dict)
    # 2026-08-14 Fix#3 — p50 격자를 **p95와 같은 방식으로** 함께 낸다. 해석(타임아웃과의
    # 교차)은 리포트가 하고, 여기서는 값만 만든다 — 이 모듈은 판정하지 않는다.
    grid_p50: dict[str, dict[str, float]] = collections.defaultdict(dict)
    hourly: dict[tuple[int, str], list[dict]] = collections.defaultdict(list)
    for w in windows:
        hourly[(int(w["at"] // 3600), w["endpoint"])].append(w)
    # 시간대 **안에서 가장 나빴던 창**. 가중평균과 반드시 함께 낸다 — 평균은 절벽을 눌러 없앤다:
    # 08-14 13시의 가중평균 p50은 2.18초(비율 0.55 — 조용하다)인데 그 시간대의 창 최대는
    # 3.53초(**0.88** — 경고선 초과)였고, **그 20~60분 뒤에 절벽이 왔다.** 평균만 보면 선행 신호가 사라진다.
    grid_p50_max: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for (hour, endpoint), items in hourly.items():
        grid[str(hour)][endpoint] = weighted(items, "p95")
        grid_p50[str(hour)][endpoint] = weighted(items, "p50")
        grid_p50_max[str(hour)][endpoint] = round(max(i["p50"] for i in items), 3)

    warnings = [
        {"hour": hour, "endpoint": endpoint, "p95": p95}
        for hour, row in sorted(grid.items(), key=lambda kv: int(kv[0]))
        for endpoint, p95 in sorted(row.items())
        if p95 > REST_LATENCY_P95_WARN_SECONDS
    ]
    # 2026-08-23 고도화#1 — **검열된 창의 목록.** p50이 타임아웃의 `P50_CENSORED_FLOOR_RATIO`배를
    # 넘은 창만 담는다 — 그 창의 p50은 중앙값이 아니라 **하한**이고, 리포트가 `≥`를 붙여 인쇄한다.
    censored_windows = []
    for w in sorted(windows, key=lambda x: (x["at"], x["endpoint"])):
        timeout = read_timeout_for_label(w["endpoint"])
        if not timeout or w["p50"] < timeout * P50_CENSORED_FLOOR_RATIO:
            continue
        cut = censored.get((w["at"], w["endpoint"]), 0)
        censored_windows.append({
            "at": _hhmm(w["at"]), "endpoint": w["endpoint"], "calls": w["n"],
            "p50": w["p50"], "read_timeout": timeout,
            "censored": cut,
            # 분모는 그 창의 **전체 호출 수**다. 100%를 넘을 수 없고, 넘으면 창 배정이 틀린 것이다.
            "censored_pct": round(min(cut / w["n"], 1.0) * 100, 1) if w["n"] else None,
        })

    return {
        "endpoints": endpoints,
        # 2026-08-23 고도화#1. **0은 두 가지다**(규약 C): 키가 있고 0이면 「검열된 창이 없었다」,
        # 키가 없으면 「그날은 이 계측 자체가 없던 버전이다」.
        "censored_window_count": len(censored_windows),
        "censored_windows": censored_windows,
        "p50_censored_floor_ratio": P50_CENSORED_FLOOR_RATIO,
        # **검열 수를 실제로 셀 수 있었는가.** 느린 호출 줄이 하나도 없으면 「0건」이 아니라
        # 「안 셌다」이고, 그 구분이 없으면 검열 0%가 「깨끗한 날」로 읽힌다.
        "censored_measured": bool(slow),
        "p95_by_hour": {h: dict(sorted(row.items())) for h, row in sorted(grid.items(), key=lambda kv: int(kv[0]))},
        "p50_by_hour": {
            h: dict(sorted(row.items())) for h, row in sorted(grid_p50.items(), key=lambda kv: int(kv[0]))
        },
        "p50_max_by_hour": {
            h: dict(sorted(row.items())) for h, row in sorted(grid_p50_max.items(), key=lambda kv: int(kv[0]))
        },
        "p95_warn_threshold": REST_LATENCY_P95_WARN_SECONDS,
        "p50_timeout_ratio_warn": REST_LATENCY_P50_TIMEOUT_RATIO_WARN,
        "global_read_timeout_seconds": GLOBAL_HTTP_READ_TIMEOUT_SECONDS,
        "warnings": warnings,
    }


def _atm_roll_metrics(rolls: list[tuple[float, str, str]]) -> dict:
    """
    입력: (초, 이전 행사가 창, 새 행사가 창) 목록 — 북 중복은 이미 제거됐다.
    계산: 롤링 이벤트 수와 **즉시 왕복**(A→B 다음 이벤트가 B→A) 횟수/비율.
    해석: 2026-08-04 고도화#3 — §2-2에서 롤링 194회 중 70회(36%)가 즉시 왕복이었고, 그것이
         사람이 읽는 로그 7,539줄 중 6,206줄(82%)을 만들었다. Fix#6(히스테리시스 0.75칸)이
         이 값을 줄이는지가 그 fix의 유일한 직접 지표다 — 없으면 매번 손으로 세야 한다
         (08-05 검증 항목에 "손으로 셀 것"이라고 적었던 바로 그 항목이다).
    실패 조건: 롤링이 없으면 count=0, round_trip_pct=None(0%가 아니다 — 분모가 없다).
    """
    round_trips = sum(
        1 for prev, cur in zip(rolls, rolls[1:]) if prev[1] == cur[2] and prev[2] == cur[1]
    )
    return {
        "count": len(rolls),
        "round_trips": round_trips,
        "round_trip_pct": round(round_trips / len(rolls) * 100, 1) if rolls else None,
    }


def _parser_audit(strict: dict[str, int], loose: collections.Counter) -> dict:
    """
    입력: 엄격 파서가 센 값들, 같은 항목의 느슨한 토큰 등장 횟수.
    계산: **엄격이 0인데 느슨이 0이 아닌** 항목을 골라낸다 — 그것이 파서가 눈이 먼 자리다.
    해석: 2026-08-04 §2-1 / 고도화#1 규약 C — *"0건 보고는 증명을 동반한다."*
         08-03에 로그 세 곳의 레벨·예외 처리를 바꾸면서 파서 세 개가 조용히 죽었고, 리포트는
         그것을 **개선(▼933 ✅)으로 표시**했다. 지표가 나아진 것과 계측기가 꺼진 것은 로그를
         직접 세보기 전에는 구분되지 않는다 — 이 함수가 그 구분을 자동화한다.
         `blind`가 비어 있지 않으면 그날 리포트의 해당 값을 **믿으면 안 된다**.
    실패 조건: 없음 — 느슨 토큰이 지나치게 짧아 오탐이 나면 `_PARSER_AUDIT_TOKENS`를 조인다
              (오탐은 ⚠ 1줄이지만, 미탐은 08-04처럼 하루치 판정을 통째로 뒤집는다).
    """
    blind = {
        key: {"strict": strict.get(key, 0), "loose": loose[key]}
        for key in _PARSER_AUDIT_TOKENS
        if strict.get(key, 0) == 0 and loose[key] > 0
    }
    return {"blind": blind, "loose_counts": dict(loose)}


def _cycle_metrics(
    cycles: list[dict],
    calls: list[tuple[float, str, str]],
    catchups: list[dict],
    process_starts: list[float] | None = None,
) -> dict:
    if not cycles:
        return {
            "count": 0, "by_hour": [], "by_mod10": [],
            "missing": {"count": 0, "list": [], "downtime_count": 0, "infra_count": 0},
            "duplicate_poll_minutes": {"count": 0, "list": [], "labelled": 0},
        }
    process_starts = process_starts or []

    call_times = [c[0] for c in calls]
    for cycle in cycles:
        lo = bisect.bisect_left(call_times, cycle["start"])
        hi = bisect.bisect_right(call_times, cycle["start"] + cycle["rest"])
        window = collections.Counter(group for _t, group, _s in calls[lo:hi])
        cycle["foreign"] = sum(v for k, v in window.items() if k != "옵션체인")
        cycle["foreign_by_group"] = {k: v for k, v in window.items() if k != "옵션체인"}

    rests = [c["rest"] for c in cycles]
    by_hour = []
    for hour, group in sorted(_bucket(cycles, lambda c: int(c["start"] // 3600)).items()):
        r = [c["rest"] for c in group]
        by_hour.append(
            {
                "hour": hour,
                "cycles": len(group),
                "rest_mean": round(statistics.mean(r), 1),
                "rest_max": round(max(r), 1),
                "over_60s": sum(1 for x in r if x > 60),
                "slip_max": round(max(c["slip"] for c in group), 1),
                "foreign_sum": sum(c["foreign"] for c in group),
                # 2026-08-14 장중 §3 / 고도화 2 — **위상 이동을 사고가 아니라 지표로.**
                #
                # 사이클이 분 안의 몇 초에 끝나는가. 08-14는 07·08시 :19 → 09시 :25 → 10시 :30
                # → 11시 :45 → 12시 :50으로 밀렸고, 그 곡선 위에 **예산 초과 138건 · 조기 포기
                # 26건 · 전멸 86분이 전부 올라앉아 있었다.** 지금까지 우리는 그 세 결과를 각각
                # 세면서 공통 원인인 이 한 줄은 안 쟀다.
                #
                # 평균이 아니라 중앙값인 이유: 밀린 사이클 한 건이 60초를 넘기면 평균이 다음 분으로
                # 넘어가 위상이 **거꾸로 돌아간 것처럼** 보인다(:55 → :05). 중앙값은 안 흔들린다.
                "end_second_median": round(statistics.median(c["end"] % 60 for c in group), 1),
            }
        )

    by_mod10 = []
    for mod10, group in sorted(_bucket(cycles, lambda c: int(c["start"] % 3600 // 60) % 10).items()):
        r = [c["rest"] for c in group]
        merged: collections.Counter = collections.Counter()
        for c in group:
            merged.update(c["foreign_by_group"])
        by_mod10.append(
            {
                "mod10": mod10,
                "cycles": len(group),
                "rest_mean": round(statistics.mean(r), 1),
                "foreign_mean": round(statistics.mean([c["foreign"] for c in group]), 1),
                "foreign_by_group": {k: round(v / len(group), 1) for k, v in merged.items()},
                "over_60s": sum(1 for x in r if x > 60),
            }
        )

    # 2026-08-10 — 결손은 **`분=` 라벨 축**으로 잰다. 라벨은 그 사이클이 실제로 적재한 분이고
    # (`main.py`가 `poll_time`을 그대로 찍는다), 파생 start는 **종료시각 − 소요 합**이라
    # 소수점 2자리 반올림 때문에 분 경계보다 몇 밀리초 앞에 떨어질 수 있다.
    #
    # 08-10 실측 — 494개 사이클 전수 검산에서 파생축이 2분을 허위 결손으로 냈다:
    #     11:09 → 파생 시작 11:08:59.996 (4ms 이르다)
    #     15:21 → 파생 시작 15:20:59.999 (1ms 이르다)
    # 라벨축으로 세면 결손 0분이고, 그것이 진실이다(그 두 분에 `분=` 라벨 사이클이 있다).
    #
    # 08-07 Fix#3이 라벨이라는 진실 소스를 만들었는데 이 지표만 옛 축에 남아 있었다.
    # 라벨이 없는 구 로그(08-07 이전)에서는 파생 start로 폴백한다 — 그 날들을 재집계할 때
    # 지표가 통째로 죽는 것이 08-04 §2-1에서 겪은 사고다.
    seen = {c["poll_minute"] or _hhmm(c["start"]) for c in cycles}
    # 격자의 양 끝도 **같은 축**에서 뽑는다. 파생 start로 끝을 잡으면 첫 사이클이 반올림으로
    # 앞 분에 떨어질 때 격자가 1분 일찍 시작하고, 그 분은 라벨이 없으므로 **경계에서 허위
    # 결손이 하나 더 생긴다.** 재는 값과 재는 범위가 같은 축이어야 한다.
    first, last = _seconds_of_label(min(seen)), _seconds_of_label(max(seen))
    missing = []
    t = first
    while t <= last:
        label = _hhmm(t)
        if label not in seen:
            missing.append(label)
        t += 60
    recovered = {c["minute"] for c in catchups}
    # 2026-08-06 §3-5 / Fix#6 — 결손을 **세 축**으로 가른다.
    downtime = _downtime_minutes(cycles, process_starts)
    downtime_missing = [m for m in missing if m in downtime]
    infra_missing = [m for m in missing if m not in downtime and m not in recovered]
    unrecovered = [m for m in missing if m not in recovered]
    return {
        "count": len(cycles),
        "first_start": _hhmm(first),
        "last_start": _hhmm(last),
        "rest_seconds": _stats(rests),
        "over_60s": sum(1 for x in rests if x > 60),
        "rows_distribution": dict(sorted(collections.Counter(c["rows"] for c in cycles).items())),
        # 2026-08-07(§2-1 / Fix#3) — **두 사이클이 같은 분 라벨로 적재한 경우.**
        #
        # 08-07 15:18에 DB가 0행인데 로그에는 사이클이 완주해 있었다. 그 사이클이 15:17:59.99x에
        # 깨어 `poll_time`이 15:17로 내려깎였고, 직전 분의 행을 UPSERT로 **덮어썼다**.
        # 행 수가 정상이라 `zero_row_count`(빈 분)로도, 결손 지표로도 안 잡혔다 —
        # 유일한 흔적은 "다음 분이 비어 있다"였고 그건 기동 아티팩트와 구분되지 않았다.
        #
        # `labelled`를 함께 내는 이유: 08-07 이전 로그에는 `분=` 라벨이 없어 `count`가
        # 구조적으로 0이다. **0을 "중복이 없었다"로 읽으면 안 된다** — 규약 C(0건 보고는
        # 증명을 동반한다)와 같은 이유다.
        "duplicate_poll_minutes": _duplicate_poll_minutes(cycles),
        # 2026-08-10 — DB 축(`db.chain_minute_coverage`)의 「0행 분」을 **원인별로 가르기 위한
        # 로그 쪽 절반**이다. 그 지표만으로는 세 원인이 한 칸에서 만난다:
        #   (a) 사이클이 돌았는데 적재가 0행     ← 여기(`zero_row_minutes`)로 식별
        #   (b) 사이클 자체가 없었다             ← `missing`으로 식별
        #   (c) 사이클의 행이 이웃 분으로 갔다   ← 위 둘 다 아닌 나머지
        # 08-10 15:15이 (a)였는데 08-07 Fix#3의 예측(불변식 `zero_row_count == 0`)은 (c)를
        # 겨냥한 것이었다 — **원인이 다른데 지표가 하나라 멀쩡한 fix가 반증으로 찍혔다.**
        "minutes_with_cycle": sorted(seen),
        "zero_row_minutes": sorted(
            c["poll_minute"] or _hhmm(c["start"]) for c in cycles if c["rows"] == 0
        ),
        "by_hour": by_hour,
        "by_mod10": by_mod10,
        "missing": {
            "count": len(missing),
            "odd": sum(1 for m in missing if int(m[3:]) % 2 == 1),
            "even": sum(1 for m in missing if int(m[3:]) % 2 == 0),
            "list": missing,
            "recovered_by_catchup": len(missing) - len(unrecovered),
            "unrecovered_count": len(unrecovered),
            # 2026-08-06 §3-5 / Fix#6 — **관측 루프가 아예 안 돌던 분.**
            #
            # 08-06 리포트 §1은 `결손 분 21분 ▲20 ⚠`을 냈고, 그 숫자를 인프라 악화로 읽으면
            # 틀린다: 21분 중 **20분이 10:04~10:23 프로세스 정지 구간**이고 사이클이 돌면서
            # 놓친 것은 13:19 1분뿐이었다. 가동 시간과 결손을 같은 분모로 섞으면
            # "인프라가 나빠졌다"와 "시스템이 꺼져 있었다"가 구분되지 않는다.
            #
            # `2026-08-05-p2`의 대가 지표가 이 때문에 「반증」으로 나왔다 — 그 fix와 인과가 없다.
            "downtime_count": len(downtime_missing),
            "downtime_list": downtime_missing,
            # 루프는 돌았는데 그 분의 사이클이 없던 분 = **진짜 인프라 결손**(회수분 제외).
            "infra_count": len(infra_missing),
            "infra_list": infra_missing,
        },
    }


def _downtime_minutes(cycles: list[dict], process_starts: list[float]) -> set[str]:
    """
    입력: 사이클 목록(시작 시각 오름차순), 프로세스 기동 시각 목록.
    반환: **관측 루프가 안 돌던 분** 라벨 집합.
    계산: 재기동 시각 T마다 [T 직전의 마지막 사이클, T 이후 첫 사이클] 사이를 공백으로 본다.
         기동 자체에 걸리는 시간(마스터파일 다운로드·WS 구독)도 데이터가 없는 시간이므로
         **첫 사이클까지**를 공백에 넣는다 — 08-06 10:23:25 기동의 첫 사이클은 10:24였다.
    해석: 그날 **첫** 기동은 건너뛴다 — 그 앞은 애초에 관측 대상이 아니다(장전 07:30 이전).
    실패 조건: 기동 표식이 없는 구버전 로그면 빈 집합 — 종전과 똑같이 전부 인프라 결손으로
              집계된다(지어내지 않는다).
    """
    if not process_starts or not cycles:
        return set()
    cycle_starts = sorted(c["start"] for c in cycles)
    out: set[str] = set()
    for started in sorted(process_starts):
        idx = bisect.bisect_left(cycle_starts, started)
        if idx == 0:
            continue  # 그날 첫 기동
        gap_from = cycle_starts[idx - 1]
        gap_to = cycle_starts[idx] if idx < len(cycle_starts) else started
        t = gap_from + 60
        while t < gap_to:
            out.add(_hhmm(t))
            t += 60
    return out


def _rest_metrics(calls: list[tuple[float, str, str]]) -> dict:
    if not calls:
        return {"total_calls": 0, "calls_per_second": None, "capacity_pct": None}
    span = calls[-1][0] - calls[0][0]
    per_second = len(calls) / span if span > 0 else None
    capacity_pct = per_second / PACER_CAPACITY_CALLS_PER_SECOND * 100 if per_second else None
    failures = [c for c in calls if c[2] != "200"]
    return {
        "total_calls": len(calls),
        "span_seconds": round(span, 1),
        "first_call": _hhmm(calls[0][0]),
        "last_call": _hhmm(calls[-1][0]),
        "calls_per_second": round(per_second, 3) if per_second else None,
        "capacity_pct": round(capacity_pct, 1) if capacity_pct else None,
        # 수요가 용량을 구조적으로 초과하기 시작하는 백오프 배율 = 1 / (수요÷용량).
        "deficit_threshold_multiplier": round(1 / (per_second / PACER_CAPACITY_CALLS_PER_SECOND), 2)
        if per_second
        else None,
        "by_group": dict(collections.Counter(g for _t, g, _s in calls).most_common()),
        "by_status": dict(collections.Counter(s for _t, _g, s in calls).most_common()),
        "by_hour": {str(h): n for h, n in sorted(collections.Counter(int(t // 3600) for t, _g, _s in calls).items())},
        "non_200": {
            "count": len(failures),
            "pct": round(len(failures) / len(calls) * 100, 2),
            "by_group": dict(collections.Counter(g for _t, g, _s in failures).most_common()),
        },
    }


def _backoff_metrics(events: list[tuple[float, str, float]], cycles: list[dict]) -> dict:
    if not events:
        return {"expand": 0, "recover": 0, "max_multiplier": 1.0, "mean_multiplier_by_hour": {}}
    events = sorted(events)
    times = [e[0] for e in events]
    by_hour: dict[int, list[float]] = collections.defaultdict(list)
    if cycles:
        # 1초 샘플 시간가중 평균 — 이벤트 개수 평균은 "오래 걸려 있던 상태"를 과소평가한다.
        t = cycles[0]["start"]
        end = cycles[-1]["start"] + 60
        while t < end:
            i = bisect.bisect_right(times, t) - 1
            by_hour[int(t // 3600)].append(events[i][2] if i >= 0 else 1.0)
            t += 1
    return {
        "expand": sum(1 for _t, kind, _m in events if kind == "확대"),
        "recover": sum(1 for _t, kind, _m in events if kind == "회복"),
        "max_multiplier": round(max(m for _t, _k, m in events), 3),
        "mean_multiplier_by_hour": {
            str(h): round(statistics.mean(v), 3) for h, v in sorted(by_hour.items())
        },
        "mean_multiplier": round(
            statistics.mean([x for v in by_hour.values() for x in v]), 3
        )
        if by_hour
        else None,
    }


def _group_bursts(calls: list[tuple[float, str, str]]) -> dict[str, list[list[float]]]:
    """그룹별 연속 호출 묶음(버스트) — 같은 그룹의 호출이 BURST_SPLIT_SECONDS 넘게 벌어지면 분리."""
    out: dict[str, list[list[float]]] = {}
    for group in sorted({g for _t, g, _s in calls}):
        times = [t for t, g, _s in calls if g == group]
        if not times:
            continue
        bursts: list[list[float]] = [[times[0]]]
        for prev, cur in zip(times, times[1:]):
            if cur - prev > BURST_SPLIT_SECONDS:
                bursts.append([])
            bursts[-1].append(cur)
        out[group] = bursts
    return out


def _burst_metrics(calls: list[tuple[float, str, str]]) -> dict:
    """
    계산: 그룹별 버스트 점유 시간 — §2-1(a)에서 만기유동성 55.5초(최대 109초)를 잡아낸 집계.
    해석: **매 분 도는 폴러(옵션체인·투자자수급)는 버스트 개념이 성립하지 않는다** — 사이클
         간 유휴가 분리 임계보다 짧아 하루가 통째로 한 버스트로 뭉친다. 그런 그룹은 표에서
         제외한다(그 폴러의 분당 점유는 `cycles.by_mod10`의 REST평균이 이미 보여준다).
         판정을 폴러 이름 하드코딩이 아니라 **물리적 성질**로 한다 — 관측 구간 대비 총 점유
         비율이 `CONTINUOUS_POLLER_DUTY_RATIO` 이상이면 "버스트를 쏘는" 게 아니라 "계속 돌고
         있는" 것이다. 이러면 앞으로 폴러 구성이 바뀌어도 따라온다.
    """
    if not calls:
        return {}
    span = max(calls[-1][0] - calls[0][0], 1.0)
    out: dict[str, dict] = {}
    for group, bursts in _group_bursts(calls).items():
        durations = [b[-1] - b[0] for b in bursts if len(b) > 1]
        if not durations:
            continue
        if sum(durations) / span >= CONTINUOUS_POLLER_DUTY_RATIO:
            continue  # 연속 가동 폴러 — 위 docstring 참고
        out[group] = {
            "burst_count": len(bursts),
            "calls_per_burst_median": statistics.median([len(b) for b in bursts]),
            "occupancy_seconds": _stats(durations),
            "start_positions_mod10": dict(
                collections.Counter(
                    f"{int(b[0] % 600 // 60)}:{int(b[0] % 60):02d}" for b in bursts
                ).most_common(3)
            ),
        }
    return out


def _stall_metrics(calls: list[tuple[float, str, str]]) -> list[dict]:
    """전 폴러 통합 호출열에서 '>5초 간격이 3회 이상 연속'인 구간 — §2-1(b) 8~9초 정체 탐지."""
    episodes: list[dict] = []
    run: list[float] = []
    start: float | None = None

    def flush() -> None:
        if len(run) >= STALL_MIN_RUN and start is not None:
            episodes.append(
                {
                    "at": _hhmm(start),
                    "mod10_minute": int(start % 600 // 60),
                    "gaps": len(run),
                    "total_seconds": round(sum(run), 1),
                    "mean_gap": round(statistics.mean(run), 1),
                }
            )

    for (t_prev, _g, _s), (t_cur, _g2, _s2) in zip(calls, calls[1:]):
        gap = t_cur - t_prev
        # 상한(BURST_SPLIT_SECONDS/2=30초)을 두는 이유: 사이클 사이의 정상적인 유휴 구간
        # (짝수분 수집이 40초에 끝나면 다음 분까지 20초 공백)을 정체로 세면 안 된다.
        if STALL_GAP_SECONDS < gap < BURST_SPLIT_SECONDS / 2:
            if not run:
                start = t_prev
            run.append(gap)
            continue
        flush()
        run, start = [], None
    flush()
    return episodes


def _censored_metrics(slow: list[dict]) -> dict:
    """2026-08-19 Fix#6 — **read timeout에 잘린 호출**(우측 검열)만 따로 센다.

    입력: 느린 호출 목록(각 항목에 `http`·`endpoint`·`at`).
    계산: 라벨별 천장 이상인 호출 수와 그 **분(minute) 분포**, 그리고 정각·30분 창의 점유율.
    해석: 상세 근거는 `_ENDPOINT_READ_TIMEOUT_BY_LABEL` 위 주석. 여기서 판정은 하지 않는다 —
         `phase_concentration`을 균등선(`phase_baseline`)과 비교하는 것은 사람과 리포트의 몫이다.
    실패 조건: 없다. 검열이 0건이면 점유율은 `None`이다 — **0.0이 아니다**: 분모가 0인
         비율을 0으로 인쇄하면 「몰리지 않았다」로 읽히는데, 사실은 「잴 것이 없었다」이다(규약 C).
    """
    censored = [s for s in slow if s["http"] >= read_timeout_for_label(s["endpoint"])]
    in_phase = sum(1 for s in censored if int(s["at"][3:]) in CENSORED_PHASE_MINUTES)
    return {
        "count": len(censored),
        # 검열은 **엔드포인트마다 천장이 달라** 통로별로 갈라 보는 것이 유일하게 옳다
        # (08-18 §3-5: `inquire-price`는 천장에 눌렸고 `inquire-balance`는 안 닿았다).
        "by_endpoint": dict(
            collections.Counter(s["endpoint"] for s in censored).most_common()
        ),
        "by_minute": dict(sorted(collections.Counter(int(s["at"][3:]) for s in censored).items())),
        "phase_minutes": sorted(CENSORED_PHASE_MINUTES),
        "phase_count": in_phase,
        "phase_concentration": round(in_phase / len(censored), 3) if censored else None,
        # 여덟 분 / 60분. 이 값을 함께 내야 「31%」가 큰지 사람이 판단할 수 있다.
        "phase_baseline": round(len(CENSORED_PHASE_MINUTES) / 60.0, 3),
        # 2026-08-19 — **점유율 ÷ 균등선.** 리포트가 이 배수를 그대로 인쇄한다.
        #
        # 종전에는 리포트가 `점유율 >= 균등선 x 2`로 「위상 문제다 / 균등선 근처다」를 **단정**했다.
        # 08-19 실측 22.4%(= 1.68배)가 그 임계 밑으로 떨어져 **「균등선 근처다 — 특정 분에 몰린
        # 것이 아니다」로 인쇄됐는데, 13.3%의 1.68배를 「근처」라고 부를 수는 없다.**
        # 뭉툭한 임계 하나로 연속량을 이분한 것이고, 규약 F/G가 반복해 막아 온 것과 같은 형태다.
        #
        # 이틀 실측이 그 위험을 그대로 보여 준다: 08-18 **2.54배** → 08-19 **1.68배**.
        # 같은 축이 이틀 만에 크게 움직였으므로 **하루치로는 위상 문제라고도 아니라고도 못 한다.**
        # 그래서 값만 내고 판정은 사람이 한다 — 이 모듈의 다른 절과 같은 원칙이다.
        "phase_ratio": (
            round((in_phase / len(censored)) / (len(CENSORED_PHASE_MINUTES) / 60.0), 2)
            if censored else None
        ),
        "samples": sorted(censored, key=lambda s: -s["http"])[:5],
    }


def _slow_call_metrics(slow: list[dict]) -> dict:
    """§4 우선순위 3 판정용 — 지연이 페이서 대기와 HTTP 중 어디로 귀속되는지."""
    if not slow:
        return {
            "count": 0, "pacer_dominant": 0, "http_dominant": 0, "samples": [],
            # 2026-08-19 Fix#6 — **키를 조용히 빼지 않는다.** 없는 키는 「실측 없음」으로 떨어져
            # 가설이 검정 불가가 되고, 그것이 08-18 §3-2가 겪은 결함의 형태다.
            "censored": _censored_metrics([]),
        }
    pacer_dominant = sum(1 for s in slow if s["pacer"] > s["http"])
    return {
        "count": len(slow),
        "pacer_dominant": pacer_dominant,
        "http_dominant": len(slow) - pacer_dominant,
        "total_seconds": _stats([s["total"] for s in slow]),
        "pacer_seconds": _stats([s["pacer"] for s in slow]),
        "http_seconds": _stats([s["http"] for s in slow]),
        "by_mod10_minute": dict(
            sorted(collections.Counter(int(s["at"][3:]) % 10 for s in slow).items())
        ),
        "censored": _censored_metrics(slow),
        "samples": sorted(slow, key=lambda s: -s["total"])[:5],
    }


def _phase_metrics(calls: list[tuple[float, str, str]]) -> dict:
    """
    계산: 그룹별 **버스트 시작 시각**의 위상(분 mod10, 분 안의 초) — §2-3 판정용.
    해석: 위상은 "그 폴러가 언제 발사를 시작하는가"이므로 **전체 호출의 초 분포가 아니라 버스트
         시작 시각**으로 재야 한다. 전체 호출로 재면 30콜을 55초에 걸쳐 쏘는 만기유동성의
         최빈값이 39초로 나와(실제 발사는 35초) 설계값 대조가 어긋난다 — 2026-08-01 최초
         실행에서 실제로 겪었다.
    """
    out: dict[str, dict] = {}
    for group, bursts in _group_bursts(calls).items():
        starts = [b[0] for b in bursts]
        if not starts:
            continue
        seconds = collections.Counter(int(s % 60) for s in starts)
        minutes = collections.Counter(int(s % 600 // 60) for s in starts)
        out[group] = {
            "mode_second": seconds.most_common(1)[0][0],
            "top_seconds": dict(seconds.most_common(3)),
            "minutes_mod10": dict(sorted(minutes.items())),
            "burst_count": len(bursts),
        }
    return out


def _bucket(items: list[dict], key) -> dict:
    out: dict = collections.defaultdict(list)
    for item in items:
        out[key(item)].append(item)
    return out


def resolve_target_date(explicit: str | None, now: datetime) -> date:
    """`--date` 인자가 없으면 오늘 — 장마감 훅에서 그대로 부르면 그날치가 된다."""
    if explicit:
        return date.fromisoformat(explicit)
    return now.date()


# 2026-08-19 (08-18 보고서 §3-3 / Fix#3) — `previous_business_day()`를 **여기서 지웠다.**
#
# 그 함수는 주말만 건너뛰면서 docstring에 *"공휴일은 파일 존재 여부로 걸러진다"* 고 적어
# 두었다. **그 가정이 08-18에 깨졌다**: 광복절 대체휴일인 08-17에도 관측 루프가 돌아
# `auto/2026-08-17_지표.json`이 실제로 생겼고, 08-17이 **월요일**이라 주말 스킵에도 안 걸렸다.
# 결과는 08-18 §1의 붉은 ⚠ 4개가 전부 «거래일 vs 휴장일» 비교였다는 것이다.
#
# 대체는 `mahdi.market_calendar.previous_trading_day(target, calendar)`다. 여기에 남겨 두지
# 않는 이유는 **같은 질문에 답이 둘 있으면 하나는 반드시 틀린 채로 쓰인다**는 것이고,
# 그 틀린 쪽이 방금 하루를 오독하게 만들었다. 이 모듈은 순수 텍스트 파서로 남는다 —
# 달력은 파일을 읽는 일이라 여기 있으면 안 된다(규약 B, `market_calendar` 모듈 docstring).
