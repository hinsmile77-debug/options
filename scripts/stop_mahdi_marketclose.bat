@echo off
setlocal
chcp 65001 >nul

REM 배치파일 자기 위치 기준으로 프로젝트 루트를 계산(절대경로 하드코딩 금지 — 다른 PC/경로에서도 동작)
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "LOG_FILE=%PROJECT_DIR%\logs\premarket_startup.log"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo [%date% %time%] ===== Mahdi 장마감 자동 종료 시작 ===== >> "%LOG_FILE%"

REM 2026-08-17: 「사람이(또는 예약이) 일부러 껐다」를 워치독에게 알린다 — **taskkill보다 먼저 쓴다.**
REM 이 표식이 없으면 워치독은 정지와 죽음을 구분할 방법이 없어 3~4분 뒤 되살린다(08-17 15:00 /
REM 15:06 / 15:28 재기동 3회가 정확히 그 경우였다). 지금까지 이 스크립트가 무사했던 것은 설계가
REM 아니라 시각 우연이다 — 실행 시각(15:45)이 감시 창 끝(liveness.WATCH_WINDOW_END)과 정확히
REM 겹쳐서 재기동이 안 걸렸을 뿐이고, 이 배치가 조금이라도 일찍 돌면 그대로 되살아난다.
REM 판정은 mtime만 보므로 내용은 사람이 읽기 위한 것이다(cmd.exe 날짜 문자열은 로케일을 탄다).
REM 표식은 다음 기동 스크립트가 **시작하면서** 지운다 — 만료를 여기서 관리하지 않는다.
echo intentional stop %date% %time% > "%PROJECT_DIR%\logs\.intentional_stop"

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

