@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo PlagiaScan - Windows setup
echo ==========================================
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.10 or newer and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist "backend\venv\Scripts\python.exe" (
  py -3 -m venv backend\venv
  if errorlevel 1 goto failed
)
call "backend\venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto failed
python -m pip install -r backend\requirements.txt
if errorlevel 1 goto failed
if not exist "backend\.env" copy "backend\.env.example" "backend\.env" >nul
if not exist "backend\.env" goto failed
echo.
echo Setup completed.
echo Edit backend\.env to add your Brave or Google Search API credentials for website search.
echo OpenAlex and Crossref academic discovery are attempted without those credentials.
pause
exit /b 0
:failed
echo.
echo Setup failed. Check the error above and confirm Python and internet access.
pause
exit /b 1
