@echo off
REM ============================================
REM  Celito Onboarding Platform - Start Server
REM ============================================
REM  Called by Windows Task Scheduler:
REM    - Boot trigger (runs without user login)
REM    - Daily at 6:15 AM
REM ============================================

cd /d "%~dp0"

REM Create logs directory if it doesn't exist
if not exist "logs" mkdir logs

REM Get today's date for log file
for /f "tokens=1-3 delims=/" %%a in ("%date%") do (
    set LOGDATE=%%c-%%a-%%b
)

REM Check if server is already running
tasklist /FI "WINDOWTITLE eq CelitoOnboard" 2>NUL | find /I "python" >NUL
if not errorlevel 1 (
    echo Server is already running. >> "logs\server_%LOGDATE%.log"
    exit /b 0
)

echo [%date% %time%] Starting Celito Onboarding Server... >> "logs\server_%LOGDATE%.log"

REM Start the server
start "CelitoOnboard" /MIN python backend\server.py >> "logs\server_%LOGDATE%.log" 2>&1

echo [%date% %time%] Server start command issued. >> "logs\server_%LOGDATE%.log"
