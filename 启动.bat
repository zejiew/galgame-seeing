@echo off
chcp 65001 >nul
title Galgame 自动录制助手
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 没找到 python。请先安装 Python 3.9 或更高版本。
    echo 下载：https://www.python.org/downloads/
    pause
    exit /b 1
)

python main.py
if errorlevel 1 (
    echo.
    echo 程序异常退出。试试先跑环境自检：
    echo     python main.py selfcheck
    pause
)
