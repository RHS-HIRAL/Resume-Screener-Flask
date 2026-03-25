@echo off
setlocal enabledelayedexpansion
title Project Environment Setup

:: --- 1. REQUEST ADMIN PRIVILEGES ---
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrative privileges...
    powershell -Command "Start-Process '%~0' -Verb RunAs"
    exit /b
)

echo ====================================================
echo          PROJECT SETUP - DATABASE ^& TOOLS
echo ====================================================

:: --- 2. CONFIGURE DATABASE PASSWORD ---
echo.
set /p DB_PASS="Enter a secure password for your local Database: "
echo Updating .env file...
if not exist ".env" (
    echo [ERROR] .env file is missing!
    echo Please ensure you have copied the .env file shared by the developer into this folder.
    pause
    exit /b
)
(for /f "delims=" %%i in (.env) do (
    set "line=%%i"
    if "!line:~0,12!"=="PG_PASSWORD=" (
        echo PG_PASSWORD=%DB_PASS%
    ) else (
        echo !line!
    )
)) > .env.tmp
move /y .env.tmp .env >nul

:: --- 3. CHECK ^& INSTALL PYTHON ---
python --version >nul 2>&1
if %errorLevel% neq 0 (
    echo Python not found. Downloading installer...
    curl -L -o py_installer.exe https://www.python.org/ftp/python/3.11.5/python-3.11.5-amd64.exe
    echo Installing Python silently...
    start /wait py_installer.exe /quiet InstallAllUsers=1 PrependPath=1
    del py_installer.exe
) else (echo [OK] Python is already installed.)

:: --- 4. CHECK ^& INSTALL POSTGRESQL ---
sc query postgresql-x64-15 >nul 2>&1
if %errorLevel% neq 0 (
    echo PostgreSQL not found. Downloading...
    curl -L -o pg_installer.exe https://get.enterprisedb.com/postgresql/postgresql-15.4-1-windows-x64.exe
    echo Installing PostgreSQL (this may take a minute)...
    start /wait pg_installer.exe --mode unattended --unattendedmodeui none --superpassword "%DB_PASS%" --servicepassword "%DB_PASS%"
    del pg_installer.exe
) else (echo [OK] PostgreSQL is already installed.)

:: --- 4.1 CREATE DATABASE ---
echo Ensuring database 'resume_screener' exists...
set "PGPASSWORD=%DB_PASS%"
"%ProgramFiles%\PostgreSQL\15\bin\psql" -U postgres -c "CREATE DATABASE resume_screener;" 2>nul
if %errorLevel% eq 0 (
    echo Database 'resume_screener' created successfully.
) else (
    echo Database 'resume_screener' already exists or could not be created.
)
set "PGPASSWORD="

:: --- 5. CHECK ^& INSTALL TESSERACT-OCR ---
if not exist "C:\Program Files\Tesseract-OCR" (
    echo Tesseract-OCR not found. Downloading...
    curl -L -o tess_installer.exe https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.1.20230401.exe
    echo Installing Tesseract silently...
    start /wait tess_installer.exe /S
    setx /M PATH "%PATH%;C:\Program Files\Tesseract-OCR"
    del tess_installer.exe
) else (echo [OK] Tesseract-OCR is already installed.)

:: --- 6. CHECK PROJECT STRUCTURE ---
echo Checking project files...
if not exist "requirements.txt" (echo [ERROR] requirements.txt missing! & pause & exit)
if not exist "run.py" (echo [ERROR] Main application file (run.py) missing! & pause & exit)

:: --- 7. VIRTUAL ENVIRONMENT ^& REQUIREMENTS ---
echo Setting up Virtual Environment...
if not exist ".venv" (
    python -m venv .venv
)
call .venv\Scripts\activate
echo Installing dependencies...
pip install --upgrade pip >nul
pip install -r requirements.txt

echo.
echo ====================================================
echo SETUP COMPLETE! You can now run launch.bat.
echo ====================================================
pause