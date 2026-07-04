@echo off
REM ------------------------------------------------------------
REM Blueprint Automation - Launch script
REM Priority: .venv\Scripts\python.exe, else PATH python
REM ------------------------------------------------------------

REM Force UTF-8 code page to avoid garbled Chinese characters
chcp 65001 >nul 2>&1

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
    echo [INFO] Using local venv python: .venv\Scripts\python.exe
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=python"
        echo [INFO] Using system python from PATH
    ) else (
        echo [ERROR] Python not found. Install Python 3.10+ or create a local venv.
        echo        For example:
        echo          python -m venv .venv
        echo          .venv\Scripts\pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM ------------------------------------------------------------
REM Auto install missing dependencies from requirements.txt
REM   - Already-satisfied packages are skipped automatically
REM ------------------------------------------------------------
echo [BOOT] Checking dependencies (requirements.txt) ...
"%PYTHON%" -m pip install -r requirements.txt --quiet
if not "%ERRORLEVEL%"=="0" (
    echo [WARN] pip install exited with code %ERRORLEVEL%, some dependencies may be missing.
    echo        Try manually: "%PYTHON%" -m pip install -r requirements.txt
)

echo.
echo [BOOT] Starting blueprint editor ...
"%PYTHON%" "%SCRIPT_DIR%src\blueprint_app.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo [EXIT] App exited with code %EXIT_CODE%
    pause
)
endlocal
