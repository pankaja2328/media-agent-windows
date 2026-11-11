@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" goto :setup
if not exist "credentials.bin" goto :setup
.venv\Scripts\python.exe run_web.py
if errorlevel 1 (
  echo.
  echo Media Agent stopped with an error. Review the message above.
  pause
)
exit /b
:setup
echo First-time setup is required. Starting setup.bat...
call setup.bat
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe run_web.py
