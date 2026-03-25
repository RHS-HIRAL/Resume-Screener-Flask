@echo off
title Screener AI — Launcher
chcp 65001 >nul 2>&1

:: Fix Working Directory & Enable Delayed Expansion
cd /d "%~dp0"
setlocal EnableDelayedExpansion

echo.
echo  ╔══════════════════════════════════════════════════════════════╗
echo  ║                  SCREENER AI — LAUNCHER                      ║
echo  ║  Verifying environment and starting application server...    ║
echo  ╚══════════════════════════════════════════════════════════════╝
echo.

:: ── 1. CHECK PROJECT STRUCTURE & .env ──────────────────────────────
echo [INFO] Verifying project files...
if not exist "run.py" (
    echo [ERROR] run.py not found! Ensure this script is in the project root.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env file is missing! Please run setup.bat first.
    pause
    exit /b 1
)
echo [OK] Project files and .env found.

:: ── 2. CHECK VIRTUAL ENVIRONMENT ───────────────────────────────────
echo [INFO] Checking virtual environment...
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found at .venv! Please run setup.bat.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python executable missing from .venv! Virtual environment may be corrupted.
    pause
    exit /b 1
)
echo [OK] Virtual environment is ready.

:: ── 3. CHECK POSTGRESQL & DATABASE ─────────────────────────────────
echo [INFO] Checking PostgreSQL and Database...
set "PG_BIN="
if exist "%ProgramFiles%\PostgreSQL" (
    for /d %%v in ("%ProgramFiles%\PostgreSQL\*") do (
        if exist "%%~v\bin\psql.exe" (
            set "PG_BIN=%%~v\bin"
        )
    )
)

if not defined PG_BIN (
    where psql >nul 2>&1
    if !errorLevel! equ 0 set "PG_BIN=PATH"
)

if not defined PG_BIN (
    echo [WARNING] PostgreSQL psql.exe not found locally. Skipping DB check.
) else (
    :: Extract DB details from .env
    set "PG_PORT=5432"
    set "PG_DATABASE=resume_screener"
    set "PG_PASS="
    for /f "tokens=1,2 delims==" %%A in (.env) do (
        if "%%A"=="PG_PORT" set "PG_PORT=%%B"
        if "%%A"=="PG_DATABASE" set "PG_DATABASE=%%B"
        if "%%A"=="PG_PASSWORD" set "PG_PASS=%%B"
    )

    set "PGPASSWORD=!PG_PASS!"
    if "!PG_BIN!"=="PATH" (
        psql -U postgres -p !PG_PORT! -lqt 2>nul | findstr /C:"!PG_DATABASE!" >nul
    ) else (
        "!PG_BIN!\psql.exe" -U postgres -p !PG_PORT! -lqt 2>nul | findstr /C:"!PG_DATABASE!" >nul
    )

    if !errorLevel! neq 0 (
        echo [WARNING] Database '!PG_DATABASE!' not found or PostgreSQL service is not running yet.
    ) else (
        echo [OK] Database '!PG_DATABASE!' is active.
    )
    set "PGPASSWORD="
)

:: ── 4. CHECK TESSERACT OCR ─────────────────────────────────────────
echo [INFO] Checking Tesseract OCR...
set "TESS_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe"
if not exist "!TESS_PATH!" (
    where tesseract >nul 2>&1
    if !errorLevel! neq 0 (
        echo [WARNING] Tesseract OCR not found. Resume parsing features may fail.
    ) else (
        echo [OK] Tesseract found in system PATH.
    )
) else (
    echo [OK] Tesseract OCR found.
)

:: ── 5. LAUNCH APPLICATION ──────────────────────────────────────────
echo.
echo ==================================================================
echo   Starting Screener AI server...
echo   Press CTRL+C in this window to stop the application safely.
echo ==================================================================
echo.

call ".venv\Scripts\activate.bat"

:: Run the python app
python run.py

if !errorLevel! neq 0 (
    echo.
    echo ──────────────────────────────────────────────────────────────────
    echo  [ERROR] The application crashed or was terminated abruptly.
    echo  Please check the error messages above to diagnose the issue.
    echo ──────────────────────────────────────────────────────────────────
    pause
)

:: Deactivate virtual environment when exiting gracefully
call deactivate >nul 2>&1