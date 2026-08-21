@echo off
REM Troubleshooting launch: keeps a console window and logs to it.
REM Use this if whispa.bat appears to do nothing.
"%~dp0.venv\Scripts\python.exe" -m whispa --console %*
pause
