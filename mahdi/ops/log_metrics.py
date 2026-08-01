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
_SLOW_CALL_RE = re.compile(
    _TS + r" WARNING:mahdi\.broker\.rest_client:느린 REST 호출 ([\d.]+)초 = "
    r"페이서대기 ([\d.]+)초 \+ HTTP ([\d.]+)초 \(배율 ([\d.]+)배, (\w+) (\S+)\)"
)
_CATCHUP_RE = re.compile(
    _TS + r" INFO:mahdi\.main:옵션체인 결손 회수: (\d\d):(\d\d) 분을 먼슬리 (\d+)레그로 채움"
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
_EXCEPTION_PREFIXES = {
    "remote_protocol_error": "httpx.RemoteProtocolError",
    "http_status_error": "httpx.HTTPStatusError",
    "read_timeout": "httpx.ReadTimeout",
    "connect_error": "httpx.ConnectError",
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
    failures: collections.Counter = collections.Counter()
    levels: collections.Counter = collections.Counter()
    qualitative: collections.Counter = collections.Counter()
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

        m = _FAILURE_RE.match(line)
        if m:
            # "옵션 체인 폴링 실패: B01608875 — {...}" → 종목/응답을 떼고 유형만 센다.
            failures[m.group(6).split(":")[0].strip()] += 1

    cycles.sort(key=lambda c: c["start"])
    return {
        "date": target.isoformat(),
        "cycles": _cycle_metrics(cycles, calls, catchups),
        "rest": _rest_metrics(calls),
        "backoff": _backoff_metrics(backoff_events, cycles),
        "bursts": _burst_metrics(calls),
        "stalls": _stall_metrics(calls),
        "slow_calls": _slow_call_metrics(slow_calls),
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
        "failures": dict(failures.most_common()),
        "overrun": {
            "count": len(overrun_seconds),
            "max_seconds": round(max(overrun_seconds), 1) if overrun_seconds else 0.0,
            "total_seconds": round(sum(overrun_seconds), 1),
        },
    }


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
