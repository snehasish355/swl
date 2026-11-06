@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restart-SWL-Band-Finder.ps1"
if errorlevel 1 (
  echo.
  echo Could not restart the SWL helper. Check the message above.
  pause
  exit /b 1
)
python "%~dp0server.py"
if errorlevel 1 (
  echo.
  echo Could not start the helper. Install Python 3 and make sure "python" is on PATH.
  pause
)

