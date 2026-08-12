"""관측 루프 생존 신호 — 프로세스가 죽었다는 것을 **누군가는 알아야 한다** (2026-08-06 §2-1 / Fix#2).

## 이 모듈이 생긴 이유

2026-08-06 10:04:00, 관측 루프와 COCKPIT이 **동시에, 흔적 없이** 사라졌다. 로그의 마지막 줄은
평범한 WS 구독 성공이고 그다음 줄이 19분 뒤의 재기동 로그다. 그 사이에 아무것도 없다:

- 종료 로그 없음 — 정상 종료 경로를 안 탔다.
- `logs/observation_loop_crash.log`는 2바이트, 최종 수정 2026-07-19 — **stderr에도 안 남았다.**
- 트레이스백 없음, 알림 없음.

**19분 동안 시스템의 어떤 부분도 자신이 죽었다는 것을 몰랐다.** 사람이 화면을 보고 알아챈 것이
10:20이었다. 그날 결손 21분의 95%가 이 구간이다.

지금은 ADVISORY라 데이터 손실로 끝났다. Phase 2에서 포지션을 들고 같은 일이 나면
**15:10 강제 평탄화가 실행되지 않는다**(v6 §13.3 "해제 불가"). 2026-07-21 보고서가
*"PID 파일 기반으로 재설계할 것"* 을 제안해두고 그대로 남아 있던 안건이 그날 현실이 됐다.

## 왜 창 제목(WINDOWTITLE)이 아니라 파일인가

기동/종료 스크립트는 `taskkill /FI "WINDOWTITLE eq ..."`로 프로세스를 찾는다. 2026-07-21에
그 방식이 실패하는 것을 실측했다 — 사람이 사고 대응 중 수동으로 창을 띄우면 제목 규약이 깨져
`taskkill`이 아무것도 못 찾고 **조용히 성공을 보고한다**. 그리고 창 제목은 "프로세스가 살아
있는가"만 답할 수 있고 "이벤트 루프가 돌고 있는가"에는 답하지 못한다.

## 규약 D — 감시자는 감시 대상과 독립한 타이머에서 나온다

하트비트를 옵션체인 폴러에 얹지 않는다. 그 폴러가 REST에 물려 60초를 넘겨도 루프 자체는
살아 있을 수 있고, 반대로 그 폴러만 살아 있고 나머지가 죽었을 수도 있다. **전용 태스크**가
고정 주기로 쓴다 — 이 파일의 나이는 정확히 *"이벤트 루프가 마지막으로 스케줄을 돌린 시각"* 이다.

같은 원칙을 2026-08-01에 CB 하트비트에서 이미 배웠다(감시 대상 이벤트에 얹으면 "이벤트가
없으면 신호도 멈춰" 죽은 것과 구분되지 않는다).

## 이 모듈이 하지 않는 것

**스스로 재기동하지 않는다.** 판단은 워치독(`scripts/watchdog_observation_loop.py`)의 몫이고,
여기서는 사실만 남긴다 — 관측 계층이 운영 조치를 내리기 시작하면 그 조치가 다시 관측을 바꾼다
(2026-07-08에 페이서 자동 적응으로 500 폭주를 겪은 것과 같은 되먹임이다).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from mahdi import session

logger = logging.getLogger("mahdi.liveness")

# 하트비트 기록 주기. 사이클 주기(60초)보다 짧아야 "한 사이클 밀림"과 "죽음"이 구분된다.
HEARTBEAT_INTERVAL_SECONDS = 30.0

# 이 나이를 넘으면 죽은 것으로 본다 = 기록 주기 x 6.
#
# 근거: 정상 상태에서 이 파일은 30초마다 갱신된다. 임계를 60초에 두면 GC 한 번, 디스크 지연 한
# 번에 오경보가 난다. 180초는 **하트비트를 다섯 번 연속 놓쳤다**는 뜻이고, 그 정도면 이벤트
# 루프가 살아서 다른 일을 하고 있을 가능성은 사실상 없다. 08-06의 공백은 19분이었다 —
# 이 임계로는 3분 안에 알았을 것이다.
#
# **오경보 비용이 미탐지 비용보다 훨씬 싸지 않다**는 점이 임계를 낮추지 않은 이유다. 워치독이
# 재기동까지 하므로, 살아 있는 루프를 죽이면 그 순간 진짜 공백이 생긴다.
HEARTBEAT_STALE_SECONDS = 180.0

_HEARTBEAT_FILENAME = ".observation_loop_heartbeat.json"


def heartbeat_path(log_dir: Path) -> Path:
    return log_dir / _HEARTBEAT_FILENAME


def write_heartbeat(path: Path, now: datetime, *, beats: int, pid: int | None = None) -> None:
    """
    입력: 하트비트 파일 경로, 현재 시각, 누적 박동 수, (선택) PID.
    계산: 임시 파일에 쓰고 **원자적으로 교체**한다 — 워치독이 쓰기 도중의 파일을 읽으면
         JSON 파싱에 실패하고, 그 실패를 "죽었다"로 오독하면 살아 있는 루프를 죽인다.
    실패 조건: 어떤 예외도 밖으로 내지 않는다(로그만) — **생존 신호를 못 썼다고 관측이
              멈추면 안 된다.** 못 쓰면 워치독이 알아서 죽은 것으로 보고 조치한다.
    """
    payload = {
        "pid": pid if pid is not None else os.getpid(),
        "at": now.isoformat(),
        "beats": beats,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.warning("생존 신호 기록 실패: %s", path, exc_info=True)


def read_heartbeat(path: Path) -> dict | None:
    """
    반환: `{"pid": int, "at": datetime, "beats": int}` 또는 None(파일 없음/깨짐).
    해석: **None은 "죽었다"가 아니라 "모른다"** 이다. 정상 종료 시 파일을 지우므로(`clear`)
         장마감 후에도 None이고, 그때 알림을 내면 매일 밤 오경보가 된다 — 호출측이 장중인지
         먼저 본다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            "pid": int(raw["pid"]),
            "at": datetime.fromisoformat(raw["at"]),
            "beats": int(raw.get("beats", 0)),
        }
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("생존 신호 읽기 실패: %s", path, exc_info=True)
        return None


