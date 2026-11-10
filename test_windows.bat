@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
"backend\venv\Scripts\python.exe" -m unittest discover -s tests -v
pause
