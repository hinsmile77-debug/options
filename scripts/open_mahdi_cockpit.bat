@echo off
setlocal
chcp 65001 >nul
REM 2026-10-10 — 백그라운드로 돌고 있는 마흐디 COCKPIT 을 브라우저에 수동으로 띄운다.
REM
REM 자동 기동(start_mahdi_premarket.bat)은 서버만 띄우고 브라우저는 열지 않는다.
REM 화면이 필요할 때 이 파일을 더블클릭한다. 몇 번을 실행해도 탭만 하나 더 열릴 뿐 서버에는 영향이 없다.
REM
REM 이 배치는 COCKPIT 창(Mahdi COCKPIT)과 무관한 별도 프로세스이고 곧 끝나므로 Chrome 은 고아가 된다
REM — 15:45 stop_mahdi_marketclose.bat 의 taskkill /T 가 Chrome 에 닿지 않는다(.streamlit\config.toml 참고).
REM
REM 서버가 아직 안 떠 있으면 빈 화면 대신 안내만 하고 끝낸다. 최대 약 30초 기다린다
REM (timeout 대신 ping — start_mahdi_premarket.bat 과 같은 이유).

set "COCKPIT_URL=http://localhost:8501"
set "WAIT_TRIES=0"

:wait_loop
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%COCKPIT_URL%/_stcore/health' | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 goto :open
set /a WAIT_TRIES+=1
if %WAIT_TRIES% GEQ 15 goto :not_running
ping -n 3 127.0.0.1 >nul
goto :wait_loop

:open
start "" "%COCKPIT_URL%"
endlocal
exit /b 0

:not_running
echo.
echo   COCKPIT 서버(%COCKPIT_URL%)가 응답하지 않는다.
echo   마흐디가 안 떠 있으면: scripts\start_mahdi_premarket.bat (휴장일엔 force)
echo.
pause
endlocal
exit /b 1