def clear_heartbeat(path: Path) -> None:
    """정상 종료 시 지운다 — 남겨두면 장마감 후 내내 "180초째 갱신 없음"으로 보인다."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.warning("생존 신호 삭제 실패: %s", path, exc_info=True)


def heartbeat_age_seconds(beat: dict | None, now: datetime) -> float | None:
    """반환: 마지막 박동 이후 경과 초. beat가 없으면 None."""
    if not beat:
        return None
    return (now - beat["at"]).total_seconds()


def is_stale(beat: dict | None, now: datetime, threshold: float = HEARTBEAT_STALE_SECONDS) -> bool:
    """
    반환: 이 박동이 죽은 것으로 볼 만큼 늙었는가.
    해석: `beat`가 None이면 **False**를 낸다 — 파일이 없는 것은 "죽음"이 아니라 "아직/이미
         안 도는 상태"다. 없는 것과 늙은 것을 같이 취급하면 장마감 후와 기동 전에 매일 두 번
         알림이 뜬다(그런 알림은 곧 무시된다).
    """
    age = heartbeat_age_seconds(beat, now)
    return age is not None and age > threshold


# ===== 워치독 판정 =====
#
# **판정은 여기(pytest로 테스트되는 파이썬), 조치는 스크립트.** `docs/동작점검/README.md`가
# `scripts/`를 얇게 유지하라고 정한 규약과 같은 이유이고, `scripts/log_marketclose_stop.py`가
# 같은 규약을 문서로 남기고 있다.

# 감시 창 — 관측 루프가 **떠 있어야 하는** 시간대.
#
# 시작을 07:40으로 두는 이유: 장전 기동은 07:30이고 루프가 첫 하트비트를 쓰는 것은 07:31 부근이다
# (마스터파일 다운로드가 앞에 있다). 07:35에 창을 열면 Docker가 느린 날마다 오경보가 난다.
# 끝은 장마감 자동 종료(15:45)와 같다 — 그 이후의 부재는 정상이다.
WATCH_WINDOW_START = dtime(7, 40)
WATCH_WINDOW_END = session.TRADING_DAY_END

# 하루 자동 재기동 상한.
#
# **재기동은 그 자체로 데이터 공백을 만든다**(08-06 실측 12~14초). 무한 재시도는 "죽는 원인"이
# 재기동으로 안 풀리는 종류일 때 하루 종일 공백을 반복 생산한다. 3회를 넘으면 사람이 봐야 하는
# 문제이므로 알림만 내고 손을 뗀다.
MAX_RESTARTS_PER_DAY = 3

# 같은 조건으로 다시 알리기까지의 최소 간격 — 1분 주기 워치독이 매분 같은 말을 하면 곧 무시된다.
ALERT_COOLDOWN_SECONDS = 600.0

ACTION_OK = "ok"
ACTION_IDLE = "idle"  # 감시 창 밖 — 아무것도 하지 않는다
ACTION_RESTART = "restart"
ACTION_ALERT_ONLY = "alert_only"  # 이상은 맞는데 재기동 상한을 썼다

REASON_MISSING = "missing"
REASON_STALE = "stale"


@dataclass(frozen=True, slots=True)
class WatchdogDecision:
    action: str
    reason: str | None = None
    detail: str = ""
    should_alert: bool = False


def in_watch_window(now: datetime) -> bool:
    return WATCH_WINDOW_START <= now.time() <= WATCH_WINDOW_END


# 기동 진행 중 표식 — 기동 스크립트가 시작할 때 만들고 끝날 때 지운다.
#
# **없으면 워치독이 기동 중인 루프를 다시 기동한다.** 기동 스크립트는 Docker 데몬을 최대
# 180초까지 기다리므로, 그 사이 하트비트는 늙은 채로 남아 있다(직전 프로세스의 것이거나 없다).
# 1분 주기 워치독이 그것을 보고 재기동을 걸면 기동이 서로를 덮어쓴다.
_STARTUP_MARKER_FILENAME = ".startup_in_progress"

# 표식이 이 나이를 넘으면 무시한다 — 기동 스크립트가 중간에 죽어 표식만 남는 경우가 있다.
# 08-06 10:20:11의 `^C`로 중단된 기동이 정확히 그 경우다(표식이 있었다면 남았을 것이다).
STARTUP_MARKER_GRACE_SECONDS = 300.0


def startup_marker_path(log_dir: Path) -> Path:
    return log_dir / _STARTUP_MARKER_FILENAME


# ===== 2026-08-12 §2-3 / Fix#8 — **감시자를 감시한다** =====
#
# 08-12에 워치독은 10:14:01에 정확히 판정하고 정확히 되살렸다. 그런데 재기동 호출이 상속된
# 파이프에 물려(`scripts/watchdog_observation_loop._restart` docstring) **15:45:02까지 매달렸고**,
# 작업 스케줄러의 `MultipleInstances=IgnoreNew`가 그동안의 매분 실행을 전부 무시했다.
# 결과: **10:20~15:40, 5시간 31분 동안 워치독이 한 번도 판정하지 않았다.**
#
# ## 왜 기존 신호로는 안 보였는가
#
# 셋 다 정상으로 보였다:
#   - 프로세스: 살아 있었다(막혀 있었을 뿐).
#   - 스케줄러: `State: Ready`, `LastTaskResult: 0`, `NumberOfMissedRuns: 0`.
#   - `watchdog.log`: 마지막 줄이 「RESTART」라 **사고 대응 중인 것처럼** 보였다.
#
# `watchdog.log`의 침묵으로도 사후에는 알 수 있다 — 실제로 08-12를 그렇게 찾았다. 그러나 그것은
# **정상일에 OK를 10분에 한 번만 남기기 때문에** 최대 10분 해상도이고, 무엇보다 *다음 날 사람이
# 읽어야* 보인다. 08-06 Fix#2가 관측 루프에 대해 고친 것과 **똑같은 사각지대**다:
# 「죽었다는 것을 누군가는 알아야 한다」.
#
# ## 규약 D를 워치독 자신에게 적용한다
#
# 관측 루프의 하트비트가 그렇듯, 이 파일은 **판정할 때마다** 갱신된다 — `ACTION_IDLE`(감시 창
# 밖)일 때도 쓴다. 그래야 「감시 창 밖이라 조용하다」와 「막혀서 조용하다」가 갈린다.
# 로그와 달리 억제가 없다(파일 하나를 덮어쓰는 것이라 볼륨 부담이 없다).
_WATCHDOG_CHECK_FILENAME = ".watchdog_last_check.json"

# 이 나이를 넘으면 워치독이 판정을 못 하고 있는 것으로 본다.
#
# 워치독은 **1분 주기**다. 임계를 2분에 두면 스케줄러 지터 한 번에 오경보가 나고, 3분은
# 「세 번 연속 못 돌았다」는 뜻이다. 관측 루프 하트비트(30초 주기 / 180초 임계 = 6배)보다
# 배수가 작은 이유: 이 배지는 **재기동을 유발하지 않는다**(사람에게 보이기만 한다). 규약 D의
# "오경보 비용이 미탐지 비용보다 훨씬 싸지 않다"는 자동 조치가 붙은 신호에만 해당한다.
WATCHDOG_CHECK_STALE_SECONDS = 180.0


def watchdog_check_path(log_dir: Path) -> Path:
    return log_dir / _WATCHDOG_CHECK_FILENAME


def write_watchdog_check(path: Path, now: datetime, *, action: str, detail: str = "") -> None:
    """
    입력: 기록 경로, 판정 시각, 그 판정의 `action`/`detail`.
    계산: 원자적 교체로 덮어쓴다 — 워치독이 쓰는 도중의 파일을 COCKPIT이 읽어 파싱에 실패하고,
         그 실패를 "워치독이 멈췄다"로 오독하면 이 배지가 자기가 잡으려는 오경보를 만든다
         (`write_heartbeat`와 같은 이유).
    해석: **`action`을 함께 남기는 것이 핵심이다.** 시각만 남기면 「돌긴 했는데 감시 창 밖이라
         아무것도 안 했다」와 「감시 창 안에서 정상이라고 판정했다」가 구분되지 않는다.
    실패 조건: 어떤 예외도 밖으로 내지 않는다 — **자기 기록을 못 썼다고 워치독이 멈추면 안 된다.**
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"at": now.isoformat(), "action": action, "detail": detail}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        logger.warning("워치독 판정 기록 실패: %s", path, exc_info=True)


