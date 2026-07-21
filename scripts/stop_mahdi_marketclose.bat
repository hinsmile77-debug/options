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

REM 2026-07-21 이상점 대응: 위 taskkill은 창 제목(WINDOWTITLE) 기반이라, 사고 대응 중 사람이
REM 새 터미널에서 수동으로 COCKPIT/관측 루프를 재시작하면(배치스크립트의 start "..." 명명
REM 규약을 거치지 않음) 창 제목이 달라져 아무것도 못 찾고 조용히 넘어간다 — 실제로 그날 15:45
REM 자동 종료가 둘 다 "No tasks running"을 남기고도 두 프로세스가 계속 살아있었다(운영점검보고서
REM 2026-07-21 §3-1). 창 제목 대신 실행 커맨드라인(mahdi.main/mahdi/dashboard/app.py 포함 여부)으로
REM 찾는 이중 안전망을 추가한다 — 어떻게 띄워졌든 실제로 무슨 코드를 실행 중인지로 찾으므로
REM 명명 규약과 무관하게 잡힌다. -ne $PID로 이 powershell 프로세스 자기 자신은 제외한다(이
REM 커맨드라인 문자열 자체에 검색어가 들어있어 자기 자신이 매칭되는 것을 막기 위함).
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*mahdi.main*' -or $_.CommandLine -like '*mahdi/dashboard/app.py*') }; foreach ($p in $procs) { Write-Output ('커맨드라인 매칭 fallback 종료: PID {0} - {1}' -f $p.ProcessId, $p.CommandLine); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; if (-not $procs) { Write-Output '커맨드라인 매칭 fallback: 잔존 프로세스 없음' }" >> "%LOG_FILE%" 2>&1

cd /d "%PROJECT_DIR%"
uv run python scripts\log_marketclose_stop.py

echo [%date% %time%] ===== 장마감 자동 종료 완료 (DB/Redis는 계속 실행) ===== >> "%LOG_FILE%"

endlocal
