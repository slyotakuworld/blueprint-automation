@echo off
REM ------------------------------------------------------------
REM Blueprint Automation - Runtime template builder
REM
REM 用 PyInstaller 把 src\blueprint_runner.py 打成单文件 BlueprintRuntime.exe。
REM 编辑器里的「📦 导出 EXE」会复制这个模板，并在末尾追加"蓝图 + 图片资源"载荷，
REM 从而生成可以脱离本项目单独运行的挂机程序。
REM
REM 用法：
REM   build_runtime.cmd                 -> 输出到项目根
REM   build_runtime.cmd <输出目录>       -> 输出到指定目录（build.cmd 用它放到 dist\BlueprintAutomation）
REM 设 BP_NO_PAUSE=1 可跳过结尾暂停（供 build.cmd 嵌套调用）
REM ------------------------------------------------------------

chcp 65001 >nul 2>&1
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "OUT_DIR=%~1"
if "%OUT_DIR%"=="" set "OUT_DIR=%SCRIPT_DIR%"

set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found.
        if not "%BP_NO_PAUSE%"=="1" pause
        exit /b 1
    )
    set "PYTHON=python"
)

echo [BOOT] Python: %PYTHON%

"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [PREP] PyInstaller not installed. Installing via pip ...
    "%PYTHON%" -m pip install PyInstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        if not "%BP_NO_PAUSE%"=="1" pause
        exit /b 1
    )
)

echo.
echo [BUILD] Building BlueprintRuntime.exe (--onefile) ...
"%PYTHON%" -m PyInstaller ^
  --name "BlueprintRuntime" ^
  --windowed ^
  --onefile ^
  --noconfirm ^
  --clean ^
  --icon "%SCRIPT_DIR%icon.png" ^
  --workpath "%SCRIPT_DIR%dist\build_runtime" ^
  --distpath "%SCRIPT_DIR%dist\runtime" ^
  --specpath "%SCRIPT_DIR%dist\build_runtime" ^
  --paths "%SCRIPT_DIR%" ^
  --hidden-import win32gui ^
  --hidden-import win32process ^
  --hidden-import win32clipboard ^
  --hidden-import win32con ^
  --hidden-import win32api ^
  --hidden-import pythoncom ^
  --hidden-import pywintypes ^
  --hidden-import psutil ^
  --hidden-import pyautogui ^
  --hidden-import pyperclip ^
  --hidden-import pydirectinput ^
  --hidden-import pynput ^
  --hidden-import pynput.keyboard ^
  --hidden-import soundcard ^
  --hidden-import pyaudio ^
  --hidden-import cv2 ^
  --hidden-import numpy ^
  --hidden-import PIL ^
  "%SCRIPT_DIR%src\blueprint_runner.py"

set "EXIT_CODE=%ERRORLEVEL%"

if exist "BlueprintRuntime.spec" del /q "BlueprintRuntime.spec" 2>nul

if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Runtime build failed with exit code %EXIT_CODE%
    if not "%BP_NO_PAUSE%"=="1" pause
    exit /b %EXIT_CODE%
)

set "SRC_EXE=%SCRIPT_DIR%dist\runtime\BlueprintRuntime.exe"
if not exist "%SRC_EXE%" (
    echo [ERROR] Expected output not found: %SRC_EXE%
    if not "%BP_NO_PAUSE%"=="1" pause
    exit /b 1
)

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%" 2>nul
copy /y "%SRC_EXE%" "%OUT_DIR%\BlueprintRuntime.exe" >nul

echo.
echo [DONE] Runtime template ready:
echo        %OUT_DIR%\BlueprintRuntime.exe
if not "%BP_NO_PAUSE%"=="1" pause
endlocal
