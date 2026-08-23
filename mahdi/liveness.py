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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from mahdi import market_calendar, session

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

# ===== 2026-08-14 §2-1·§3-3 / Fix#2 — **살아 있는데 아무것도 못 가져오는 상태** =====
#
# 08-14 14:00~15:23, 옵션체인이 **84분 연속으로 한 행도 적재되지 않았다.** 그 84분 동안
# 워치독 판정은 49건 **전부 OK**였고 재기동 0회였다 — 박동이 30초마다 정확히 찍혔기 때문이다.
# 감시 대상이 「이벤트 루프가 도는가」 하나로 정의돼 있어서 **그 84분은 정의상 정상**이었다.
# 당일 로그에 `slack`·`경보` 문구는 0건이고, ERROR 86건은 파일에만 남았다.
#
# 08-06이 「프로세스가 죽은 것을 아무도 몰랐다」였다면 08-14는 **「프로세스가 살아서 아무것도
# 못 가져오는 것을 아무도 몰랐다」**이다. 같은 파일의 다른 결함이고, 고치는 자리도 같다.
#
# ## 재기동하지 않는다
#
# 08-14의 원인은 우리 쪽이 아니었다 — KIS `inquire-price`의 p50이 4.05초로 전역 read
# timeout(4.0초)을 넘어섰고(§2-2), 그 순간 20레그 순차 수집의 기대 성공 수가 0에 수렴했다.
# 우리 REST 수요는 오히려 전일의 80%였다. **재기동은 아무것도 안 고치고 관측만 12~14초
# 끊는다**(08-06 실측). 그래서 이 판정은 **알림 전용**이다 — 조치는 사람이 정한다
# (`DECISION_LOG` 결정 7: 레버 발동은 사람이 한다).
INGEST_STALE_MINUTES = 10

# 적재 감시 창 — **정규장 안쪽으로 좁게** 잡는다.
#
# 09:00 이전: 장전에도 사이클은 돌지만(08-14 장전 62분 연속 관측) 기동 직후에는 마스터 파일
#            다운로드·토큰·WS 구독 32건이 앞에 있어 적재가 비는 분이 정상적으로 존재한다.
# 15:30 이후: 15:30 단일가 종료와 15:45 종료 배치 사이는 폴링이 자연스럽게 잦아드는 구간이다.
#            여기에 임계를 걸면 **매일** 오경보가 난다 — 규약 D의 "오경보 비용이 미탐지 비용보다
#            훨씬 싸지 않다"가 그대로 적용된다. 무시되기 시작한 배지는 없는 배지와 같다.
#
# 08-14의 84분은 14:00에 시작했으므로 이 창으로 잡힌다(14:10에 첫 `degraded`).
#
# ===== 2026-08-23(08-21 §1-13 / §4 Fix#1) — 끝을 15:15에서 **15:30**으로 넓혔다 =====
#
# ## 무엇이 안 보였나
#
# 08-21의 당일 최장 빈손은 **26분(15:05~15:30)** 이고 임계 20분을 그날 처음 넘겼다.
# 그런데 종전 창(15:15)에서는 **그 절반을 아무도 안 보고 있었다** — 15:20:01과 15:30:02의
# 판정이 `OK — 정상`으로 인쇄됐고, 그때 프로그램은 26분째 빈손이었다.
#
# ## 왜 15:30인가 — 08-20 지시(15:20)로는 부족하다
#
# 08-20 §4 Fix#1은 `session.CLOSING_AUCTION_START`를 쓰라고 적었는데 그 값은 **15:35**
# (파생시장 종가 단일가)이고, 그 지시를 「현물 마감 15:20」으로 옮겨 적은 판본도 있었다.
# **둘 다 틀렸다**: 15:35는 너무 넓어 매일 오경보를 부르고, 15:20은 08-21 구멍의 **10분을
# 그대로 남긴다.** 답은 **유가증권시장 장 마감 동시호가의 끝**이고 그 값이 15:30이다
# (`session.EQUITY_CLOSING_AUCTION_END` — 시작 15:20과 한자리에 두었다).
#
# ## 대가는 숫자로 걸었다
#
# 창을 넓히면 그 15분에서 오경보가 늘 수 있다. `hypotheses.yaml`의
# `2026-08-21-fix1-ingest-watch-covers-closing-auction`이 대가를 **DEGRADED 하루 24건 이하**
# (08-21 실측 16건의 1.5배)로 못박아 뒀다 — 넘으면 넓힌 것이 손해다.
INGEST_WATCH_START = dtime(9, 0)
INGEST_WATCH_END = session.EQUITY_CLOSING_AUCTION_END

