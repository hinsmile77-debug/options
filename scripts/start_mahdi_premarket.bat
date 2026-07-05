@echo off
setlocal

REM 배치파일 자기 위치 기준으로 프로젝트 루트를 계산(절대경로 하드코딩 금지 — 다른 PC/경로에서도 동작)
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "LOG_FILE=%PROJECT_DIR%\logs\premarket_startup.log"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo [%date% %time%] ===== Mahdi 장전 기동 시작 ===== >> "%LOG_FILE%"

cd /d "%PROJECT_DIR%"

REM Docker 데몬이 이미 응답하면 Desktop 재실행 생략
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ready

set "DOCKER_DESKTOP_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if not exist "%DOCKER_DESKTOP_EXE%" (
    echo [%date% %time%] 경고: Docker Desktop.exe를 찾지 못함(%DOCKER_DESKTOP_EXE%) - 수동 확인 필요 >> "%LOG_FILE%"
    goto :docker_wait_skip
)

echo [%date% %time%] Docker Desktop 실행 >> "%LOG_FILE%"
start "" "%DOCKER_DESKTOP_EXE%"

set "DOCKER_WAIT_TRIES=0"
:docker_wait_loop
set /a DOCKER_WAIT_TRIES+=1
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ready
if %DOCKER_WAIT_TRIES% GEQ 36 (
    echo [%date% %time%] 경고: Docker 데몬이 180초 내에 준비되지 않음 - 그대로 진행 >> "%LOG_FILE%"
    goto :docker_wait_skip
)
ping -n 6 127.0.0.1 >nul
goto :docker_wait_loop

:docker_ready
echo [%date% %time%] Docker 데몬 준비 완료 >> "%LOG_FILE%"

:docker_wait_skip

echo [%date% %time%] docker compose up -d >> "%LOG_FILE%"
docker compose up -d >> "%LOG_FILE%" 2>&1

echo [%date% %time%] COCKPIT 대시보드 실행 (새 창) >> "%LOG_FILE%"
start "Mahdi COCKPIT" cmd /k "cd /d %PROJECT_DIR% && uv run streamlit run mahdi/dashboard/app.py >> logs\cockpit.log 2>&1"

echo [%date% %time%] 관측 루프 실행 (새 창) >> "%LOG_FILE%"
start "Mahdi Observation Loop" cmd /k "cd /d %PROJECT_DIR% && uv run python -m mahdi.main >> logs\observation_loop.log 2>&1"

echo [%date% %time%] ===== 기동 스크립트 종료 (창들은 계속 실행 중) ===== >> "%LOG_FILE%"

endlocal
