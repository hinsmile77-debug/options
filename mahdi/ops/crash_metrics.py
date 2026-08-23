"""관측 루프가 **왜 죽었는가** — 크래시 로그를 지표로 끌어온다 (2026-08-19).

## 이 모듈이 생긴 이유

2026-08-19에 워치독이 두 번 재기동했다(09:54:01 · 10:36:01, 둘 다 「생존 신호 stale」).
첫 번째의 사유는 이것이다:

    psycopg.OperationalError: connection failed: ... server closed the connection unexpectedly

DB 컨테이너가 **09:50:56에 재시작**됐고(`docker inspect` StartedAt), 6초 뒤 관측 루프가
`handle_message`의 `db.get_connection()`에서 그 예외를 맞고 죽었다.

**그 사유는 `logs/observation_loop_crash.log`에만 있었다.** `observation_loop.log`에는
`OperationalError`가 **0건**이다 — 예외가 로깅을 거치지 않고 프로세스를 끝냈기 때문이다.
그리고 그 파일을 읽는 코드가 이 저장소에 **하나도 없었다**. 그래서 그날 리포트는
「재기동 2회」까지만 말하고 **왜**에는 답하지 못했다.

08-18 보고서 §3-2(*"성공한 왕복이 어떤 지표에도 안 잡힌다"*)와 **같은 계열의 결함**이다:
사실은 파일에 있는데 그것을 세는 목록에 대상이 없었다.

## 왜 예외 정책은 안 바꾸는가

`main._WS_DISCONNECT_ERRORS` 주석과 `run_observation_loop_forever` docstring이 두 곳에서
*"DB 예외는 재시도로 해결되지 않는 별개의 문제라 그대로 전파해 사람이 보게 한다"*고 명시한다.
그 결정을 **하루치 관측으로 뒤집지 않는다** — 08-18 보고서가 Fix#1에서 정확히 그 실수를 했다.
게다가 그날 설계는 의도대로 작동했다: 루프가 크게 죽었고, 워치독이 3분 만에 되살렸고,
크래시 로그가 원인을 정확히 적었다. **없던 것은 회복이 아니라 그 원인을 읽는 눈이다.**

## 날짜 귀속 — 표식이 없으면 셀 수 없다

이 파일은 `start_mahdi_premarket.bat`의 `2>>`로 append되는 **가공 없는 stderr**이고,
2026-08-19까지 **타임스탬프가 한 줄도 없었다**. 07-19부터 트레이스백 세 개가 날짜 없이
쌓여 있었다. 그래서 같은 날 bat에 기동 표식 한 줄을 넣었고, 이 파서는 그 표식으로 구간을
가른다. 표식 **이전**의 트레이스백은 날짜를 알 수 없으므로 `unattributed`로 따로 센다 —
**「오늘 것이 아니다」와 「날짜를 모른다」는 다른 사실**이고, 후자를 0으로 접으면 과거
크래시가 오늘 것으로 둔갑하거나 그 반대가 된다.
"""

from __future__ import annotations

import collections
import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger("mahdi.ops.crash_metrics")

CRASH_LOG_FILENAME = "observation_loop_crash.log"

