@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title flat-hunter update
".venv\Scripts\python.exe" -m flathunter update
if exist "data\needs_pip" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  ".venv\Scripts\python.exe" -m playwright install chromium
  del "data\needs_pip"
)
echo Restart the bot: close its window and open start.bat.
pause
