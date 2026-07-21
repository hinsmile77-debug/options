"""장마감 자동 종료(stop_mahdi_marketclose.bat)가 정상적으로 실행됐을 때, 직전 정상 종료
시각 대비 경과 시간을 premarket_startup.log에 남기고 마커를 갱신한다 (2026-07-20 고도화).

날짜 연산·문자열 포매팅을 배치파일(cmd.exe)에서 직접 하지 않고 이 스크립트로 위임한다 — 이
작업 중 cmd.exe REM/goto 조합이 인코딩 관련으로 예측 못한 방식으로 깨지는 것을 실제로 겪었고
(격리 테스트 중 재현), 그중 하나는 회복이 어려운 방식으로 멈추기까지 했다. 라이브 예약
스크립트(장전/장마감 배치파일)에는 그 위험을 반영하지 않기로 결정했다 — 이 스크립트를 배치
파일이 한 줄로 호출하기만 하면, 실제 로직(파일 읽기/쓰기, 날짜 비교, 한글 포매팅)은 전부
파이썬(pytest로 테스트됨) 쪽에 있어 같은 위험이 없다.

실행: python scripts/log_marketclose_stop.py (인자 없음, stop_mahdi_marketclose.bat이 taskkill
이후 호출한다)
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi import notify
from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db

LOG_FILE = PROJECT_ROOT / "logs" / "premarket_startup.log"
LAST_STOP_MARKER_FILE = PROJECT_ROOT / "logs" / ".last_marketclose_stop.txt"

# 2026-07-21 이상점 대응(운영점검보고서 §3-1): stop_mahdi_marketclose.bat의 taskkill(창 제목
# 기반)이 사고 대응 중 수동 재시작된 프로세스를 못 찾고 "No tasks running"만 남긴 채 실제로는
# COCKPIT/관측 루프가 계속 살아있던 사례가 있었다. 같은 배치파일에 추가한 PowerShell 커맨드라인
# fallback kill 이후에도 남아있는지 여기서 다시 한번 커맨드라인 기준으로 확인한다.
_REMAINING_PROCESS_CHECK_COMMAND = [
    "powershell", "-NoProfile", "-Command",
    "(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
    "($_.CommandLine -like '*mahdi.main*' -or $_.CommandLine -like '*mahdi/dashboard/app.py*') }"
    ").Count",
]


def log_gap_and_update_marker() -> None:
    """
    계산: LAST_STOP_MARKER_FILE에 남아있는 직전 정상 종료 시각과 현재 시각의 차이를
         premarket_startup.log에 한 줄 남긴 뒤, 이번 종료 시각으로 마커를 갱신한다.
    실패 조건: 마커 파일이 없으면(최초 실행) 또는 파싱 실패하면 그 사실만 남기고 비교는
              생략한다. 이 스크립트 자체가 실패해도(권한 등) 예외를 삼키고 종료 코드는 0을
              유지한다 — stop_mahdi_marketclose.bat의 나머지 흐름(장마감 종료 완료 로그)을
              막으면 안 된다.
    """
    now = db.local_now()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if LAST_STOP_MARKER_FILE.exists():
        try:
            last = datetime.fromisoformat(LAST_STOP_MARKER_FILE.read_text(encoding="utf-8").strip())
            gap_hours = (now - last).total_seconds() / 3600
            line = f"[{now:%Y-%m-%d %H:%M:%S}] 직전 정상 종료: {last:%Y-%m-%d %H:%M:%S} ({gap_hours:.1f}시간 전)\n"
        except (ValueError, OSError):
            line = f"[{now:%Y-%m-%d %H:%M:%S}] 직전 정상 종료 기록 파싱 실패\n"
    else:
        line = f"[{now:%Y-%m-%d %H:%M:%S}] 직전 정상 종료 기록 없음(최초 실행 또는 마커 파일 삭제됨)\n"

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)

    LAST_STOP_MARKER_FILE.write_text(now.isoformat(), encoding="utf-8")


def _count_remaining_mahdi_processes() -> int:
    """
    계산: stop_mahdi_marketclose.bat의 taskkill + PowerShell fallback kill 이후에도
         COCKPIT/관측 루프가 실제로 종료됐는지 커맨드라인 기준으로 재확인한다.
    실패 조건: PowerShell 호출 자체가 실패하면(타임아웃 등) 예외를 그대로 올린다 — 호출측이
              "확인 실패"와 "정말 0개 남음"을 구분해야 하므로 여기서 0으로 뭉개면 안 된다.
    """
    result = subprocess.run(
        _REMAINING_PROCESS_CHECK_COMMAND, capture_output=True, text=True, timeout=20, check=True,
    )
    return int(result.stdout.strip() or "0")


def check_remaining_processes_and_alert() -> None:
    """
    계산: 종료 시도 후 마흐디 프로세스가 남아있는지 확인해 (1) DB(shutdown_check_log)에 항상
         기록해 COCKPIT "오늘의 점검 요약"이 재시작 없이 "직전 장마감이 실제로 깨끗했는지"를
         보여줄 수 있게 하고(2026-07-21, 운영점검보고서 §5-3 "종료 신뢰성 배지"), (2) 남아있는
         경우에만 premarket_startup.log에 경고를 남기고 Slack으로도 알린다(§4 "종료 결과 검증
         알림") — 지금까지는 taskkill이 아무것도 못 찾아도 아무도 알아채지 못한 채 조용히
         넘어갔다.
    실패 조건: 확인 자체가 실패해도(PowerShell 없음 등) 조용히 넘어간다. DB 기록 실패도
              마찬가지로 삼킨다 — 이 점검/배지 기능 하나 때문에 장마감 종료 스크립트의 나머지
              흐름(마커 갱신 등)이 막히면 안 된다.
    """
    now = db.local_now()
    try:
        remaining = _count_remaining_mahdi_processes()
    except Exception:
        return

    try:
        with db.get_connection() as conn:
            db.record_shutdown_check(conn, now, remaining)
    except Exception:
        pass

    if remaining <= 0:
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(
            f"[{now:%Y-%m-%d %H:%M:%S}] 경고: 장마감 종료 시도 후에도 마흐디 프로세스 "
            f"{remaining}개가 남아있음(창 제목/커맨드라인 fallback 모두 실패했을 가능성 — "
            "수동 확인 필요)\n"
        )
    notify.notify_sync(
        f"장마감 자동 종료 후에도 프로세스 {remaining}개가 남아있습니다 — 수동 확인 필요",
        level="WARNING",
    )


if __name__ == "__main__":
    try:
        log_gap_and_update_marker()
    except Exception:
        pass
    try:
        check_remaining_processes_and_alert()
    except Exception:
        pass
