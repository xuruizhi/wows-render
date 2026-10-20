@echo off
chcp 936 >nul
setlocal

title 战舰世界 · 高清渲染

cd /d "%~dp0"

echo ============================================
echo   战舰世界 高清渲染  -  网页服务
echo ============================================
echo.

rem ---- locate Python ----
set "PY="
if exist "C:\Program Files\Python313\python.exe" set "PY=C:\Program Files\Python313\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
)
if not defined PY (
    echo [错误] 找不到 Python，请先安装 Python 3.11 以上版本
    echo        然后修改本文件里的 PY 变量指向 python.exe
    pause
    exit /b 1
)
echo Python : %PY%

rem ---- game dir: env var WOWS_GAME_DIR takes precedence ----
if defined WOWS_GAME_DIR (
    echo Game   : %WOWS_GAME_DIR%  (来自环境变量 WOWS_GAME_DIR)
) else (
    echo Game   : 内置默认路径  (如需修改，请设置环境变量 WOWS_GAME_DIR)
)

rem ---- sanity check dependencies ----
if not exist "..\_tools\blender-4.5.10-windows-x64\blender.exe" (
    echo [警告] 未找到 Blender: ..\_tools\blender-4.5.10-windows-x64\blender.exe
)
if not exist "..\_tools\model-exporter\wows-gltf-exporter.exe" (
    echo [警告] 未找到模型导出器: ..\_tools\model-exporter\wows-gltf-exporter.exe
)
if defined WOWS_GAME_DIR (
    if not exist "%WOWS_GAME_DIR%" (
        echo [警告] WOWS_GAME_DIR 指向的目录不存在: %WOWS_GAME_DIR%
    )
)
if not exist "ships.json" (
    echo [提示] ships.json 不存在，正在生成船只清单...
    "%PY%" build_catalog.py
    if errorlevel 1 (
        echo [错误] 生成船只清单失败
        pause
        exit /b 1
    )
)

rem ---- port: use first argument, default 8770 ----
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8770"

echo 地址   : http://127.0.0.1:%PORT%
echo.
echo 浏览器会自动打开。关闭本窗口即停止服务。
echo.

rem ---- open browser after a short delay ----
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:%PORT%/"

"%PY%" server.py --port %PORT%

echo.
echo 服务已停止。
pause
