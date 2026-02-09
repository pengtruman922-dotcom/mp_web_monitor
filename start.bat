@echo off
chcp 65001 >nul
title 政策情报助手
echo ========================================
echo   政策情报助手 - 启动中...
echo ========================================
echo.

cd /d "%~dp0"

echo [1/3] 释放端口 8090...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8090 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo [2/3] 检查依赖...
python -c "import fastapi, playwright, openai" 2>nul
if errorlevel 1 (
    echo 正在安装依赖...
    pip install -r requirements.txt
    python -m playwright install chromium
)

echo [3/3] 启动服务...
echo.
echo   访问地址: http://localhost:8090
echo   按 Ctrl+C 停止服务
echo.
python -m app.main
pause
