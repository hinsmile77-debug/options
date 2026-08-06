@echo off
setlocal
chcp 65001 >nul
REM 관측 루프 워치독 — Windows 작업 스케줄러가 **1분 주기**로 호출한다 (2026-08-06 §2-1 / Fix#2).
REM
REM 등록(관리자 PowerShell에서 한 번만, 경로는 이 파일 위치에 맞춰 바꿀 것):
REM   schtasks /Create /TN "Mahdi Watchdog" /SC MINUTE /MO 1 ^
REM     /TR "\"%~f0\"" /RL HIGHEST /F
REM
REM 창을 띄우지 않는다 — 1분마다 콘솔이 깜빡이면 사람이 곧 스케줄을 꺼버린다.
REM (작업 스케줄러 등록 시 "사용자가 로그온한 경우에만 실행" + "숨김"을 함께 켠다.)
REM
REM PYTHONUTF8: 기동 스크립트와 같은 이유 — 파이썬이 파일에 쓰는 인코딩은 콘솔 코드페이지가
REM 아니라 OS 시스템 로캘을 따르므로 chcp로는 logs/watchdog.log의 한글을 못 고친다.
set PYTHONUTF8=1

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
cd /d "%PROJECT_DIR%"

uv run python scripts\watchdog_observation_loop.py

endlocal
