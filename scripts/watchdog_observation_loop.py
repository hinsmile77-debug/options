"""관측 루프 워치독 — 1분마다 생존 신호를 보고, 죽어 있으면 알리고 되살린다.

2026-08-06 §2-1 / Fix#2. 그날 10:04~10:23 관측 루프와 COCKPIT이 흔적 없이 죽어 있었고
**19분 동안 아무도 몰랐다**(사람이 화면을 보고 10:20에 알아챘다). 상세 근거는 `mahdi/liveness.py`.

실행: `python scripts/watchdog_observation_loop.py` (인자 없음)
      Windows 작업 스케줄러가 **1분 주기**로 `scripts/watchdog_mahdi.bat`을 통해 호출한다.

## 이 스크립트가 얇은 이유

판정(`liveness.decide()`)은 전부 `mahdi/liveness.py`에 있고 여기서는 파일 I/O와 프로세스
기동만 한다 — `docs/동작점검/README.md`의 규약이다("로직은 파이썬 모듈에, `scripts/`는 얇게").
그래야 "언제 재기동해야 하는가"를 pytest가 검사할 수 있다.

## 재기동 수단으로 장전 기동 스크립트를 그대로 쓰는 이유

`start_mahdi_premarket.bat`은 이미 (1) 잔존 프로세스 정리, (2) Docker 확인, (3) 마이그레이션
재적용, (4) COCKPIT + 관측 루프 기동을 **멱등하게** 한다. 워치독이 자기만의 기동 경로를 따로
만들면 그 경로는 하루 한 번도 안 쓰이다가 정작 필요한 날 처음 실행된다 — 그때 처음 깨진다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi import liveness, market_calendar, notify
from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db

LOG_DIR = PROJECT_ROOT / "logs"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
STATE_FILE = LOG_DIR / ".watchdog_state.json"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_mahdi_premarket.bat"

# 기동 스크립트는 Docker 데몬을 최대 180초까지 기다린다 — 그보다 넉넉히 잡되 무한은 아니다.
#
# **이 값을 줄이지 말 것.** 2026-08-12 보고서 초안이 "실측 10초니까 120초로 줄이자"고 적었는데
# 그것은 틀렸다 — Docker 대기 180초는 `cmd /c bat`의 **안**에서 흐르므로 이 타임아웃에 그대로
# 포함된다. 120초로 줄이면 Docker가 느린 아침마다 기동을 마이그레이션 중간에 죽인다.
_RESTART_TIMEOUT_SECONDS = 300

# 적재 조회의 상한 (2026-08-14 Fix#2).
#
# **2초를 넘기지 않는다.** 워치독은 1분 주기이고, DB가 굳은 날 여기서 오래 매달리면 08-12에
# 겪은 무력화가 그대로 재현된다(그때는 상속된 파이프였고 이번엔 DB다 — 원인은 달라도 결과는
# 같다: 작업 스케줄러의 `MultipleInstances=IgnoreNew`가 그 사이 매분 실행을 전부 버린다).
#
# connect와 statement 양쪽에 건다 — 붙는 데서 막히는 것과 쿼리에서 막히는 것은 다른 경로다.
_INGEST_QUERY_TIMEOUT_SECONDS = 2


def _log(line: str) -> None:
    """워치독 자신의 로그는 관측 루프 로그와 **분리한다** — 관측 루프가 죽은 구간의 기록이므로
    같은 파일에 쓰면 "그 시간대엔 아무 로그도 없다"는 사실 자체가 흐려진다."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with WATCHDOG_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_state(state: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _restart() -> tuple[bool, str]:
    """기동 스크립트를 실행한다. 반환: (성공 여부, 요약).

    ## 표준 출력을 **캡처하지 않는다** (2026-08-12 §2-3 / Fix#1)

    종전에는 `capture_output=True`였다. 그 한 인자가 08-12에 워치독을 **5시간 31분** 세웠다.

    기동 스크립트는 `start "..." cmd /k ...`로 COCKPIT과 관측 루프를 새 창에 띄운다. 캡처를
    켜면 파이썬이 파이프를 만들어 자식(`cmd /c bat`)에 물리는데, 그 핸들이 **손자(새 창의
    프로세스)에게까지 상속된다.** bat이 끝나도 손자가 파이프 쓰기 끝을 쥐고 있으므로 부모는
    EOF를 못 받는다. 더 나쁜 것은 `timeout`조차 상한이 못 된다는 점이다 — `subprocess.run`은
    `TimeoutExpired` 처리에서 자식을 죽인 뒤 `communicate()`를 **다시** 부르고, 그것이 같은
    파이프에서 또 막힌다.

    08-12 실측(같은 PC에서 재현):

        capture_output=True   timeout=8 인데 **350.1초** 만에 반환
        capture_output=False  **0.1초**

    운영에서는 관측 루프가 15:45 종료될 때까지 파이프가 안 닫혀, 워치독이 10:14:01부터
    15:45:02까지 매달려 있었다. 작업 스케줄러가 `MultipleInstances=IgnoreNew`라 그동안 매분
    실행이 전부 무시됐다 — **재기동에 성공한 그 순간부터 장 마감까지 감시가 없었다.**

    ## 캡처를 없애도 잃는 것이 없다

    기동 스크립트는 자기 출력을 전부 `logs/premarket_startup.log`에 적는다. 08-12에 「300초
    안에 끝나지 않음」이 오보임을 증명한 것도 그 로그였다(10:14:02 시작 → 10:14:12 종료).
    워치독이 그 출력을 읽은 적은 한 번도 없다 — `result.stdout`은 어디서도 안 쓰인다.

    `DEVNULL`을 쓰는 이유(상속을 그냥 두지 않고): 상속하면 워치독을 띄운 숨김 콘솔로 bat의
    출력이 흘러들어 간다. 지금은 아무도 안 보지만, 언제 무엇이 그 핸들을 붙들지는 우리가
    통제하지 못한다 — **이 함수가 다시는 남의 수명에 묶이지 않게** 명시적으로 끊는다.
    """
    if not START_SCRIPT.exists():
        return False, f"기동 스크립트를 찾지 못함: {START_SCRIPT}"
    try:
        result = subprocess.run(
            ["cmd", "/c", str(START_SCRIPT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_RESTART_TIMEOUT_SECONDS,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return False, f"기동 스크립트가 {_RESTART_TIMEOUT_SECONDS}초 안에 끝나지 않음"
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 알림까지는 가야 한다
        return False, f"기동 스크립트 실행 실패: {exc!r}"
    if result.returncode != 0:
        return False, f"기동 스크립트 종료 코드 {result.returncode}"
    return True, "기동 스크립트 실행 완료"


def _recent_ingest_minutes(now: datetime) -> int | None:
    """반환: 직전 `liveness.INGEST_STALE_MINUTES`분 동안 `option_analysis_1m`에 **행이 있던 분 수**.
             못 읽으면 **None("모른다")** — 0이 아니다.

    2026-08-14 Fix#2. 워치독이 「가져오고 있는가」를 보는 유일한 창이다.

    ## 왜 `mahdi.data.db.get_connection()`을 안 쓰는가

    그 헬퍼는 타임아웃 없이 `psycopg.connect(dsn)`을 부른다. 감시자가 감시 대상의 DB에
    무제한으로 매달리는 것이 정확히 규약 D가 금지하는 결합이다 — 여기서만 상한을 걸어 연다.

    ## 왜 `underlying`으로 안 거르는가

    이 함수가 답하는 질문은 「이 시스템이 무엇이든 가져오고 있는가」다. 기초자산을 걸면 그
    상수가 워치독과 수집기 사이에서 조용히 갈라질 수 있고(08-11 `PRIORITY_SERIES_LABEL`이
    그래서 계약 테스트를 얻었다), 그 대가로 얻는 정밀도는 여기서 쓸모가 없다 —
    **어느 북이든 한 행이라도 들어왔으면 수집 경로는 살아 있다.**

    실패 조건: 어떤 예외도 밖으로 내지 않는다. DB가 죽었다고 워치독이 죽으면 안 된다.
    """
    since = now - timedelta(minutes=liveness.INGEST_STALE_MINUTES)
    try:
        import psycopg

        from mahdi.config.settings import get_db_settings

        with psycopg.connect(
            get_db_settings().dsn,
            connect_timeout=_INGEST_QUERY_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={_INGEST_QUERY_TIMEOUT_SECONDS * 1000}",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(DISTINCT date_trunc('minute', timestamp)) "
                    "FROM option_analysis_1m WHERE timestamp >= %s AND timestamp <= %s",
                    (since, now),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — 못 읽은 것은 「모른다」다
        _log(f"[{now:%Y-%m-%d %H:%M:%S}] 적재 조회 실패(판정은 박동 축으로만 한다): {exc!r}")
        return None
    return int(row[0]) if row and row[0] is not None else 0


def main() -> None:
    now = db.local_now()
    beat = liveness.read_heartbeat(liveness.heartbeat_path(LOG_DIR))
    state = _read_state()
    # 2026-08-17 — 사람이 일부러 껐는가. **적재 조회보다 먼저 본다**: 정지 중이라면 DB를 열
    # 이유가 없다(규약 D — 감시자가 매분 감시 대상의 DB에 붙는 결합을 하나라도 덜 만든다).
    stopped_at = liveness.intentional_stop_at(
        liveness.intentional_stop_path(LOG_DIR), now, beat
    )
    # 2026-08-17 2차 — 오늘이 등재된 휴장일인가. 워치독은 1분마다 **새 프로세스**로 뜨므로
    # `@lru_cache`가 매번 비어 달력 수정이 즉시 반영된다(관측 루프는 다음 기동까지 옛 값을
    # 쓴다 — 오늘이 휴장일임을 뒤늦게 알았을 때 먼저 멈춰야 하는 쪽이 워치독이다).
    holiday = market_calendar.holiday_name(now, market_calendar.load_holiday_calendar())
    # 적재 감시 창 밖에서는 **DB를 아예 안 연다** — 밤새 매분 커넥션을 만들 이유가 없고,
    # 창 밖의 「적재 0분」은 이상이 아니라 정상이다(`liveness.INGEST_WATCH_*` 주석).
    ingest = (
        _recent_ingest_minutes(now)
        if stopped_at is None and holiday is None and liveness.in_ingest_window(now)
        else None
    )
    decision = liveness.decide(
        beat, now, state,
        # 기동 스크립트가 도는 중이면 판정을 보류한다 — 안 그러면 기동이 서로를 덮어쓴다.
        starting=liveness.startup_in_progress(liveness.startup_marker_path(LOG_DIR), now),
        ingest_minutes_recent=ingest,
        stopped_at=stopped_at,
        holiday=holiday,
    )

    # 2026-08-12 Fix#8 — **판정했다는 사실 자체를 남긴다.** 로그보다 먼저, 그리고 IDLE에도 쓴다:
    # 08-12에 워치독이 5시간 31분 막혀 있었는데 `watchdog.log`의 마지막 줄이 「RESTART」라
    # 사고 대응 중인 것처럼 보였고, 그 침묵을 아무도 실시간으로 못 봤다. 상세 근거는
    # `liveness.watchdog_check_path` 위 주석. **`_restart()` 앞에 두는 것이 중요하다** —
    # 재기동은 최대 300초가 걸리고, 그 300초는 실제로 "판정하지 않은 시간"이 맞다.
    liveness.write_watchdog_check(
        liveness.watchdog_check_path(LOG_DIR), now,
        action=decision.action, detail=decision.detail,
    )

    stamp = f"[{now:%Y-%m-%d %H:%M:%S}]"
    if decision.action == liveness.ACTION_IDLE:
        # 감시 창 밖의 IDLE은 안 남긴다 — 매분 한 줄이면 하루 1,000줄이다.
        #
        # **의도적 정지는 예외로 남긴다**(2026-08-17). 이 구간은 「감시 창 밖이라 조용한 것」이
        # 아니라 **감시 창 안인데 우리가 감시를 껐다**는 뜻이고, 다음날 로그를 읽는 사람에게
        # 그 둘은 완전히 다른 사실이다. 08-12 Fix#8이 `.watchdog_last_check.json`을 만든 것과
        # 같은 이유이고, 주기도 `OK`와 같이 10분에 한 줄로 맞춘다.
        #
        # **휴장일도 같이 남긴다**(2026-08-17 2차). 달력이 틀려 거래일을 휴장일로 적은 날,
        # 이 줄이 그 오답을 드러내는 유일한 자리다 — 그날은 관측도 알림도 없으므로 다른
        # 신호가 하나도 안 나온다. 감시 창 안에서만 찍히므로 하루 최대 49줄이다.
        if now.minute % 10 == 0 and decision.reason in (
            liveness.REASON_INTENTIONAL_STOP, liveness.REASON_HOLIDAY,
        ):
            _log(f"{stamp} IDLE — {decision.detail}")
        return

    if decision.action == liveness.ACTION_OK:
        # 정상도 기록은 남긴다 — 다음날 "워치독이 돌기는 했는가"를 물을 수 있어야 한다.
        # 다만 10분에 한 번만(1분 주기 x 매번이면 로그가 이 줄로 채워진다).
        if now.minute % 10 == 0:
            _log(f"{stamp} OK — {decision.detail}")
        return

    if decision.action == liveness.ACTION_DEGRADED:
        # **생존 신호 이상이 아니다** — 박동은 정상이고 적재만 끊겼다. 문구를 갈라 두지 않으면
        # 다음날 로그를 읽는 사람이 08-14의 「살아 있는데 비어 있다」를 죽음으로 오독한다.
        message = f"관측 루프 적재 정지({decision.reason}) — {decision.detail}"
    else:
        message = f"관측 루프 생존 신호 이상({decision.reason}) — {decision.detail}"
    _log(f"{stamp} {decision.action.upper()} — {message}")

    if decision.action == liveness.ACTION_RESTART:
        ok, summary = _restart()
        _log(f"{stamp} 재기동 시도: {summary}")
        message += f" · 자동 재기동 {'성공' if ok else '실패'}({summary})"

    if decision.should_alert:
        notify.notify_sync(message, level="CRITICAL")

    _write_state(liveness.next_state(state, now, decision))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # 워치독이 죽으면 그것을 감시할 것이 없다 — 최소한 자기 로그에는 남긴다.
        _log(f"워치독 자체 실패: {exc!r}")
