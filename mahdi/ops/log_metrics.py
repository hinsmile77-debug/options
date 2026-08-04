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
from datetime import date, datetime, timedelta
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
    r"\(rows=(\d+)(?:, 재시도함)?, 밀림=([\d.-]+)초, 타폴러동시호출추정=(\S+?)\)"
)
_OVERRUN_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:옵션 체인 폴링 사이클이 주기\([\d.]+초\)를 초과해 "
    r"스케줄이 ([\d.]+)초 밀렸습니다"
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
# 2026-08-04(Fix#8) — 수집 예산 초과(`mahdi.main.LOG_CHAIN_BUDGET_EXCEEDED`).
_BUDGET_RE = re.compile(
    _TS + r" WARNING:mahdi\.main:옵션체인 수집 예산\([\d.]+초\) 초과 — 남은 (\d+)레그를 포기하고 (\d+)레그로"
)
_LEVEL_RE = re.compile(_TS + r" (INFO|WARNING|ERROR|CRITICAL|DEBUG):(\S+?):")
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
}
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
    "overrun": "스케줄이",
    "backoff": "레이트리밋 백오프",
    "slow_calls": "느린 REST 호출",
    "catchups": "옵션체인 결손 회수",
    "rest_latency": "REST 응답시간",
    "atm_rolls": "ATM 롤링",
    "budget_exceeded": "수집 예산",
    "remote_protocol_error": "RemoteProtocolError",
    "read_timeout": "ReadTimeout",
    "connect_error": "ConnectError",
    "kis_error_response": _KIS_ERROR_BODY_TOKEN,
}


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


