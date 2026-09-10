#!/bin/bash
# Real-time A-share screening dashboard launcher
# Double-click this file to start the dashboard server

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || true)"

if [ -z "$PYTHON" ]; then
    echo "未找到 python3，请先安装 Python 3。"
    exit 1
fi

# 默认 auto 模式启动时会实测直连、proxy_ports.json 中的候选代理端口、
# 环境代理和 macOS 系统代理，按实际可用延迟择优；换代理软件只改配置文件。
echo "网络路径：启动时实测直连、候选代理、环境代理和系统代理后择优"
echo "代理端口配置：$SCRIPT_DIR/scripts/proxy_ports.json"

echo "========================================="
echo "  A股实时筛选看板"
echo "  端口: 8765"
echo "  浏览器将自动打开 http://localhost:8765"
echo "  按 Ctrl+C 停止"
echo "========================================="
echo ""

exec "$PYTHON" "$SCRIPT_DIR/scripts/realtime_dashboard.py"
