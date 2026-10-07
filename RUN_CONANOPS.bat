@echo off
title ConanOps Launcher
cd /d "%~dp0"

echo ============================================
echo  ConanOps Launcher
echo ============================================
echo.

echo Checking for Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [PROBLEM] Python was not found on this computer.
    echo.
    echo Go to https://python.org/downloads, download and run the installer,
    echo and make sure to check the box that says "Add python.exe to PATH"
    echo before clicking Install. Then double-click this file again.
    echo.
    pause
    exit /b
)
echo Python found: OK
echo.

echo Installing required packages - this can take a minute, please wait...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [PROBLEM] Something went wrong installing packages. Scroll up to see
    echo the error message in red/white text above this line.
    echo.
    pause
    exit /b
)
echo.
echo Packages installed: OK
echo.

echo Starting ConanOps now...
echo (a separate app window will open - check your taskbar)
echo.
echo This launcher window will close on its own in a few seconds.
echo ConanOps itself will keep running after that - closing THIS
echo window does not close the app anymore.
echo.

rem "start" launches ConanOps as its own separate, independent process
rem instead of running inline in this window. "pythonw" (not "python")
rem runs it with no console attached at all, so it isn't tied to this
rem launcher window's lifetime - closing this window, or it closing
rem itself below, will NOT close ConanOps.
start "" pythonw main.py

timeout /t 4 /nobreak >nul
exit /b
