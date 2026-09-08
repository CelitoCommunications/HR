@echo off
echo ============================================
echo  Celito Onboarding Platform - Install Deps
echo ============================================
echo.
echo Upgrading pip...
python -m pip install --upgrade pip
echo.
echo Installing dependencies...
python -m pip install flask waitress msal requests python-docx
echo.
echo ============================================
echo  Installation complete!
echo ============================================
pause
