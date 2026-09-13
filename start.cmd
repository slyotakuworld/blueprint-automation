@echo off
setlocal
cd /d "%~dp0"

node --version >nul 2>&1
if errorlevel 1 (
    echo Node.js is not installed or not on PATH.
    echo Please install Node.js 18+ from https://nodejs.org/
    pause
    exit /b 1
)

node start.mjs %*
endlocal
