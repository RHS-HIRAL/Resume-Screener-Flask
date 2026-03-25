@echo off
title Launching Application
setlocal

:: --- 1. CHECK FOR VENV ---
if not exist ".venv" (
    echo [ERROR] Setup not detected. Please run setup.bat first.
    pause
    exit /b
)

:: --- 2. CHECK PROJECT FILES ---
if not exist ".env" (
    echo [ERROR] .env file is missing.
    pause
    exit /b
)

:: --- 3. ACTIVATE ^& LAUNCH ---
echo Starting Application...
call .venv\Scripts\activate

:: Start browser after a 3-second delay to allow server to boot
start /b cmd /c "timeout /t 3 >nul && start http://127.0.0.1:5001"

:: Start the Python App (Update 'app.py' to your actual filename)
echo Server is running. Close this window to stop the app.
python run.py

pause
 