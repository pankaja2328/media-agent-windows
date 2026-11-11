@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
echo ========================================
echo        Media Agent Secure Setup
echo ========================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer from python.org.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating private Python environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)
echo Installing required packages...
.venv\Scripts\python.exe -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error
echo.
.venv\Scripts\python.exe setup_config.py
if errorlevel 1 goto :error
echo.
echo Setup finished successfully.
pause
exit /b 0
:error
echo.
echo Setup did not finish. Review the message above, then run setup.bat again.
pause
exit /b 1
