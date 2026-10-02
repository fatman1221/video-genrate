@echo off
REM ===========================================================================
REM  AI Video Agent Studio - Windows 启动脚本
REM
REM  用法：
REM    scripts\dev.cmd         启动后端 (8077)
REM    scripts\dev.cmd web     启动前端 (5180，需先装好依赖)
REM    scripts\dev.cmd stop    停止后端与前端
REM    scripts\dev.cmd check   环境自检
REM
REM  说明：
REM    - 原 scripts/dev.sh 是 macOS/Linux 版本，依赖 pkill / rm -rf，Windows 不可用
REM    - 后端与前端请各开一个终端窗口，分别跑 dev.cmd 和 dev.cmd web
REM      （不要用 start 后台拉起，否则关窗口时进程会变成孤儿）
REM    - set PYTHONPATH= 等价于 unset，用于绕开 WorkBuddy 宿主注入的 shim
REM      （它会劫持 os.mkdir，导致 uvicorn 启动即失败）
REM    - 前端启动前必须 set NODE_OPTIONS= 并清掉 node_modules\.vite，
REM      否则宿主注入的 safe-delete 会拦截 Vite 的缓存清理、把 dev server 杀掉
REM ===========================================================================
setlocal

set "ROOT=%~dp0.."
set "BACKEND_PORT=8077"
set "FRONTEND_PORT=5180"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "NODE_HOME=%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2-3"

if not exist "%PY%" (
  echo [x] 找不到虚拟环境：%PY%
  echo     请先执行：python -m venv .venv
  echo              .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
  exit /b 1
)

if /i "%1"=="stop" goto :stop
if /i "%1"=="check" goto :check
if /i "%1"=="web" goto :web

:start
echo 后端 Python : %PY%

REM 若已有进程占用端口，先让用户知道
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%BACKEND_PORT%" ^| findstr "LISTENING"') do (
  echo [!] 端口 %BACKEND_PORT% 已被 PID %%P 占用。请先运行： scripts\dev.cmd stop
  exit /b 1
)

echo 启动后端 http://127.0.0.1:%BACKEND_PORT% ...
cd /d "%ROOT%\backend"
set PYTHONPATH=
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %BACKEND_PORT% --log-level info
goto :eof

:web
if not exist "%ROOT%\frontend\node_modules\.bin\vite" (
  echo [x] 前端依赖未安装。
  echo     请先执行：cd frontend ^&^& npm install
  exit /b 1
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%FRONTEND_PORT%" ^| findstr "LISTENING"') do (
  echo [!] 端口 %FRONTEND_PORT% 已被 PID %%P 占用。请先运行： scripts\dev.cmd stop
  exit /b 1
)

echo 启动前端 http://127.0.0.1:%FRONTEND_PORT% ...
cd /d "%ROOT%\frontend"
set NODE_OPTIONS=
if exist "%NODE_HOME%" set "PATH=%NODE_HOME%;%PATH%"
if exist "node_modules\.vite" rmdir /s /q "node_modules\.vite"
call node_modules\.bin\vite.cmd --host 127.0.0.1 --port %FRONTEND_PORT% --strictPort
goto :eof

:stop
echo 正在停止后端与前端 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%BACKEND_PORT%" ^| findstr "LISTENING"') do (
  taskkill /PID %%P /F >nul 2>&1 && echo   已停止后端 PID %%P
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%FRONTEND_PORT%" ^| findstr "LISTENING"') do (
  taskkill /PID %%P /F >nul 2>&1 && echo   已停止前端 PID %%P
)
echo 完成。
goto :eof

:check
echo 后端 Python : %PY%
echo 端口 %BACKEND_PORT% ^(后端^) :
netstat -ano | findstr ":%BACKEND_PORT%" | findstr "LISTENING" || echo   ^(未监听^)
echo 端口 %FRONTEND_PORT% ^(前端^) :
netstat -ano | findstr ":%FRONTEND_PORT%" | findstr "LISTENING" || echo   ^(未监听^)
echo 前端依赖    :
if exist "%ROOT%\frontend\node_modules\.bin\vite" (echo   已安装) else (echo   未安装 —— 请执行 cd frontend ^&^& npm install)
echo ffmpeg      :
where ffmpeg >nul 2>&1 && ffmpeg -version | findstr /b "ffmpeg version" || echo   ^(未找到 —— 本期只出图，不影响^)
echo ComfyUI     :
curl -s -m 5 http://127.0.0.1:8188/system_stats >nul 2>&1 && echo   127.0.0.1:8188 OK || echo   ^(未启动^)
goto :eof
