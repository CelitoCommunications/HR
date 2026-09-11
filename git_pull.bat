@echo off
REM ============================================
REM  Celito Onboarding Platform - Git Pull & Restart
REM ============================================
REM  1. Stops the running server (same method as Task Scheduler)
REM  2. Pulls latest code from GitHub
REM  3. Starts the server with a visible window for troubleshooting
REM  4. Confirms it's listening on port 8780
REM ============================================

cd /d "%~dp0"

set "PYTHON=C:\Users\celitoadmin\AppData\Local\Programs\Python\Python314\python.exe"
set "SERVER_SCRIPT=backend\server.py"

echo ============================================
echo  Celito Onboarding - Git Pull ^& Restart
echo ============================================
echo.

REM ── Step 1: Stop the server if running (same as daily_restart.ps1) ──
echo [Step 1] Stopping Celito Onboarding server...
REM Kill any Python process running backend\server.py (matches Task Scheduler method)
for /f "tokens=2" %%P in ('wmic process where "CommandLine like '%%backend\\server.py%%'" get ProcessId /value 2^>NUL ^| findstr "ProcessId"') do (
    echo          Stopping PID %%P...
    taskkill /PID %%P /F >NUL 2>&1
)
REM Also try window title fallback in case it was started by an older script
taskkill /FI "WINDOWTITLE eq CelitoOnboard" /F >NUL 2>&1
timeout /t 3 /nobreak >NUL
echo          Done.
echo.

REM ── Step 2: Pull latest from GitHub ────────────────────────
echo [Step 2] Pulling latest code from GitHub...
git fetch origin main
if errorlevel 1 (
    echo.
    echo *** ERROR: Git fetch failed! Check network/credentials.     ***
    echo *** Server is NOT running. Start it manually.               ***
    pause
    exit /b 1
)
git reset --hard origin/main
git clean -fd --exclude=config/ --exclude=logs/ --exclude=*.db
echo          Pull complete (synced to latest).
echo.

REM ── Step 3: Start the server (visible window for troubleshooting) ──
echo [Step 3] Starting Celito Onboarding server...
echo          Python: %PYTHON%
echo          Script: %SERVER_SCRIPT%
start "CelitoOnboard" "%PYTHON%" %SERVER_SCRIPT%
echo          Server started in a visible window.
timeout /t 8 /nobreak >NUL
echo.

REM ── Step 4: Verify it's listening on port 8780 ────────────
echo [Step 4] Checking port 8780...
netstat -ano | findstr ":8780.*LISTENING" >NUL 2>&1
if not errorlevel 1 (
    echo.
    echo ============================================
    echo  SUCCESS - Server is running on port 8780
    echo  URL: https://dash.celito.net/onboard
    echo ============================================
) else (
    echo.
    echo ============================================
    echo  WARNING - Nothing listening on port 8780!
    echo  Check the Python window for errors, or
    echo  check logs\server_*.log
    echo ============================================
)
echo.
pause
