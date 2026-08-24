#!/bin/bash
# keep_proxy_alive.sh — 代理保活守护脚本
# 作用：
#   1. 检测 Clash(7897) 是否存活，不存活则尝试启动 Clash Verge
#   2. 检测 macOS 系统代理(scutil)是否被重置，被重置则自动恢复 HTTP 代理
# 说明：本脚本只负责"让系统代理配置不丢失"，看板本身已改为直连兜底，不受代理断开影响。
# 由 ~/Library/LaunchAgents/com.luqiang.keepclashproxy.plist 每30秒调用一次。

PROXY_HOST="127.0.0.1"
PROXY_PORT="7897"
LOG="$HOME/Library/Logs/keep_proxy.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"
}

# 确保日志目录存在
mkdir -p "$(dirname "$LOG")"

# ---- 1. 检测 Clash 端口是否存活 ----
port_up=0
if nc -z -w 2 "$PROXY_HOST" "$PROXY_PORT" 2>/dev/null; then
    port_up=1
fi

if [ "$port_up" -eq 0 ]; then
    # 进程可能掉，尝试启动 Clash Verge（GUI）
    if pgrep -f "Clash Verge" >/dev/null 2>&1; then
        log "端口 $PROXY_PORT 不通，但 Clash Verge 进程在，可能正在重启，跳过启动"
    else
        log "端口 $PROXY_PORT 不通，尝试启动 Clash Verge"
        open -a "Clash Verge" 2>/dev/null
    fi
else
    # 端口正常，检查系统代理是否被重置（HTTP 和 HTTPS 都要在：东财接口全是 HTTPS，
    # 只开 HTTP 不开 HTTPS 一样等于断网 —— 2026-07-30 实测踩坑）
    proxy_dump=$(scutil --proxy 2>/dev/null)
    cur_enable=$(echo "$proxy_dump" | grep "HTTPEnable" | sed -n 's/.*: \([01]\).*/\1/p' | head -1)
    cur_proxy=$(echo "$proxy_dump" | grep "HTTPProxy" | sed -n 's/.*: \([0-9.]*\).*/\1/p' | head -1)
    cur_port=$(echo "$proxy_dump" | grep "HTTPPort" | sed -n 's/.*: \([0-9]*\).*/\1/p' | head -1)
    cur_https_enable=$(echo "$proxy_dump" | grep "HTTPSEnable" | sed -n 's/.*: \([01]\).*/\1/p' | head -1)
    cur_https_proxy=$(echo "$proxy_dump" | grep "HTTPSProxy" | sed -n 's/.*: \([0-9.]*\).*/\1/p' | head -1)
    cur_https_port=$(echo "$proxy_dump" | grep "HTTPSPort" | sed -n 's/.*: \([0-9]*\).*/\1/p' | head -1)

    if [ "$cur_enable" != "1" ] || [ "$cur_proxy" != "$PROXY_HOST" ] || [ "$cur_port" != "$PROXY_PORT" ] \
       || [ "$cur_https_enable" != "1" ] || [ "$cur_https_proxy" != "$PROXY_HOST" ] || [ "$cur_https_port" != "$PROXY_PORT" ]; then
        log "检测到系统代理被重置(http=$cur_enable:$cur_proxy:$cur_port https=$cur_https_enable:$cur_https_proxy:$cur_https_port)，自动恢复..."
        # 遍历所有网络服务重设 HTTP/HTTPS 代理（忽略禁用服务与说明行）
        while IFS= read -r svc; do
            [ -z "$svc" ] && continue
            case "$svc" in
                \*|*\**) continue ;;   # 跳过禁用服务
            esac
            networksetup -setwebproxy "$svc" "$PROXY_HOST" "$PROXY_PORT" >/dev/null 2>&1
            networksetup -setsecurewebproxy "$svc" "$PROXY_HOST" "$PROXY_PORT" >/dev/null 2>&1
            networksetup -setwebproxystate "$svc" on >/dev/null 2>&1
            networksetup -setsecurewebproxystate "$svc" on >/dev/null 2>&1
        done < <(networksetup -listallnetworkservices 2>/dev/null | grep -v "^\*" | grep -v "denotes")
        log "系统代理已恢复: $PROXY_HOST:$PROXY_PORT"
    fi
fi

exit 0
