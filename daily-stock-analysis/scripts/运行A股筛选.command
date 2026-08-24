#!/bin/zsh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$(command -v python3 || true)"

if [[ -z "$PYTHON_BIN" ]]; then
  osascript -e 'display alert "未找到 python3" message "请先安装 Python 3，或把这个工具交给会配置的人处理。"'
  exit 1
fi

cd "$SCRIPT_DIR"

"$PYTHON_BIN" "$SCRIPT_DIR/a_share_screen_gui.py"
STATUS=$?

if [[ $STATUS -ne 0 ]]; then
  mkdir -p "$HOME/Documents/Others/股票/筛选结果"
  OUT="$HOME/Documents/Others/股票/筛选结果/A股筛选结果_$(date +%Y%m%d_%H%M).md"
  "$PYTHON_BIN" "$SCRIPT_DIR/a_share_daily_screen.py" --mode strict --format md --save "$OUT"
  if [[ -f "$OUT" ]]; then
    open -R "$OUT"
  fi
fi

# 脚本结束后自动关闭终端窗口
osascript -e 'tell application "Terminal" to close front window' 2>/dev/null &
