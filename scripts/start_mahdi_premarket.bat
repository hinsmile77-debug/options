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

REM 2026-08-17 2차: **오늘 시장이 안 열리면 아무것도 하지 않는다.**
REM
REM 08-15(토)·08-16(일)에 워치독이 주말에 시스템 전체를 부팅했고, 08-17(광복절 대체공휴일)에는
REM 이 스크립트가 월~금 트리거를 타고 정상적으로 떠서 종일 돌았다. 그 하루가 option_analysis_1m에
REM 9,640행을 남겼는데 IV 고유값은 30(=레그 수)이었다 — 값이 얼어붙은 하루가 영업일(9,194행)보다
REM 많이 적재된 것이고, 행 수로는 구분이 안 된다.
REM
REM **표식 생성보다 앞에 둔다** — 건너뛸 날에는 .startup_in_progress도 만들지 않는다(만들면
REM 300초 동안 워치독 판정이 보류되는데, 그 사이 아무 일도 안 일어날 것이 확실하다).
REM
REM 판정은 mahdi\market_calendar.py에 있고 이 스크립트는 종료 코드만 본다. 문자열 출력을
REM 파싱하지 않는 이유는 cmd.exe의 문자열 처리가 로케일·인코딩을 타기 때문이다(이 저장소가
REM 2026-08-03에 실제로 다친 지점).
REM
REM 우회: `start_mahdi_premarket.bat force` — 달력이 틀렸거나 휴장일에 일부러 띄울 때 쓴다.
REM 워치독의 재기동 경로(_restart)는 인자 없이 부르므로 **자동 재기동은 이 게이트를 못 넘는다** —
REM 그것이 옳다(휴장일에 워치독이 되살릴 이유가 없고, 애초에 워치독도 그날은 IDLE이다).
if /i "%~1"=="force" goto :trading_day_ok
echo [%date% %time%] 거래일 점검 >> "%LOG_FILE%"
call uv run python scripts\check_trading_day.py
if errorlevel 1 goto :not_a_trading_day
goto :trading_day_ok

:not_a_trading_day
echo [%date% %time%] ===== 비거래일 - 기동하지 않는다 (uv run python scripts\check_trading_day.py 참고) ===== >> "%LOG_FILE%"
echo.
echo   비거래일이라 마흐디를 띄우지 않았다. 그래도 띄우려면: scripts\start_mahdi_premarket.bat force
echo.
endlocal
exit /b 0

:trading_day_ok

