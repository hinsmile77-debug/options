@echo off
setlocal
chcp 65001 >nul

REM ============================================================================
REM 사람이 장중에 마흐디를 **일부러** 내릴 때 쓰는 스크립트 (2026-08-17 신설).
REM
REM ## 왜 이 파일이 따로 필요한가
REM
REM 08-17 15:00 / 15:06 / 15:28, 워치독이 관측 루프를 세 번 되살렸다. 세 번 다 사람이 코드를
REM 고치려고 창을 닫은 직후였다. 워치독은 정상이었다 — 그날 「제대로 끄는 방법」이 15:45 장마감
REM 배치 하나뿐이었고, 낮에 내리려는 사람에게는 **창을 닫는 것 말고 선택지가 없었다.**
REM 창 닫기는 워치독에게 죽음과 똑같이 보인다(하트비트가 늙는다). 그래서 되살아났다.
REM
REM stop_mahdi_marketclose.bat을 낮에 그냥 쓰면 안 되는 이유는 아래 §2에 적었다.
REM
REM ## 되살리는 법
REM
REM     scripts\start_mahdi_premarket.bat
REM
REM 기동 스크립트가 시작하면서 정지 표식을 지우므로 **워치독 감시가 자동으로 되살아난다.**
REM 손으로 지울 것은 없다. 오늘 안에 다시 안 띄우더라도 표식은 내일 날짜 검사에서 무효가 된다
REM (mahdi/liveness.py:intentional_stop_at 조건 2).
REM ============================================================================

REM 배치파일 자기 위치 기준으로 프로젝트 루트를 계산(절대경로 하드코딩 금지 — 다른 PC/경로에서도 동작)
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "LOG_FILE=%PROJECT_DIR%\logs\premarket_startup.log"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo [%date% %time%] ===== Mahdi 수동 정지 시작 (사람이 일부러 내림) ===== >> "%LOG_FILE%"
echo [%date% %time%] ===== Mahdi 수동 정지 시작 (사람이 일부러 내림) =====

REM §1 — 정지 의도를 **taskkill보다 먼저** 남긴다.
REM 순서가 뒤집히면 그 사이에 워치독이 판정을 돌릴 수 있고, 그 한 번의 판정에는 「죽음」만 보인다.
REM 판정은 mtime만 보므로 내용은 사람이 로그를 읽을 때를 위한 것이다.
echo intentional stop %date% %time% > "%PROJECT_DIR%\logs\.intentional_stop"
echo [%date% %time%] 정지 표식 기록 — 워치독이 이 프로세스를 되살리지 않는다 >> "%LOG_FILE%"

REM 창 제목 기반 + 커맨드라인 매칭의 이중 안전망. stop_mahdi_marketclose.bat과 **같은 쌍**이다 —
REM 2026-07-21에 창 제목만으로는 사고 대응 중 수동으로 띄운 창을 못 잡고 조용히 성공을 보고하는
REM 것을 실측했다(운영점검보고서 2026-07-21 §3-1). 낮에 내리는 상황은 정확히 그 "사람이 손으로
REM 띄운 창"이 섞여 있을 때이므로 여기서는 fallback이 더 중요하다.
taskkill /F /T /FI "WINDOWTITLE eq Mahdi COCKPIT*" >> "%LOG_FILE%" 2>&1
taskkill /F /T /FI "WINDOWTITLE eq Mahdi Observation Loop*" >> "%LOG_FILE%" 2>&1
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*mahdi.main*' -or $_.CommandLine -like '*mahdi/dashboard/app.py*') }; foreach ($p in $procs) { Write-Output ('커맨드라인 매칭 fallback 종료: PID {0} - {1}' -f $p.ProcessId, $p.CommandLine); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; if (-not $procs) { Write-Output '커맨드라인 매칭 fallback: 잔존 프로세스 없음' }" >> "%LOG_FILE%" 2>&1

REM 생존 신호를 지운다. 관측 루프는 taskkill로 죽으므로 정리 코드를 못 돈다 — 파일이 남으면
REM 워치독 로그의 사유가 계속 `stale`("떴다가 죽었다")로 찍혀, 다음날 원인을 찾는 사람이
REM 의도적 정지를 프리즈로 오독한다. 지우면 `missing`("아예 안 떴다")이 되어 사실과 맞는다.
del /q "%PROJECT_DIR%\logs\.observation_loop_heartbeat.json" 2>nul

REM §2 — daily_ops_report / collect_evidence는 **여기서 돌리지 않는다.**
REM
REM 그 둘은 장마감 산출물이다(stop_mahdi_marketclose.bat 참고). 낮 12시에 돌리면 그날치 지표와
REM 증거 다이제스트가 **반나절짜리 반쪽으로 덮인다** — 파일명이 날짜 단위라 조용히 덮어쓰고,
REM 그 사실은 다음날 그 파일을 근거로 판단할 때까지 드러나지 않는다. 하루의 기록은 하루가
REM 끝났을 때 남긴다. 15:45 배치가 그대로 돌면서 정상적으로 만든다.

echo [%date% %time%] ===== Mahdi 수동 정지 완료 (DB/Redis는 계속 실행) ===== >> "%LOG_FILE%"
echo [%date% %time%] ===== Mahdi 수동 정지 완료 (DB/Redis는 계속 실행) =====
echo.
echo   워치독 감시가 보류됐다. 되살리려면: scripts\start_mahdi_premarket.bat
echo.

endlocal
