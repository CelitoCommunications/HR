@echo off
REM ============================================
REM  Celito Onboarding Platform - Git Pull & Restart
REM ============================================
REM  1. Stops the running server (if any)
REM  2. Pulls latest code from GitHub
REM  3. Starts the server back up
REM  4. Confirms it's listening on port 8780
REM ============================================

cd /d "%~dp0"

echo ============================================
echo  Celito Onboarding - Git Pull ^& Restart
echo ============================================
echo.

REM ── Step 1: Stop the server if running ─────────────────────
echo [Step 1] Stopping Celito Onboarding server...
taskkill /FI "WINDOWTITLE eq CelitoOnboard" /F >NUL 2>&1
if not errorlevel 1 (
    echo          Server stopped.
    REM Give it a moment to release the port
    timeout /t 3 /nobreak >NUL
) else (
    echo          No running server found — skipping.
)
echo.

REM ── Step 2: Pull latest from GitHub ────────────────────────
echo [Step 2] Pulling latest code from GitHub...
git pull origin main
if errorlevel 1 (
    echo.
    echo *** ERROR: Git pull failed! Resolve conflicts and try again. ***
    echo *** Server is NOT running. Run start_onboard.bat manually.   ***
    pause
    exit /b 1
)
echo          Pull complete.
echo.

REM ── Step 3: Start the server ───────────────────────────────
echo [Step 3] Starting Celito Onboarding server...
start "CelitoOnboard" /MIN python backend\server.py
echo          Server start command issued. Waiting for startup...
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
    echo  Check logs\server_*.log for errors.
    echo ============================================
)
echo.
pause
