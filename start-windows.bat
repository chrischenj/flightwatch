@echo off
chcp 936 >nul
title 分舱段购票助手 - 关闭此窗口即停止服务
cd /d "%~dp0"

set "URL=http://127.0.0.1:8765/"

rem 如果服务已经在跑，就不要重复启动，直接打开浏览器
where curl >nul 2>nul
if errorlevel 1 goto :start_server
curl.exe -s -o nul --max-time 2 http://127.0.0.1:8765/api/health
if not errorlevel 1 (
  echo 服务已经在运行。
  start "" "%URL%"
  timeout /t 2 >nul
  exit /b 0
)

:start_server
echo ============================================================
echo   分舱段购票助手
echo   启动后浏览器会自动打开 %URL%
echo   如果没自动打开，就手动把这个地址复制到浏览器
echo   关掉这个黑色窗口 = 停止服务
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto :try_py

python -m flightwatch serve --port 8765
goto :stopped

:try_py
where py >nul 2>nul
if errorlevel 1 goto :no_python

py -3 -m flightwatch serve --port 8765
goto :stopped

:no_python
echo [错误] 没有找到 Python。
echo.
echo 请先安装 Python 3.9 或以上版本： https://www.python.org/downloads/
echo 安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
echo.
pause
exit /b 1

:stopped
echo.
echo 服务已停止。如需再次使用，请重新双击本文件。
pause
