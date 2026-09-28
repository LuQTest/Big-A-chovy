@echo off
rem ===================================================================
rem  A-share web workbench launcher (Windows)
rem
rem  NOTE 1: This file is deliberately ASCII-only. cmd.exe parses .bat
rem  content with the system ANSI codepage (CP936 on zh-CN Windows), so
rem  UTF-8 Chinese text in commands/comments gets mis-decoded and breaks
rem  parsing. The Python program prints its own (Chinese) messages.
rem
rem  NOTE 2: Control flow is intentionally FLAT. Nested parenthesised
rem  blocks plus trailing redirections (e.g. `cmd >nul 2>nul` inside an
rem  `if (...)` inside another `if (...)`) make cmd mis-parse and abort
rem  with ". was unexpected at this time". Use `if ... goto` instead.
rem
rem  Behaviour: the server opens in a minimized window and this launcher
rem  returns at once, so closing the launcher window does not stop it.
rem  Set WORKBENCH_DEBUG=1 to run in the foreground in this window.
rem ===================================================================
chcp 65001 >nul
cd /d "%~dp0"

rem Keep dependency/Python state inside the project. The user-level cache
rem lives outside the workspace and may be denied there (uv reports:
rem "Failed to initialize cache ... os error 5").
if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%~dp0.uv-cache"
if not defined UV_PYTHON_INSTALL_DIR set "UV_PYTHON_INSTALL_DIR=%~dp0.uv-python"

set "WB_PY="

rem ---- 1) Project-local virtualenv: fastest and works offline ----
if exist ".venv\Scripts\python.exe" (
    set "WB_PY=.venv\Scripts\python.exe"
    goto :run
)

rem ---- 2) Real system Python ----
rem The Microsoft Store 0-byte stub is also named python.exe, so
rem `where python` alone misreports it as present; require that it can
rem actually execute code.
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys" >nul 2>nul
if errorlevel 1 goto :no_python
set "WB_PY=python"
goto :run

:no_python
rem ---- 3) uv fallback: provision Python 3.13 + dependencies ----
echo [launcher] No usable system Python; falling back to uv ...
where uv >nul 2>nul
if not errorlevel 1 goto :run_uv
echo [launcher] uv not found; installing it ...
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo [launcher] Failed to install uv. Aborting.
    exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

:run_uv
uv run --python 3.13 --with requests --with pyyaml --with tzdata python daily-stock-analysis/scripts/web_workbench.py %*
exit /b %errorlevel%

:run
if defined WORKBENCH_DEBUG (
    %WB_PY% daily-stock-analysis/scripts/web_workbench.py %*
) else (
    start "A-share workbench" /min %WB_PY% daily-stock-analysis/scripts/web_workbench.py %*
)
exit /b %errorlevel%
