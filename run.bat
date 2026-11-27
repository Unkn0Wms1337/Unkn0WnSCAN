@echo off
chcp 65001 >nul
title Unkn0WnSCAN bot
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://python.org
    pause
    exit /b 1
  )
  py bot.py
) else (
  python bot.py
)

echo.
echo === Bot stopped. Press any key to close. ===
pause >nul