ACTION_OK = "ok"
ACTION_IDLE = "idle"  # 감시 창 밖이거나 판정을 보류할 이유가 있다 — 아무것도 하지 않는다
ACTION_RESTART = "restart"
ACTION_ALERT_ONLY = "alert_only"  # 이상은 맞는데 재기동 상한을 썼다
ACTION_DEGRADED = "degraded"  # 살아 있는데 적재가 없다 — 알림만, 재기동은 하지 않는다

REASON_MISSING = "missing"
REASON_STALE = "stale"
REASON_NO_INGEST = "no_ingest"
REASON_INTENTIONAL_STOP = "intentional_stop"  # 사람이 일부러 껐다 — 되살리지 않는다
REASON_HOLIDAY = "holiday"  # 등재된 휴장일 — 오늘은 감시할 것이 없다


@dataclass(frozen=True, slots=True)
class WatchdogDecision:
    action: str
    reason: str | None = None
    detail: str = ""
    should_alert: bool = False


def in_watch_window(now: datetime) -> bool:
    """반환: 지금이 관측 루프가 **떠 있어야 하는** 시각인가.

    2026-08-17 2차 — **주말을 여기서 뺀다.** 종전에는 `now.time()`만 봐서 요일을 몰랐고,
    그 결과 08-15(토) 07:40:02와 08-16(일) 10:14:39에 **워치독이 주말에 시스템 전체를
    부팅했다**(장전 기동 작업은 월~금 트리거라 안 떴는데 워치독이 대신 띄웠다). 두 날 모두
    재기동 상한을 채우고 `ALERT_ONLY`를 94줄·113줄 쏟았다.

    주말 판정은 `market_calendar.is_weekend()`가 한다 — **계산이라 파일이 필요 없으므로**
    이 순수 함수 안에 둘 수 있다. 휴장일은 사람이 확인한 파일이 있어야 알 수 있어서 여기가
    아니라 `decide(holiday=...)`로 주입한다(`starting`/`stopped_at`과 같은 패턴).
    """
    if market_calendar.is_weekend(now):
        return False
    return WATCH_WINDOW_START <= now.time() <= WATCH_WINDOW_END


def in_ingest_window(now: datetime) -> bool:
    """반환: 지금이 **적재가 있어야 하는** 시간대인가(`INGEST_WATCH_*` 주석 참고).

    감시 창(`in_watch_window`)보다 좁다 — 워치독은 07:40~15:45에 판정하지만 「적재 0분」을
    이상으로 볼 수 있는 것은 정규장 안쪽뿐이다.
    """
    return INGEST_WATCH_START <= now.time() <= INGEST_WATCH_END


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


