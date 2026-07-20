@echo off
setlocal
chcp 65001 >nul
REM chcp는 cmd.exe 콘솔/외부 EXE(taskkill 등) 출력에만 적용된다 — start로 띄우는 COCKPIT/관측
REM 루프는 별도 cmd 창(콘솔)이라 이 chcp를 상속하지 않고, 게다가 Python이 리다이렉트된 파일에
REM 쓰는 인코딩은 콘솔 코드페이지가 아니라 OS 시스템 로캘(ANSI 코드페이지)을 따르므로 chcp로는
REM 애초에 못 고친다 — logs\cockpit.log/observation_loop.log의 한글 로그가 깨지는 걸 실측 확인
REM (2026-07-09) 후 PYTHONUTF8=1로 해결. 이 값은 set으로 지정하면 하위(start로 띄우는) 프로세스에도
REM 환경변수로 상속된다.
set PYTHONUTF8=1

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
    echo [%date% %time%] 경고: Docker Desktop.exe를 찾지 못함 ^(%DOCKER_DESKTOP_EXE%^) - 수동 확인 필요 >> "%LOG_FILE%"
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

REM 2026-07-20 로그 위생: cockpit.log는 observation_loop.log와 달리(2026-07-19, Python
REM RotatingFileHandler로 직접 소유하도록 교체됨) 여전히 cmd.exe append 리다이렉트로만 쌓여
REM 로테이션이 없다. 같은 무한 누적 문제가 재발할 수 있어 기동 시점(하루 1회)마다 크기를
REM 확인해 임계값(10MB)을 넘으면 .1로 회전 후 새로 시작한다.
REM 주의: 이 REM 블록은 꺾쇠 기호(리다이렉트/파이프 기호)를 절대 쓰지 않는다 — 2026-07-20
REM 최초 버전이 설명문에 그 기호를 그대로 적었다가 cmd.exe가 그걸 실제 연산자로 해석해
REM 파싱 오류를 낸 적이 있다(REM은 이런 기호 앞에서 완전한 주석이 아니다).
REM 아래 로직도 괄호 블록 대신 goto/라벨을 쓴다 — 위 Docker 대기 루프와 같은 이유
REM (2026-07-07 괄호 파싱 버그 참고).
set "COCKPIT_LOG=%PROJECT_DIR%\logs\cockpit.log"
if not exist "%COCKPIT_LOG%" goto :cockpit_log_rotate_skip
for %%F in ("%COCKPIT_LOG%") do set "COCKPIT_LOG_SIZE=%%~zF"
if %COCKPIT_LOG_SIZE% LSS 10485760 goto :cockpit_log_rotate_skip
echo [%date% %time%] cockpit.log가 10MB를 넘어 회전(.1로 이동) >> "%LOG_FILE%"
move /y "%COCKPIT_LOG%" "%COCKPIT_LOG%.1" >nul

:cockpit_log_rotate_skip

echo [%date% %time%] COCKPIT 대시보드 실행 (새 창) >> "%LOG_FILE%"
start "Mahdi COCKPIT" cmd /k "cd /d %PROJECT_DIR% && uv run streamlit run mahdi/dashboard/app.py >> logs\cockpit.log 2>&1"

echo [%date% %time%] 관측 루프 실행 (새 창) >> "%LOG_FILE%"
REM 2026-07-19(§5-5 로그 위생): stdout을 여기서 logs\observation_loop.log로 리다이렉트하면
REM Python 로깅(mahdi.main._configure_logging()의 RotatingFileHandler)이 회전시키는 파일을
REM 이 리다이렉트가 계속 원래 경로에 append해 덮어써 로테이션이 무의미해진다(105MB까지 무한
REM 누적됐던 원인) — 이제 Python이 그 파일을 직접 소유하므로 stdout은 콘솔 창에만 보이게 두고,
REM stderr(로깅 설정이 끝나기 전 극초반 크래시 등 로깅으로 못 잡는 것)만 별도의 회전 없는
REM 크래시 전용 로그로 남긴다.
start "Mahdi Observation Loop" cmd /k "cd /d %PROJECT_DIR% && uv run python -m mahdi.main 2>>logs\observation_loop_crash.log"

echo [%date% %time%] ===== 기동 스크립트 종료 (창들은 계속 실행 중) ===== >> "%LOG_FILE%"

endlocal
