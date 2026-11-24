@echo off
chcp 65001 >nul
title Unkn0WnSCAN bot
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" bot.py
echo.
echo === Bot stopped. Press any key to close. ===
pause >nul
