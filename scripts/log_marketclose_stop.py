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

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db

LOG_FILE = PROJECT_ROOT / "logs" / "premarket_startup.log"
LAST_STOP_MARKER_FILE = PROJECT_ROOT / "logs" / ".last_marketclose_stop.txt"


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


if __name__ == "__main__":
    try:
        log_gap_and_update_marker()
    except Exception:
        pass
