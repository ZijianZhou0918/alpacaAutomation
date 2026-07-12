@echo off
setlocal
chcp 65001 >nul
title MA5 每日复盘
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON%" (
    "%PYTHON%" "%~dp0open_daily_review.py"
) else (
    py -3 "%~dp0open_daily_review.py"
)

if errorlevel 1 (
    echo.
    echo MA5 每日复盘启动失败，请查看 outputs\logs\daily_review_server.err.log。
    pause
)

endlocal
