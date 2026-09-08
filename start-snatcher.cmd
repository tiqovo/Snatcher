@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m snatcher
) else (
    python -m snatcher
)

set "exit_code=%errorlevel%"
echo.
if not "%exit_code%"=="0" echo 程序已结束，退出代码：%exit_code%
pause
exit /b %exit_code%