# ===== 2026-08-23 (08-21 §1-17 / §4 Fix#7) — **사람이 일부러 끈 것을 죽음으로 세지 않는다** =====
#
# ## 08-21에 무슨 일이 있었나
#
# 리포트 §11-1-1은 「사유 없는 죽음」을 `기동 횟수 − 사유가 남은 죽음 − 1`로 셌다. 그 `− 1`은
# 「장마감 정상 종료도 사유를 안 남긴다」를 뺀 것이고, 하드코딩된 상수다. 그날 12:18에 사람이
# `stop_mahdi_manual.bat`으로 프로그램을 내렸고, 그 정지가 **그 1건으로 잡혔다.**
#
# **표식은 둘이나 있었다**: `logs/.intentional_stop` 파일과 `premarket_startup.log`의
# `===== Mahdi 수동 정지 시작 (사람이 일부러 내림) =====` 줄. 워치독은 그것을 정확히 읽었고
# (재기동 시도 0회, `liveness.intentional_stop_at`), **지표만 몰랐다.**
#
# `collect_evidence`의 크래시 mtime 오탐과 같은 병이다 — 판정이 표식을 안 보고 숫자 하나만 본다.
#
# ## 왜 상수 `1`을 세는 값으로 바꾸는가
#
# 종료 종류는 셋이다: 장마감 자동 종료 · 사람의 수동 정지 · 설명 안 되는 소멸. 앞의 둘은
# **로그에 문구가 있고**, 마지막 하나만 없다. 「없는 것」을 세려면 「있는 것」을 다 빼야 하는데,
# 종전 식은 그중 하나를 상수로 박아 두 번째를 세 번째로 만들었다.
#
# ## 대가 — 경보를 끄는 것이 아니라 고치는 것이다
#
# 표식 **없이** 사라진 기동에는 여전히 떠야 한다. 08-19 10:32이 그 형태였고(표식 없이 사라진 뒤
# 워치독이 3분 만에 되살렸다), `tests/test_ops_crash_metrics.py`가 그 하루를 재현으로 고정한다.
STARTUP_LOG_FILENAME = "premarket_startup.log"

# `premarket_startup.log`의 종료 표식. **「완료」 줄을 센다** — 「시작」과 「완료」가 쌍으로
# 남으므로 한쪽만 세야 하고, 완료 쪽이 「실제로 끝났다」에 더 가깝다.
# 포맷 원본: `scripts/stop_mahdi_manual.bat` · `scripts/stop_mahdi_marketclose.bat`
_INTENTIONAL_STOP_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\]\s*=+\s*Mahdi 수동 정지 완료"
)
_CLEAN_SHUTDOWN_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\]\s*=+\s*장마감 자동 종료 완료"
)

# `start_mahdi_premarket.bat`이 `echo [%date% %time%] ===== 관측 루프 기동 =====`로 남기는 줄.
# `%time%`은 한 자리 시각에 **앞 공백**이 붙고(` 7:30:00.78`) 소수점 이하가 따라온다 —
# `premarket_startup.log` 실측 형식이 그렇고, 그 형식을 그대로 받는다.
_START_MARKER_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})(?:[.,]\d+)?\]\s*=+\s*관측 루프 기동"
)

# 트레이스백 마지막 줄 — `psycopg.OperationalError: ...` / `KeyboardInterrupt` 등.
#
# **점을 포함한 전체 이름을 잡는다**(`psycopg.OperationalError`). 마지막 조각만 세면
# 서로 다른 모듈의 같은 이름(`OperationalError`)이 한 칸에 합쳐져 원인 귀속이 흐려진다.
_EXCEPTION_RE = re.compile(r"^(\w+(?:\.\w+)*(?:Error|Exception|Interrupt|Exit))(?::\s*(.*))?$")

# 트레이스백 머리 줄. 표식이 없는 구간에서 **죽음의 개수 상한**을 세는 데 쓴다 —
# 그 구간은 경계가 없어 서로 갈라낼 수 없고, 구간 하나를 1건으로 세면 08-19에 쌓여 있던
# 트레이스백 셋이 하나로 뭉개진다.
_TRACEBACK_HEAD_RE = re.compile(r"^Traceback \(most recent call last\):")

# 트레이스백 프레임 — 마지막 프레임이 「우리 코드의 어디였나」에 답한다.
_FRAME_RE = re.compile(r'^\s+File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')

# 사유 한 줄에서 보존할 길이. 원문은 크래시 로그에 그대로 있으므로 여기서는 **식별에 필요한
# 만큼만** 남긴다 — 전문을 실으면 psycopg의 여러 줄짜리 메시지가 사이드카를 부풀린다.
_DETAIL_MAX_CHARS = 200


