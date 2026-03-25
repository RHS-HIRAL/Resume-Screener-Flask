@echo off
title Screener AI — Environment Setup
chcp 65001 >nul 2>&1

:: ── 0. REQUEST ADMIN PRIVILEGES ────────────────────────────────────
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Requesting administrative privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/k', '\"%~f0\"' -Verb RunAs"
    exit /b
)

:: ── Fix Working Directory & Safely Enable Delayed Expansion ────────
cd /d "%~dp0"
setlocal EnableDelayedExpansion

:: ═══════════════════════════════════════════════════════════════════
::  SCREENER AI — FULL ENVIRONMENT SETUP
:: ═══════════════════════════════════════════════════════════════════

:: Ensure installers directory exists to prevent download crashes
if not exist "%~dp0installers" mkdir "%~dp0installers" >nul 2>&1

:: ── Set up logging ──────────────────────────────────────────────────
set "LOG_FILE=%~dp0setup_log.txt"
echo. > "%LOG_FILE%"
echo [%date% %time%] Setup started >> "%LOG_FILE%"

echo.
echo  ╔══════════════════════════════════════════════════════════════╗
echo  ║            SCREENER AI — ENVIRONMENT SETUP                   ║
echo  ║  Python 3.10.11 · PostgreSQL · Tesseract · Dependencies      ║
echo  ╚══════════════════════════════════════════════════════════════╝
echo.

:: ── 1. VERIFY .env EXISTS ───────────────────────────────────────────
echo [STEP 1/9] Checking .env configuration file...
echo [%date% %time%] STEP 1: Checking .env >> "%LOG_FILE%"
if not exist "%~dp0.env" (
    echo.
    echo  ┌─────────────────────────────────────────────────────────────┐
    echo  │  ERROR: .env file not found in the project folder.          │
    echo  │  Please copy the .env template shared by your team lead     │
    echo  │  into:  %~dp0                                               │
    echo  └─────────────────────────────────────────────────────────────┘
    echo.
    pause
    exit /b 1
)
echo [OK] .env file found.
echo.

:: ── 2. CHECK INTERNET CONNECTIVITY ─────────────────────────────────
echo [STEP 2/9] Checking internet connectivity...
echo [%date% %time%] STEP 2: Checking internet >> "%LOG_FILE%"
ping -n 1 8.8.8.8 >nul 2>&1
if %errorLevel% neq 0 (
    echo [WARNING] No internet detected. Offline components will be skipped.
    set "OFFLINE=1"
) else (
    echo [OK] Internet connection available.
    set "OFFLINE=0"
)
echo.

:: ── 3. INSTALL PYTHON 3.10.11 ──────────────────────────────────────
echo [STEP 3/9] Checking Python 3.10.11...
echo [%date% %time%] STEP 3: Checking Python >> "%LOG_FILE%"

set "PYTHON_OK=0"
where python >nul 2>&1
if %errorLevel% equ 0 (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
    echo !PY_VER! | findstr /C:"3.10.11" >nul
    if !errorLevel! equ 0 (
        set "PYTHON_OK=1"
        echo [OK] Python 3.10.11 is already installed.
    ) else (
        echo [INFO] Found !PY_VER! — project requires 3.10.11 specifically.
    )
)

if "!PYTHON_OK!"=="0" (
    where py >nul 2>&1
    if !errorLevel! equ 0 (
        py -3.10 --version 2>nul | findstr /C:"3.10.11" >nul
        if !errorLevel! equ 0 (
            set "PYTHON_OK=1"
            echo [OK] Python 3.10.11 found via py launcher.
        )
    )
)

if "!PYTHON_OK!"=="0" (
    set "PY_INSTALLER=%~dp0installers\python-3.10.11-amd64.exe"

    if "!OFFLINE!"=="1" (
        if not exist "!PY_INSTALLER!" (
            echo [ERROR] Python not found and no internet.
            pause
            exit /b 1
        )
    )

    if not exist "!PY_INSTALLER!" (
        echo [INFO] Downloading Python 3.10.11...
        powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe' -OutFile \"!PY_INSTALLER!\" -UseBasicParsing" 2>&1
        if not exist "!PY_INSTALLER!" (
            echo [ERROR] Download failed.
            pause
            exit /b 1
        )
    )

    echo [INFO] Installing Python 3.10.11 silently...
    "!PY_INSTALLER!" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_launcher=1
    call :RefreshPath
)
echo.

set "PYTHON_BIN=python"
python --version 2>nul | findstr /C:"3.10" >nul
if %errorLevel% neq 0 (
    py -3.10 --version >nul 2>&1
    if !errorLevel! equ 0 (
        set "PYTHON_BIN=py -3.10"
    )
)

:: ── 4. INSTALL POSTGRESQL ───────────────────────────────────────────
echo [STEP 4/9] Checking for PostgreSQL...
echo [%date% %time%] STEP 4: Checking PostgreSQL >> "%LOG_FILE%"

set "PG_FOUND=0"
set "PG_BIN="

