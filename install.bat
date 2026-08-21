@echo off
REM One-time setup. Run this once, then use whispa.bat every day.
setlocal

where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.11+ from python.org
  echo and tick "Add Python to PATH" during setup.
  pause
  exit /b 1
)

echo Creating virtual environment...
py -3 -m venv "%~dp0.venv"
if errorlevel 1 goto :failed

echo Installing dependencies (this downloads ~200 MB, one time)...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :failed

echo.
echo Done. Run whispa.bat to start dictating.
echo The speech model downloads on first launch (about 150 MB for base.en).
pause
exit /b 0

:failed
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
