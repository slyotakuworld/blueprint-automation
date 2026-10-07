@echo off
rem ============================================================
rem start.cmd - ASCII-only launcher. No real logic lives here:
rem   1. verify Node.js is on PATH
rem   2. forward every argument to start.mjs (menu / orchestration)
rem   3. keep the console open in interactive mode so the log stays readable
rem
rem Why the console closed before: when this file is double-clicked, cmd.exe
rem owns a temporary console and destroys it the moment the batch ends, taking
rem the whole run log with it.
rem
rem Rule: no arguments  -> interactive menu -> pause before closing.
rem       with arguments -> command mode (dev/deps/build/...) -> stay chainable.
rem Set BA_NO_PAUSE=1 to suppress the pause in interactive mode as well.
rem ============================================================
setlocal
cd /d "%~dp0"

node --version >nul 2>&1
if errorlevel 1 (
    echo Node.js is not installed or not on PATH.
    echo Please install Node.js 18+ from https://nodejs.org/
    if not "%BA_NO_PAUSE%"=="1" pause
    exit /b 1
)

node start.mjs %*
set "RC=%ERRORLEVEL%"

if "%~1"=="" if not "%BA_NO_PAUSE%"=="1" (
    echo.
    if not "%RC%"=="0" echo [start.cmd] exit code: %RC%
    echo Press any key to close this window . . .
    pause >nul
)

endlocal & exit /b %RC%