:: Dynamically check for ANY version of PostgreSQL installed in Program Files
if exist "%ProgramFiles%\PostgreSQL" (
    for /d %%v in ("%ProgramFiles%\PostgreSQL\*") do (
        if exist "%%~v\bin\psql.exe" (
            set "PG_BIN=%%~v\bin"
            set "PG_FOUND=1"
        )
    )
)

if "!PG_FOUND!"=="1" (
    echo [OK] PostgreSQL found at !PG_BIN!
    goto SkipPGInstall
)

set "PG_INSTALLER=%~dp0installers\postgresql-15-windows-x64.exe"

if "!OFFLINE!"=="1" (
    if not exist "!PG_INSTALLER!" (
        echo [ERROR] PostgreSQL not found and no internet.
        pause
        exit /b 1
    )
)

if not exist "!PG_INSTALLER!" (
    echo [INFO] Downloading PostgreSQL 15...
    powershell -Command "Invoke-WebRequest -Uri 'https://get.enterprisedb.com/postgresql/postgresql-15.12-1-windows-x64.exe' -OutFile \"!PG_INSTALLER!\" -UseBasicParsing" 2>&1
)

if not exist "!PG_INSTALLER!" (
    echo [ERROR] Failed to download PostgreSQL installer.
    pause
    exit /b 1
)

echo.
set /p "PG_PASS=Enter a password for PostgreSQL (postgres): "
echo.
echo [INFO] Installing PostgreSQL 15 silently...

"!PG_INSTALLER!" --mode unattended --unattendedmodeui none --superpassword "!PG_PASS!" --servicepassword "!PG_PASS!" --servicename postgresql-x64-15 --serverport 5432

call :UpdateEnvPassword "!PG_PASS!"
call :RefreshPath

:: Find the newly installed bin folder
if exist "%ProgramFiles%\PostgreSQL" (
    for /d %%v in ("%ProgramFiles%\PostgreSQL\*") do (
        if exist "%%~v\bin\psql.exe" (
            set "PG_BIN=%%~v\bin"
        )
    )
)

:SkipPGInstall
if defined PG_BIN set "PATH=!PG_BIN!;!PATH!"
echo.

:: ── 5. CREATE DATABASE ──────────────────────────────────────────────
echo [STEP 5/9] Setting up the database...
echo [%date% %time%] STEP 5: Setting up Database >> "%LOG_FILE%"

if not defined PG_PASS (
    for /f "tokens=2 delims==" %%p in ('findstr /b "PG_PASSWORD" "%~dp0.env"') do set "PG_PASS=%%p"
)

set "PG_PORT=5432"
for /f "tokens=2 delims==" %%p in ('findstr /b "PG_PORT" "%~dp0.env"') do set "PG_PORT=%%p"
set "PG_DATABASE=resume_screener"
for /f "tokens=2 delims==" %%p in ('findstr /b "PG_DATABASE" "%~dp0.env"') do set "PG_DATABASE=%%p"

set /a "PG_RETRIES=0"
:WaitForPG
set "PGPASSWORD=!PG_PASS!"
if defined PG_BIN (
    "!PG_BIN!\psql.exe" -U postgres -p !PG_PORT! -c "SELECT 1;" >nul 2>&1
) else (
    psql -U postgres -p !PG_PORT! -c "SELECT 1;" >nul 2>&1
)
if %errorLevel% neq 0 (
    set /a "PG_RETRIES+=1"
    if !PG_RETRIES! lss 5 (
        echo [INFO] Waiting for PostgreSQL to start... ^(!PG_RETRIES!/5^)
        timeout /t 3 >nul
        goto WaitForPG
    ) else (
        echo [WARNING] Cannot connect to PostgreSQL.
        set "PGPASSWORD="
        goto SkipDB
    )
)

if defined PG_BIN (
    "!PG_BIN!\psql.exe" -U postgres -p !PG_PORT! -lqt 2>nul | findstr /C:"!PG_DATABASE!" >nul
) else (
    psql -U postgres -p !PG_PORT! -lqt 2>nul | findstr /C:"!PG_DATABASE!" >nul
)

if %errorLevel% equ 0 (
    echo [OK] Database '!PG_DATABASE!' already exists.
    goto SkipDB
)

echo [INFO] Creating database '!PG_DATABASE!'...
if defined PG_BIN (
    "!PG_BIN!\psql.exe" -U postgres -p !PG_PORT! -c "CREATE DATABASE !PG_DATABASE! ENCODING 'UTF8';" >nul 2>&1
) else (
    psql -U postgres -p !PG_PORT! -c "CREATE DATABASE !PG_DATABASE! ENCODING 'UTF8';" >nul 2>&1
)

:SkipDB
set "PGPASSWORD="
echo.

:: ── 6. INSTALL TESSERACT OCR ────────────────────────────────────────
echo [STEP 6/9] Checking Tesseract OCR...
echo [%date% %time%] STEP 6: Checking Tesseract >> "%LOG_FILE%"
set "TESS_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe"

if exist "%TESS_PATH%" (
    echo [OK] Tesseract OCR is already installed.
    goto SkipTessInstall
)