# ===== 2026-08-17 — **의도적 정지와 죽음은 다른 사건이다** =====
#
# 08-17 15:00:01 / 15:06:01 / 15:28:02, 워치독이 관측 루프를 세 번 되살렸다. 셋 다 정상이었다 —
# 판정도 조치도 설계대로였다. **틀린 것은 입력이었다.** 그날 세 번의 정지는 전부 사람이 코드를
# 고치려고 창을 닫은 것이었는데, `decide()`가 보는 입력에는 「사람이 껐다」를 표현할 자리가
# 아예 없었다. 감시 창 · 기동 표식 · 하트비트 나이 · 적재 분 수 — 이 넷 어디에도 의도가 없으므로
# **의도적 정지와 죽음은 이 함수 안에서 정의상 같은 사건**이었다.
#
# 대가는 재기동 3회로 끝나지 않았다:
#   - 하루 재기동 상한(`MAX_RESTARTS_PER_DAY`)이 15:28에 전부 소진됐다. 그때부터 15:45까지
#     **진짜 사고가 났다면 워치독은 알림만 내고 손을 뗐을 것이다.**
#   - CRITICAL 알림 2건이 오발됐다(15:00:01, 15:06은 쿨다운에 눌림, 15:28:02).
#   - 정규장 시간대 옵션체인 결손 약 12분(14:56~15:01, 15:03~15:07, 15:25~15:29).
#
# ## `.last_marketclose_stop.txt`로는 왜 안 되는가
#
# 그 파일은 이미 있었다. 그런데 쓰는 곳은 `scripts/log_marketclose_stop.py` 하나이고 읽는 곳은
# `docs/동작점검/tools/collect_evidence.py`(사후 증거 다이제스트)뿐이다 — **판정 경로가 그 파일을
# 한 번도 안 본다.** 게다가 그것은 「마지막으로 정상 종료한 시각」이라 *지금 정지 중인가*에 답하지
# 못한다. 답해야 하는 질문이 다르므로 파일도 다르다.
#
# ## 정식 15:45 종료가 지금까지 무사했던 것은 설계가 아니라 시각 우연이다
#
# `WATCH_WINDOW_END`(15:45)와 종료 배치(15:45)가 정확히 겹쳐서 재기동이 안 걸렸을 뿐이다.
# 07:40~15:45 사이라면 **정식 종료 스크립트로 꺼도** 3~4분 뒤 되살아난다(정지 임계 180초 +
# 워치독 1분 주기 = 08-17 실측 3분10초~3분37초).
_INTENTIONAL_STOP_FILENAME = ".intentional_stop"


def intentional_stop_path(log_dir: Path) -> Path:
    return log_dir / _INTENTIONAL_STOP_FILENAME