def read_watchdog_check(path: Path) -> dict | None:
    """반환: `{"at": datetime, "action": str, "detail": str}` 또는 None(없음/깨짐).

    **None은 "멈췄다"가 아니라 "모른다"** 이다 — 이 파일이 생기기 전 버전으로 돌던 날과
    워치독이 미등록인 PC가 둘 다 None이다. 판정은 호출측이 감시 창과 함께 한다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            "at": datetime.fromisoformat(raw["at"]),
            "action": str(raw.get("action", "")),
            "detail": str(raw.get("detail", "")),
        }
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("워치독 판정 기록 읽기 실패: %s", path, exc_info=True)
        return None


def watchdog_check_age_seconds(check: dict | None, now: datetime) -> float | None:
    """반환: 워치독이 마지막으로 **판정한** 이후 경과 초. 기록이 없으면 None."""
    if not check:
        return None
    return (now - check["at"]).total_seconds()


def startup_in_progress(
    path: Path, now: datetime, grace_seconds: float = STARTUP_MARKER_GRACE_SECONDS
) -> bool:
    """반환: 기동 스크립트가 지금 돌고 있는가(표식이 있고 아직 안 늙었는가).

    표식의 **내용은 읽지 않는다** — cmd.exe가 쓴 날짜 문자열은 로케일에 따라 형식이 바뀌고,
    그 파싱이 깨지면 워치독이 조용히 오판한다. 파일 mtime만 본다.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (now.timestamp() - mtime) < grace_seconds