set "TESS_INSTALLER=%~dp0installers\tesseract-ocr-w64-setup.exe"

if "%OFFLINE%"=="1" (
    echo [WARNING] Tesseract skipped ^(offline^).
    goto SkipTessInstall
)

if not exist "%TESS_INSTALLER%" (
    echo [INFO] Downloading Tesseract OCR...
    powershell -Command "Invoke-WebRequest -Uri 'https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.4.20240503.exe' -OutFile \"%TESS_INSTALLER%\" -UseBasicParsing" 2>&1
)

if not exist "%TESS_INSTALLER%" (
    echo [ERROR] Failed to download Tesseract.
    goto SkipTessInstall
)

echo [INFO] Installing Tesseract OCR silently...
"%TESS_INSTALLER%" /S
setx /M PATH "%PATH%;C:\Program Files\Tesseract-OCR" >nul 2>&1
set "PATH=%PATH%;C:\Program Files\Tesseract-OCR"

:SkipTessInstall
echo.

:: ── 7. VERIFY REQUIRED PROJECT FILES ───────────────────────────────
echo [STEP 7/9] Verifying project structure...
echo [%date% %time%] STEP 7: Verifying structure >> "%LOG_FILE%"
set "MISSING=0"
for %%f in (run.py requirements.txt config.py) do (
    if not exist "%~dp0%%f" set "MISSING=1"
)
if "%MISSING%"=="1" (
    echo [ERROR] Project structure is incomplete.
    pause
    exit /b 1
)
echo [OK] Project structure verified.
echo.

:: ── 8. CREATE VIRTUAL ENVIRONMENT ──────────────────────────────────
echo [STEP 8/9] Setting up Python virtual environment...
echo [%date% %time%] STEP 8: Setting up venv >> "%LOG_FILE%"

if exist "%~dp0.venv\Scripts\python.exe" (
    echo [OK] Virtual environment exists.
    goto SkipVenv
)

echo [INFO] Creating .venv...
%PYTHON_BIN% -m venv "%~dp0.venv"

:SkipVenv
echo.

:: ── 9. INSTALL PYTHON DEPENDENCIES ─────────────────────────────────
echo [STEP 9/9] Installing Python dependencies...
echo [%date% %time%] STEP 9: Installing dependencies >> "%LOG_FILE%"

call "%~dp0.venv\Scripts\activate.bat"
python -m pip install --upgrade pip wheel setuptools --quiet
echo [INFO] Installing project requirements...
python -m pip install -r "%~dp0requirements.txt"
if %errorLevel% neq 0 (
    echo [ERROR] Failed to install packages.
    pause
    exit /b 1
)

echo.
echo [%date% %time%] Setup completed successfully >> "%LOG_FILE%"

:: Offer to generate encryption key
echo Would you like to generate an HR_ENCRYPTION_KEY now? (Y/N)
set /p "GEN_KEY="
if /i not "%GEN_KEY%"=="Y" goto EndSetup

call "%~dp0.venv\Scripts\activate.bat"
set "PY_CMD=from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
for /f "delims=" %%k in ('python -c "%PY_CMD%"') do set "ENC_KEY=%%k"

echo.
echo Generated key: %ENC_KEY%
echo.
findstr /C:"HR_ENCRYPTION_KEY=" "%~dp0.env" >nul 2>&1
if %errorLevel% equ 0 (
    call :UpdateEnvKey "%ENC_KEY%"
) else (
    echo HR_ENCRYPTION_KEY=%ENC_KEY% >> "%~dp0.env"
)
echo [OK] HR_ENCRYPTION_KEY added to .env

:EndSetup
echo.
pause
exit /b 0


:: ════════════════════════════════════════════════════════════════════
::  HELPER SUBROUTINES
:: ════════════════════════════════════════════════════════════════════
:UpdateEnvPassword
set "NEW_PASS=%~1"
set "TEMP_ENV=%~dp0.env.tmp"
(for /f "usebackq delims=" %%i in ("%~dp0.env") do (
    set "line=%%i"
    if "!line:~0,12!"=="PG_PASSWORD=" (echo PG_PASSWORD=!NEW_PASS!) else (echo !line!)
)) > "!TEMP_ENV!"
move /y "!TEMP_ENV!" "%~dp0.env" >nul
goto :eof

:UpdateEnvKey
set "NEW_KEY=%~1"
set "TEMP_ENV=%~dp0.env.tmp"
(for /f "usebackq delims=" %%i in ("%~dp0.env") do (
    set "line=%%i"
    if "!line:~0,18!"=="HR_ENCRYPTION_KEY=" (echo HR_ENCRYPTION_KEY=!NEW_KEY!) else (echo !line!)
)) > "!TEMP_ENV!"
move /y "!TEMP_ENV!" "%~dp0.env" >nul
goto :eof

:RefreshPath
for /f "skip=2 tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH 2^>nul') do set "SYS_PATH=%%b"
for /f "skip=2 tokens=2*" %%a in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "USER_PATH=%%b"
set "PATH=%SYS_PATH%;%USER_PATH%"
goto :eof