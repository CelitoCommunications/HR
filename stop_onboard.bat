@echo off
REM ============================================
REM  Celito Onboarding Platform - Stop Server
REM ============================================
REM  Called by Windows Task Scheduler:
REM    - Daily at 7:45 PM
REM ============================================

cd /d "%~dp0"

REM Get today's date for log file
for /f "tokens=1-3 delims=/" %%a in ("%date%") do (
    set LOGDATE=%%c-%%a-%%b
)

echo [%date% %time%] Stopping Celito Onboarding Server... >> "logs\server_%LOGDATE%.log"

REM Kill the server process by window title
taskkill /FI "WINDOWTITLE eq CelitoOnboard" /F >NUL 2>&1

if not errorlevel 1 (
    echo [%date% %time%] Server stopped successfully. >> "logs\server_%LOGDATE%.log"
) else (
    echo [%date% %time%] No running server found. >> "logs\server_%LOGDATE%.log"
)
