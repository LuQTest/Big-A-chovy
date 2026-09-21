@echo off
rem A股 Web 工作台启动器（Windows）
rem 优先使用系统 Python；没有则回退到 uv（自动安装 Python 3.13 + 依赖）
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul && (
    python daily-stock-analysis/scripts/web_workbench.py %*
    goto :eof
)

where uv >nul 2>nul || (
    echo [启动器] 未找到 Python 和 uv，正在安装 uv ...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

uv run --python 3.13 --with requests --with pyyaml --with tzdata python daily-stock-analysis/scripts/web_workbench.py %*