def _hhmm(seconds_of_day: float) -> str:
    total = int(seconds_of_day)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}"


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
    failures: collections.Counter = collections.Counter()
    levels: collections.Counter = collections.Counter()
    qualitative: collections.Counter = collections.Counter()
    audit_loose: collections.Counter = collections.Counter()
    overrun_seconds: list[float] = []
    total_bytes = 0
    httpx_bytes = 0
    total_lines = 0
    human_lines = 0

    for line in lines:
        raw = line.encode("utf-8")
        total_bytes += len(raw) + 1
        total_lines += 1
        is_httpx = "INFO:httpx:" in line
        if is_httpx:
            httpx_bytes += len(raw) + 1
        else:
            human_lines += 1

        for key, marker in _QUALITATIVE_MARKERS.items():
            if marker in line:
                qualitative[key] += 1
        for key, prefix in _EXCEPTION_PREFIXES.items():
            if line.startswith(prefix):
                qualitative[key] += 1
        for key, marker in _HANDLED_EXCEPTION_MARKERS.items():
            if marker in line:
                qualitative[key] += 1
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
                    "rest": rest,
                    "db": db_s,
                    "rows": int(m.group(10)),
                    "slip": float(m.group(11)),
                    "concurrent_reported": None if not concurrent.isdigit() else int(concurrent),
                }
            )
            continue

        m = _OVERRUN_RE.match(line)
        if m:
            overrun_seconds.append(float(m.group(6)))
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
                {"at": _hhmm(_seconds_of_day(m)), "skipped": int(m.group(6)), "collected": int(m.group(7))}
            )
            continue

        m = _FAILURE_RE.match(line)
        if m:
            # "옵션 체인 폴링 실패: B01608875 — {...}" → 종목/응답을 떼고 유형만 센다.
            failures[m.group(6).split(":")[0].strip()] += 1
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
        }
    )
    return {
        "date": target.isoformat(),
        "cycles": _cycle_metrics(cycles, calls, catchups),
        "rest": _rest_metrics(calls),
        "backoff": _backoff_metrics(backoff_events, cycles),
        "bursts": _burst_metrics(calls),
        "stalls": _stall_metrics(calls),
        "slow_calls": _slow_call_metrics(slow_calls),
        "rest_latency": _rest_latency_metrics(latency_windows),
        "atm_rolls": _atm_roll_metrics(atm_rolls),
        "budget_exceeded": {
            "count": len(budget_events),
            "skipped_legs_total": sum(e["skipped"] for e in budget_events),
            "samples": budget_events[:10],
        },
        "catchups": {"count": len(catchups), "minutes": [c["minute"] for c in catchups]},
        "poller_phase": _phase_metrics(calls),
        "log_volume": {
            "total_bytes": total_bytes,
            "total_lines": total_lines,
            "httpx_bytes": httpx_bytes,
            "httpx_pct": round(httpx_bytes / total_bytes * 100, 1) if total_bytes else None,
            "human_lines": human_lines,
            "by_level": dict(levels),
        },
        "qualitative": dict(qualitative),
        "parser_audit": _parser_audit(strict_counts, audit_loose),
        "failures": dict(failures.most_common()),
        "overrun": {
            "count": len(overrun_seconds),
            "max_seconds": round(max(overrun_seconds), 1) if overrun_seconds else 0.0,
            "total_seconds": round(sum(overrun_seconds), 1),
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


def _rest_latency_metrics(windows: list[dict]) -> dict:
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
    hourly: dict[tuple[int, str], list[dict]] = collections.defaultdict(list)
    for w in windows:
        hourly[(int(w["at"] // 3600), w["endpoint"])].append(w)
    for (hour, endpoint), items in hourly.items():
        grid[str(hour)][endpoint] = weighted(items, "p95")

    warnings = [
        {"hour": hour, "endpoint": endpoint, "p95": p95}
        for hour, row in sorted(grid.items(), key=lambda kv: int(kv[0]))
        for endpoint, p95 in sorted(row.items())
        if p95 > REST_LATENCY_P95_WARN_SECONDS
    ]
    return {
        "endpoints": endpoints,
        "p95_by_hour": {h: dict(sorted(row.items())) for h, row in sorted(grid.items(), key=lambda kv: int(kv[0]))},
        "p95_warn_threshold": REST_LATENCY_P95_WARN_SECONDS,
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


def _cycle_metrics(cycles: list[dict], calls: list[tuple[float, str, str]], catchups: list[dict]) -> dict:
    if not cycles:
        return {"count": 0, "by_hour": [], "by_mod10": [], "missing": {"count": 0, "list": []}}

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

    seen = {_hhmm(c["start"]) for c in cycles}
    first, last = cycles[0]["start"], cycles[-1]["start"]
    missing = []
    t = first
    while t <= last:
        label = _hhmm(t)
        if label not in seen:
            missing.append(label)
        t += 60
    recovered = {c["minute"] for c in catchups}
    unrecovered = [m for m in missing if m not in recovered]
    return {
        "count": len(cycles),
        "first_start": _hhmm(first),
        "last_start": _hhmm(last),
        "rest_seconds": _stats(rests),
        "over_60s": sum(1 for x in rests if x > 60),
        "rows_distribution": dict(sorted(collections.Counter(c["rows"] for c in cycles).items())),
        "by_hour": by_hour,
        "by_mod10": by_mod10,
        "missing": {
            "count": len(missing),
            "odd": sum(1 for m in missing if int(m[3:]) % 2 == 1),
            "even": sum(1 for m in missing if int(m[3:]) % 2 == 0),
            "list": missing,
            "recovered_by_catchup": len(missing) - len(unrecovered),
            "unrecovered_count": len(unrecovered),
        },
    }


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


def _slow_call_metrics(slow: list[dict]) -> dict:
    """§4 우선순위 3 판정용 — 지연이 페이서 대기와 HTTP 중 어디로 귀속되는지."""
    if not slow:
        return {"count": 0, "pacer_dominant": 0, "http_dominant": 0, "samples": []}
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


def previous_business_day(target: date) -> date:
    """델타 비교 기준일 후보 — 주말은 건너뛴다(공휴일은 파일 존재 여부로 걸러진다)."""
    day = target - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day
