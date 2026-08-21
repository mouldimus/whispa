@echo off
REM Normal launch: no console window, collapses straight to the tray.
REM The on-screen pill shows loading progress while the model warms up.
start "" "%~dp0.venv\Scripts\pythonw.exe" -m whispa %*