def write_intentional_stop(path: Path, now: datetime) -> None:
    """정지 의도를 남긴다(`mahdi/main.py`의 Ctrl+C 경로용 — 배치 스크립트는 `echo`로 쓴다).

    내용은 **사람이 로그를 읽을 때를 위한 것**이고 판정은 mtime만 본다(`startup_in_progress`와
    같은 이유 — cmd.exe가 쓴 날짜 문자열은 로케일을 탄다).

    실패 조건: 어떤 예외도 밖으로 내지 않는다. 표식을 못 썼다고 종료가 막히면 안 된다 — 못 쓰면
              워치독이 종전처럼 되살릴 뿐이고, 그것은 **안전한 쪽의 실패**다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"intentional stop at {now.isoformat()}\n", encoding="utf-8")
    except Exception:
        logger.warning("정지 표식 기록 실패: %s", path, exc_info=True)


def intentional_stop_at(path: Path, now: datetime, beat: dict | None = None) -> datetime | None:
    """반환: **아직 유효한** 정지 표식의 시각. 표식이 없거나 이미 소비됐으면 None.

    세 조건을 **모두** 만족해야 유효하다:

    1. 파일이 있다.
    2. 표식의 **날짜가 오늘**이다 — 밤사이 PC가 꺼져 아침 기동 스크립트가 못 돈 날, 어제
       15:45의 표식이 오늘 07:40 감시 창을 침묵시키는 것을 막는다. 기동 스크립트의 `del`이
       주 만료 경로이고 이것은 그 경로가 실패했을 때의 두 번째 그물이다.
    3. 표식보다 **나중의 박동이 없다.** 표식이 남은 채로 사람이 별도 터미널에서 루프를 띄우면
       (2026-07-21에 실제로 있었던 일 — 창 제목 규약을 안 타는 경로라 기동 스크립트의 `del`을
       거치지 않는다) 박동이 표식보다 새로워지고, 그 순간 표식은 **자동으로 무효**가 된다.
       시간 상수를 하나도 안 늘리고 "이미 소비된 의도"를 걸러내는 자리다.

    해석: 이 판정이 True인 동안 워치독은 그날 남은 시간의 감시를 **실제로 하지 않는다.** 그것이
         「사람이 껐다」의 문자 그대로의 의미이고, 그래서 워치독이 그 구간을 10분에 한 줄
         기록하고 COCKPIT이 초록 대신 경고를 낸다(`ACTION_IDLE`을 감시 창 안에서 받은 배지).

    실패 조건: 못 읽으면 **None(= 감시를 계속한다)** 이다. `_no_ingest_detail`이 「모른다」를
              정상으로 접는 것과 방향이 반대인데, 이유가 다르다 — 거기서 접지 않으면 DB가 죽은 날
              `degraded` 폭주가 나고, 여기서는 **감시자가 의심스러울 때 감시하는 쪽으로 넘어져야**
              한다. 잘못 감시하면 재기동 1회지만, 잘못 침묵하면 08-06의 19분이 돌아온다.
    """
    try:
        stamped = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
    if stamped.date() != now.date():
        return None
    if beat is not None and beat["at"] > stamped:
        return None
    return stamped


# ===== 2026-08-17 2차 — **창 닫기 버튼은 Ctrl+C가 아니다** =====
#
# 08-17의 세 번의 정지는 전부 창 닫기였다. 근거: 그날 로그에 `Ctrl+C로 종료합니다`가 0건인데
# 프로세스는 정상 사이클 로그 한복판에서 끊겼다 — KeyboardInterrupt 경로를 못 탔다는 뜻이다.
#
# Windows는 콘솔 창이 닫힐 때 `CTRL_CLOSE_EVENT`를 보내는데 **파이썬은 이것을 기본으로 잡지
# 않는다.** `signal` 모듈이 다루는 것은 Ctrl+C(`CTRL_C_EVENT`)와 Ctrl+Break뿐이고, 닫기는
# 예외도 `atexit`도 `finally`도 태우지 않은 채 프로세스를 끝낸다. 그래서 표식을 쓸 자리가
# 파이썬 코드 안에 존재하지 않았다 — `SetConsoleCtrlHandler`로 직접 만드는 수밖에 없다.
#
# ## 핸들러는 **다른 스레드**에서 불린다
#
# OS가 전용 스레드를 만들어 호출한다. asyncio 루프도 우리 코드의 어떤 락도 그 스레드에서는
# 기댈 수 없다 — 그래서 이 경로가 하는 일은 **파일 두 개를 쓰는 동기 I/O뿐**이다. 로깅은
# 락을 잡으므로 파일을 다 쓴 **뒤에** 최선 노력으로만 한다(막혀도 표식은 이미 남아 있다).
#
# ## 예산은 약 5초다
#
# `CTRL_CLOSE_EVENT`에서 핸들러가 돌아오면(또는 시간이 다하면) 시스템이 프로세스를 끝낸다.
# 파일 두 개 쓰기는 마이크로초 단위라 여유가 크지만, **여기에 다른 일을 더하지 말 것** —
# 이 예산은 우리 것이 아니라 OS가 빌려준 것이다.
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

# OS가 콜백을 부를 때까지 살아 있어야 하는 참조.
#
# **모듈 전역으로 붙들지 않으면 GC가 가져간다.** ctypes 콜백 객체는 파이썬 쪽 참조가 사라지는
# 순간 해제되는데 OS의 핸들러 테이블에는 그 주소가 그대로 남는다 — 창을 닫는 순간 OS가
# **해제된 메모리를 호출한다.** 재현이 어렵고 로그도 안 남는 종류의 사고다.
_console_handler_ref = None


def is_intentional_console_stop(event: int) -> bool:
    """반환: 이 콘솔 제어 이벤트를 「사람이 일부러 껐다」로 볼 것인가.

    **`CTRL_CLOSE_EVENT` 하나뿐이다.** 나머지를 왜 빼는지가 이 함수의 내용이다:

    - `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` — 파이썬 기본 핸들러가 `KeyboardInterrupt`를 내야
      하고, 그 경로는 `mahdi/main.py`가 이미 덮는다. 여기서 True를 내고 핸들러가 TRUE를
      반환하면 **핸들러 체인이 끊겨 KeyboardInterrupt 자체가 사라진다** — 표식 하나 얻자고
      정상 종료 경로를 부수는 셈이다.
    - `CTRL_LOGOFF_EVENT` / `CTRL_SHUTDOWN_EVENT` — **PC 재부팅은 정지 의도가 아니다.**
      장전 기동은 07:30 주간 트리거라 낮에 재부팅해도 다시 돌지 않는다. 그날 시스템을 되살릴
      수 있는 유일한 주체가 워치독인데, 여기에 표식을 남기면 **재부팅 한 번이 그날 감시를
      통째로 끈다.** 창 하나를 닫는 것과 PC를 내리는 것은 의도의 범위가 다르다.

    `taskkill /F`(워치독의 재기동 경로)는 애초에 어떤 이벤트도 보내지 않는다 —
    `TerminateProcess`라 핸들러가 불리지 않는다. 자동 재기동이 표식을 남길 길은 없다.
    """
    return event == CTRL_CLOSE_EVENT


def install_console_stop_handler(on_stop: Callable[[], None]) -> bool:
    """창 닫기를 잡아 `on_stop()`을 부르도록 OS에 핸들러를 건다. 반환: 실제로 걸었는가.

    입력: 인자 없는 콜백. **다른 스레드에서, 약 5초 예산 안에서** 불린다는 전제로 쓸 것 —
         파일 쓰기 같은 짧은 동기 작업만 넣는다.
    계산: Windows에서만 `kernel32.SetConsoleCtrlHandler`를 건다. 다른 OS에서는 아무것도 하지
         않고 False를 낸다(이 프로젝트는 Windows에서 돌지만, 테스트가 다른 곳에서 돌 수 있다).
    해석: False는 「못 걸었다」이고 그것은 **치명적이지 않다** — 표식이 없으면 워치독이 종전처럼
         되살릴 뿐이다. 안전한 쪽의 실패다.
    실패 조건: 어떤 예외도 밖으로 내지 않는다. 이 배선을 못 했다고 관측 루프가 안 뜨면 본말이
              뒤집힌다 — 08-06 이후 이 파일이 일관되게 지켜온 규칙이다.
    """
    global _console_handler_ref
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        def _handler(event: int) -> bool:
            if not is_intentional_console_stop(event):
                # **FALSE를 반환해 다음 핸들러에게 넘긴다** — Ctrl+C가 여기서 멈추면 안 된다.
                return False
            try:
                on_stop()
            except Exception:  # noqa: BLE001 — 이 스레드에서 예외를 내면 아무도 못 본다
                pass
            try:
                logger.info("콘솔 창이 닫혔다 — 정지 표식을 남기고 종료한다(워치독 보류).")
            except Exception:  # noqa: BLE001 — 로깅 락은 남의 스레드가 쥐고 있을 수 있다
                pass
            # TRUE = 우리가 처리했다. 어느 쪽을 반환하든 시스템은 곧 프로세스를 끝내지만,
            # TRUE로 체인을 끊어 **다른 핸들러가 이 종료에 끼어들 여지를 없앤다.**
            return True

        prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        handler = prototype(_handler)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.SetConsoleCtrlHandler(handler, True):
            logger.warning(
                "콘솔 종료 핸들러 등록 실패(errno=%s) — 창을 닫으면 워치독이 되살린다",
                ctypes.get_last_error(),
            )
            return False
        _console_handler_ref = handler  # GC가 가져가면 OS가 죽은 콜백을 부른다
        return True
    except Exception:  # noqa: BLE001
        logger.warning("콘솔 종료 핸들러 등록 중 예외 — 표식 없이 진행한다", exc_info=True)
        return False


def decide(
    beat: dict | None,
    now: datetime,
    state: dict | None = None,
    *,
    stale_seconds: float = HEARTBEAT_STALE_SECONDS,
    starting: bool = False,
    ingest_minutes_recent: int | None = None,
    stopped_at: datetime | None = None,
    holiday: str | None = None,
) -> WatchdogDecision:
    """
    입력: 마지막 박동(`read_heartbeat()` 결과), 현재 시각, 워치독 상태
         (`{"date": "YYYY-MM-DD", "restarts": int, "last_alert_at": iso}`),
         (선택) 직전 `INGEST_STALE_MINUTES`분 동안 옵션체인이 **적재된 분 수**,
         (선택) 유효한 정지 표식의 시각(`intentional_stop_at()` 결과).
    계산: 감시 창 안에서 박동이 **없거나 늙었으면** 재기동을 지시한다. 하루 상한을 넘겼으면
         알림만 낸다. 박동이 정상이어도 정규장 안에서 **적재가 0분이면** `ACTION_DEGRADED`를
         낸다(2026-08-14 Fix#2). 알림은 `ALERT_COOLDOWN_SECONDS`로 눌러 같은 말을 반복하지 않는다.
    해석: **"없음"과 "늙음"을 구분해서 사유로 남긴다.** 없음은 *아예 안 떴다*(08-06 10:20의
         중단된 기동이 그 예다), 늙음은 *떴다가 죽었다*(10:04)이다. 조치는 같지만 다음날
         원인을 찾을 때 이 구분이 출발점이 된다.
         세 번째 사유 `no_ingest`는 *살아 있는데 못 가져온다*(08-14 14:00~15:23)이고,
         **조치가 다르다** — 재기동하지 않는다.
         상태의 `date`가 오늘이 아니면 재기동 카운터를 0으로 본다 — 프로세스가 하루 단위라
         상한도 하루 단위다.
    실패 조건: 없다(순수 함수). 시각 판단만 하고 파일도 프로세스도 건드리지 않는다.
              `ingest_minutes_recent=None`은 **「모른다」**이고 종전과 완전히 같은 판정을 낸다.
              `stopped_at=None`(기본값)도 마찬가지다 — 이 인자를 안 주면 08-17 이전과 판정이
              한 글자도 달라지지 않는다.
    """
    if not in_watch_window(now):
        # 주말은 시각과 무관하게 창 밖이다(2026-08-17 2차) — 사유를 갈라 두지 않으면 다음날
        # 로그를 읽는 사람이 "토요일 09시인데 왜 창 밖이지"에서 멈춘다.
        detail = "주말 — 감시 창 밖" if market_calendar.is_weekend(now) else "감시 창 밖"
        return WatchdogDecision(ACTION_IDLE, detail=detail)
    # 2026-08-17 2차 — 등재된 휴장일. **`starting`보다 앞이다**: 휴장일에 기동 스크립트가
    # 도는 것 자체가 이상 신호이고, 그때 「기동 진행 중」으로 덮어버리면 그 사실이 로그에서
    # 사라진다. 주말과 달리 이것은 **사람이 확인해 적은 사실**이므로 이름을 그대로 인쇄한다 —
    # 달력이 틀렸을 때 그 줄이 오답을 드러내는 유일한 자리다.
    if holiday:
        return WatchdogDecision(
            ACTION_IDLE, REASON_HOLIDAY,
            detail=f"휴장일로 등재됨({holiday}) — 오늘은 관측하지 않는다",
        )
    if starting:
        return WatchdogDecision(ACTION_IDLE, detail="기동 진행 중 — 판정 보류")
    # 2026-08-17 — 사람이 일부러 껐다. **`starting` 바로 다음, 하트비트보다 앞**이 자리다:
    # 정지 표식이 유효하다는 것은 이미 "박동이 없거나 늙은 것이 정상"이라는 뜻이므로, 그 아래
    # 판정을 돌리면 무엇이 나오든 오답이다. 두 표식이 겹칠 때 `starting`이 이기는 이유는 그쪽이
    # **더 짧게 살고 더 구체적**이기 때문이다(기동 스크립트는 시작하면서 정지 표식을 지운다 —
    # 그 사이 몇 밀리초 동안만 둘이 공존한다).
    if stopped_at is not None:
        return WatchdogDecision(
            ACTION_IDLE, REASON_INTENTIONAL_STOP,
            detail=f"의도적 정지 표식({stopped_at:%H:%M:%S}) — 판정 보류",
        )

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
        # 박동은 정상이다. 여기서 **한 겹 더 본다** — 08-14의 84분이 이 자리에서 OK로 닫혔다.
        no_ingest = _no_ingest_detail(ingest_minutes_recent, now)
        if no_ingest is not None:
            return WatchdogDecision(
                ACTION_DEGRADED, REASON_NO_INGEST,
                detail=f"{no_ingest}(마지막 박동 {age:.0f}초 전 — 프로세스는 살아 있다)",
                should_alert=_alert_due(state, now),
            )
        # ===== 2026-08-23(08-21 §1-13 / §4 Fix#1) — 창 밖의 「정상」은 **모른다**는 뜻이다 =====
        #
        # 적재 감시창 밖에서는 `ingest_minutes_recent`가 무엇이든 `_no_ingest_detail()`이 None을
        # 낸다. 즉 이 자리의 판정은 **박동만 보고 한 말**인데, 종전 문구 「정상」은 적재까지
        # 괜찮다는 인상을 준다. 08-21 15:20·15:30이 정확히 그랬다 — 그 두 분에 프로그램은
        # 26분째 빈손이었고 화면은 초록이었다.
        #
        # **`ACTION_IDLE`로 바꾸지 않는다.** `scripts/watchdog_observation_loop.py`가 IDLE을
        # 「기록 안 함」으로 처리하므로, 바꾸면 그 구간이 로그에서 통째로 사라진다 —
        # 「모른다」를 「없다」로 바꾸는 것은 지금보다 나쁘다. 바꾸는 것은 **문구뿐**이다.
        #
        # 창 **안**에서 적재가 정상이면 종전 문구를 그대로 쓴다(회귀 없음).
        if not in_ingest_window(now):
            return WatchdogDecision(
                ACTION_OK,
                detail=f"박동 정상 · 적재 감시 창 밖(적재 상태 모름) — 마지막 박동 {age:.0f}초 전",
            )
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


def _no_ingest_detail(
    ingest_minutes_recent: int | None,
    now: datetime,
    window_minutes: int = INGEST_STALE_MINUTES,
) -> str | None:
    """반환: 적재가 끊겼다고 볼 만하면 사유 문자열, 아니면 None.

    **`None` 입력은 「정상」이 아니라 「모른다」다.** DB를 못 읽은 워치독은 종전과 똑같이
    판정해야 한다 — 여기서 `None`을 0으로 접으면 **DB가 죽은 날 워치독이 매분 `degraded`를
    외치고**, 그 순간 이 배지는 아무도 안 보는 배지가 된다. 2026-08-12 Fix#1이 남긴 교훈이
    정확히 이것이다: 감시자를 감시 대상에 묶지 마라.
    """
    if ingest_minutes_recent is None:
        return None
    if not in_ingest_window(now):
        return None
    if ingest_minutes_recent > 0:
        return None
    return (
        f"직전 {window_minutes}분 동안 옵션체인 적재가 **0분**이다 — 살아 있지만 아무것도 "
        "가져오지 못하고 있다. 자동 재기동은 하지 않는다(원인이 우리 쪽이 아닐 수 있다)"
    )


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


# ===== 2026-08-23 (08-21 §1-11 · §1-13 / §4 Fix#1·#4) — DEGRADED **사건**의 시작과 끝 =====
#
# ## 무엇이 없었나
#
# 08-21에 워치독은 24분 동안 「수집이 멈췄다」를 **14번 외쳤고**, 회복하자 그냥 `OK — 정상`으로
# 돌아갔다. 로그에는 **사건의 시작도 끝도 없다** — 같은 문구 14줄이 있을 뿐이다. 그래서
#
#   · 「몇 분째인가」를 사람이 줄을 세어 구해야 했고(13:39~14:02),
#   · 사후에 사건 수를 세려면 비-OK **16행을 손으로 묶어야** 했다(세 구간: 13:39~14:02 · 14:44 · 15:14).
#
# 필요한 것은 알림 채널이 아니라 **로그에 남는 두 문장**이다: 진행 중인 줄의 「연속 N분째」와
# 끝나는 자리의 종료 줄. ⛔ Slack은 제안하지 않는다(`NEXT_TODO.md` 2026-08-01 보류 확정).
#
# ## 왜 상태를 따로 든는가 — `.watchdog_state.json`에 못 얹는다
#
# 워치독은 1분마다 **새 프로세스**로 뜨므로(작업 스케줄러) 직전 판정을 기억하려면 파일이
# 필요하다. 그런데 `next_state()`는 **매번 새 dict를 만들어 세 키만 남긴다** — 거기 얹은 값은
# 조용히 사라지고, 그러면 이 카운터가 영원히 1에서 멈춘다. 08-19의 `_MISSING_CHECK_STATE`가
# 별도 파일이 된 것과 **정확히 같은 이유**이고, 그 주석이 이 함정을 이미 적어 뒀다.
#
# ## 무기록 구간을 회복으로 읽지 않는다
#
# 워치독 자신이 멈춘 사이(08-12에 5시간 31분 있었다) 적재가 회복됐는지 우리는 **모른다**.
# 마지막 기록에서 이 값보다 오래 떨어져 있으면 「회복」이라고 말하지 않고, 몇 분을 못 봤는지를
# 적어 닫는다 — 규약 C(「없었다」와 「셀 수 없었다」는 다르다)를 종료 줄에도 적용한다.
DEGRADED_EPISODE_GAP_MINUTES = 5.0

_DEGRADED_EPISODE_FILENAME = ".watchdog_degraded_episode.json"


def degraded_episode_path(log_dir: Path) -> Path:
    return log_dir / _DEGRADED_EPISODE_FILENAME


def _live_episode(state: dict | None, now: datetime) -> dict | None:
    """반환: **이어서 셀 수 있는** 에피소드. 날짜가 다르거나 기록이 끊겼으면 None."""
    if not state or state.get("date") != now.date().isoformat():
        return None
    try:
        last = datetime.fromisoformat(str(state["last_at"]))
        minutes = int(state["minutes"])
        since = datetime.fromisoformat(str(state["since"]))
    except (KeyError, TypeError, ValueError):
        return None
    if minutes < 1 or now < last:
        return None
    return {"since": since, "last_at": last, "minutes": minutes}


def track_degraded_episode(
    state: dict | None, now: datetime, action: str,
) -> tuple[dict | None, str | None, str | None]:
    """
    입력: 직전 에피소드 상태(파일에서 읽은 dict, 없으면 None), 현재 시각, 이번 판정의 `action`.
    계산: DEGRADED가 이어지는 동안 분을 세고, 다른 판정으로 넘어가는 **그 한 번**에 종료 문구를 만든다.
    반환: `(다음 상태, 진행 중 문구, 종료 문구)`.
         - 다음 상태가 `None`이면 호출측은 파일을 지운다(에피소드가 닫혔다).
         - 진행 중 문구는 DEGRADED 판정 detail 뒤에 붙는다(`연속 3분째(13:39부터)`).
         - 종료 문구는 **한 번만** 나온다 — 이미 닫힌 뒤의 OK에는 `None`이다.
    해석: 순수 함수다(파일도 시계도 안 건드린다) — 그래야 `tests/test_liveness.py`가
         「14분 이어지고 15분째에 한 줄」을 시각 시퀀스로 검사할 수 있다.
    실패 조건: 없다. 상태가 깨져 있으면 새 에피소드로 시작한다(못 읽은 과거를 지어내지 않는다).
    """
    live = _live_episode(state, now)
    stale = live is not None and (now - live["last_at"]) > timedelta(
        minutes=DEGRADED_EPISODE_GAP_MINUTES
    )

    if action == ACTION_DEGRADED:
        if live is None or stale:
            nxt = {
                "date": now.date().isoformat(),
                "since": now.isoformat(timespec="seconds"),
                "last_at": now.isoformat(timespec="seconds"),
                "minutes": 1,
            }
        else:
            nxt = {
                "date": now.date().isoformat(),
                "since": live["since"].isoformat(timespec="seconds"),
                "last_at": now.isoformat(timespec="seconds"),
                "minutes": live["minutes"] + 1,
            }
        note = f"연속 {nxt['minutes']}분째({live['since'] if live and not stale else now:%H:%M}부터)"
        # 무기록 뒤에 다시 DEGRADED면 **이어 세지 않고 새로 센다.** 그 사이를 못 봤기 때문이다.
        if stale:
            gap = int((now - live["last_at"]).total_seconds() // 60)
            return nxt, f"{note} · 직전 에피소드({live['since']:%H:%M}~{live['last_at']:%H:%M}, "\
                        f"{live['minutes']}분)와 사이에 **{gap}분 무기록**", None
        return nxt, note, None

    if live is None:
        return None, None, None

    span = f"{live['since']:%H:%M}~{live['last_at']:%H:%M}"
    if stale:
        gap = int((now - live["last_at"]).total_seconds() // 60)
        return None, None, (
            f"적재 정지 {live['minutes']}분 지속({span}) — 그 뒤 **{gap}분 무기록**이라 "
            "언제 회복했는지 모른다(회복이 아니라 관측이 끊긴 것이다)"
        )
    if action == ACTION_OK:
        return None, None, f"적재 정지 {live['minutes']}분 지속 후 회복({span})"
    # 회복이 아니다 — 박동 이상·의도적 정지·감시 창 종료 중 하나로 사건이 끝났다.
    # **「회복」이라고 쓰지 않는 것이 요점이다**: 적재가 돌아온 것이 아니라 관측이 바뀐 것이다.
    return None, None, (
        f"적재 정지 {live['minutes']}분 지속({span}) — 회복이 아니라 판정이 "
        f"`{action.upper()}`로 바뀌며 끝났다"
    )