def decide(
    beat: dict | None,
    now: datetime,
    state: dict | None = None,
    *,
    stale_seconds: float = HEARTBEAT_STALE_SECONDS,
    starting: bool = False,
) -> WatchdogDecision:
    """
    입력: 마지막 박동(`read_heartbeat()` 결과), 현재 시각, 워치독 상태
         (`{"date": "YYYY-MM-DD", "restarts": int, "last_alert_at": iso}`).
    계산: 감시 창 안에서 박동이 **없거나 늙었으면** 재기동을 지시한다. 하루 상한을 넘겼으면
         알림만 낸다. 알림은 `ALERT_COOLDOWN_SECONDS`로 눌러 같은 말을 반복하지 않는다.
    해석: **"없음"과 "늙음"을 구분해서 사유로 남긴다.** 없음은 *아예 안 떴다*(08-06 10:20의
         중단된 기동이 그 예다), 늙음은 *떴다가 죽었다*(10:04)이다. 조치는 같지만 다음날
         원인을 찾을 때 이 구분이 출발점이 된다.
         상태의 `date`가 오늘이 아니면 재기동 카운터를 0으로 본다 — 프로세스가 하루 단위라
         상한도 하루 단위다.
    실패 조건: 없다(순수 함수). 시각 판단만 하고 파일도 프로세스도 건드리지 않는다.
    """
    if not in_watch_window(now):
        return WatchdogDecision(ACTION_IDLE, detail="감시 창 밖")
    if starting:
        return WatchdogDecision(ACTION_IDLE, detail="기동 진행 중 — 판정 보류")

    age = heartbeat_age_seconds(beat, now)
    if beat is None:
        reason, detail = REASON_MISSING, "생존 신호 파일 없음 — 관측 루프가 뜨지 않았다"
    elif age is not None and age > stale_seconds:
        reason = REASON_STALE
        detail = (
            f"생존 신호가 {age:.0f}초째 갱신되지 않음(임계 {stale_seconds:.0f}초, "
            f"PID {beat.get('pid')})"
        )
    else:
        return WatchdogDecision(ACTION_OK, detail=f"정상 — 마지막 박동 {age:.0f}초 전")

    restarts = _restarts_today(state, now)
    should_alert = _alert_due(state, now)
    if restarts >= MAX_RESTARTS_PER_DAY:
        return WatchdogDecision(
            ACTION_ALERT_ONLY, reason,
            detail=f"{detail} — 오늘 자동 재기동 {restarts}회로 상한({MAX_RESTARTS_PER_DAY}) 도달, "
                   "사람이 봐야 하는 문제다",
            should_alert=should_alert,
        )
    return WatchdogDecision(ACTION_RESTART, reason, detail=detail, should_alert=should_alert)


def _restarts_today(state: dict | None, now: datetime) -> int:
    if not state or state.get("date") != now.date().isoformat():
        return 0
    try:
        return int(state.get("restarts", 0))
    except (TypeError, ValueError):
        return 0


def _alert_due(state: dict | None, now: datetime) -> bool:
    if not state:
        return True
    raw = state.get("last_alert_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return now - last >= timedelta(seconds=ALERT_COOLDOWN_SECONDS)


def next_state(state: dict | None, now: datetime, decision: WatchdogDecision) -> dict:
    """판정 결과를 반영한 다음 상태 — 날짜가 바뀌면 카운터를 접는다."""
    today = now.date().isoformat()
    restarts = _restarts_today(state, now)
    if decision.action == ACTION_RESTART:
        restarts += 1
    last_alert = (state or {}).get("last_alert_at")
    if (state or {}).get("date") != today:
        last_alert = None
    if decision.should_alert:
        last_alert = now.isoformat()
    return {"date": today, "restarts": restarts, "last_alert_at": last_alert}
