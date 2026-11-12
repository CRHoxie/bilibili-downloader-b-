@echo off
cd /d "%~dp0"
title B 站视频下载器

rem 依赖检查与安装交给 bootstrap.py（无控制台，用自绘进度窗）
rem 这里只负责找到 Python 并隐藏启动它，控制台会立刻关闭

set "PYCMD="
python --version >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto :run
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto :run
goto :nopython

:run
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw bootstrap.py
) else (
    start "" %PYCMD% bootstrap.py
)
exit /b 0

:nopython
echo.
echo [错误] 未检测到 Python。
echo 请安装 Python 3.9 或更高版本，安装时勾选 "Add Python to PATH"。
echo 下载地址: https://www.python.org/downloads/
echo.
pause
exit /b 1
