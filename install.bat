@echo off
setlocal EnableExtensions EnableDelayedExpansion
title whispa setup

REM ---------------------------------------------------------------------------
REM One-time setup. Installs Python if it is missing, then whispa's
REM dependencies. Run this once; after that use whispa.bat.
REM ---------------------------------------------------------------------------

set "ROOT=%~dp0"
set "PYVER=3.12.10"
set "PYEXE="
set "PYARG="

echo.
echo  ==========================================
echo    whispa setup
echo  ==========================================
echo.

REM ---------- 1. find a usable Python ----------------------------------------
call :find_python
if defined PYEXE goto :have_python

echo  Python 3.11 or newer was not found on this PC.
echo  whispa needs it, and can install it for you now
echo  (Python %PYVER%, just for your user account - no admin needed).
echo.
choice /C YN /N /M "  Install Python %PYVER% now?  [Y/N] "
if errorlevel 2 goto :no_python
echo.
call :install_python
call :find_python
if defined PYEXE goto :have_python
goto :install_failed

:have_python
for /f "delims=" %%v in ('"%PYEXE%" %PYARG% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYFULL=%%v"
echo  Python !PYFULL! found.
echo.

REM ---------- 2. virtual environment ------------------------------------------
echo  Creating the virtual environment...
if exist "%ROOT%.venv" (
  echo  ^(an existing .venv was found and will be reused^)
) else (
  "%PYEXE%" %PYARG% -m venv "%ROOT%.venv"
  if errorlevel 1 goto :venv_failed
)
if not exist "%ROOT%.venv\Scripts\python.exe" goto :venv_failed

REM ---------- 3. dependencies -------------------------------------------------
echo.
echo  Installing dependencies. This downloads about 200 MB and only happens once.
echo.
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 goto :deps_failed

REM ---------- 4. done ---------------------------------------------------------
echo.
echo  ==========================================
echo    Setup complete.
echo  ==========================================
echo.
echo   Start whispa      :  whispa.bat
echo   Tap or hold F9 to dictate.
echo.
echo   Debugging         :  whispa-console.bat  (or the tray's Settings menu)
echo   Start at login    :  tray icon - Settings - Start with Windows
echo.
echo   The speech model (about 150 MB) downloads the first time you run it.
echo.
pause
exit /b 0

REM ===========================================================================
REM Helpers
REM ===========================================================================

:find_python
REM Sets PYEXE/PYARG to the first interpreter that is 3.11 or newer.
REM The explicit paths at the end matter: straight after we install Python,
REM this window's PATH is still the old one, so "python" would not be found.
set "PYEXE="
set "PYARG="
call :test_py "py" "-3.12"
if defined PYEXE exit /b
call :test_py "py" "-3.13"
if defined PYEXE exit /b
call :test_py "py" "-3.11"
if defined PYEXE exit /b
call :test_py "py" "-3"
if defined PYEXE exit /b
call :test_py "python" ""
if defined PYEXE exit /b
call :test_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" ""
if defined PYEXE exit /b
call :test_py "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" ""
if defined PYEXE exit /b
call :test_py "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" ""
exit /b

:test_py
set "CAND=%~1"
set "CARG=%~2"
"%CAND%" %CARG% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 exit /b
set "PYEXE=%CAND%"
set "PYARG=%CARG%"
exit /b

:install_python
REM Prefer winget when it is available: it handles the download, the signature
REM check and the upgrade path. Fall back to python.org otherwise.
where winget >nul 2>&1
if errorlevel 1 goto :download_python
echo  Installing via winget...
winget install --id Python.Python.3.12 -e --source winget ^
  --accept-package-agreements --accept-source-agreements --disable-interactivity
call :find_python
if defined PYEXE exit /b
echo  winget did not produce a usable Python; falling back to python.org.
echo.

:download_python
set "ARCHSUF=-amd64"
if /I "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCHSUF=-arm64"
if /I "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 set "ARCHSUF="
set "PYFILE=python-%PYVER%%ARCHSUF%.exe"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/%PYFILE%"
set "PYDL=%TEMP%\%PYFILE%"

echo  Downloading %PYFILE% ...
if exist "%PYDL%" del /q "%PYDL%" >nul 2>&1
where curl >nul 2>&1
if errorlevel 1 goto :download_with_powershell
curl -L --fail -o "%PYDL%" "%PYURL%"
goto :after_download

:download_with_powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYDL%'"

:after_download
if not exist "%PYDL%" goto :download_failed

echo  Running the Python installer. Please accept any prompt it shows.
REM PrependPath puts it on PATH for future windows; InstallAllUsers=0 keeps it
REM per-user so no administrator rights are needed.
start /wait "" "%PYDL%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del /q "%PYDL%" >nul 2>&1
echo  Python installer finished.
echo.
exit /b

REM ---------------------------------------------------------------------------
REM Failure paths
REM ---------------------------------------------------------------------------

:no_python
echo.
echo  Setup stopped. Install Python 3.11 or newer from:
echo      https://www.python.org/downloads/windows/
echo  and be sure to tick "Add Python to PATH", then run install.bat again.
echo.
pause
exit /b 1

:download_failed
echo.
echo  Could not download the Python installer.
echo  Check your internet connection, or install Python manually from:
echo      https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:install_failed
echo.
echo  Python was installed but this window still cannot see it.
echo  Close this window, open a new one, and run install.bat again -
echo  that is usually all it takes.
echo.
pause
exit /b 1

:venv_failed
echo.
echo  Could not create the virtual environment in:
echo      %ROOT%.venv
echo  If that folder exists but is broken, delete it and run install.bat again.
echo.
pause
exit /b 1

:deps_failed
echo.
echo  Dependency installation failed - see the messages above.
echo  A proxy or antivirus blocking pip is the usual cause.
echo.
pause
exit /b 1
