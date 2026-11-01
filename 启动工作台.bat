@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "课程工作台-答案中心.exe" (
  echo   正在启动课程工作台…
  start "" "课程工作台-答案中心.exe"
  exit /b
)

if exist "课程工作台.exe" (
  start "" "课程工作台.exe"
  exit /b
)

echo   没找到 exe，改用 Python 启动…
where python >nul 2>nul
if not errorlevel 1 (
  python sync\cli.py desktop
  exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 sync\cli.py desktop
  exit /b
)
echo   未找到 Python。请先按 README 安装开发依赖，或使用已打包的 exe。
pause
