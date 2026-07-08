@echo off
REM ------------------------------------------------------------
REM Blueprint Automation - PyInstaller build script
REM Single dist/ layout:
REM   dist\build\                 Intermediate work files
REM   dist\BlueprintAutomation\   Final EXE folder
REM ------------------------------------------------------------

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found.
        pause
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
        pause
        exit /b 1
    )
)

echo.
echo [PREP] Clean up old artifacts ...
if exist "build" echo   Removing top-level build/ ... & rmdir /s /q "build" 2>nul
if not exist "dist" mkdir "dist" 2>nul
if exist "dist\build" echo   Cleaning dist/build/ workpath ... & rmdir /s /q "dist\build" 2>nul
mkdir "dist\build" 2>nul

echo.
echo [BUILD] Running PyInstaller (--onedir, single dist layout) ...
"%PYTHON%" -m PyInstaller ^
  --name "BlueprintAutomation" ^
  --windowed ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --icon "%SCRIPT_DIR%icon.png" ^
  --workpath "%SCRIPT_DIR%dist\build" ^
  --distpath "%SCRIPT_DIR%dist" ^
  --specpath "%SCRIPT_DIR%dist\build" ^
  --add-data "%SCRIPT_DIR%config\doc;config\doc" ^
  --add-data "%SCRIPT_DIR%config\llm_defaults.json;config" ^
  --add-data "%SCRIPT_DIR%images;images" ^
  --add-data "%SCRIPT_DIR%icon.png;." ^
  "%SCRIPT_DIR%src\blueprint_app.py"

set "EXIT_CODE=%ERRORLEVEL%"

if exist "BlueprintAutomation.spec" echo [CLEAN] Removing stray spec from root (moved to dist/build) ... & del /q "BlueprintAutomation.spec" 2>nul

echo.
if "%EXIT_CODE%"=="0" (
    echo [DONE] Build succeeded!
    echo   Output layout (single top-level dist/ only)
    echo     Final EXE      = dist\BlueprintAutomation\BlueprintAutomation.exe
    echo     Work artifacts = dist\build
    echo   Run it by double-click or
    echo     dist\BlueprintAutomation\BlueprintAutomation.exe
) else (
    echo [ERROR] Build failed with exit code %EXIT_CODE%
    echo   See the PyInstaller log above for details.
)
pause
endlocal