REM 2026-08-06(§2-1 / Fix#2): 생존 신호를 지운다.
REM 관측 루프는 taskkill로 죽으므로 정리 코드를 못 돈다 — 파일이 남으면 장마감 이후 내내
REM "N초째 갱신 없음"으로 보인다. 감시 창(~15:45) 밖이라 알림이 뜨지는 않지만, 다음날 아침
REM 창이 열리는 07:40에 **어제 것이 늙은 채로** 판정 대상이 된다.
del /q "%PROJECT_DIR%\logs\.observation_loop_heartbeat.json" 2>nul

cd /d "%PROJECT_DIR%"
uv run python scripts\log_marketclose_stop.py

REM 2026-08-24(08-24 §3-3 / Fix#5) — **완료 줄은 여기서 찍는다.** 종전에는 이 줄이 파일
REM 맨 끝(daily_ops_report·collect_evidence 뒤)에 있었고, 그래서 08-24에 지표가
REM 15:45:14~15:46:06에 도는 동안 자기 근거가 될 줄은 **15:46:09에** 찍혔다 — 3.9초 차이로
REM `crash.unexplained_deaths = 1`(사유 없이 끝난 기동)이라는 오보가 났다. 실제로 「끝난」
REM 시점은 taskkill이고 지표·증거 생성은 그 뒤의 후처리다.
REM
REM ⚠ **대가**: 이 줄이 앞으로 오면 후처리가 실패한 날에도 「완료」가 찍힌다. 그래서 문구에
REM `프로세스 정지 · 후처리 진행`을 붙여 이 줄이 무엇을 보증하는지 문장 자체가 말하게 한다.
REM `crash_metrics.py`가 판정에 쓰는 접두(`장마감 자동 종료 완료`)는 그대로 둔다 —
REM 문구를 바꾸면서 파서를 안 고치면 08-04(362건이 0건으로 보고)를 우리 손으로 재현한다.
echo [%date% %time%] ===== 장마감 자동 종료 완료 (프로세스 정지 · 후처리 진행, DB/Redis는 계속 실행) ===== >> "%LOG_FILE%"

REM 2026-08-01(운영점검보고서 2026-07-31 §5-2): 하루치 운영 지표를 자동 집계해
REM docs\동작점검\auto\에 마크다운 + JSON 사이드카로 남긴다. 이 시점이 적기인 이유는 두 가지다 —
REM 위 taskkill로 관측 루프가 이미 종료돼 **로그가 완결**돼 있고, DB/Redis는 의도적으로 계속
REM 실행 중이라 SQL 집계가 가능하다. 스크립트가 최상위에서 예외를 삼키므로(exit 0) 실패해도
REM 뒤의 증거 생성까지 정상적으로 진행된다. **종료 표식(위 44~54행)은 이미 찍혀 있다** —
REM 2026-08-24 Fix#5로 그 줄이 이 앞으로 왔다.
REM
REM 2026-08-03(운영점검보고서 §3-1): 이 줄만 출력이 파일로 리다이렉트되는데, **Python이
REM 리다이렉트된 파일에 쓰는 인코딩은 콘솔 코드페이지(위 chcp 65001)가 아니라 OS 시스템
REM 로캘(ANSI=cp949)을 따른다** — 그래서 08-03 로그에 리포트 경로의 한글이 전부 깨져 남았다
REM ("docs\??????\auto\2026-08-03_??.md"). start_mahdi_premarket.bat 상단이 이미 같은 함정을
REM 문서화해 뒀는데 stop 쪽에는 그 대책이 빠져 있었다. 프로세스 단위로 UTF-8을 강제한다.
set PYTHONIOENCODING=utf-8
uv run python scripts\daily_ops_report.py >> "%LOG_FILE%" 2>&1

REM 2026-08-13: 증거 다이제스트. 위 지표(daily_ops_report)가 **하루치 숫자**라면 이쪽은
REM **하루의 뼈대와 사건**이다 — 기동/종료 시퀀스, 관측 루프 생사, 워치독 자신의 무기록 구간,
REM 크래시, 커밋 선후, 레버 상태, 오늘 판정할 가설, 자동 적신호. 지표에 안 실리는 것들이라
REM 그동안 점검 세션이 매번 손으로 훑던 부분이다.
REM
REM **반드시 daily_ops_report 뒤에 온다** — 이 스크립트의 §10이 방금 만들어진 지표의 §0/§1을
REM 발췌해 싣기 때문이다. 앞에 두면 그 절이 "없음"으로 비고, 그 빈 자리는 조용하다.
REM
REM stdlib만 쓰므로 DB/Docker가 꺼져 있어도 돈다. 실패해도 배치는 오류에서 멈추지 않는다 —
REM **종료 절차를 막지 않는 것이 이 자리의 조건**이다. 2026-08-24 Fix#5 이후 종료 표식은
REM 이 줄보다 **앞**에 찍히므로, 이 스크립트가 실패해도 「끝났다」는 사실 자체는 남는다.
REM 날짜는 스크립트가 KST로 계산한다(`--out-dir`). `%date%`는 OS 로캘을 따라 형식이 바뀌어
REM PC마다 다른 파일명을 만든다 — daily_ops_report.py가 `--out-dir`을 두는 것과 같은 이유다.
REM 2026-08-19: `--prune-days 7` — 증거 다이제스트만 7일치를 남긴다.
REM
REM **하루에 한 번, 여기서만 돈다.** 장전·장중 회차에 붙이면 같은 일을 하루 네 번 시도하게
REM 되고, 그중 하나가 밀린 실행이면(08-17 486분·08-18 298분) 엉뚱한 시각에 지운다.
REM 종료 배치는 하루의 끝이 확정된 자리라 여기가 맞다.
REM
REM **왜 증거만인가** — 08-19 실측: 08-13~08-18 점검 문서 13편이 증거 파일을 30번 인용했는데
REM 전부 당일 것이고 과거분 인용은 0건이었다(수명 하루). 반면 `_지표.json`은 08-18 신설된
REM `mahdi/ops/campaign.py`가 여러 날을 접는 원자재라(min_days 10) 삭제 대상이 아니고,
REM 루트 보고서는 git 추적이라 지워도 용량이 안 줄고 grep 대상만 잃는다(소급 인용 꼬리 43일).
REM 스크립트가 `_증거_*.md` 파일명 패턴으로만 지우므로 여기서 숫자를 바꿔도 그 범위는 안 넓어진다.
uv run python "docs\동작점검\tools\collect_evidence.py" --phase post --out-dir "docs\동작점검\auto" --prune-days 7 >> "%LOG_FILE%" 2>&1

endlocal
