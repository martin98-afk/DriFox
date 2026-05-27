@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 飘狐 DriFox - 截图分析
echo =============================
echo.

py -3.12 -u capture_and_analyze.py %*

if errorlevel 10 (
    echo.
    echo =============================
    echo 需要配置 MiniMax API Key
    echo =============================
    echo 方法1: set MINIMAX_API_KEY=你的密钥
    echo 方法2: 在 %USERPROFILE%\.minimax\api_key 文件中写入密钥
    echo.
    set /p key="请直接输入 API Key: "
    if defined key (
        set MINIMAX_API_KEY=%key%
        py -3.12 -u capture_and_analyze.py --no-screenshot
    )
)

pause