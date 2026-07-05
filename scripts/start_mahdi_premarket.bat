@echo off
setlocal

REM 배치파일 자기 위치 기준으로 프로젝트 루트를 계산(절대경로 하드코딩 금지 — 다른 PC/경로에서도 동작)
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "LOG_FILE=%PROJECT_DIR%\logs\premarket_startup.log"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo [%date% %time%] ===== Mahdi 장전 기동 시작 ===== >> "%LOG_FILE%"

cd /d "%PROJECT_DIR%"

echo [%date% %time%] docker compose up -d >> "%LOG_FILE%"
docker compose up -d >> "%LOG_FILE%" 2>&1

echo [%date% %time%] COCKPIT 대시보드 실행 (새 창) >> "%LOG_FILE%"
start "Mahdi COCKPIT" cmd /k "cd /d %PROJECT_DIR% && uv run streamlit run mahdi/dashboard/app.py"

echo [%date% %time%] 관측 루프 실행 (새 창) >> "%LOG_FILE%"
start "Mahdi Observation Loop" cmd /k "cd /d %PROJECT_DIR% && uv run python -m mahdi.main"

echo [%date% %time%] ===== 기동 스크립트 종료 (창들은 계속 실행 중) ===== >> "%LOG_FILE%"

endlocal
