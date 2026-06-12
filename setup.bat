@echo off
cd /d "%~dp0"
title MeetingScribe setup
echo ============================================
echo   MeetingScribe - one-time setup
echo   (needs internet, downloads ~2 GB of
echo    Python packages; takes a few minutes)
echo ============================================
echo.

rem Keep the venv out of OneDrive-synced folders (it has ~20k small files).
set "VENVDIR=venv"
echo %~dp0 | find /i "OneDrive" >nul && set "VENVDIR=%LOCALAPPDATA%\MeetingScribe\venv"

if exist "%VENVDIR%\Scripts\python.exe" (
    echo Existing environment found - updating packages...
    goto :install
)

rem ---- find a suitable Python (3.10 - 3.12) ----
set PY=
for %%v in (3.11 3.12 3.10) do (
    py -%%v -c "print()" >nul 2>&1 && set PY=py -%%v&& goto :found
)
python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1 && set PY=python&& goto :found

echo No suitable Python found. Trying to install Python 3.11 with winget...
winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
echo.
echo If Python just got installed, CLOSE this window and run setup.bat again.
pause
exit /b 1

:found
echo Using Python: %PY%
%PY% -m venv "%VENVDIR%"
if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
)

:install
"%VENVDIR%\Scripts\python.exe" -m pip install --upgrade pip
if exist requirements.lock.txt (
    echo Installing exact tested package versions...
    "%VENVDIR%\Scripts\python.exe" -m pip install -r requirements.lock.txt && goto :done
    echo Exact versions failed - falling back to flexible versions...
)
"%VENVDIR%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Package install failed - check your internet connection and run setup.bat again.
    pause
    exit /b 1
)

:done
echo.
echo ============================================
echo   Setup complete!
echo   Double-click run.bat to start MeetingScribe.
echo ============================================
pause
