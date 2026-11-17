@echo off
rem ==========================================================================
rem  Agricultural Knowledge Graph - one-click launcher
rem
rem  1) console to UTF-8 so the Python output below renders correctly
rem  2) locate the project folder from this file location (%~dp0) instead of
rem     hard-coding it - keeps working when the path has Chinese characters
rem  3) run the preflight self-check, then start the Flask server
rem
rem  This file is deliberately pure ASCII: cmd.exe reads a .bat line by line
rem  using the CURRENT console code page, so Chinese text inside a .bat gets
rem  mis-decoded before "chcp 65001" takes effect. All Chinese messages are
rem  printed by the Python side instead. It also uses CRLF line endings -
rem  LF-only batch files break parenthesised "if (...)" blocks.
rem
rem  Arguments are forwarded, e.g.:  <this file> --check --no-llm
rem ==========================================================================
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0code"
if errorlevel 1 (
    echo [FAIL] Cannot enter the "code" folder next to this script.
    echo        Keep this .bat in the project root, beside "code" and "database".
    pause
    exit /b 1
)

if not exist "scripts\preflight.py" (
    echo [FAIL] "scripts\preflight.py" not found under:
    echo        %CD%
    pause
    exit /b 1
)

set "PY=E:\xunihuanjing\venv\jiaotong\Scripts\python.exe"
if not exist "%PY%" (
    echo [WARN] Project venv not found, falling back to "python" on PATH.
    set "PY=python"
)

echo.
"%PY%" scripts\preflight.py %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo   [ preflight exited with code %RC% ]
    echo.
)
echo   Server stopped. Press any key to close this window.
pause >nul
exit /b %RC%
