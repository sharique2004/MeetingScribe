@echo off
cd /d "%~dp0"
title MeetingScribe
set "VENVPY=venv\Scripts\python.exe"
rem When the app lives in a OneDrive-synced folder, setup.bat puts the venv
rem in %LOCALAPPDATA% instead so OneDrive doesn't sync thousands of files.
if not exist "%VENVPY%" set "VENVPY=%LOCALAPPDATA%\MeetingScribe\venv\Scripts\python.exe"
if not exist "%VENVPY%" (
    echo MeetingScribe is not set up on this PC yet - run setup.bat first.
    pause
    exit /b 1
)
echo Starting MeetingScribe... a browser tab will open shortly.
"%VENVPY%" app.py
pause
