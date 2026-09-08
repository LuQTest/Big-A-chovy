#!/bin/bash
# Real-time A-share screening dashboard launcher
# Double-click this file to start the dashboard server

cd "$(dirname "$0")"

# 优先使用 PATH 中的 python3（README 要求 Python 3.10+）
PYTHON="$(command -v python3)"
if [ -z "$PYTHON" ]; then
    echo "未找到 python3，请先安装 Python 3.10 或更高版本"
    read -p "按回车退出..."
    exit 1
fi

# 东财数据必须走代理(直连被封)。这里只检测并打印系统代理，**不固化到环境变量**，
# 让看板 REQUESTS_SESSION(trust_env=True) 每次从系统代理(scutil)实时读取，
# 配合 keep_proxy_alive 守护，代理被 macOS 重置后可自动恢复，无需重启看板。
PROXY_HOST=$(scutil --proxy | sed -n 's/^[[:space:]]*HTTPProxy[[:space:]]*:[[:space:]]*//p' | head -1 | tr -d ' ')
PROXY_PORT=$(scutil --proxy | sed -n 's/^[[:space:]]*HTTPPort[[:space:]]*:[[:space:]]*//p' | head -1 | tr -d ' ')
if [ -n "$PROXY_HOST" ] && [ -n "$PROXY_PORT" ]; then
    echo "检测到系统代理: ${PROXY_HOST}:${PROXY_PORT}（看板将实时读取，不固化到环境变量）"
else
    echo "未检测到系统代理，东财数据需代理，请确认 Clash 已开启系统代理"
fi

echo "========================================="
echo "  A股实时筛选看板"
echo "  端口: 8765"
echo "  浏览器将自动打开 http://localhost:8765"
echo "  按 Ctrl+C 停止"
echo "========================================="
echo ""

exec "$PYTHON" "$(dirname "$0")/scripts/realtime_dashboard.py"
