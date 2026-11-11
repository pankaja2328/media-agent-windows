@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
if not exist "credentials.bin" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe run_agent.py
