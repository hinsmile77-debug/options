@echo off
setlocal

set "PROJECT_DIR=C:\Users\82108\PycharmProjects\options"
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
