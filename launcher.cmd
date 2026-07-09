@echo off
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "PORT=8799"
echo ┌─ poly-btc launcher ─────────────────────────────
echo │  启动中... 浏览器将自动打开 http://127.0.0.1:%PORT%
echo │  关闭: 在此窗口按 Ctrl+C
echo └─────────────────────────────────────────────────

set "PY_CMD="
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>nul
  if %ERRORLEVEL% EQU 0 set "PY_CMD=python"
)

if not defined PY_CMD (
  echo.
  echo 未找到 Python 3。请先安装 Python 3, 然后重新双击此文件。
  echo 下载地址: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PY_CMD% launcher\launcher.py --port %PORT%

echo.
echo launcher 已退出。可关闭此窗口。
pause
