@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The project virtual environment is missing.
  echo Create it with: py -m venv .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "futures_paper\dashboard.py"
pause
