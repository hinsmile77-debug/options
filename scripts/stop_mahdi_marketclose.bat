@echo off
setlocal

set "PROJECT_DIR=C:\Users\82108\PycharmProjects\options"
set "LOG_FILE=%PROJECT_DIR%\logs\premarket_startup.log"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo [%date% %time%] ===== Mahdi 장마감 자동 종료 시작 ===== >> "%LOG_FILE%"

taskkill /F /T /FI "WINDOWTITLE eq Mahdi COCKPIT*" >> "%LOG_FILE%" 2>&1
taskkill /F /T /FI "WINDOWTITLE eq Mahdi Observation Loop*" >> "%LOG_FILE%" 2>&1

echo [%date% %time%] ===== 장마감 자동 종료 완료 (DB/Redis는 계속 실행) ===== >> "%LOG_FILE%"

endlocal
