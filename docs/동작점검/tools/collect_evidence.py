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


def run_git(root, args, timeout=25):
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
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

        for key, tokens in ALWAYS_QUOTE.items():
            if any(t in msg for t in tokens):
                self.quoted[key].append((hhmmss, level, truncate(msg, 240)))

    def gaps(self):
        lo, hi = hhmm_to_min(GAP_SCAN[0]), hhmm_to_min(GAP_SCAN[1])
        pts = sorted(x for x in self.minutes_seen if lo <= x <= hi)
        return [(a, b, b - a) for a, b in zip(pts, pts[1:]) if b - a >= GAP_THRESHOLD_MIN]

    def anchor_hits(self, phases):
        out = []
        for at, label, ph in ANCHORS:
            if ph not in phases:
                continue
            t = hhmm_to_min(at)
            hits = sum(1 for x in self.minutes_seen if abs(x - t) <= ANCHOR_WINDOW_MIN)
            out.append((at, label, hits))
        return out


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
        A(f"- `observation_loop_crash.log`: {fmt_bytes(crash.stat().st_size)} · 최종 {mt:%m-%d %H:%M}"
          + ("  ← **오늘 갱신됨 ⚠**" if mt.date() == day else ""))
        if mt.date() == day:
            flags.append("크래시 로그가 오늘 갱신됐다 — 트레이스백 마지막 프레임을 반드시 인용할 것")
            A("")
            A("```")
            L.extend(read_text(crash).splitlines()[-12:])
            A("```")
    A("")

    # ---- 7. 레버 ----
    A("## 7. 레버 상태 — 오늘 그 코드가 실제로 돌았는가")
    A("")
    A("| 레버 | 위치 | 현재 줄 |")
    A("|---|---|---|")
    for key, rel in LEVER_KEYS:
        p = root / rel
        found = "**파일 없음**"
        if p.exists():
            hits = [ln.strip() for ln in read_text(p).splitlines()
                    if key in ln and not ln.strip().startswith("#")]
            found = truncate(hits[0], 90) if hits else "**키 없음(기본값으로 동작)**"
        A(f"| `{key}` | `{rel}` | {found} |")
    A("")
    A("> **규약 H — 레버가 꺼진 날의 숫자로 그 레버의 가설을 판정하지 않는다.**")
    A("> 2026-08-12에 「오늘 켤 유일한 레버」가 안 켜졌는데 지표는 켜진 전제로 판정했다(§1-1).")
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

    # ---- 9. 산출물 ----
    A("## 9. 산출물 존재 점검")
    A("")
    prev_hint = (day - timedelta(days=1)).isoformat()
    targets = [
        (f"docs/동작점검/auto/{D}_지표.md", "post"),
        (f"docs/동작점검/auto/{D}_지표.json", "post"),
        (f"docs/동작점검/{D}_마흐디_운영점검보고서.md", "post"),
        (f"docs/동작점검/auto/{prev_hint}_지표.json", "pre"),
    ]
    A("| 파일 | 기대 국면 | 상태 | 크기 | 최종기록 |")
    A("|---|---|---|---|---|")
    for rel, ph in targets:
        state, size, mt = stat_line(root / rel)
        A(f"| `{rel}` | {ph} | {state} | {size} | {mt} |")
        if ph == "post" and "post" in cfg_phases and state.startswith("**없음"):
            flags.append(f"장후 산출물 누락: `{rel}`")
    A("")
    A("> 전일 사이드카(`_지표.json`)가 없으면 **전일 델타가 통째로 빈다.** 로그는 이틀치만")
    A("> 남으므로 그 델타는 나중에 복구할 수 없다.")
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
        out_arg = str(Path(args.out_dir) / f"{day.isoformat()}_증거_{args.phase}.md")
    if out_arg:
        outp = Path(out_arg)
        if not outp.is_absolute():
            outp = root / outp
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
        eprint(f"[collect_evidence] 저장: {outp} ({fmt_bytes(len(text.encode('utf-8')))})")
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