def _segment_crash(lines: list[str]) -> dict | None:
    """한 기동 구간의 stderr에서 **마지막 예외**와 그 직전 프레임을 뽑는다.

    반환: `{"cause", "detail", "last_frame"}` 또는 예외가 없으면 `None`
         (= 그 기동은 죽지 않았거나 아직 살아 있다).
    해석: **마지막** 예외를 취한다 — 파이썬은 `During handling of the above exception` /
         `The above exception was the direct cause of` 로 예외를 겹쳐 쌓고, 프로세스를
         실제로 끝낸 것은 맨 아래 것이다. 첫 예외를 취하면 08-19가 «asyncio.CancelledError»로
         보고됐을 것이다.
    실패 조건: 없다.
    """
    cause = detail = last_frame = None
    frame_buffer = None
    for line in lines:
        frame = _FRAME_RE.match(line.rstrip("\n"))
        if frame:
            # **구분자를 둘 다 자른다.** `Path(...).name`은 실행 플랫폼의 구분자만 알아서,
            # POSIX에서 Windows 경로를 읽으면 파일명이 통째로 남는다 — 이 파일은 운영 PC의
            # stderr이고 파서는 어디서든 돌 수 있어야 한다(pytest 포함).
            basename = re.split(r"[\\/]", frame["file"])[-1]
            frame_buffer = f"{basename}:{frame['line']} in {frame['func']}"
            continue
        # 콘솔이 남긴 `^C`가 줄 앞에 붙어 있을 수 있다(bat 창이 Ctrl+C로 끊긴 흔적) —
        # 그것 때문에 예외 줄을 못 읽으면 사유가 통째로 사라진다. 실제로 08-19 로그의
        # 세 트레이스백이 전부 `^C`로 시작한다.
        stripped = line.rstrip("\n").lstrip("^C").strip()
        match = _EXCEPTION_RE.match(stripped)
        if match:
            cause = match.group(1)
            detail = (match.group(2) or "").strip()[:_DETAIL_MAX_CHARS] or None
            last_frame = frame_buffer
    if cause is None:
        return None
    return {"cause": cause, "detail": detail, "last_frame": last_frame}


def parse(lines: list[str], target: date) -> dict:
    """
    입력: `observation_loop_crash.log`의 줄들(여러 날치가 섞여 있어도 된다), 대상 날짜.
    계산: 기동 표식으로 구간을 가르고, **그날 표식**에 속한 구간의 크래시 사유를 센다.
    해석: `starts`와 `crashes`를 **나란히** 낸다. 08-19는 표식 없이 기동 3회(07:30 · 09:54 ·
         10:36)에 크래시 사유 1건이었다 — 둘을 함께 보면 **「두 번은 사유가 안 남았다」**가
         드러난다. 한쪽만 세면 그 침묵이 안 보인다.
    실패 조건: 없다 — 형식이 안 맞는 줄은 해당 구간 본문으로 흘려보낸다.
    """
    segments: list[tuple[date | None, str | None, list[str]]] = [(None, None, [])]
    for line in lines:
        marker = _START_MARKER_RE.match(line)
        if marker:
            day = date.fromisoformat(marker.group(1))
            at = f"{int(marker.group(2)):02d}:{marker.group(3)}:{marker.group(4)}"
            segments.append((day, at, []))
            continue
        segments[-1][2].append(line)

    starts = 0
    crashes: list[dict] = []
    unattributed = 0
    for day, at, body in segments:
        crash = _segment_crash(body)
        if day is None:
            # 표식 **이전**의 본문 — 날짜를 알 수 없다. 「오늘 것이 아니다」로 접지 않는다.
            #
            # 이 구간은 표식이 없으므로 죽음을 서로 갈라낼 수 없다. 그래서 **트레이스백 머리
            # 줄을 센다** — 예외 연쇄(`During handling of ...`)가 있으면 실제 죽음보다 많이
            # 세어지므로 이 값은 **상한**이고, 호출측이 그렇게 인쇄한다.
            # 구간 하나로 「1건」이라고 세면 08-19의 트레이스백 셋이 하나로 뭉개진다.
            unattributed += sum(1 for line in body if _TRACEBACK_HEAD_RE.match(line.lstrip("^C")))
            continue
        if day != target:
            continue
        starts += 1
        if crash:
            crashes.append({"at": at, **crash})

    return {
        "starts": starts,
        "crashes": len(crashes),
        "causes": dict(collections.Counter(c["cause"] for c in crashes).most_common()),
        "events": crashes,
        # **「날짜를 모르는 트레이스백이 몇 개인가」.** 표식을 넣기 전(2026-08-19 이전)에 쌓인
        # 것들이 여기 잡힌다. 0이 될 때까지는 `crashes`가 그날 전부라고 단정할 수 없다.
        "unattributed": unattributed,
        # 표식 자체가 하나도 없으면 이 파서는 아무것도 귀속하지 못한다 — 그 사실을 값으로 낸다
        # (규약 C: 「크래시가 없었다」와 「셀 수 없었다」는 다르다).
        "marker_present": starts > 0,
    }


