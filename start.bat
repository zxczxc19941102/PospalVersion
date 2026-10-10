@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 银豹版本下载链接速查

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    echo 未找到可用的 Python 解释器。
    echo 请安装 Python 3，或使用项目自带的 .venv\Scripts\python.exe 运行 gui.py
    pause
    exit /b 1
)

echo 使用解释器：%PY%
"%PY%" gui.py
if errorlevel 1 (
    echo.
    echo 启动失败。若提示缺少依赖，请执行：
    echo     "%PY%" -m pip install -r requirements.txt
    pause
)
