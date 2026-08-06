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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi import liveness, notify
from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db

LOG_DIR = PROJECT_ROOT / "logs"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
STATE_FILE = LOG_DIR / ".watchdog_state.json"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_mahdi_premarket.bat"

# 기동 스크립트는 Docker 데몬을 최대 180초까지 기다린다 — 그보다 넉넉히 잡되 무한은 아니다.
_RESTART_TIMEOUT_SECONDS = 300


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
    """기동 스크립트를 실행한다. 반환: (성공 여부, 요약)."""
    if not START_SCRIPT.exists():
        return False, f"기동 스크립트를 찾지 못함: {START_SCRIPT}"
    try:
        result = subprocess.run(
            ["cmd", "/c", str(START_SCRIPT)],
            capture_output=True, text=True,
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


def main() -> None:
    now = db.local_now()
    beat = liveness.read_heartbeat(liveness.heartbeat_path(LOG_DIR))
    state = _read_state()
    decision = liveness.decide(
        beat, now, state,
        # 기동 스크립트가 도는 중이면 판정을 보류한다 — 안 그러면 기동이 서로를 덮어쓴다.
        starting=liveness.startup_in_progress(liveness.startup_marker_path(LOG_DIR), now),
    )

    if decision.action == liveness.ACTION_IDLE:
        return  # 감시 창 밖 — 로그도 남기지 않는다(매분 한 줄이면 하루 1,000줄이다)

    stamp = f"[{now:%Y-%m-%d %H:%M:%S}]"
    if decision.action == liveness.ACTION_OK:
        # 정상도 기록은 남긴다 — 다음날 "워치독이 돌기는 했는가"를 물을 수 있어야 한다.
        # 다만 10분에 한 번만(1분 주기 x 매번이면 로그가 이 줄로 채워진다).
        if now.minute % 10 == 0:
            _log(f"{stamp} OK — {decision.detail}")
        return

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
