@echo off
:: Arena → Odoo Sync — Windows Setup & Run
:: Double-click this file or run: setup.bat
:: To stop: close the terminal window, or run: setup.bat stop

setlocal
cd /d "%~dp0"

if "%1"=="stop" (
    if exist app.pid (
        set /p PID=<app.pid
        taskkill /PID %PID% /F >nul 2>&1
        del app.pid
        echo Stopped.
    ) else (
        echo No running instance found.
    )
    exit /b 0
)

:: ── Find Python ────────────────────────────────────────
set PYTHON=
where python >nul 2>&1 && (
    python -c "import sys; assert sys.version_info >= (3,10)" 2>nul && set PYTHON=python
)
if "%PYTHON%"=="" (
    where python3 >nul 2>&1 && (
        python3 -c "import sys; assert sys.version_info >= (3,10)" 2>nul && set PYTHON=python3
    )
)
if "%PYTHON%"=="" (
    echo ERROR: Python 3.10+ not found.
    echo Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

%PYTHON% --version

:: ── Virtual environment ────────────────────────────────
if not exist venv (
    echo Creating virtual environment...
    %PYTHON% -m venv venv
)

call venv\Scripts\activate.bat

:: ── Install dependencies ───────────────────────────────
echo Installing dependencies...
pip install -q -r requirements.txt

:: ── Kill old instance ──────────────────────────────────
if exist app.pid (
    set /p OLD_PID=<app.pid
    taskkill /PID %OLD_PID% /F >nul 2>&1
    del app.pid
)

:: ── Start ──────────────────────────────────────────────
set PRODUCTION=1
echo.
echo   Starting Arena-Odoo Sync (production mode)...
echo   Dashboard: http://localhost:5000
echo   Close this window to stop.
echo.

python main.py
