@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title flat-hunter: choose AI
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m flathunter ai
pause
