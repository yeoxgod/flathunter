@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title flat-hunter bot (close this window to stop)

if not exist ".venv\Scripts\python.exe" (
  echo Not installed yet. Run setup.bat first.
  pause
  exit /b 1
)

:loop
if exist "data\needs_pip" (
  echo Installing updated packages...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  ".venv\Scripts\python.exe" -m playwright install chromium
  del "data\needs_pip"
)
".venv\Scripts\python.exe" -m flathunter run
if %errorlevel%==3 (
  echo Bot is not configured. Run setup.bat.
  pause
  exit /b 1
)
if %errorlevel%==4 (
  echo Restarting after update...
  goto loop
)
echo Bot stopped. Restarting in 15 seconds. Close the window to stop.
timeout /t 15 >nul
goto loop
