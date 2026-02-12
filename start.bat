@echo off
chcp 65001 >nul
title 政策情报助手
echo ========================================
echo   政策情报助手 - 启动中...
echo ========================================
echo.

cd /d "%~dp0"

echo [1/2] 检查依赖...
python -c "import fastapi, playwright, openai" 2>nul
if errorlevel 1 (
    echo 正在安装依赖...
    pip install -r requirements.txt
    python -m playwright install chromium
)

echo [2/2] 启动服务...
echo.
echo   默认地址: http://localhost:8090
echo   若端口被占用将自动切换，请查看下方日志
echo   按 Ctrl+C 停止服务
echo.
python -m app.main
pause
