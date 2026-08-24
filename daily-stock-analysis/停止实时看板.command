#!/bin/bash
# 停止 A股实时筛选看板
# 双击此文件即可关闭正在运行的看板（释放端口 8765）

cd "$(dirname "$0")"

echo "========================================="
echo "  停止 A股实时筛选看板"
echo "  端口: 8765"
echo "========================================="
echo ""

# 1) 按进程名结束（覆盖 python 主进程 + 可能的包装进程）
PIDS=$(pgrep -f "realtime_dashboard.py" || true)
if [ -n "$PIDS" ]; then
    echo "发现看板进程: $PIDS，正在结束..."
    kill $PIDS 2>/dev/null
else
    echo "未发现 realtime_dashboard.py 进程。"
fi

# 2) 兜底：若 8765 端口仍被占用，按端口再杀一次
sleep 2
PID8765=$(lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PID8765" ]; then
    echo "端口 8765 仍被 PID $PID8765 占用，强制结束..."
    kill "$PID8765" 2>/dev/null
    sleep 1
fi

# 3) 确认结果
if lsof -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    echo ""
    echo "⚠️  8765 端口仍被占用，请手动检查："
    echo "    lsof -iTCP:8765 -sTCP:LISTEN"
else
    echo ""
    echo "✅ 看板已关闭，8765 端口已释放。"
fi

echo ""
read -p "按回车退出..."
