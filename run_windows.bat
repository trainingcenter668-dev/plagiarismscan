@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\venv\Scripts\python.exe" (
  echo Project is not set up. Run setup_windows.bat first.
  pause
  exit /b 1
)
if not exist "backend\.env" (
  copy "backend\.env.example" "backend\.env" >nul
  echo Created backend\.env. Add your web search API key if you want website discovery.
)
cd backend
"venv\Scripts\python.exe" app.py
