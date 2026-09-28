@echo off
rem ===================================================================
rem  Keep-alive for the A-share web workbench (Windows Task Scheduler).
rem
rem  Scheduled every 15 min on weekdays between 09:15 and 15:10:
rem    - if 127.0.0.1:8765 is already listening -> exit at once
rem    - otherwise start the workbench (minimized, detached)
rem
rem  Keep this file ASCII-only with CRLF line endings. cmd.exe parses
rem  .bat content using the system ANSI codepage (CP936 here), so UTF-8
rem  Chinese text in it would be mis-decoded and break parsing.
rem ===================================================================
setlocal
set "PORT=8765"
set "ROOT=%~dp0"
set "SCRIPT=daily-stock-analysis/scripts/web_workbench.py"

rem --- already running? then nothing to do ---
netstat -ano | findstr /R /C:"TCP.*:%PORT%.*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [keepalive] port %PORT% is up, nothing to do
    goto :eof
)

echo [keepalive] port %PORT% is down, starting workbench ...

rem Start-Process detaches the server from this script, so the workbench
rem keeps running after the scheduled task completes.
if exist "%ROOT%.venv\Scripts\python.exe" (
    powershell -NoProfile -NonInteractive -Command "Start-Process -FilePath '%ROOT%.venv\Scripts\python.exe' -ArgumentList '%SCRIPT%','--no-browser' -WorkingDirectory '%ROOT%' -WindowStyle Minimized"
    goto :eof
)

rem --- fallback: let uv provision the interpreter + dependencies ---
powershell -NoProfile -NonInteractive -Command "Start-Process -FilePath 'uv' -ArgumentList 'run','--python','3.13','--with','requests','--with','pyyaml','--with','tzdata','python','%SCRIPT%','--no-browser' -WorkingDirectory '%ROOT%' -WindowStyle Minimized"

endlocal
