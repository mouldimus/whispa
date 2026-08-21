@echo off
REM Start whispa with a console window so you can see what it is doing.
REM Swap python.exe for pythonw.exe below to run it silently in the tray.
"%~dp0.venv\Scripts\python.exe" -m whispa %*
