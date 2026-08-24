#!/bin/bash
# 自动同步 A股选股系统(股票/)到 GitHub (LuQTest/Big-A-chovy)
# 由 LaunchAgent com.luqiang.syncstock 每 5 分钟触发一次
# 无变更时零操作退出；有变更才 commit + push

REPO="/Users/luqiang/Documents/Others/股票"
LOG="$HOME/Library/Logs/sync_to_github.log"

cd "$REPO" || exit 1

git add -A 2>>"$LOG"

# 无变更则不 commit/push（git diff --cached --quiet 返回 0=无差异, 1=有差异）
if git diff --cached --quiet; then
  exit 0
fi

# 有变更：提交并推送
git commit -m "auto-sync: $(date '+%Y-%m-%d %H:%M') 筛选结果/决策记录自动同步" 2>>"$LOG"

if git push origin main 2>>"$LOG"; then
  echo "[$(date '+%F %T')] synced OK: $(git rev-parse --short HEAD)" >>"$LOG"
else
  echo "[$(date '+%F %T')] PUSH FAILED (network/auth), will retry next cycle" >>"$LOG"
fi
