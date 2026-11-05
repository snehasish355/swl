@echo off
setlocal
cd /d "%~dp0"
python server.py
if errorlevel 1 (
  echo.
  echo Could not start the helper. Install Python 3 and make sure "python" is on PATH.
  pause
)

