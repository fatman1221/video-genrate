@echo off
chcp 65001 >nul
title 图片 / 视频超分操作台
cd /d "%~dp0"

rem 清掉宿主注入的 PYTHONPATH：它会让 Python 加载到非预期模块
set PYTHONPATH=
set PYTHONHOME=

rem 优先用托管 Python（版本目录名会变，取找到的最后一个）
set PYBIN=
for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if exist "%%D\python.exe" set PYBIN=%%D\python.exe
)
if not defined PYBIN (
  where python >nul 2>nul && set PYBIN=python
)
if not defined PYBIN (
  echo [x] 找不到 Python。请安装后重试，或手动指定：
  echo     set PYBIN=C:\path\to\python.exe ^&^& start.cmd
  pause
  exit /b 1
)

echo 使用 Python: %PYBIN%
"%PYBIN%" "%~dp0server.py" %*
echo.
echo 服务已退出。
pause
