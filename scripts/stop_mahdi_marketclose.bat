@echo off
setlocal
chcp 65001 >nul

REM 배치파일 자기 위치 기준으로 프로젝트 루트를 계산(절대경로 하드코딩 금지 — 다른 PC/경로에서도 동작)
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "LOG_FILE=%PROJECT_DIR%\logs\premarket_startup.log"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo [%date% %time%] ===== Mahdi 장마감 자동 종료 시작 ===== >> "%LOG_FILE%"

taskkill /F /T /FI "WINDOWTITLE eq Mahdi COCKPIT*" >> "%LOG_FILE%" 2>&1
taskkill /F /T /FI "WINDOWTITLE eq Mahdi Observation Loop*" >> "%LOG_FILE%" 2>&1

cd /d "%PROJECT_DIR%"
uv run python scripts\log_marketclose_stop.py

echo [%date% %time%] ===== 장마감 자동 종료 완료 (DB/Redis는 계속 실행) ===== >> "%LOG_FILE%"

endlocal
