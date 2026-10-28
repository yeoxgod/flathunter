@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title flat-hunter setup

where py >nul 2>nul
if %errorlevel%==0 goto have_python
echo Python not found. Installing Python 3.12 with winget...
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto python_failed
echo.
echo Python installed. Close this window and run setup.bat again.
pause
exit /b 0

:python_failed
echo.
echo Could not install Python automatically.
echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then run setup.bat again.
pause
exit /b 1

:have_python
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
)
if not exist ".venv\Scripts\python.exe" goto venv_failed

echo Installing packages, this takes a minute...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto pip_failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto pip_failed

".venv\Scripts\python.exe" -m flathunter setup
if errorlevel 1 goto wizard_failed

call "%~dp0start.bat"
exit /b 0

:venv_failed
echo Could not create the virtual environment.
pause
exit /b 1

:pip_failed
echo Package installation failed. Check the internet connection and run setup.bat again.
pause
exit /b 1

:wizard_failed
echo Setup was not finished. Run setup.bat again.
pause
exit /b 1
