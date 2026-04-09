@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  SigmoCatClash - Windows Launcher
::  Detects Python, creates .venv, installs deps,
::  validates .env, then runs the bot.
::  No interactive pauses - exits with error code on failure.
:: ============================================================

set "ROOT=%~dp0"
set "LOG_DIR=%ROOT%logs"
set "VENV=%ROOT%.venv"
set "LOGFILE=%LOG_DIR%\startup.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo.
echo  ==============================================
echo   SigmoCatClash - Discord Bot Launcher
echo  ==============================================
echo.

:: ── Python detection ──────────────────────────────────────────
:: Try py launcher first, then fall back to python
set "PY="

py -3 --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PY=py -3"
    goto :have_python
)

python --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PY=python"
    goto :have_python
)

echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
exit /b 1

:have_python
for /f "tokens=*" %%V in ('!PY! --version 2^>^&1') do echo [INFO]  Python: %%V

:: ── Virtual environment ───────────────────────────────────────
if not exist "%VENV%\Scripts\activate.bat" (
    echo [SETUP] Creating virtual environment...
    !PY! -m venv "%VENV%"
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment.
        echo         Try running:  !PY! -m venv "%VENV%"
        exit /b 1
    )
    echo [OK]    Virtual environment created.
)

echo [INFO]  Activating virtual environment...
call "%VENV%\Scripts\activate.bat"

:: ── Dependencies ──────────────────────────────────────────────
echo [SETUP] Upgrading pip...
python -m pip install --upgrade pip --quiet

echo [SETUP] Installing dependencies...
pip install -r "%ROOT%requirements.txt"
if !errorlevel! neq 0 (
    echo [ERROR] pip install failed.
    exit /b 1
)
echo [OK]    Dependencies installed.

:: ── Token check — env var takes priority (Railway / CI), .env for local ───────
if defined DISCORD_TOKEN (
    echo [OK]    DISCORD_TOKEN found in environment.
    goto :token_ok
)

if not exist "%ROOT%.env" (
    echo [ERROR] .env not found and DISCORD_TOKEN env var is not set.
    echo         Copy .env.example to .env and set DISCORD_TOKEN.
    exit /b 1
)

findstr /r "^DISCORD_TOKEN=." "%ROOT%.env" >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] DISCORD_TOKEN is empty in .env!
    echo         Open .env and paste your bot token.
    exit /b 1
)
echo [OK]    DISCORD_TOKEN found in .env.

:token_ok

:: ── Launch ────────────────────────────────────────────────────
echo.
echo [INFO]  Starting SigmoCatClash... ^(Ctrl+C to stop^)
echo         Log file: logs\bot.log
echo  ==============================================
echo.

python bot.py
if !errorlevel! neq 0 (
    echo.
    echo [ERROR] bot.py exited with code !errorlevel!
    echo         Check logs\bot.log for details.
    exit /b !errorlevel!
)

endlocal