def parse_shutdowns(lines: list[str], target: date) -> dict:
    """
    입력: `premarket_startup.log`의 줄들(여러 날치가 섞여 있어도 된다), 대상 날짜.
    계산: 그날의 **의도적 종료** 두 종류를 센다 — 장마감 자동 종료와 사람의 수동 정지.
    해석: 상세 근거는 `STARTUP_LOG_FILENAME` 위 주석. 둘을 **합치지 않는다**: 하나는 스케줄러가
         매일 하는 일이고 다른 하나는 사람이 그날 내린 결정이다. 08-21에 후자가 1건 있었고
         그것이 「사유 없이 끝난 기동」으로 잘못 세어졌다.
    실패 조건: 없다 — 형식이 안 맞는 줄은 건너뛴다.
    """
    intentional = clean = 0
    for line in lines:
        m = _INTENTIONAL_STOP_RE.match(line)
        if m and m.group(1) == target.isoformat():
            intentional += 1
            continue
        m = _CLEAN_SHUTDOWN_RE.match(line)
        if m and m.group(1) == target.isoformat():
            clean += 1
    return {"intentional_stops": intentional, "clean_shutdowns": clean}


def unexplained_deaths(starts: int, crashes: int, intentional: int, clean: int) -> int:
    """반환: **아무 표식도 안 남기고 사라진** 기동 수.

    `기동 − 사유가 남은 죽음 − 장마감 자동 종료 − 사람의 수동 정지`. 음수는 0으로 접는다 —
    종료 표식이 기동보다 많은 날(전날 프로세스를 오늘 껐다)에 음수를 인쇄하면 그 자체가 오보다.

    ⚠ **이 값이 0이라고 「아무 일도 없었다」가 아니다.** 08-19 10:32처럼 표식 없이 사라진
    기동이 있는 날에는 1 이상이어야 하고, 그 하루를 재현 테스트가 고정한다.
    """
    return max(starts - crashes - intentional - clean, 0)


def collect(log_dir: Path, target: date) -> dict | None:
    """반환: `parse()` 결과에 종료 회계를 더한 것. 크래시 로그가 없으면 None
    (= 「모른다」, 「크래시가 없었다」가 아니다).

    2026-08-23 Fix#7 — `premarket_startup.log`를 **함께** 읽는다. 그 파일이 없으면 종료 수는
    `None`으로 두고(0이 아니다 — 규약 C) 「사유 없는 죽음」도 계산하지 않는다.
    """
    path = Path(log_dir) / CRASH_LOG_FILENAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("크래시 로그 읽기 실패: %s", path, exc_info=True)
        return None
    result = parse(lines, target)

    startup_path = Path(log_dir) / STARTUP_LOG_FILENAME
    try:
        startup_lines = startup_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        # 못 읽었다 = **모른다.** 0으로 접으면 08-21의 오탐이 그대로 돌아온다.
        result.update(
            {"intentional_stops": None, "clean_shutdowns": None, "unexplained_deaths": None}
        )
        return result

    shutdowns = parse_shutdowns(startup_lines, target)
    result.update(shutdowns)
    result["unexplained_deaths"] = unexplained_deaths(
        result["starts"], result["crashes"],
        shutdowns["intentional_stops"], shutdowns["clean_shutdowns"],
    )
    return result
