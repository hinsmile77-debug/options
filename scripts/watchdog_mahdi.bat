@echo off
setlocal
REM Observation-loop watchdog. Task Scheduler calls this every minute.
REM Logic lives in scripts/watchdog_observation_loop.py + mahdi/liveness.py;
REM this file only sets up the environment. Korean rationale is in those
REM docstrings, which are UTF-8 safe. Keep this file ASCII.
REM
REM ============ ASCII ONLY - DO NOT PUT NON-ASCII TEXT IN THIS FILE ============
REM 2026-08-11: this script had never run. "chcp 65001" plus UTF-8 Korean
REM comments desynced the cmd.exe batch read offset (cmd tracks a byte offset
REM into the file and 3-byte characters shift it). cmd resumed parsing in the
REM middle of a comment line, lost the REM prefix, and executed the fragments.
REM One of those fragments was a "schtasks /Create" example that was sitting
REM inside a comment - it actually ran. "cd /d" also failed, so the script only
REM worked when the caller already happened to be in the project root.
REM
REM Two rules follow from that, and both are load-bearing:
REM   1. No non-ASCII in this file - not even in comments.
REM   2. No runnable command inside a comment. The registration procedure is in
REM      docs/dev_memory/CURRENT_STATE.md instead.
REM =============================================================================
REM
REM PYTHONUTF8=1 is what makes logs/watchdog.log come out as UTF-8. chcp cannot
REM do it: Python picks its file encoding from the OS system locale, not from
REM the console code page.
set PYTHONUTF8=1

REM Project root is resolved from this file's own location - no absolute paths,
REM so the repo stays portable across PCs.
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
if not defined PROJECT_DIR exit /b 1
cd /d "%PROJECT_DIR%" || exit /b 1

uv run python scripts\watchdog_observation_loop.py

endlocal
