#!/usr/bin/env python3
"""network_path.py — 东财接口「多路径实测延迟、择优使用」模块。

设计目标：彻底不依赖任何代理软件 / 系统代理设置（scutil / keep_proxy_alive）。
路径优先级完全由代码内的实测数据决定：

  1. 本地直连（永远是一个候选）
  2. 本机候选代理端口（proxy_ports.json 的 candidate_ports，软件无关）
  3. 环境变量 HTTP(S)_PROXY（存在才纳入，仅作为候选之一）
  4. macOS 系统代理（scutil 读取，仅作为候选之一，读不到也不影响）

对每条路径并发实测「真实东财行情接口」延迟（必须返回合法 EM JSON 才算通），
按延迟升序排序供 fetch_json 依次使用；结果带 TTL 缓存，全部失败时短路缓存 30s。
直连若被东财限速/封锁（超时、403、非 JSON），会自动被测出并让位给代理路径，
反之亦然——不预设任何路径"一定可用"，也不预设"直连会被封"。

多端点探测（2026-09-09 增强）：
    原来只测 push2delay 一个端点，但引擎实际会打到 82.push2(资金流) / push2his(K线)
    等不同域名。历史故障：82.push2 曾在某出口超时而 push2delay 正常，单端点探测发现不了。
    现在主端点必须通，其余端点失败只记为「降级」并加排序惩罚，不一票否决。

切换粘性（2026-09-09 增强）：
    实测两条路常只差 1~2ms（如 175.9 vs 177.2），纯按延迟排序会因测量噪声来回
    抖动、白白浪费连接池。当前路径只要在最快路径 STICKY_MARGIN_MS 以内就保持不变。

直接运行本文件可看诊断表：python3 network_path.py
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests
    requests.packages.urllib3.disable_warnings()
except Exception:  # 引擎在无 requests 环境走 urllib 兜底，本模块静默降级
    requests = None

# ---- 配置 ----
# 健康检查端点。第一个为主端点：必须通，该路径才算可用；
# 其余为辅助端点：不通只记降级 + 排序惩罚（不同域名可能表现不同）。
PROBE_ENDPOINTS: Tuple[Tuple[str, str, Dict[str, Any]], ...] = (
    ("push2delay", "https://push2delay.eastmoney.com/api/qt/clist/get",
     {"pn": 1, "pz": 1, "fs": "m:1+t:2", "fields": "f12,f14"}),
    ("82push2", "https://82.push2.eastmoney.com/api/qt/clist/get",
     {"pn": 1, "pz": 1, "fs": "m:0+t:6", "fields": "f12,f14"}),
    ("push2his", "https://push2his.eastmoney.com/api/qt/stock/kline/get",
     {"secid": "1.600000", "klt": "101", "fqt": "1", "lmt": "1",
      "fields1": "f1", "fields2": "f51"}),
)
PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) daily-stock-analysis/1.0",
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    "Accept": "application/json,text/plain,*/*",
}
PROBE_TIMEOUT = 3.0        # 单端点探测超时（秒）
CACHE_TTL = 300.0          # 成功结果的缓存时长（秒）
NEGATIVE_TTL = 30.0        # 全部失败的负缓存时长（秒），避免网络断开时每次请求都探测
SLOW_PATH_MS = 2000.0      # 超过此延迟视为「太慢」，有替代路径时不用它
STICKY_MARGIN_MS = 50.0    # 当前路径比最快路径慢不超过这个值就不切换（防抖动）
DEGRADE_PENALTY_MS = 1000.0  # 每个不通的辅助端点的排序惩罚
FAIL_STREAK_LIMIT = 3      # 连续失败几次后进入冷却
COOLDOWN_SEC = 60.0        # 冷却时长（秒），期间不再探测该路径

DEFAULT_LOCAL_PROXY_PORTS: Tuple[int, ...] = (7890, 7897)
# 与 keep_proxy_alive.sh 共用同一份配置，换代理软件只改这一处
_PORTS_CONFIG_PATH = Path(__file__).resolve().parent / "proxy_ports.json"


def load_candidate_ports() -> Tuple[int, ...]:
    """从 proxy_ports.json 读候选端口；文件缺失或格式错误时退回默认值。

    注意：keep_proxy_alive.sh 读的是同一个文件，换代理软件改一处即可。
    """
    try:
        if _PORTS_CONFIG_PATH.exists():
            data = json.loads(_PORTS_CONFIG_PATH.read_text(encoding="utf-8"))
            ports = data.get("candidate_ports")
            if isinstance(ports, list):
                parsed = tuple(int(p) for p in ports if str(p).strip().isdigit())
                if parsed:
                    return parsed
    except Exception:
        pass
    return DEFAULT_LOCAL_PROXY_PORTS


LOCAL_PROXY_PORTS: Tuple[int, ...] = load_candidate_ports()

_lock = threading.Lock()
_cached_paths: Optional[List[Dict[str, Any]]] = None   # 排好序的可用路径
_cached_at: float = 0.0
_cached_ok: bool = False
_proxy_sessions: Dict[str, Any] = {}                   # proxy_url -> requests.Session
_current_label: Optional[str] = None                   # 当前选中路径，用于切换粘性
_fail_streak: Dict[str, int] = {}                      # label -> 连续失败次数
_cooldown_until: Dict[str, float] = {}                 # label -> 冷却截止时间戳
_last_switch_reason: str = ""                          # 最近一次切换原因（诊断用）


def _scutil_proxy_url() -> str:
    """读 macOS 系统代理（仅作为候选，读不到/非 darwin 一律返回空）。"""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            m = re.search(r"HTTPSProxy\s*:\s*([\d.]+)", out.stdout or "")
            pm = re.search(r"HTTPSPort\s*:\s*(\d+)", out.stdout or "")
            if m and pm:
                return f"http://{m.group(1)}:{pm.group(1)}"
    except Exception:
        pass
    return ""


def candidate_paths() -> List[Tuple[str, Optional[str]]]:
    """枚举候选路径 [(label, proxy_url|None)]，proxy_url=None 表示直连。"""
    paths: List[Tuple[str, Optional[str]]] = [("直连", None)]
    seen: set[str] = set()

    def _add(url: str) -> None:
        if not url or url in seen:
            return
        try:
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname or p.port is None:
                return
        except Exception:
            return
        seen.add(url)
        paths.append((f"代理{p.hostname}:{p.port}", url))

    # 本机端口优先：环境变量代理（如 IDE 沙盒注入的）只作兜底候选
    for port in LOCAL_PROXY_PORTS:
        _add(f"http://127.0.0.1:{port}")
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        _add(env_proxy)
    _add(_scutil_proxy_url())
    return paths


def _session_for(proxy_url: Optional[str]):
    """复用与 fetch 同一个 Session：探测结果即请求表现，避免探测/请求两张皮。"""
    if requests is None:
        return None
    if proxy_url is None:
        s = requests.Session()
        s.trust_env = False
        return s
    s = _proxy_sessions.get(proxy_url)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": proxy_url, "https": proxy_url}
        _proxy_sessions[proxy_url] = s
    return s


def _is_em_json(data: Any) -> bool:
    # clist/get 合法返回形如 {"rc":0,...,"data":{...}}；本地端口误答 200 时会在这里被拒
    return isinstance(data, dict) and ("rc" in data or "data" in data)


def _probe_requests(proxy_url: Optional[str]) -> Dict[str, Optional[float]]:
    """用 requests 依次探测各端点，返回 {端点名: 延迟ms 或 None}。"""
    result: Dict[str, Optional[float]] = {}
    session = _session_for(proxy_url)
    if session is None:
        return {name: None for name, _, _ in PROBE_ENDPOINTS}
    for name, url, params in PROBE_ENDPOINTS:
        try:
            t0 = time.perf_counter()
            resp = session.get(url, params=params, headers=PROBE_HEADERS,
                               timeout=PROBE_TIMEOUT, verify=False)
            resp.raise_for_status()
            data = resp.json()
            latency = (time.perf_counter() - t0) * 1000.0
            result[name] = round(latency, 1) if _is_em_json(data) else None
        except Exception:
            result[name] = None
    return result


def _probe_urllib(proxy_url: Optional[str]) -> Dict[str, Optional[float]]:
    """无 requests 环境用标准库探测（与引擎 urllib 兜底同一传输方式）。"""
    result: Dict[str, Optional[float]] = {}
    ssl_ctx = ssl._create_unverified_context()
    for name, url, params in PROBE_ENDPOINTS:
        try:
            handlers = [urllib.request.HTTPSHandler(context=ssl_ctx)]
            if proxy_url:
                handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
            else:
                handlers.append(urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                         headers=PROBE_HEADERS)
            t0 = time.perf_counter()
            with opener.open(req, timeout=PROBE_TIMEOUT) as resp:
                if resp.status != 200:
                    result[name] = None
                    continue
                data = json.loads(resp.read().decode("utf-8", "replace"))
            latency = (time.perf_counter() - t0) * 1000.0
            result[name] = round(latency, 1) if _is_em_json(data) else None
        except Exception:
            result[name] = None
    return result


def _probe_all(proxy_url: Optional[str]) -> Dict[str, Optional[float]]:
    """按当前环境可用的传输库探测所有端点（requests 优先，否则标准库）。"""
    if requests is not None:
        return _probe_requests(proxy_url)
    return _probe_urllib(proxy_url)


def _probe_one(label: str, proxy_url: Optional[str]) -> Optional[Dict[str, Any]]:
    """实测单条路径；主端点不通返回 None，否则返回带降级信息的路径描述。"""
    raw = _probe_all(proxy_url)
    primary_name = PROBE_ENDPOINTS[0][0]
    primary = raw.get(primary_name)
    if primary is None:
        return None  # 主端点不通 → 这条路径不可用
    degraded = [name for name, _, _ in PROBE_ENDPOINTS[1:] if raw.get(name) is None]
    # 综合延迟：各端点实测值的最大值（保守），再加降级惩罚
    observed = [v for v in raw.values() if v is not None]
    score = max(observed) + DEGRADE_PENALTY_MS * len(degraded)
    return {
        "label": label,
        "proxy": proxy_url,
        "latency_ms": round(primary, 1),      # 主端点实测延迟（展示用）
        "score": round(score, 1),             # 排序用（含降级惩罚）
        "degraded": degraded,                 # 不通的辅助端点
        "endpoints": raw,
    }


def _in_cooldown(label: str) -> bool:
    return time.time() < _cooldown_until.get(label, 0.0)


def record_failure(label: str) -> None:
    """请求失败时调用：累计失败次数，达阈值后该路径进入冷却。"""
    with _lock:
        _fail_streak[label] = _fail_streak.get(label, 0) + 1
        if _fail_streak[label] >= FAIL_STREAK_LIMIT:
            _cooldown_until[label] = time.time() + COOLDOWN_SEC


def record_success(label: str) -> None:
    with _lock:
        _fail_streak.pop(label, None)
        _cooldown_until.pop(label, None)


def probe_paths() -> List[Dict[str, Any]]:
    """并发探测所有候选路径，返回按 score 升序的可用路径列表（不读缓存）。"""
    from concurrent.futures import ThreadPoolExecutor

    candidates = candidate_paths()
    # 冷却中的路径先跳过；若全部在冷却则本轮全部放行，避免无路可走
    active = [c for c in candidates if not _in_cooldown(c[0])] or candidates
    results: List[Optional[Dict[str, Any]]] = [None] * len(active)

    with ThreadPoolExecutor(max_workers=max(2, len(active))) as pool:
        futs = [pool.submit(_probe_one, label, proxy) for label, proxy in active]
        for i, fut in enumerate(futs):
            try:
                results[i] = fut.result(timeout=(PROBE_TIMEOUT + 1.0) * len(PROBE_ENDPOINTS))
            except Exception:
                results[i] = None
    paths = [r for r in results if r is not None]
    if not paths:
        return []
    # 有替代路径时剔除「太慢」的；全都慢则保留最快的那个。
    # 注意：必须用主端点实测延迟(latency_ms)判断，不能用含降级惩罚的 score——
    # 否则辅助端点不通时惩罚把 score 抬到 2000+，会把所有路径误判成"太慢"只剩一条。
    usable = [p for p in paths if p["latency_ms"] <= SLOW_PATH_MS]
    paths = usable if usable else [min(paths, key=lambda p: p["latency_ms"])]
    paths.sort(key=lambda r: r["score"])
    return _apply_sticky(paths)


def _apply_sticky(paths: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """当前路径只要在最快路径 STICKY_MARGIN_MS 以内就保持不动，避免抖动。"""
    global _current_label, _last_switch_reason
    if not _current_label or len(paths) < 2:
        return paths
    cur = next((p for p in paths if p["label"] == _current_label), None)
    if cur is None:
        return paths
    fastest = paths[0]
    if cur is fastest:
        return paths
    if cur["score"] <= fastest["score"] + STICKY_MARGIN_MS:
        _last_switch_reason = f"保持 {cur['label']}（比最快慢 {cur['score'] - fastest['score']:.1f}，未超粘性阈值）"
        return [cur] + [p for p in paths if p is not cur]
    _last_switch_reason = f"{cur['label']} → {fastest['label']}（快 {cur['score'] - fastest['score']:.1f}）"
    return paths


def best_paths(max_age: float = CACHE_TTL) -> List[Dict[str, Any]]:
    """带缓存的可用路径列表（按实测延迟升序，最快在前）。"""
    global _cached_paths, _cached_at, _cached_ok
    with _lock:
        now = time.time()
        if _cached_paths is not None:
            ttl = CACHE_TTL if _cached_ok else NEGATIVE_TTL
            if now - _cached_at <= ttl:
                return list(_cached_paths)
    paths = probe_paths()
    with _lock:
        _cached_paths = paths
        _cached_ok = bool(paths)
        _cached_at = time.time()
    return list(paths)


def invalidate() -> None:
    """请求失败时由 fetch_json 调用：丢弃缓存，下一次重新实测。"""
    global _cached_paths, _cached_at, _cached_ok
    with _lock:
        _cached_paths = None
        _cached_at = 0.0
        _cached_ok = False


def ordered_sessions(direct_session) -> List[Tuple[str, Any]]:
    """auto 模式专用：返回 [(label, requests.Session)]，实测最快路径排最前。

    direct_session 复用调用方已有的直连会话；代理路径在模块内建会话（trust_env=False，
    不跟随系统代理，保证路径与实测完全一致）。
    """
    global _current_label
    if requests is None:
        return [("直连", direct_session)]
    paths = best_paths()
    sessions: List[Tuple[str, Any]] = []
    for path in paths:
        proxy = path["proxy"]
        sessions.append((path["label"], direct_session if proxy is None else _session_for(proxy)))
    if not sessions:
        # 一条都不通时仍给直连一个机会（可能刚断网恢复，负缓存 30s 后会重测）
        sessions.append(("直连", direct_session))
        _current_label = "直连"
    else:
        _current_label = paths[0]["label"]
    return sessions


def best_proxy_url() -> Optional[str]:
    """实测最优路径的代理 URL；最优是直连（或全不通）时返回 None。

    注意：必须取排序后的第一条判断。原实现遍历找「第一个代理」，
    会导致直连明明更快时仍返回某个代理，看板因此永远走代理。
    """
    paths = best_paths()
    if not paths:
        return None
    return paths[0]["proxy"]  # 直连时 proxy 为 None


def has_working_path() -> bool:
    """是否至少有一条路径能真正拿到东财数据（直连通了也算）。"""
    return bool(best_paths())


def warm_up() -> List[Dict[str, Any]]:
    """启动时预热：实测一遍并填充缓存，返回路径表。"""
    return best_paths()


def diagnostics() -> Dict[str, Any]:
    """诊断信息：配置来源、候选端口、各路径实测、粘性/冷却状态。"""
    with _lock:
        return {
            "ports_config": str(_PORTS_CONFIG_PATH),
            "ports_config_exists": _PORTS_CONFIG_PATH.exists(),
            "candidate_ports": list(LOCAL_PROXY_PORTS),
            "probe_endpoints": [name for name, _, _ in PROBE_ENDPOINTS],
            "current_label": _current_label,
            "last_switch_reason": _last_switch_reason,
            "fail_streak": dict(_fail_streak),
            "cooldown_until": {k: round(v - time.time(), 1)
                               for k, v in _cooldown_until.items() if v > time.time()},
            "paths": best_paths(),
        }


def _main() -> int:
    paths = probe_paths()
    print(f"探测端点: {', '.join(name for name, _, _ in PROBE_ENDPOINTS)}（第一个为主端点，必须通）")
    print(f"候选端口: {list(LOCAL_PROXY_PORTS)}  (来源: {_PORTS_CONFIG_PATH.name}"
          f"{'' if _PORTS_CONFIG_PATH.exists() else '，文件不存在用默认值'})")
    if not paths:
        print("结果: 所有路径均不可用")
        return 1
    print("\n结果（按综合评分排序，最快在前）:")
    for p in paths:
        deg = f"  降级: {','.join(p['degraded'])}" if p["degraded"] else ""
        detail = "  ".join(f"{k}={'×' if v is None else f'{v:.0f}ms'}"
                           for k, v in p["endpoints"].items())
        print(f"  实测{p['latency_ms']:>7.1f}ms  评分{p['score']:>8.1f}  "
              f"{p['label']:<24} {detail}{deg}")
    if _last_switch_reason:
        print(f"\n切换判定: {_last_switch_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
