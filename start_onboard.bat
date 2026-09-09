@echo off
REM ============================================
REM  Celito Onboarding Platform - Start Server
REM ============================================
REM  Called by Windows Task Scheduler at boot.
REM  Designed to run as SYSTEM (no desktop session).
REM ============================================

cd /d "%~dp0"

REM Create logs directory if it doesn't exist
if not exist "logs" mkdir logs

REM Use WMIC for locale-independent date (SYSTEM account may differ)
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>NUL') do set DT=%%I
set LOGDATE=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%

REM Check if server.py is already running (works without a desktop session)
wmic process where "CommandLine like '%%backend\\server.py%%'" get ProcessId 2>NUL | find /C /V "" > "%TEMP%\celito_count.tmp"
set /p PROC_COUNT=<"%TEMP%\celito_count.tmp"
del "%TEMP%\celito_count.tmp" 2>NUL
REM WMIC output has 3 header/blank lines, so >3 means a process exists
if %PROC_COUNT% GTR 3 (
    echo [%LOGDATE% %time%] Server is already running, skipping start. >> "logs\server_%LOGDATE%.log"
    exit /b 0
)

echo [%LOGDATE% %time%] Starting Celito Onboarding Server... >> "logs\server_%LOGDATE%.log"

REM Start python directly (no 'start /MIN' - SYSTEM has no desktop).
REM Python runs in the foreground so Task Scheduler keeps it alive.
python backend\server.py >> "logs\server_%LOGDATE%.log" 2>&1

REM If we get here, the server exited
echo [%LOGDATE% %time%] Server process exited with code %ERRORLEVEL%. >> "logs\server_%LOGDATE%.log"
exit /b %ERRORLEVEL%