REM 2026-08-06(§2-1 / Fix#2): 워치독에게 "지금 기동 중"이라고 알린다.
REM 이 표식이 없으면 1분 주기 워치독이 **기동 중인 루프를 다시 기동한다** — 아래 Docker 대기가
REM 최대 180초라 그 사이 생존 신호는 늙은 채로(또는 없는 채로) 남아 있기 때문이다.
REM 표식의 내용은 아무도 읽지 않는다(mtime만 본다) — cmd.exe의 날짜 문자열은 로케일을 탄다.
echo startup > "%PROJECT_DIR%\logs\.startup_in_progress"

REM 2026-08-17: 기동을 시작한 순간 「의도적 정지」는 철회된 것이다 — 정지 표식을 여기서 지운다.
REM **끝이 아니라 시작에서 지우는 것이 중요하다.** 끝에서 지우면 이 스크립트가 중간에(예: Docker
REM 데몬을 180초 기다리다가) 죽었을 때 표식이 살아남아 워치독이 그날 내내 침묵한다 — 되살릴 것이
REM 없는 상태로 조용해지는 것이 08-06에 19분을 만든 바로 그 모양이다. 시작에서 지우면 그 경우
REM .startup_in_progress가 300초 뒤 만료되고 워치독이 정상적으로 되살린다.
REM 워치독의 재기동도 이 스크립트를 그대로 쓰므로(watchdog_observation_loop._restart) 표식 정리
REM 경로가 하나뿐이다 — 하루 한 번도 안 쓰이는 두 번째 경로를 만들지 않는다.
del /q "%PROJECT_DIR%\logs\.intentional_stop" 2>nul

REM 2026-07-22 이상점 대응(운영점검보고서 2026-07-22 §1-1): 어제(07-21) 08:15에 수동으로 뜬
REM COCKPIT/관측 루프가 그날 15:45 자동 종료 실패 이후에도 밤새 좀비로 남아, 오늘 아침까지
REM 그 사이 커밋된 새 코드(종료 신뢰성 배지 등)를 하나도 못 읽은 채 에러를 반복했다.
REM stop_mahdi_marketclose.bat에 있는 것과 동일한 이중 안전망(창 제목 taskkill + 커맨드라인
REM 매칭 PowerShell fallback)을 기동 스크립트에도 대칭으로 추가한다 — 지금까지 "종료"만
REM 견고화됐고 "기동 전" 잔존 프로세스 점검은 전혀 없었다. 잔존 프로세스가 없는 게 정상
REM 상황(매일 아침 대부분 이 경우)이므로 taskkill이 아무것도 못 찾아도 실패로 취급하지 않는다.
echo [%date% %time%] 기동 전 잔존 프로세스 정리 >> "%LOG_FILE%"
taskkill /F /T /FI "WINDOWTITLE eq Mahdi COCKPIT*" >> "%LOG_FILE%" 2>&1
taskkill /F /T /FI "WINDOWTITLE eq Mahdi Observation Loop*" >> "%LOG_FILE%" 2>&1
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*mahdi.main*' -or $_.CommandLine -like '*mahdi/dashboard/app.py*') }; foreach ($p in $procs) { Write-Output ('기동 전 커맨드라인 매칭 fallback 종료: PID {0} - {1}' -f $p.ProcessId, $p.CommandLine); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; if (-not $procs) { Write-Output '기동 전 커맨드라인 매칭 fallback: 잔존 프로세스 없음' }" >> "%LOG_FILE%" 2>&1

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

REM 2026-07-21 이상점 대응: db/migrations/*.sql이 파일로만 커밋되고 라이브 컨테이너엔 반영 안
REM 된 채로 남는 사고가 두 번(010/011) 있었다 — docker-entrypoint-initdb.d는 볼륨 최초 생성
REM 시 1회만 실행되고, 그 뒤로는 사람이 기억해서 docker exec psql로 수동 적용해야 하는 구조라
REM 재발이 예정돼 있었음(dev_memory/NEXT_TODO.md 2026-07-21 항목 참고). 모든 마이그레이션이
REM CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / create_hypertable(if_not_exists=>
REM TRUE) / COMMENT ON COLUMN으로만 작성돼 있어(2026-07-21 전수 확인) 매일 재실행해도 안전 —
REM 기동 시점마다 파일명 오름차순(001, 002, ...)으로 전부 재적용해 "적용을 깜빡함" 자체를
REM 구조적으로 없앤다. 개별 파일이 실패해도(예: DB가 막 떠서 아직 준비 안 됨) 경고만 남기고
REM 다음 파일로 넘어간다 — 한 파일 실패로 기동 전체가 멈추면 안 된다.
echo [%date% %time%] DB 마이그레이션 적용(db/migrations, 전체 재실행) >> "%LOG_FILE%"
for /f "delims=" %%f in ('dir /b /on "%PROJECT_DIR%\db\migrations\*.sql"') do call :apply_migration "%PROJECT_DIR%\db\migrations\%%f"
goto :migrations_done

:apply_migration
docker exec -i mahdi_timescaledb psql -U mahdi -d mahdi -v ON_ERROR_STOP=1 < "%~1" >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto :migration_failed
echo [%date% %time%] 마이그레이션 적용 완료 - %~nx1 >> "%LOG_FILE%"
goto :eof
:migration_failed
echo [%date% %time%] 경고: 마이그레이션 적용 실패 - %~nx1 (계속 진행) >> "%LOG_FILE%"
goto :eof

:migrations_done

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

REM 2026-08-14 고도화 5 — 오늘 발동일인 레버가 꺼진 채 뜨려는지 여기서 알린다.
REM 설정은 기동 시 로드되므로 이 줄을 지나면 오늘 그 레버를 켤 창이 닫힌다.
REM 이 점검은 기동을 막지 않는다(항상 종료 코드 0) — 상세 근거는 scripts\check_lever_due.py.
REM 콘솔에 그대로 찍는다: 로그 파일로 보내면 아침에 아무도 안 본다.
echo [%date% %time%] 레버 발동일 점검 >> "%LOG_FILE%"
call uv run python scripts\check_lever_due.py

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

REM 2026-08-06 Fix#2 — 기동이 끝났으므로 워치독의 판정 보류를 푼다. 관측 루프는 방금 떴고
REM 첫 생존 신호를 곧(수십 초 안에) 쓴다. 임계가 180초라 그 사이는 여유가 있다.
del /q "%PROJECT_DIR%\logs\.startup_in_progress" 2>nul

echo [%date% %time%] ===== 기동 스크립트 종료 (창들은 계속 실행 중) ===== >> "%LOG_FILE%"

endlocal
