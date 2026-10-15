@echo off
REM Vex YouTube Downloader - launcher
REM Installs dependencies on first run, then starts the app.

cd /d "%~dp0"

py -c "import PyQt6, yt_dlp" 2>nul
if errorlevel 1 (
    echo First run - installing requirements, this takes a minute...
    py -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Install failed. Open Command Prompt in this folder and run:
        echo     py -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

py Vex.py

if errorlevel 1 pause