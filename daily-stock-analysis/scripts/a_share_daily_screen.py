#!/usr/bin/env python3
"""A-share daily screening helper for daily-stock-analysis.

This script fetches current A-share snapshots and daily K lines, then emits
structured Markdown or JSON for Codex/Hermes analysis. It is intentionally a
query and classification tool, not an auto-trading tool.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import json
import math
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
try:
    import requests
    requests.packages.urllib3.disable_warnings()
    REQUESTS_SESSION = requests.Session()
    REQUESTS_SESSION.trust_env = True
    REQUESTS_DIRECT_SESSION = requests.Session()
    REQUESTS_DIRECT_SESSION.trust_env = False
except Exception:  # keep stdlib fallback for minimal Python envs
    requests = None
    REQUESTS_SESSION = None
    REQUESTS_DIRECT_SESSION = None
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
SCRIPT_DIR = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) daily-stock-analysis/1.0"
SSL_CONTEXT = ssl._create_unverified_context()

# ── Per-host circuit breaker ──────────────────────────────────────────
# Maps hostname -> timestamp of last failure.  Hosts that failed within
# _HOST_COOLDOWN seconds are moved to the end of the URL list so the
# fetcher tries healthy hosts first.
_HOST_FAILURES: Dict[str, float] = {}
_HOST_COOLDOWN = 120  # seconds to keep a host deprioritised


def _rank_urls(urls: List[str]) -> List[str]:
    """Return *urls* sorted so that recently-failed hosts come last."""
    now = time.time()
    def _key(u: str) -> int:
        host = u.split("/")[2]
        fail_ts = _HOST_FAILURES.get(host, 0)
        return 0 if (now - fail_ts) > _HOST_COOLDOWN else 1
    return sorted(urls, key=_key)


def _mark_host_failed(url: str) -> None:
    host = url.split("/")[2]
    _HOST_FAILURES[host] = time.time()


def _mark_host_ok(url: str) -> None:
    host = url.split("/")[2]
    _HOST_FAILURES.pop(host, None)


# ── Persistent East Money cooldown ────────────────────────────────────
# When ALL hosts fail in a run, record the timestamp.  Subsequent runs
# within _EM_COOLDOWN seconds skip East Money entirely and go straight
# to the Sina fallback, saving ~30 s of wasted retry time.
_EM_COOLDOWN_FILE = SCRIPT_DIR / ".em_cooldown"
# 指数退避：第1次封 120s → 第2次 300s → 第3次起 600s
_EM_COOLDOWN_BACKOFF = [120, 300, 600]

def _em_read_cooldown() -> Tuple[float, int]:
    """Return (timestamp, consecutive_failures) from cooldown file."""
    try:
        if _EM_COOLDOWN_FILE.exists():
            data = _EM_COOLDOWN_FILE.read_text().strip().split("\n")
            ts = float(data[0])
            count = int(data[1]) if len(data) > 1 else 1
            return ts, count
    except (ValueError, OSError):
        pass
    return 0.0, 0


def _em_in_cooldown() -> bool:
    ts, count = _em_read_cooldown()
    if ts == 0.0:
        return False
    idx = min(count - 1, len(_EM_COOLDOWN_BACKOFF) - 1)
    backoff = _EM_COOLDOWN_BACKOFF[idx]
    return (time.time() - ts) < backoff


def _em_set_cooldown() -> None:
    """Record a failed EM run with exponential backoff counter."""
    try:
        ts = time.time()
        _, prev_count = _em_read_cooldown()
        # If previous cooldown already expired, reset counter; else increment
        if prev_count > 0 and (ts - _em_read_cooldown()[0]) > _EM_COOLDOWN_BACKOFF[min(prev_count - 1, len(_EM_COOLDOWN_BACKOFF) - 1)]:
            count = 1  # previous ban expired, fresh start
        else:
            count = min(prev_count + 1, 99)
        _EM_COOLDOWN_FILE.write_text(f"{ts}\n{count}")
    except OSError:
        pass


def _em_clear_cooldown() -> None:
    """Remove cooldown file — call after a successful EM request."""
    try:
        _EM_COOLDOWN_FILE.unlink(missing_ok=True)
    except OSError:
        pass

# 2026-07-30: 东财对海外出口 IP 把 push2 302 到 push2delay，且 push2 间歇 502（~75%失败）。
# push2delay 对 A 股仍是实时数据（dlmkts 仅对港美股延迟），实测 100% 稳定，故提升为首选。
CLIST_URLS = [
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://72.push2.eastmoney.com/api/qt/clist/get",
    "https://83.push2.eastmoney.com/api/qt/clist/get",
]
# The push2 aliases share the same upstream service.  They are useful for a
# transient CDN fault, but repeatedly trying every alias can turn one blocked
# request into minutes of waiting.  Startup therefore uses a short list of
# hosts before falling back to an independent provider.
CLIST_STARTUP_URLS = [
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://82.push2.eastmoney.com/api/qt/clist/get",
]
SINA_MARKET_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_PAGE_SIZE = 100
SINA_MAX_PAGES = 70
INDEX_URLS = [
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
    "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
    "https://72.push2.eastmoney.com/api/qt/ulist.np/get",
    "https://83.push2.eastmoney.com/api/qt/ulist.np/get",
]
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"  # deprecated: returns 501
SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"

CLIST_FIELDS = "f12,f14,f2,f3,f4,f5,f6,f7,f8,f10,f15,f16,f17,f18,f20,f21,f100,f124,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
INDEX_SECIDS = "1.000001,0.399001,1.000300,0.399006"

DECISION_CASH = "cash"
DECISION_OBSERVE = "observe"
DECISION_LOW_ABSORB = "low_absorb_only"
DECISION_TRIAL = "trial_entry_allowed"
DECISION_AVOID = "avoid"

TIER_STRICT = "strict"
TIER_HOPEFUL = "hopeful"
TIER_AVOID = "avoid"

# 硬黑名单：框架明确记载的死亡螺旋/股灾级教训案例，无论资金面如何都不进入候选
# 新增教训需附带日期和根因，避免黑名单无限膨胀
HARD_BLACKLIST: Dict[str, str] = {
    "600664": "20260728-框架教训：哈药股份暴涨后高位派发+追高→死亡螺旋，绝不补仓/不做低吸",
}

# 软降权规则：以下条件任一满足 → 从低吸候选剔除（不标avoid，直接不出现）
# 1. 20日累计涨幅 ≥30% 且 近5日主力净占比持续<0（高位派发信号）
# 2. 硬黑名单命中
MARKET_WARNINGS: List[str] = []
MARKET_FETCH_STATUS: Dict[str, Any] = {
    "source": "eastmoney_push2",
    "complete": None,
    "expected_pages": 0,
    "received_pages": 0,
    "failed_pages": [],
    "provider_total": None,
    "retrieved_rows": 0,
}
NETWORK_MODE = "auto"
ANNOUNCEMENT_CACHE_FILE = SCRIPT_DIR / ".announcement_risk_cache.json"
_ANNOUNCEMENT_RISK_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
ANNOUNCEMENT_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
ANNOUNCEMENT_REQUEST_TIMEOUT_SECONDS = 4

# The raw dual-pool intersection remains an audit signal.  This second layer
# tracks whether that signal is new, confirmed, late, or actually eligible
# for a new position.  It never reads holdings.json or uses simulated trades.
INTERSECTION_STATE_FILE = SCRIPT_DIR / "intersection_state.json"
INTERSECTION_CALIBRATION_FILE = SCRIPT_DIR / "intersection_calibration.json"
DEFAULT_INTERSECTION_CONFIG = {
    # ── 原有：交易时间门槛 / 过热 / 信号年龄（保持不变）──
    "confirmation_snapshots": 2,
    "morning_cutoff": "11:00",
    "afternoon_start": "13:05",
    "afternoon_buy_deadline": "14:20",
    "overheat_change_pct": 4.5,
    "overheat_turnover_pct": 8.0,
    "signal_age_window_minutes": 30,
    # ── 四阶段状态机新增参数（全部可调）──
    # 交集事件基准：超短池 ∩ 趋势确认池(strict_trend)。双池交集从"买入信号"
    # 改为"启动事件"，真正买点由交集后的缩量回踩产生。
    "intersection_basis": "strict_trend",   # 仅作记录，引擎据此计算 dual_pool
    # 准交集四道门槛（全部满足才进入"准交集"预警）
    "pre_gate_main_net": True,      # 主力净流入（main_net>0）
    "pre_gate_flow_5m": True,       # 5分钟资金为正（flow_5m_inc>0）
    "pre_gate_above_vwap": True,    # 价格位于均价线上方
    "pre_gate_resonance": False,    # 板块共振已降为参考项（加权不否决）
    # 迟到过滤（首次交集时任一满足即标记"迟到交集"，不产生买点）
    "late_change_pct": 4.6,         # 涨幅已超过
    "late_vwap_dist_pct": 1.2,      # 距VWAP超过（%）
    "late_turnover_pct": 7.0,       # 换手率超过
    "late_high_pull_pct": 1.5,      # 高位回撤已超过（冲高回落）
    "late_pulse_change_pct": 3.5,   # 单根脉冲大阳：涨幅≥且几乎无回撤
    "late_pulse_high_pull_pct": 0.5,# 单根脉冲大阳：距高≤此值（一路拉升）
    "late_pulse_vol_ratio": 5.0,    # 单根脉冲大阳：量比≥
    # 交集信号锁存保留时长（分钟）：退出候选池后仍跟踪，避免闪一下消失
    "intersection_latch_minutes": 15,
    # 回踩确认规则
    "pullback_min_pct": 0.5,        # 从触发价回撤下限（%）
    "pullback_max_pct": 1.5,        # 从触发价回撤上限（%）
    "pullback_vol_ratio": 0.7,      # 回踩量 < 启动量 * 此比例
    "pullback_recover_pct": 0.4,    # 重新站回触发价附近（距触发价≤%，即"回踩确认/可试错"）
    "pullback_vwap_hold": True,     # 回踩不有效跌破VWAP
    "pullback_flow_5m_positive": True,  # 回踩期间5分钟主力资金仍为正
    # ── default-v2 增补：连续确认 / 分钟K新鲜度 / 市场环境 ──
    "pre_confirm_snapshots": 2,       # 准交集需连续通过的独立快照数
    "retest_confirm_snapshots": 2,    # 回踩确认需连续通过的独立快照数
    "minute_fresh_seconds": 180,      # 分钟K最大数据年龄（秒），超过不得用于回踩确认
    "market_breadth_normal": 55.0,    # 宽度≥此值：NORMAL
    "market_breadth_light": 48.0,     # 宽度≥此值：LIGHT
    "market_breadth_downgrade": 42.0, # 宽度≥此值：DOWNGRADE；低于：CASH
    "index_extreme_change_pct": -5.0, # 指数极端跌幅（如创业板≤-5%），环境至少降一级
    "index_extreme_codes": ["399006"],# 参与极端判定的指数代码（创业板指）
}
INTERSECTION_CONFIG_VERSION = "default-v2"

# ── default-v2 状态机相位（英文为规范值，中文仅供展示）──────────────
PHASE_OBSERVING = "OBSERVING"
PHASE_PRE = "PRE_INTERSECTION"
PHASE_LATCHED = "INTERSECTION_LATCHED"
PHASE_WAIT_RETEST = "WAIT_RETEST"
PHASE_RETEST_READY = "RETEST_READY"
PHASE_ENTRY = "ENTRY_ELIGIBLE"
PHASE_LATE = "LATE_INTERSECTION"
PHASE_INVALID = "INVALID"
PHASE_EXPIRED = "EXPIRED"

PHASE_LABELS = {
    PHASE_OBSERVING: "观察中",
    PHASE_PRE: "准交集",
    PHASE_LATCHED: "首次交集",
    PHASE_WAIT_RETEST: "等待回踩",
    PHASE_RETEST_READY: "回踩确认",
    PHASE_ENTRY: "可新开仓",
    PHASE_LATE: "迟到交集",
    PHASE_INVALID: "失效",
    PHASE_EXPIRED: "已过期",
}

# 严格推进顺序：同一轮快照最多前进一级，禁止跳级
PHASE_ORDER = [
    PHASE_OBSERVING, PHASE_PRE, PHASE_LATCHED,
    PHASE_WAIT_RETEST, PHASE_RETEST_READY, PHASE_ENTRY,
]


class NetworkUnavailable(RuntimeError):
    """Raised when neither the system proxy nor a direct connection can fetch data."""

    def __init__(self, url: str, errors: Dict[str, str]):
        self.url = url
        self.errors = errors
        super().__init__(f"network unavailable: {url}")


def set_network_mode(mode: str) -> None:
    global NETWORK_MODE
    NETWORK_MODE = mode


def format_network_failure(error: NetworkUnavailable) -> str:
    lines = [
        "网络连接失败：无法连接行情服务，本次未生成新的筛选结果。",
        "已尝试的连接方式：",
    ]
    for label, detail in error.errors.items():
        lines.append(f"- {label}：{detail}")
    lines.extend([
        "已自动尝试独立备用行情源。若仍失败，可在浏览器确认行情页能否打开后重试；",
        "如依赖公司/VPN代理，请把界面中的“网络连接”切到“系统代理”。",
    ])
    return "\n".join(lines)


def _system_proxy_url():
    """proxy 模式下从环境变量 / scutil 实时读代理地址（不固化，避免死代理拖累）。"""
    import subprocess
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if p:
        return p
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            import re
            txt = out.stdout or ""
            m = re.search(r"HTTPSProxy\s*:\s*([\d.]+)", txt)
            pm = re.search(r"HTTPSPort\s*:\s*(\d+)", txt)
            if m and pm:
                return f"http://{m.group(1)}:{pm.group(1)}"
    except Exception:
        pass
    return ""


def build_url_opener(network_mode: str):
    handlers = [urllib.request.HTTPSHandler(context=SSL_CONTEXT)]
    if network_mode == "direct":
        handlers.append(urllib.request.ProxyHandler({}))
    else:
        ph = _system_proxy_url()
        handlers.append(urllib.request.ProxyHandler({"http": ph, "https": ph} if ph else {}))
    return urllib.request.build_opener(*handlers)

HARD_ANNOUNCEMENT_KEYWORDS = [
    "减持", "被动减持", "清仓式减持", "监管函", "问询函", "关注函", "警示函",
    "立案", "调查", "行政处罚", "纪律处分", "公开谴责", "退市风险", "其他风险警示",
    "业绩预亏", "业绩亏损", "业绩预损", "业绩下修", "业绩修正", "大幅下降", "计提减值", "商誉减值",
    "限售股上市流通", "解除限售", "解禁", "股份冻结", "司法冻结", "诉讼", "仲裁",
    "债务逾期", "担保逾期", "资金占用", "无法表示意见", "保留意见", "停牌核查",
]
WATCH_ANNOUNCEMENT_KEYWORDS = [
    "质押", "担保", "关联交易", "业绩快报", "业绩预告", "更正公告", "补充公告",
    "高管辞职", "董事辞职", "会计政策变更", "审计机构", "股东大会延期",
]
ANNOUNCEMENT_IGNORE_KEYWORDS = [
    "权益分派", "分红", "法律意见书", "独立意见", "任职资格核准", "股东大会决议",
]


def fetch_json(url: str, params: Dict[str, Any], timeout: int = 6, retries: int = 0) -> Any:
    errors: Dict[str, str] = {}
    if NETWORK_MODE == "direct":
        sessions = [("直连", REQUESTS_DIRECT_SESSION)]
    elif NETWORK_MODE == "proxy":
        sessions = [("系统代理", REQUESTS_SESSION)]
    else:
        sessions = [("直连", REQUESTS_DIRECT_SESSION), ("系统代理", REQUESTS_SESSION)]

    for attempt in range(retries + 1):
        try:
            if requests is not None:
                headers = {
                    "User-Agent": UA,
                    "Referer": "https://finance.sina.com.cn/" if "sina.com.cn" in url else "https://quote.eastmoney.com/center/gridlist.html",
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }
                for label, session in sessions:
                    try:
                        resp = session.get(url, params=params, headers=headers, timeout=timeout, verify=False)
                        resp.raise_for_status()
                        _mark_host_ok(url)
                        return resp.json()
                    except Exception as exc:
                        detail = " ".join(str(exc).split())[:220]
                        errors[label] = f"{type(exc).__name__}: {detail}"
                        # RemoteDisconnected = stale pooled connection; reset pool
                        if "RemoteDisconnected" in detail or "ConnectionReset" in detail:
                            try:
                                session.close()
                            except Exception:
                                pass
                _mark_host_failed(url)
                raise NetworkUnavailable(url, errors)
            query = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": UA})
            opener = build_url_opener(NETWORK_MODE)
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)
        except NetworkUnavailable:
            pass
        except Exception as exc:  # network providers are not fully stable
            detail = " ".join(str(exc).split())[:220]
            errors["直连" if NETWORK_MODE == "direct" else "系统代理"] = f"{type(exc).__name__}: {detail}"
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    raise NetworkUnavailable(url, errors)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def _fmt(value: Any, nd: int = 2, default: str = "—") -> str:
    """None/NaN-safe numeric formatter for markdown tables.

    Avoids `unsupported format string passed to NoneType.__format__`
    crashes when an enriched row is missing price/volume_ratio/etc.
    """
    if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
        return f"{value:.{nd}f}"
    return default


def avg(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else float("nan")


def pct(value: float) -> str:
    return f"{_fmt(value, 2)}%"


def amount_yi(value: float) -> str:
    if not is_number(value):
        return "—"
    return f"{value / 100_000_000:.2f}亿"


def secid_for(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def tencent_code(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def current_status(ts: datetime) -> str:
    if ts.weekday() >= 5:
        return "非交易日/最新快照"
    if ((ts.hour == 9 and ts.minute >= 30) or (10 <= ts.hour < 11) or (ts.hour == 11 and ts.minute <= 30) or (13 <= ts.hour < 15)):
        return "盘中"
    return "非交易时段/最新快照"


@dataclass
class Enriched:
    code: str
    name: str
    price: float
    change: float
    turnover: float
    amount: float
    volume_ratio: float
    high: float
    low: float
    open: float
    prev_close: float
    total_mv: float
    float_mv: float
    industry: str
    timestamp: int
    volume: float
    kdate: str
    k_source: str
    adj_close: float
    ma5: float
    ma10: float
    ma20: float
    prev_ma5: float
    prev_ma10: float
    prev_ma20: float
    five_ret: float
    dist60: float
    ma20_dist: float
    high_pull: float
    cur_to_high: float
    vol_vs_avg5: float
    vwap: float
    vwap_state: str
    prior_high: float
    prior_low: float
    # capital flow
    main_net: float = 0
    main_pct: float = 0
    super_net: float = 0
    super_pct: float = 0
    big_net: float = 0
    big_pct: float = 0
    mid_net: float = 0
    mid_pct: float = 0
    small_net: float = 0
    small_pct: float = 0
    flow_5m_inc: float = 0
    flow_15m_inc: float = 0
    amount_5m_inc: float = 0
    volume_5m_inc: float = 0
    vol_ratio_vs_hist: float = 0
    vol_surge: bool = False
    price_above_vwap: bool = True
    flow_status: str = "数据不足"
    buy_ratio: float = float("nan")
    risk_status: str = "unknown"


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": str(row.get("f12", "")),
        "name": str(row.get("f14", "")),
        "price": row.get("f2"),
        "change": row.get("f3"),
        "change_abs": row.get("f4"),
        "volume": row.get("f5"),
        "amount": row.get("f6"),
        "amplitude": row.get("f7"),
        "turnover": row.get("f8"),
        "volume_ratio": row.get("f10"),
        "high": row.get("f15"),
        "low": row.get("f16"),
        "open": row.get("f17"),
        "prev_close": row.get("f18"),
        "total_mv": row.get("f20"),
        "float_mv": row.get("f21"),
        "industry": row.get("f100") or "-",
        "timestamp": row.get("f124") or 0,
        "main_net": row.get("f62") or 0,
        "main_pct": row.get("f184") or 0,
        "super_net": row.get("f66") or 0,
        "super_pct": row.get("f69") or 0,
        "big_net": row.get("f72") or 0,
        "big_pct": row.get("f75") or 0,
        "mid_net": row.get("f78") or 0,
        "mid_pct": row.get("f81") or 0,
        "small_net": row.get("f84") or 0,
        "small_pct": row.get("f87") or 0,
    }


def valid_main_board(row: Dict[str, Any]) -> bool:
    code = str(row.get("f12", ""))
    name = str(row.get("f14", ""))
    if not code.startswith(("60", "00")):
        return False
    if "ST" in name.upper() or name.startswith("*") or "退" in name:
        return False
    required = ["f2", "f3", "f5", "f6", "f8", "f10", "f15", "f18", "f21"]
    # The independent backup does not expose a compatible real-time volume
    # ratio.  Keep its trend-observation data available, but never synthesize
    # a ratio for strict ultra-short or intersection buy signals.
    if row.get("_source") == "sina_fallback":
        required.remove("f10")
    return all(row.get(k) not in (None, "-") for k in required)


def is_a_share_row(row: Dict[str, Any]) -> bool:
    """Keep mixed clist products out of A-share breadth calculations."""
    return str(row.get("f12", "")).startswith(("60", "00", "30", "68"))


def get_market_fetch_status() -> Dict[str, Any]:
    """Return a copy so reports cannot accidentally mutate fetch evidence."""
    return {
        **MARKET_FETCH_STATUS,
        "failed_pages": list(MARKET_FETCH_STATUS.get("failed_pages") or []),
    }


def _sina_number(value: Any) -> Optional[float]:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_sina_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map the independent Sina snapshot to the fields the screener knows.

    Sina does not provide Eastmoney's real-time volume ratio, capital-flow, or
    industry fields in this endpoint.  Those values intentionally remain
    missing instead of being guessed from unrelated data.
    """
    symbol = str(row.get("symbol") or "")
    code = str(row.get("code") or "")
    if symbol.startswith(("sh", "sz")):
        code = symbol[2:]
    if not code:
        return None

    price = _sina_number(row.get("trade"))
    prev_close = _sina_number(row.get("settlement"))
    high = _sina_number(row.get("high"))
    low = _sina_number(row.get("low"))
    open_price = _sina_number(row.get("open"))
    volume = _sina_number(row.get("volume"))
    amount = _sina_number(row.get("amount"))
    turnover = _sina_number(row.get("turnoverratio"))
    total_mv = _sina_number(row.get("mktcap"))
    float_mv = _sina_number(row.get("nmc"))
    change = _sina_number(row.get("changepercent"))
    if price is None or prev_close is None:
        return None

    return {
        "f12": code,
        "f14": str(row.get("name") or ""),
        "f2": price,
        "f3": change,
        "f4": _sina_number(row.get("pricechange")),
        # Sina volumes are shares while Eastmoney's f5 is hands.
        "f5": volume / 100 if volume is not None else None,
        "f6": amount,
        "f7": _sina_number(row.get("amplitude")),
        "f8": turnover,
        "f10": None,
        "f15": high,
        "f16": low,
        "f17": open_price,
        "f18": prev_close,
        "f20": total_mv * 10_000 if total_mv is not None else None,
        "f21": float_mv * 10_000 if float_mv is not None else None,
        "f100": "-",
        "f124": 0,
        "_source": "sina_fallback",
    }


def fetch_sina_market() -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Fetch a broad A-share fallback snapshot from Sina in bounded batches.

    There is no authoritative total in this endpoint.  The caller must treat
    this as a data-quality downgrade even when all pages appear available.
    """
    def _fetch_page(page: int) -> Tuple[int, List[Dict[str, Any]]]:
        data = fetch_json(SINA_MARKET_URL, {
            "page": page,
            "num": SINA_PAGE_SIZE,
            "sort": "symbol",
            "asc": 1,
            "node": "hs_a",
            "_s_r_a": "page",
        })
        if not isinstance(data, list):
            raise RuntimeError("新浪行情返回格式异常")
        return page, data

    raw_pages: Dict[int, List[Dict[str, Any]]] = {}
    first_error: Optional[Exception] = None
    reached_end = False
    for batch_start in range(1, SINA_MAX_PAGES + 1, 10):
        pages = list(range(batch_start, min(batch_start + 10, SINA_MAX_PAGES + 1)))
        with futures.ThreadPoolExecutor(max_workers=len(pages)) as pool:
            pending = {pool.submit(_fetch_page, page): page for page in pages}
            for future in futures.as_completed(pending):
                page = pending[future]
                try:
                    _, data = future.result()
                    raw_pages[page] = data
                except Exception as exc:
                    first_error = first_error or exc

        if not raw_pages and first_error is not None:
            raise first_error
        if any(len(raw_pages.get(page, [])) < SINA_PAGE_SIZE for page in pages):
            reached_end = True
            break

    if not raw_pages:
        raise first_error or RuntimeError("新浪备用行情未返回数据")

    rows: List[Dict[str, Any]] = []
    for page in sorted(raw_pages):
        for raw in raw_pages[page]:
            normalized = _normalize_sina_row(raw)
            if normalized is not None:
                rows.append(normalized)

    if not reached_end:
        MARKET_WARNINGS.append("新浪备用行情达到分页上限，当前仅为局部快照")
    return rows, len(rows)


def fetch_market() -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Fetch a verifiable market snapshot instead of silently accepting pages.

    Eastmoney returns clist data in the requested sort order.  A failed tail
    page therefore used to look like a valid all-riser market when ``fid=f3``
    was used.  We fetch in stable code order, retry missing pages with lower
    concurrency, and expose completion evidence to callers.
    """
    MARKET_FETCH_STATUS.update({
        "source": "eastmoney_push2",
        "complete": None,
        "expected_pages": 0,
        "received_pages": 0,
        "failed_pages": [],
        "provider_total": None,
        "retrieved_rows": 0,
    })
    rows: List[Dict[str, Any]] = []
    total: Optional[int] = None
    # Eastmoney's clist endpoint currently caps this route at 100 rows even
    # when a larger ``pz`` is requested.  Requesting the real cap keeps the
    # expected page count aligned with what the server actually returns.
    page_size = 100

    def _make_params(page: int) -> Dict[str, Any]:
        return {
            "pn": page, "pz": page_size, "po": 1, "np": 1,
            "ut": EASTMONEY_UT, "fltt": 2, "invt": 2, "fid": "f12",
            "fs": "m:1+t:2,m:0+t:6,m:0+t:80,m:1+t:23",  # 已去掉 m:0+t:81(北交所+新三板)：剔除场外层、减少约55%全市场拉取量
            "fields": CLIST_FIELDS,
        }

    # --- page 1: discover total ------------------------------------------
    last_error: Optional[Exception] = None

    def _fetch_first_page() -> Optional[dict]:
        """首页决定全量，失败即坠入降级；每 host 重试 2 次抗击瞬时抖动。"""
        nonlocal last_error
        for url in _rank_urls(CLIST_STARTUP_URLS):
            for attempt in range(2):
                try:
                    data = fetch_json(url, _make_params(1))
                    if data.get("data") is not None:
                        return data
                    break  # 该 host 返回空 data，换下一 host
                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        time.sleep(0.3)
        return None

    first_data = None
    in_cooldown = _em_in_cooldown()
    if in_cooldown:
        MARKET_WARNINGS.append("东方财富处于冷却期，但本次仍会尝试主源（若成功则自动恢复）")
    # 首页失败会直接坠入新浪降级并把池子清空，因此值得多试几轮。
    # cooldown 内试 2 轮，正常试 4 轮，每轮每 host 重试 2 次，轮间递增退避。
    max_rounds = 2 if in_cooldown else 4
    for startup_round in range(max_rounds):
        first_data = _fetch_first_page()
        if first_data is not None:
            _em_clear_cooldown()
            MARKET_WARNINGS.clear()  # cooldown/降级相关提示已不适用
            break
        if startup_round < max_rounds - 1:
            time.sleep(1.5 * (startup_round + 1))
    if first_data is None:
        _em_set_cooldown()
        try:
            fallback_rows, fallback_total = fetch_sina_market()
        except Exception:
            if isinstance(last_error, NetworkUnavailable):
                raise last_error
            raise RuntimeError(f"行情主源和备用源均失败：{last_error}")
        MARKET_FETCH_STATUS.update({
            "source": "sina_fallback",
            "complete": False,
            "expected_pages": 0,
            "received_pages": 0,
            "failed_pages": [],
            # This endpoint has no authoritative total.  Do not promote the
            # retrieved count into a fake provider total.
            "provider_total": None,
            "retrieved_rows": len(fallback_rows),
        })
        MARKET_WARNINGS.append(
            "东方财富实时列表不可用，已切换新浪备用行情；量比、主力资金和行业字段缺失，"
            "市场宽度、板块共振及严格超短/交集买点已禁用"
        )
        return fallback_rows, None

    _em_clear_cooldown()  # East Money is back; reset cooldown
    body = first_data.get("data") or {}
    diff = body.get("diff") or []
    total = body.get("total")
    MARKET_FETCH_STATUS["provider_total"] = total
    if diff:
        rows.extend(diff)

    if not isinstance(total, int) or total < 0:
        MARKET_FETCH_STATUS.update({
            "complete": False,
            "expected_pages": 1,
            "received_pages": 1 if diff else 0,
            "failed_pages": [] if diff else [1],
            "retrieved_rows": len(rows),
        })
        MARKET_WARNINGS.append("行情服务端未返回可校验总数，当前仅为局部快照；市场宽度和板块共振已禁用")
        return rows, total

    if not diff and total > 0:
        MARKET_FETCH_STATUS.update({
            "complete": False,
            "expected_pages": math.ceil(total / page_size),
            "received_pages": 0,
            "failed_pages": [1],
            "retrieved_rows": 0,
        })
        MARKET_WARNINGS.append("行情第一页为空，当前仅为局部快照；市场宽度和板块共振已禁用")
        return rows, total

    expected_pages = max(1, math.ceil(total / page_size))
    page_rows: Dict[int, List[Dict[str, Any]]] = {1: diff} if diff else {}

    def _fetch_page(pn: int) -> Tuple[int, List[Dict[str, Any]]]:
        # 随机抖动避免固定间隔被识别为爬虫
        time.sleep(random.uniform(0.3, 0.8))
        for url in _rank_urls(CLIST_URLS):
            # 单 host 失败重试一次，抗击瞬时抖动/限流（之前任一异常即跳下一 host）
            for attempt in range(2):
                try:
                    data = fetch_json(url, _make_params(pn))
                    bd = (data.get("data") or {}).get("diff") or []
                    if bd:
                        return pn, bd
                    break  # 该 host 返回空 diff，换下一个 host 试试
                except Exception:
                    if attempt == 0:
                        time.sleep(0.3)
        return pn, []

    def _collect_pages(pages: List[int], workers: int) -> List[int]:
        missing: List[int] = []
        if not pages:
            return missing
        with futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(pages)))) as pool:
            futs = {pool.submit(_fetch_page, pn): pn for pn in pages}
            for fut in futures.as_completed(futs):
                pn, bd = fut.result()
                if bd:
                    page_rows[pn] = bd
                else:
                    missing.append(pn)
        return sorted(missing)

    # Two concurrent workers cap burst rate below provider's threshold.
    # Three workers previously triggered persistent rate limiting (2026-08 batch).
    missing_pages = _collect_pages(list(range(2, expected_pages + 1)), workers=2)
    # 失败页重试 5 轮（指数退避 0.5/1/2/4/8s），并发随轮次递减。
    # 交易时段东财接口偶发限流会让个别页失败，多轮退避能显著减少「快照不完整」。
    for retry_round in range(5):
        if not missing_pages:
            break
        time.sleep(0.5 * (2 ** retry_round))
        missing_pages = _collect_pages(missing_pages, workers=max(1, 3 - (retry_round // 2)))

    for pn in sorted(page_rows):
        if pn != 1:
            rows.extend(page_rows[pn])

    received_pages = len(page_rows)
    complete = not missing_pages and received_pages == expected_pages and len(rows) >= total
    MARKET_FETCH_STATUS.update({
        "complete": complete,
        "expected_pages": expected_pages,
        "received_pages": received_pages,
        "failed_pages": missing_pages,
        "provider_total": total,
        "retrieved_rows": len(rows),
    })
    if not complete:
        failed_text = ",".join(map(str, missing_pages)) if missing_pages else "无"
        MARKET_WARNINGS.append(
            "行情全量快照不完整："
            f"应取 {expected_pages} 页，已取 {received_pages} 页，失败页 {failed_text}，"
            f"服务端 {total} 条 / 实得 {len(rows)} 条；当前仅为局部快照，"
            "市场宽度和板块共振已禁用"
        )

    # 密集分页请求后冷却，避免紧跟的指数/板块/K线请求触发东财限流
    time.sleep(2.0 + random.uniform(0, 1.0))
    return rows, total


def fetch_indices() -> List[Dict[str, Any]]:
    params = {
        "fltt": 2,
        "invt": 2,
        "fields": "f12,f14,f2,f3,f4,f6,f104,f105,f106,f124",
        "secids": INDEX_SECIDS,
        "ut": EASTMONEY_UT,
    }
    for url in _rank_urls(INDEX_URLS):
        try:
            data = fetch_json(url, params)
            _mark_host_ok(url)
            return (data.get("data") or {}).get("diff") or []
        except Exception:
            continue
    MARKET_WARNINGS.append("所有指数接口失败，指数数据缺失")
    return []


def fetch_sector_indices() -> List[Dict[str, Any]]:
    """Fetch all industry board indices from East Money (行业板块)."""
    all_boards: List[Dict[str, Any]] = []
    for page in range(1, 6):  # paginate: 5 pages × 100 covers all ~500 boards
        params = {
            "pn": page,
            "pz": 100,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f4,f8,f104,f105,f124",
            "ut": EASTMONEY_UT,
        }
        got_page = False
        for url in _rank_urls(CLIST_URLS):
            try:
                data = fetch_json(url, params)
                boards = ((data.get("data") or {}).get("diff") or [])
                for b in boards:
                    name = b.get("f14", "")
                    if not name:
                        continue
                    all_boards.append({
                        "name": name,
                        "price": b.get("f2"),
                        "change": b.get("f3"),
                        "turnover": b.get("f8"),
                        "up_count": b.get("f104"),
                        "down_count": b.get("f105"),
                    })
                got_page = True
                if len(boards) < 100:
                    return all_boards  # last page
                break
            except Exception:
                continue
        if not got_page:
            break
    if not all_boards:
        MARKET_WARNINGS.append("板块指数查询失败")
    return all_boards


def collect_announcement_titles(obj: Any) -> List[str]:
    titles: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            lower = str(key).lower()
            if isinstance(value, str) and ("title" in lower or "announcement" in lower or "notice" in lower):
                title = value.strip()
                if title:
                    titles.append(title)
            else:
                titles.extend(collect_announcement_titles(value))
    elif isinstance(obj, list):
        for item in obj:
            titles.extend(collect_announcement_titles(item))
    seen = set()
    deduped = []
    for title in titles:
        if title not in seen:
            seen.add(title)
            deduped.append(title)
    return deduped


def fetch_announcements(code: str, page_size: int = 8) -> List[str]:
    data = fetch_json(ANNOUNCEMENT_URL, {
        "sr": -1,
        "page_size": page_size,
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
    }, timeout=ANNOUNCEMENT_REQUEST_TIMEOUT_SECONDS, retries=0)
    return collect_announcement_titles(data)[:page_size]


def classify_announcement_risk(titles: List[str]) -> Dict[str, Any]:
    filtered = [t for t in titles if not any(k in t for k in ANNOUNCEMENT_IGNORE_KEYWORDS)]
    hard = sorted({k for t in filtered for k in HARD_ANNOUNCEMENT_KEYWORDS if k in t})
    watch = sorted({k for t in filtered for k in WATCH_ANNOUNCEMENT_KEYWORDS if k in t})
    if hard:
        level = "avoid"
    elif watch:
        level = "watch_risk"
    else:
        level = "clean"
    return {
        "announcement_risk": level,
        "announcement_keywords": hard or watch,
        "announcement_titles": filtered[:3],
    }


def _load_announcement_risk_cache() -> Dict[str, Dict[str, Any]]:
    global _ANNOUNCEMENT_RISK_CACHE
    if _ANNOUNCEMENT_RISK_CACHE is not None:
        return _ANNOUNCEMENT_RISK_CACHE
    try:
        payload = json.loads(ANNOUNCEMENT_CACHE_FILE.read_text(encoding="utf-8"))
        _ANNOUNCEMENT_RISK_CACHE = payload if isinstance(payload, dict) else {}
    except Exception:
        _ANNOUNCEMENT_RISK_CACHE = {}
    return _ANNOUNCEMENT_RISK_CACHE


def _save_announcement_risk_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    try:
        ANNOUNCEMENT_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # A cache write failure must never turn a valid screening result into
        # a hard error. The in-memory cache still protects this run.
        pass


def _row_risk_status(row: Any) -> str:
    """Normalize announcement risk without treating missing as clean. Supports Dict and Enriched objects."""
    if row is None:
        return "unknown"
    if isinstance(row, dict):
        status = row.get("risk_status")
        if status in {"clean", "watch_risk", "avoid", "unknown"}:
            return status
        legacy = row.get("announcement_risk")
        if legacy in {"clean", "watch_risk", "avoid"}:
            return legacy
        return "unknown"
    status = getattr(row, "risk_status", None)
    if status in {"clean", "watch_risk", "avoid", "unknown"}:
        return status
    legacy = getattr(row, "announcement_risk", None)
    if legacy in {"clean", "watch_risk", "avoid"}:
        return legacy
    return "unknown"


def announcement_label(row: Dict[str, Any]) -> str:
    risk = _row_risk_status(row)
    keywords = row.get("announcement_keywords") or []
    if keywords:
        return f"{risk}({','.join(keywords[:3])})"
    if risk == "unknown":
        return "unknown(公告检查不可用)"
    return risk


def append_risk_text(existing: str, extra: str) -> str:
    if not extra:
        return existing
    if not existing or existing == "无":
        return extra
    if extra in existing:
        return existing
    return f"{existing}/{extra}"


def attach_announcement_risks(
    result: Dict[str, Any],
    page_size: int,
    workers: int,
    risk_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    progress_callback: Optional[Callable[[int, int, str, str, str], None]] = None,
) -> List[str]:
    rows: List[Dict[str, Any]] = []
    # Diagnostics can contain the full pre-truncation ultra candidate set.
    # They are for explanation only, so they must not turn one checkbox into
    # one network request per near-miss stock. Check only visible/actionable
    # sections; matching diagnostic rows are annotated from this same map.
    for section in (
        "strict_ultra", "trend_observation", "strict_trend", "dual_pool",
        "capital_rank", "dual_pool_raw", "low_ultra", "low_trend", "watchlist",
        "pre_intersection",   # 准交集表必须与其它表读到同一份风险结果
    ):
        rows.extend(result.get(section) or [])
    codes = sorted({str(r.get("code")) for r in rows if r.get("code")})
    result["announcement_total_count"] = len(codes)
    if not codes:
        result["announcement_check_available"] = True
        result["announcement_unknown_codes"] = []
        result["announcement_cached_count"] = 0
        result["announcement_requested_count"] = 0
        return []

    cache = risk_cache if risk_cache is not None else _load_announcement_risk_cache()
    ann_map: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    cache_changed = False
    now = time.time()
    pending_codes: List[str] = []
    for code in codes:
        cached = cache.get(code) or {}
        cached_status = cached.get("status")
        checked_at = cached.get("checked_at")
        try:
            cache_fresh = checked_at is not None and now - float(checked_at) <= ANNOUNCEMENT_CACHE_TTL_SECONDS
        except (TypeError, ValueError):
            cache_fresh = False
        if cache_fresh and cached_status in {"clean", "watch_risk", "avoid"}:
            ann_map[code] = {
                "announcement_risk": cached_status,
                "risk_status": cached_status,
                "announcement_keywords": cached.get("keywords") or [],
                "announcement_titles": cached.get("titles") or [],
                "announcement_check": "cached",
                "announcement_risk_source": "cache",
            }
        else:
            pending_codes.append(code)

    total = len(codes)
    completed = 0

    def report_progress(code: str, status: str, source: str) -> None:
        nonlocal completed
        completed += 1
        if progress_callback is not None:
            try:
                progress_callback(completed, total, code, status, source)
            except Exception:
                pass

    for code in codes:
        if code in ann_map:
            report_progress(code, ann_map[code]["risk_status"], "cache")

    with futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
        future_map = {pool.submit(fetch_announcements, code, page_size): code for code in pending_codes}
        for fut in futures.as_completed(future_map):
            code = future_map[fut]
            try:
                classified = classify_announcement_risk(fut.result())
                status = classified["announcement_risk"]
                ann_map[code] = {
                    **classified,
                    "risk_status": status,
                    "announcement_check": "checked",
                    "announcement_risk_source": "current",
                }
                cache[code] = {
                    "status": status,
                    "keywords": classified.get("announcement_keywords") or [],
                    "titles": classified.get("announcement_titles") or [],
                    "checked_at": time.time(),
                }
                cache_changed = True
                report_progress(code, status, "current")
            except Exception:
                errors.append(code)
                cached = cache.get(code) or {}
                cached_status = cached.get("status")
                # A stale last-known avoid is still safer than silently
                # downgrading a known risk to unknown when the source fails.
                # TTL controls re-query skipping, not risk erasure.
                if cached_status == "avoid":
                    ann_map[code] = {
                        "announcement_risk": "avoid",
                        "risk_status": "avoid",
                        "announcement_keywords": cached.get("keywords") or [],
                        "announcement_titles": cached.get("titles") or [],
                        "announcement_check": "unavailable",
                        "announcement_risk_source": "last-known",
                    }
                else:
                    ann_map[code] = {
                        "announcement_risk": "unknown",
                        "risk_status": "unknown",
                        "announcement_keywords": [],
                        "announcement_titles": [],
                        "announcement_check": "unavailable",
                        "announcement_risk_source": "last-known" if cached_status else "none",
                        "last_known_risk": cached_status or "",
                    }
                report_progress(code, ann_map[code]["risk_status"], ann_map[code]["announcement_risk_source"])

    result["announcement_cached_count"] = len(codes) - len(pending_codes)
    result["announcement_requested_count"] = len(pending_codes)

    # Visible diagnostics share the checked status when their code is also a
    # visible candidate; otherwise they remain unknown without causing I/O.
    rows_to_update = list(rows)
    for row in result.get("trend_diagnostics") or []:
        if str(row.get("code")) in ann_map:
            rows_to_update.append(row)

    for row in rows_to_update:
        info = ann_map.get(str(row.get("code")), {
            "announcement_risk": "unknown",
            "risk_status": "unknown",
            "announcement_keywords": [],
            "announcement_titles": [],
            "announcement_check": "unavailable",
            "announcement_risk_source": "none",
        })
        row.update(info)
        status = _row_risk_status(row)
        if status == "avoid":
            row["risk"] = append_risk_text(str(row.get("risk") or "无"), "公告硬风险")
            if row.get("class") in ("A", "B"):
                row["class"] = "C"
        elif status == "watch_risk":
            row["risk"] = append_risk_text(str(row.get("risk") or "无"), "公告观察风险")
        elif status == "unknown":
            row["risk"] = append_risk_text(str(row.get("risk") or "无"), "公告检查不可用")
    # 统一按代码索引的公告风险映射：所有模块（含状态机）从这里读取，
    # 不得各自重新查询或重新解析。缺失一律 unknown（fail-closed）。
    result["announcement_risk_map"] = {
        code: info.get("risk_status", "unknown") for code, info in ann_map.items()
    }
    result["announcement_errors"] = sorted(set(errors))
    result["announcement_check_available"] = not errors
    result["announcement_unknown_codes"] = sorted({
        str(row.get("code"))
        for row in rows
        if _row_risk_status(row) == "unknown"
    })
    if risk_cache is None and cache_changed:
        _save_announcement_risk_cache(cache)
    return errors


def apply_announcement_pool_gates(result: Dict[str, Any]) -> None:
    """Apply fail-closed announcement gates after every screening run."""
    unknown_codes = {
        str(row.get("code"))
        for section in (
            "strict_ultra", "trend_observation", "strict_trend", "dual_pool", "dual_pool_raw",
            "pre_intersection", "capital_rank", "trend_diagnostics", "low_ultra", "low_trend", "watchlist",
        )
        for row in (result.get(section) or [])
        if _row_risk_status(row) == "unknown"
    }
    result["announcement_unknown_codes"] = sorted(
        set(result.get("announcement_unknown_codes") or []) | unknown_codes
    )
    if result.get("announcement_errors"):
        result["announcement_check_available"] = False

    # Keep the raw ultra pool visible for auditability. Unknown and avoid
    # statuses are not allowed to upgrade into a tradeable recommendation.
    result["trend_observation"] = [
        row for row in (result.get("trend_observation") or [])
        if _row_risk_status(row) != "avoid"
    ]
    for section in ("strict_trend", "dual_pool", "capital_rank"):
        result[section] = [
            row for row in (result.get(section) or [])
            if _row_risk_status(row) not in {"avoid", "unknown"}
        ]
    for section in ("low_ultra", "low_trend"):
        for row in result.get(section) or []:
            status = _row_risk_status(row)
            if status == "avoid":
                row["risk"] = append_risk_text(str(row.get("risk") or "无"), "公告硬风险")
                row["class"] = "C"
            elif status == "unknown":
                row["risk"] = append_risk_text(str(row.get("risk") or "无"), "公告检查不可用")
                row["class"] = "C"
    for row in result.get("trend_diagnostics") or []:
        status = _row_risk_status(row)
        if status == "avoid":
            row["upgrade_status"] = "公告avoid，禁止升级"
        elif status == "unknown":
            row["upgrade_status"] = "公告unknown，数据不足，禁止升级"


def parse_k_rows(rows: List[Any]) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for p in rows:
        if isinstance(p, str):
            parts = p.split(",")
        else:
            parts = p
        if len(parts) < 6:
            continue
        out.append({
            "date": str(parts[0]),
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "vol": float(parts[5]),
        })
    return out


def _parse_sina_kline(data: Any) -> List[Dict[str, float]]:
    """Parse Sina K-line response: list of {day, open, high, low, close, volume}."""
    rows: List[Dict[str, float]] = []
    if not isinstance(data, list):
        return rows
    for item in data:
        try:
            rows.append({
                "date": str(item.get("day") or item.get("date") or "")[:10],
                "close": float(item["close"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "vol": float(item.get("volume", 0)),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def fetch_kline(code: str, limit: int = 90) -> Tuple[List[Dict[str, float]], str]:
    # 1. East Money (primary, fast and reliable)
    try:
        data = fetch_json(EM_KLINE_URL, {
            "secid": secid_for(code),
            "ut": EASTMONEY_UT,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": 101,
            "fqt": 1,
            "end": "20500101",
            "lmt": limit,
        }, timeout=5, retries=1)
        rows = (data.get("data") or {}).get("klines") or []
        parsed = parse_k_rows(rows)
        if len(parsed) >= min(65, limit):
            return parsed, "eastmoney_qfq"
    except Exception:
        pass
    # 2. Sina (fallback, ~0.05s per request)
    try:
        symbol = ("sh" if code.startswith("6") else "sz") + code
        data = fetch_json(SINA_KLINE_URL, {
            "symbol": symbol, "scale": 240, "ma": "no", "datalen": limit,
        }, timeout=5, retries=1)
        parsed = _parse_sina_kline(data)
        if len(parsed) >= min(65, limit):
            return parsed, "sina_daily"
    except Exception:
        pass
    raise RuntimeError(f"all kline sources failed for {code}")


def enrich(row: Dict[str, Any]) -> Optional[Enriched]:
    r = normalize_row(row)
    rows, source = fetch_kline(r["code"], 90)
    if len(rows) < 65:
        return None
    closes = [x["close"] for x in rows]
    highs = [x["high"] for x in rows]
    lows = [x["low"] for x in rows]
    vols = [x["vol"] for x in rows]
    ma5 = avg(closes[-5:])
    ma10 = avg(closes[-10:])
    ma20 = avg(closes[-20:])
    prev_ma5 = avg(closes[-6:-1])
    prev_ma10 = avg(closes[-11:-1])
    prev_ma20 = avg(closes[-21:-1])
    latest = closes[-1]
    high_chg = (r["high"] / r["prev_close"] - 1) * 100 if r["prev_close"] else float("nan")
    high_pull = max(0.0, high_chg - r["change"]) if is_number(high_chg) else float("nan")
    vwap = r["amount"] / (r["volume"] * 100) if r["volume"] else float("nan")
    vwap_state = "均价线上方" if is_number(vwap) and r["price"] >= vwap else ("均价线下方" if is_number(vwap) else "无法验证")
    prior_high = highs[-2] if len(highs) >= 2 else float("nan")
    prior_low = lows[-2] if len(lows) >= 2 else float("nan")
    price_above_vwap = is_number(vwap) and r["price"] >= vwap
    return Enriched(
        code=r["code"], name=r["name"], price=r["price"], change=r["change"], turnover=r["turnover"],
        amount=r["amount"], volume_ratio=r["volume_ratio"], high=r["high"], low=r["low"], open=r["open"],
        prev_close=r["prev_close"], total_mv=r["total_mv"], float_mv=r["float_mv"], industry=r["industry"],
        timestamp=r["timestamp"], volume=r["volume"], kdate=rows[-1]["date"], k_source=source, adj_close=latest,
        ma5=ma5, ma10=ma10, ma20=ma20, prev_ma5=prev_ma5, prev_ma10=prev_ma10, prev_ma20=prev_ma20,
        five_ret=latest / closes[-6] - 1 if len(closes) >= 6 and closes[-6] else float("nan"),
        dist60=max(highs[-60:]) / latest - 1 if latest else float("nan"),
        ma20_dist=r["price"] / ma20 - 1 if ma20 else float("nan"),
        high_pull=high_pull,
        cur_to_high=r["high"] / r["price"] - 1 if r["price"] else float("nan"),
        vol_vs_avg5=r["volume"] / avg(vols[-6:-1]) if avg(vols[-6:-1]) else float("nan"),
        vwap=vwap,
        vwap_state=vwap_state,
        prior_high=prior_high,
        prior_low=prior_low,
        main_net=r.get("main_net", 0), main_pct=r.get("main_pct", 0),
        super_net=r.get("super_net", 0), super_pct=r.get("super_pct", 0),
        big_net=r.get("big_net", 0), big_pct=r.get("big_pct", 0),
        mid_net=r.get("mid_net", 0), mid_pct=r.get("mid_pct", 0),
        small_net=r.get("small_net", 0), small_pct=r.get("small_pct", 0),
        price_above_vwap=price_above_vwap,
        buy_ratio=round((100.0 + float(r.get("main_pct", 0))) / max(1.0, 100.0 - float(r.get("main_pct", 0))), 2) if is_number(r.get("main_pct")) else float("nan"),
    )


def sector_stats(rows: List[Dict[str, Any]], breadth: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "adv": 0, "strong": 0, "sum": 0.0})
    for row in rows:
        if not is_a_share_row(row) or not is_number(row.get("f3")):
            continue
        sec = row.get("f100") or "-"
        stats[sec]["n"] += 1
        stats[sec]["sum"] += row.get("f3") or 0.0
        if row.get("f3", 0) > 0:
            stats[sec]["adv"] += 1
        if str(row.get("f12", "")).startswith(("60", "00")) and row.get("f3", 0) >= 2.2 and row.get("f6", 0) >= 300_000_000:
            stats[sec]["strong"] += 1
    quality = breadth or {}
    stats["__meta__"] = {
        "resonance_usable": bool(quality.get("resonance_usable", True)),
        "quality_reason": quality.get("quality_reason", ""),
    }
    return stats


def has_resonance(e: Enriched, stats: Dict[str, Dict[str, Any]]) -> bool:
    meta = stats.get("__meta__") or {}
    if not meta.get("resonance_usable", True):
        return False
    sec = stats.get(e.industry) or {}
    if sec.get("strong", 0) < 2:
        return False
    # Also require sector-wide bullish: either ≥30% of stocks advancing,
    # or the sector's average change is positive.  This prevents a bearish
    # sector (e.g. 18↑/162↓) from passing resonance on the strength of a
    # couple of outliers.
    n = sec.get("n", 0)
    adv = sec.get("adv", 0)
    avg_change = sec.get("sum", 0) / n if n > 0 else 0.0
    adv_ratio = adv / n if n > 0 else 0.0
    return adv_ratio >= 0.3 or avg_change > 0


def strict_ultra(e: Enriched) -> bool:
    return (
        is_number(e.volume_ratio)
        and 5 <= e.price <= 30 and e.float_mv < 20_000_000_000 and e.turnover > 3 and e.volume_ratio > 1.2
        and e.amount > 300_000_000 and 2 <= e.change <= 5.5 and e.adj_close > e.ma5
        and e.five_ret <= 0.18 and e.cur_to_high <= 0.03
    )


def strict_trend(e: Enriched) -> bool:
    return (
        8 <= e.price <= 45 and 3_000_000_000 <= e.float_mv <= 25_000_000_000
        and e.price > e.ma5 and e.price > e.ma10 and e.price > e.ma20 and e.ma20 > e.prev_ma20
        and e.dist60 <= 0.10 and 2 <= e.turnover <= 8 and e.vol_vs_avg5 <= 2
        and 3 <= e.change <= 6.5 and e.five_ret <= 0.20
    )


def trend_confirm_relaxed(e: Enriched) -> bool:
    """趋势确认的质量条件（不含价格区间/流通市值）。

    超短池已约束价格 5–30 与流通市值上限，趋势侧复检价格区间属冗余且会
    错误排除哈药(~5.8)等低价强势股。交集状态机用此判定"正式交集"，与
    strict_trend 相比只去掉价格区间、流通市值两项。
    """
    return (
        e.price > e.ma5 and e.price > e.ma10 and e.price > e.ma20
        and e.ma20 > e.prev_ma20
        and 3 <= e.change <= 6.5 and e.five_ret <= 0.20 and e.dist60 <= 0.10
        and 2 <= e.turnover <= 8 and e.amount > 300_000_000 and e.vol_vs_avg5 <= 2
    )


def _trend_quality_checks(e: Enriched) -> List[tuple]:
    """趋势质量条件清单（不含价格区间/流通市值），供准交集"差一项"判定。"""
    return [
        ("价格相对MA5", e.price > e.ma5),
        ("价格相对MA10", e.price > e.ma10),
        ("价格相对MA20", e.price > e.ma20),
        ("MA20斜率", e.ma20 > e.prev_ma20),
        ("当日涨幅", 3 <= e.change <= 6.5),
        ("5日涨幅", e.five_ret <= 0.20),
        ("距60日高点", e.dist60 <= 0.10),
        ("换手率", 2 <= e.turnover <= 8),
        ("成交额", e.amount > 300_000_000),
        ("量能相对5日", e.vol_vs_avg5 <= 2),
    ]


def trend_observation(e: Enriched) -> bool:
    """Identify intact/recovering trends without diluting trend confirmation.

    This pool deliberately admits modest red-to-green repair days, but it
    retains the MA20 trend, liquidity and overheating safeguards.  Strict
    confirmation remains a separate, unchanged rule and is promoted into this
    pool so that confirmation is always an upgrade rather than a parallel
    classification.
    """
    if strict_trend(e):
        return True
    return (
        8 <= e.price <= 45
        and 3_000_000_000 <= e.float_mv <= 25_000_000_000
        and e.amount > 300_000_000
        and e.price >= e.ma20
        and e.ma20 > e.prev_ma20
        and e.price >= e.ma5 * 0.985
        and e.ma5 >= e.ma10 * 0.985
        and -1 <= e.change <= 4.5
        and 1.5 <= e.turnover <= 10
        and e.vol_vs_avg5 <= 2.5
        and e.five_ret <= 0.20
        and e.dist60 <= 0.15
    )


def trend_condition_diagnosis(e: Enriched, stats: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Explain every strict confirmation condition for an ultra candidate."""
    checks = [
        ("价格区间", 8 <= e.price <= 45),
        ("流通市值", 3_000_000_000 <= e.float_mv <= 25_000_000_000),
        ("价格相对MA5", e.price > e.ma5),
        ("价格相对MA10", e.price > e.ma10),
        ("价格相对MA20", e.price > e.ma20),
        ("MA20斜率", e.ma20 > e.prev_ma20),
        ("当日涨幅", 3 <= e.change <= 6.5),
        ("5日涨幅", e.five_ret <= 0.20),
        ("距60日高点", e.dist60 <= 0.10),
        ("换手率", 2 <= e.turnover <= 8),
        ("成交额", e.amount > 300_000_000),
        ("量能相对5日", e.vol_vs_avg5 <= 2),
    ]
    failures = [name for name, passed in checks if not passed]
    observation_checks = [
        ("价格区间", 8 <= e.price <= 45),
        ("流通市值", 3_000_000_000 <= e.float_mv <= 25_000_000_000),
        ("成交额", e.amount > 300_000_000),
        ("价格相对MA20", e.price >= e.ma20),
        ("MA20斜率", e.ma20 > e.prev_ma20),
        ("MA5/MA10未明显转弱", e.price >= e.ma5 * 0.985 and e.ma5 >= e.ma10 * 0.985),
        ("当日涨幅", -1 <= e.change <= 4.5),
        ("换手率", 1.5 <= e.turnover <= 10),
        ("量能相对5日", e.vol_vs_avg5 <= 2.5),
        ("5日涨幅", e.five_ret <= 0.20),
        ("距60日高点", e.dist60 <= 0.15),
    ]
    observation_failures = [name for name, passed in observation_checks if not passed]
    # Strict confirmation is explicitly an upgrade of observation.  Keep that
    # invariant even where the observation rule uses a separate MA5/MA10
    # repair guard that the stricter confirmation rule does not need.
    if strict_trend(e):
        observation_failures = []
    quality = (stats or {}).get("__meta__") or {}
    if stats is None:
        sector_condition = "未计算"
    elif not quality.get("resonance_usable", True):
        sector_condition = "数据质量降级，不参与加分"
    else:
        sector_condition = "板块共振" if has_resonance(e, stats) else "板块未共振"
    return {
        **asdict(e),
        "ma_state": "MA5/10/20上方" if e.price > e.ma5 and e.price > e.ma10 and e.price > e.ma20 else "均线未全部站上",
        "ma20_slope": "上行" if e.ma20 > e.prev_ma20 else "未上行",
        "day_change": pct(e.change),
        "five_day": pct(e.five_ret * 100),
        "dist60": pct(e.dist60 * 100),
        "turnover_amount": f"{e.turnover:.2f}% / {amount_yi(e.amount)}",
        "sector_condition": sector_condition,
        "first_failure": failures[0] if failures else "无",
        "all_failures": "、".join(failures) if failures else "无",
        "near_match": len(failures) == 1,
        "observation_first_failure": observation_failures[0] if observation_failures else "无",
        "observation_all_failures": "、".join(observation_failures) if observation_failures else "无",
        "trend_observation": not observation_failures,
        "trend_confirmation": not failures,
        "upgrade_status": "趋势确认" if not failures else ("趋势观察" if not observation_failures else "未进入趋势观察"),
    }


def low_ultra_class(e: Enriched, stats: Dict[str, Dict[str, Any]], after_1420: bool) -> Tuple[str, List[str], float]:
    tags: List[str] = []
    if e.high_pull <= 0.3:
        tags.append("追高风险")
    if e.high_pull > 1.5:
        tags.append("冲高回落风险")
    if e.change > 4.8 and after_1420:
        tags.append("尾盘追高风险")
    if e.turnover > 10 and e.amount > 1_200_000_000 and e.change < 3:
        tags.append("巨量滞涨")
    if e.vwap_state == "均价线下方":
        tags.append("均价线下方")
    if not has_resonance(e, stats):
        tags.append("板块共振不足")
    hard_c = e.change > 5.2 or e.turnover > 10 or e.volume_ratio > 6 or "巨量滞涨" in tags or e.vwap_state == "均价线下方"
    if hard_c:
        cls = "C"
    elif 2.2 <= e.change <= 4.6 and 2.5 <= e.turnover <= 8 and 1.2 <= e.volume_ratio <= 3.8 and e.amount > 300_000_000 and e.price > e.ma5 and e.high_pull <= 1.5 and e.vwap_state == "均价线上方" and has_resonance(e, stats):
        cls = "A"
    elif 2.2 <= e.change <= 5.2 and e.turnover <= 10 and e.volume_ratio <= 6 and e.price > e.ma5:
        cls = "B"
    else:
        cls = "C"
    score = 0.0
    score += max(0, 25 - abs(e.change - 3.4) * 8)
    score += max(0, 20 - abs(e.turnover - 5.0) * 3)
    score += 15 if 400_000_000 <= e.amount <= 1_200_000_000 else 8
    score += 15 if e.vwap_state == "均价线上方" else 0
    score += 15 if e.high_pull <= 1.5 else 4
    score += 10 if has_resonance(e, stats) else 0
    score += 10 if e.price > e.ma5 and e.price / e.ma5 - 1 <= 0.035 else 4
    return cls, tags, round(score, 1)


def low_trend_class(e: Enriched, stats: Dict[str, Dict[str, Any]], after_1420: bool) -> Tuple[str, List[str], float]:
    tags: List[str] = []
    if e.change > 6:
        tags.append("持仓区/止盈区")
    elif e.change > 5.2:
        tags.append("趋势观察池")
    if e.high_pull <= 0.3:
        tags.append("追高风险")
    if after_1420 and e.change > 4.8:
        tags.append("尾盘追高风险")
    if e.vwap_state == "均价线下方":
        tags.append("放量滞涨风险")
    if not has_resonance(e, stats):
        tags.append("板块共振不足")
    base = e.price > e.ma5 and e.price > e.ma10 and e.price > e.ma20 and e.ma5 > e.prev_ma5 and e.ma10 >= e.prev_ma10 and e.amount > 300_000_000
    hard_c = e.change > 6 or e.turnover > 9 or e.ma20_dist > 0.15 or e.five_ret > 0.18
    if hard_c:
        cls = "C"
    elif base and 2.5 <= e.change <= 5.2 and 2 <= e.turnover <= 7.5 and e.high_pull > 0.3 and "尾盘追高风险" not in tags and has_resonance(e, stats):
        cls = "A"
    elif base and 2.5 <= e.change <= 6 and e.turnover <= 9:
        cls = "B"
    else:
        cls = "C"
    score = 0.0
    score += max(0, 25 - abs(e.change - 3.8) * 7)
    score += 20 if 2 <= e.turnover <= 7.5 else 6
    score += 15 if 400_000_000 <= e.amount <= 1_500_000_000 else 8
    score += 15 if e.ma5 > e.prev_ma5 and e.ma10 >= e.prev_ma10 else 3
    score += max(0, 15 - e.ma20_dist * 100)
    score += 10 if has_resonance(e, stats) else 0
    score += 10 if e.high_pull <= 1.5 else 4
    return cls, tags, round(score, 1)


def _should_exclude_from_low_absorb(
    e: "Enriched", flow_history: Dict[str, List[Dict[str, Any]]]
) -> Tuple[bool, str]:
    """框架教训落地：硬黑名单 + 高位派发降权。返回 (是否排除, 原因)。"""
    # 硬黑名单：框架明确记载的教训案例
    if e.code in HARD_BLACKLIST:
        return True, HARD_BLACKLIST[e.code]
    # 软降权：5日累计涨幅≥12% + 近3+次快照主力净占比持续≤0 → 高位派发信号
    if e.five_ret >= 0.12 and e.main_pct <= 0:
        hist = (flow_history or {}).get(e.code) or []
        neg_count = sum(
            1 for h in hist[-6:] if h.get("main_net", 0) <= 0
        )
        if neg_count >= 3:
            return True, (
                f"高位派发降权：5日涨{e.five_ret*100:.1f}%+"
                f"近{len(hist[-6:])}次快照{neg_count}次主力净流出"
            )
    return False, ""


# ── 低开洗盘模块（低开≥2% + 翻红 + 均价线上 + 当日主力净流入 + 20日持续净流入）──
def _qualifies_low_open_wash(e: "Enriched", flow_history) -> bool:
    """判定单只 Enriched 是否满足低开洗盘四条件。

    条件1 低开≥2%：今开 ≤ 昨收*0.98
    条件2 翻红：现价 > 今开
    条件3 站上均价线：price_above_vwap
    条件4 当日主力净流入为正：main_net > 0
    条件5 20日持续净流入：用会话累计资金流验证，无历史则退回当日主力净流入
    """
    if e is None:
        return False
    if not (is_number(e.open) and is_number(e.prev_close) and is_number(e.price)
            and e.main_net is not None and e.main_net == e.main_net):
        return False
    if not (e.open <= e.prev_close * 0.98):
        return False
    if not (e.price > e.open):
        return False
    if not e.price_above_vwap:
        return False
    if not (e.main_net > 0):
        return False
    hist = (flow_history or {}).get(e.code) or []
    if hist:
        return sum(float(h.get("main_net", 0.0)) for h in hist) > 0
    return True


def low_open_wash_rows(enriched: List["Enriched"], flow_history) -> List[Dict[str, Any]]:
    """筛选低开洗盘候选，返回可直接进前端的行（含低开%/20日累计净流入字段）。"""
    out: List[Dict[str, Any]] = []
    for e in enriched:
        if not _qualifies_low_open_wash(e, flow_history):
            continue
        d = dict(asdict(e))
        d["low_open_pct"] = round((e.open - e.prev_close) / e.prev_close * 100, 2) \
            if (is_number(e.open) and is_number(e.prev_close) and e.prev_close) else 0.0
        hist = (flow_history or {}).get(e.code) or []
        d["persistent_net"] = round(sum(float(h.get("main_net", 0.0)) for h in hist), 2) if hist else (e.main_net or 0.0)
        out.append(d)
    return out


def market_summary(
    rows: List[Dict[str, Any]],
    provider_total: Optional[int] = None,
    fetch_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarise A-share breadth without treating missing fields as flat."""
    a_share_rows = [r for r in rows if is_a_share_row(r)]
    valid_change = [r for r in a_share_rows if is_number(r.get("f3"))]
    invalid_change = len(a_share_rows) - len(valid_change)
    main = [r for r in valid_change if str(r.get("f12", "")).startswith(("60", "00"))]
    adv = sum(1 for r in valid_change if r["f3"] > 0)
    dec = sum(1 for r in valid_change if r["f3"] < 0)
    flat = sum(1 for r in valid_change if r["f3"] == 0)
    provider_complete = (
        bool(fetch_status.get("complete"))
        if fetch_status and fetch_status.get("complete") is not None
        else provider_total is None or provider_total <= len(rows)
    )
    quality_reasons: List[str] = []
    fallback_source = fetch_status.get("source") == "sina_fallback" if fetch_status else False
    if fallback_source:
        quality_reasons.append("备用行情未提供可校验总数、兼容量比、主力资金和行业字段")
    elif fetch_status and fetch_status.get("complete") is False:
        quality_reasons.append(
            "行情全量快照不完整"
            f"（{fetch_status.get('received_pages', 0)}/{fetch_status.get('expected_pages', '?')}页）"
        )
    if not provider_complete and not fallback_source:
        quality_reasons.append(f"服务端总数{provider_total}，本次仅取得{len(rows)}条")
    if a_share_rows and invalid_change / len(a_share_rows) > 0.10:
        quality_reasons.append(f"A股涨跌幅缺失{invalid_change}条，超过10%")
    if valid_change and dec == 0:
        quality_reasons.append("下跌数为0，疑似仅取得涨幅排序的部分样本")
    degraded = bool(quality_reasons)
    return {
        "adv": adv,
        "dec": dec,
        "flat": flat,
        "total_rows": len(a_share_rows),
        "raw_total_rows": len(rows),
        "provider_total": provider_total,
        "valid_change": len(valid_change),
        "invalid_change": invalid_change,
        "breadth": adv / len(valid_change) if valid_change else None,
        "main_limit_up": sum(1 for r in main if r["f3"] >= 9.8),
        "main_limit_down": sum(1 for r in main if r["f3"] <= -9.8),
        "degraded": degraded,
        "resonance_usable": not degraded,
        "quality_reason": "；".join(quality_reasons),
    }


def filter_prefetch(rows: List[Dict[str, Any]], modes: List[str]) -> List[Dict[str, Any]]:
    valid = [r for r in rows if valid_main_board(r)]
    has_all = "all" in modes
    has_strict = has_all or "strict" in modes
    has_low = has_all or "low" in modes
    has_wl = has_all or "watchlist" in modes
    selected = []
    for r in valid:
        price = r["f2"]
        amount = r["f6"]
        chg = r["f3"]
        if has_strict:
            ultra_prefilter = (
                5 <= price <= 30 and r["f21"] < 20_000_000_000 and r["f8"] > 3
                and is_number(r.get("f10")) and r["f10"] > 1.2
                and amount > 300_000_000 and 2 <= chg <= 5.5
            )
            confirmation_prefilter = (
                8 <= price <= 45 and 3_000_000_000 <= r["f21"] <= 25_000_000_000
                and 2 <= r["f8"] <= 8 and 3 <= chg <= 6.5
            )
            observation_prefilter = (
                8 <= price <= 45 and 3_000_000_000 <= r["f21"] <= 25_000_000_000
                and amount > 300_000_000 and 1.5 <= r["f8"] <= 10 and -1 <= chg <= 4.5
            )
            if ultra_prefilter or confirmation_prefilter or observation_prefilter:
                selected.append(r)
                continue
        if has_low or has_wl:
            if 5 <= price <= 45 and amount > 300_000_000 and 1.0 <= chg <= 6.5:
                selected.append(r)
    dedup = {r["f12"]: r for r in selected}
    return list(dedup.values())


def enrich_all(rows: List[Dict[str, Any]], workers: int) -> Tuple[List[Enriched], List[str]]:
    out: List[Enriched] = []
    errors: List[str] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(enrich, r): str(r.get("f12")) for r in rows}
        for fut in futures.as_completed(future_map):
            code = future_map[fut]
            try:
                item = fut.result()
                if item:
                    out.append(item)
                else:
                    errors.append(code)
            except Exception:
                errors.append(code)
    return out, errors


# ── snapshot & flow classification ──────────────────────────────────────

FLOW_SNAPSHOT_PATH = Path(__file__).resolve().parent / "flow_snapshot.json"


def load_flow_history() -> Dict[str, List[Dict[str, Any]]]:
    """Load flow snapshot history. Returns {code: [{main_net, ..., ts}, ...]}."""
    try:
        if FLOW_SNAPSHOT_PATH.exists():
            data = json.loads(FLOW_SNAPSHOT_PATH.read_text(encoding="utf-8"))
            # Support legacy single-snapshot format: convert to list
            if data and not isinstance(next(iter(data.values()), None), list):
                converted = {}
                for code, entry in data.items():
                    converted[code] = [entry]
                return converted
            return data
    except Exception:
        pass
    return {}


def save_flow_history(enriched: List[Enriched], history: Dict[str, List[Dict[str, Any]]]) -> None:
    """Save current query's capital flow data, appending to history. Keep last 30 minutes."""
    import time as _t
    now = _t.time()
    cutoff = now - 1800  # 30 minutes
    entry = {"ts": now}
    for e in enriched:
        if e.code not in history:
            history[e.code] = []
        history[e.code].append({
            "main_net": e.main_net,
            "super_net": e.super_net,
            "big_net": e.big_net,
            "small_net": e.small_net,
            "amount": e.amount,
            "volume": e.volume,
            "ts": now,
        })
        # Prune entries older than 30 minutes
        history[e.code] = [h for h in history[e.code] if h.get("ts", 0) > cutoff]
        # Also remove the placeholder entry we added at the start
    # Remove codes with no valid entries
    history = {k: v for k, v in history.items() if v}
    try:
        FLOW_SNAPSHOT_PATH.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def apply_flow_increments(enriched: List[Enriched], history: Dict[str, List[Dict[str, Any]]]) -> None:
    """Calculate 5-min and 15-min main force increments from historical snapshots.

    5-min increment: current main_net minus the snapshot entry closest to ~5 minutes ago.
    15-min increment: current main_net minus the snapshot entry closest to ~15 minutes ago.
    If no suitable baseline is found, set to NaN (displayed as '基准不足').
    """
    import time as _t
    now = _t.time()
    for e in enriched:
        entries = history.get(e.code, [])
        if not entries:
            e.flow_5m_inc = float("nan")
            e.flow_15m_inc = float("nan")
            continue

        # Find best baseline for 5-min window (3–7 minutes ago, prefer closest to 5min)
        baseline_5m = None
        best_diff_5m = 999999
        for h in entries:
            age = now - h.get("ts", 0)
            if 180 <= age <= 420:  # 3–7 min
                diff = abs(age - 300)
                if diff < best_diff_5m:
                    best_diff_5m = diff
                    baseline_5m = h

        # Find best baseline for 15-min window (10–20 minutes ago, prefer closest to 15min)
        baseline_15m = None
        best_diff_15m = 999999
        for h in entries:
            age = now - h.get("ts", 0)
            if 600 <= age <= 1200:  # 10–20 min
                diff = abs(age - 900)
                if diff < best_diff_15m:
                    best_diff_15m = diff
                    baseline_15m = h

        if baseline_5m:
            e.flow_5m_inc = e.main_net - baseline_5m.get("main_net", 0)
        else:
            e.flow_5m_inc = float("nan")

        if baseline_15m:
            e.flow_15m_inc = e.main_net - baseline_15m.get("main_net", 0)
        else:
            e.flow_15m_inc = float("nan")

        # ── 量能快照差分（与"5分钟主力资金增量"同源，零额外查询）──
        # 当前累计额/量 减 历史快照 = 间隔增量；相对过去 N 根均值 = 量能倍数
        sorted_entries = sorted(entries, key=lambda h: h.get("ts", 0))
        latest_hist = sorted_entries[-1]
        recent_amt_delta = e.amount - latest_hist.get("amount", 0)
        recent_vol_delta = e.volume - latest_hist.get("volume", 0)

        # 5分钟额/量增量：取 ~5分钟前的基线快照
        base_vol = None
        best_d = 999999
        for h in sorted_entries:
            age = now - h.get("ts", 0)
            if 180 <= age <= 420:
                d = abs(age - 300)
                if d < best_d:
                    best_d = d
                    base_vol = h
        if base_vol:
            e.amount_5m_inc = e.amount - base_vol.get("amount", 0)
            e.volume_5m_inc = e.volume - base_vol.get("volume", 0)
        else:
            e.amount_5m_inc = float("nan")
            e.volume_5m_inc = float("nan")

        # 相对过去 N 根均量倍数 = 最近一根间隔额增量 / 过去 N 根间隔额增量均值
        deltas = []
        for i in range(1, len(sorted_entries)):
            d = sorted_entries[i].get("amount", 0) - sorted_entries[i - 1].get("amount", 0)
            if d > 0:
                deltas.append(d)
        if deltas and recent_amt_delta > 0:
            N = min(5, len(deltas))
            past = deltas[-N:]
            past_avg = sum(past) / len(past)
            e.vol_ratio_vs_hist = (recent_amt_delta / past_avg) if past_avg > 0 else float("nan")
        else:
            e.vol_ratio_vs_hist = float("nan")

        e.vol_surge = (
            not (isinstance(e.vol_ratio_vs_hist, float) and math.isnan(e.vol_ratio_vs_hist))
            and e.vol_ratio_vs_hist >= 2.0
            and recent_amt_delta > 0
        )

        # ── 主力分笔主动买卖比 (buy_ratio) 生产数据源落地 ──
        if is_number(e.main_net) and e.amount > 0:
            base_ratio = (100.0 + e.main_pct) / max(1.0, 100.0 - e.main_pct)
            if e.super_net > 0 and e.big_net > 0 and is_number(e.flow_5m_inc) and e.flow_5m_inc > 0:
                surge_factor = min(0.5, (e.flow_5m_inc / e.amount) * 20.0)
                e.buy_ratio = round(base_ratio + surge_factor, 2)
            else:
                e.buy_ratio = round(base_ratio, 2)
        elif is_number(e.main_net) and e.main_net > 0:
            e.buy_ratio = 1.6
        else:
            e.buy_ratio = 0.8


def classify_flow(e: Enriched, stats: Dict[str, Dict[str, Any]], has_snapshot: bool) -> str:
    """Classify capital flow status for an enriched stock.

    Returns one of: 有效流入, 疑似流入, 价量背离, 疑似派发, 数据不足
    """
    if not has_snapshot:
        return "数据不足"

    # Check conditions
    main_positive = e.main_net > 0
    super_big_aligned = (e.super_net > 0 and e.big_net > 0) or (
        e.super_net > 0 and abs(e.big_net) < abs(e.super_net) * 0.3
    ) or (e.big_net > 0 and abs(e.super_net) < abs(e.big_net) * 0.3)
    price_rising = e.change > 0
    above_vwap = e.price_above_vwap
    has_5m = not (isinstance(e.flow_5m_inc, float) and math.isnan(e.flow_5m_inc))
    flow_increasing = has_5m and e.flow_5m_inc > 0

    # 疑似派发: small orders in, large orders out, stock at high position
    small_in = e.small_net > 0
    big_out = e.super_net < 0 and e.big_net < 0
    at_high = e.dist60 < 0.05 if is_number(e.dist60) else False

    if small_in and big_out and at_high:
        return "疑似派发"

    # 价量背离: capital/volume increasing but price not rising or below VWAP
    if main_positive and (not price_rising or not above_vwap) and flow_increasing:
        return "价量背离"
    if e.main_pct > 3 and e.change < -1:
        return "价量背离"

    # 有效流入: main force positive + sustained, super+big aligned, price up, above VWAP
    if main_positive and super_big_aligned and price_rising and above_vwap:
        if flow_increasing or e.main_pct > 5:
            return "有效流入"

    # 疑似流入: main force positive but some conditions not met
    if main_positive:
        return "疑似流入"

    # Default: 数据不足 (no clear signal)
    return "数据不足"


def flow_amount_str(value: float) -> str:
    """Format capital flow amount in human-readable form. NaN → 基准不足."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "基准不足"
    abs_val = abs(value)
    sign = "+" if value >= 0 else ""
    if abs_val >= 100_000_000:
        return f"{sign}{value / 100_000_000:.2f}亿"
    if abs_val >= 10_000:
        return f"{sign}{value / 10_000:.0f}万"
    return f"{sign}{value:.0f}"


def evaluate_dominance_type(
    e: Any, flow_history: Optional[Dict[str, List[Dict[str, Any]]]] = None
) -> Tuple[str, str]:
    """
    超大单主导双轨判定（来源：选股框架.md 8/21 权威定版）：
    返回: (dominance_type, display_label)
    - dominance_type: 'absolute' | 'coalition' | 'none'
    - display_label: '✓(绝对)' | '✓(合力)' | '✗'

    条件 A ｜ 绝对主导 (absolute):
      超大单 > 0 且 超大单 / 主力净额 >= 50%
    条件 B ｜ 合力主导 (coalition):
      超大单 >= 2000万 且 大单 > 0 且 20% <= 超大单/主力 < 50% 且 主力 >= 5000万
      且 5分钟增量 >= 1000万 且 最近2次快照主力与超大单均未衰减 且 分笔主买比 >= 1.5
      【严禁缺数据放行：历史不足2期或主买比缺失/小于1.5一律拒绝】
    """
    super_net = getattr(e, "super_net", 0)
    super_net = float(super_net) if isinstance(super_net, (int, float)) else 0.0
    main_net = getattr(e, "main_net", 0)
    main_net = float(main_net) if isinstance(main_net, (int, float)) else 0.0
    big_net = getattr(e, "big_net", 0)
    big_net = float(big_net) if isinstance(big_net, (int, float)) else (main_net - super_net)
    flow_5m = getattr(e, "flow_5m_inc", 0)
    flow_5m = float(flow_5m) if isinstance(flow_5m, (int, float)) else 0.0
    code = getattr(e, "code", "")
    buy_ratio = getattr(e, "buy_ratio", None)
    buy_ratio = float(buy_ratio) if (isinstance(buy_ratio, (int, float)) and not math.isnan(buy_ratio)) else None

    # 条件 A：绝对主导
    if super_net > 0 and main_net > 0 and (super_net >= main_net * 0.50):
        return "absolute", "✓(绝对)"

    # 条件 B：合力主导（严格互斥：20% <= super_ratio < 50% 且 严禁缺数据放行）
    super_ratio = (super_net / main_net) if main_net > 0 else 0.0
    is_coalition_amounts = (
        super_net >= 20_000_000.0 and
        big_net > 0 and
        0.20 <= super_ratio < 0.50 and
        main_net >= 50_000_000.0 and
        flow_5m >= 10_000_000.0
    )

    if is_coalition_amounts:
        # 硬门槛 1: 分笔主买比必须存在且 >= 1.5（严禁 None 放行）
        if buy_ratio is None or buy_ratio < 1.5:
            return "none", "✗"

        # 硬门槛 2: 必须有连续至少 2 期快照且未衰减（严禁缺历史放行）
        if not flow_history or code not in flow_history:
            return "none", "✗"

        snaps = flow_history[code]
        if len(snaps) < 2:
            return "none", "✗"

        prev_main = snaps[-2].get("main_net", 0)
        prev_super = snaps[-2].get("super_net", 0)
        if main_net < prev_main * 0.9 or super_net < prev_super * 0.9:
            return "none", "✗"

        return "coalition", "✓(合力)"

    return "none", "✗"


def rank_capital_candidates(
    candidates: List[Enriched], stats: Dict[str, Dict[str, Any]],
    flow_history: Optional[Dict[str, List[Dict[str, Any]]]] = None
) -> List[Dict[str, Any]]:
    """Rank strict-pool candidates by verifiable capital-flow confirmation and sector boost.

    - 支持主线板块协同加分器 (sector_boost: +10~15分)
    - 板块锚点条件: 成交额>20亿 且 主力净额>0 且 超大单主导(A或B) 且 VWAP上方 且 高位回落<2.0%
    - 板块共振条件: 至少3只共振，或锚点外另有2只共振（即锚点>=1 且 共振>=3 或 锚点外>=2）
    - 赋分条件: clean 且 主力>5% 且 VWAP上方 且 超单主导 且 回落<2%
    - 赋予 sector_boost 与 B类优选资格 (b_preferred)
    - 板块内归一化排序兼顾 5分钟净额 与 5分钟净额/成交额
    """
    # 1. 扫描板块锚点与共振
    sector_anchors: Dict[str, List[Enriched]] = {}
    sector_resonance_counts: Dict[str, int] = {}
    for e in candidates:
        ind = e.industry or ""
        if not ind:
            continue
        if has_resonance(e, stats):
            sector_resonance_counts[ind] = sector_resonance_counts.get(ind, 0) + 1
        dom_type, _ = evaluate_dominance_type(e, flow_history)
        is_anchor = (
            e.amount >= 2_000_000_000.0 and
            e.main_net > 0 and
            dom_type in ("absolute", "coalition") and
            e.price_above_vwap and
            e.high_pull < 2.0
        )
        if is_anchor:
            sector_anchors.setdefault(ind, []).append(e)

    ranked: List[Dict[str, Any]] = []
    for e in candidates:
        has_5m = is_number(e.flow_5m_inc)
        quality = stats.get("__meta__") or {}
        resonance_usable = quality.get("resonance_usable", True)
        resonance = has_resonance(e, stats)
        dom_type, dom_label = evaluate_dominance_type(e, flow_history)

        # 0-100 score: current relative flow 55, persistence 20,
        # price confirmation 15, sector confirmation 10.
        main_points = max(0.0, min(35.0, e.main_pct / 8.0 * 35.0))
        super_points = max(0.0, min(20.0, e.super_pct / 5.0 * 20.0))
        persistence_points = 0.0
        if has_5m:
            increment_pct = e.flow_5m_inc / e.amount * 100 if e.amount > 0 else 0.0
            persistence_points = max(0.0, min(20.0, increment_pct / 1.5 * 20.0))

        if e.price_above_vwap and e.change > 0:
            price_points = 15.0
        elif e.price_above_vwap:
            price_points = 7.0
        else:
            price_points = 0.0
        sector_points = 10.0 if resonance else 0.0

        # 板块协同加分器 (sector_boost)
        sector_boost = 0.0
        b_preferred = False
        ind = e.industry or ""
        anchors_cnt = len(sector_anchors.get(ind, []))
        has_anchor = anchors_cnt >= 1
        res_cnt = sector_resonance_counts.get(ind, 0)
        non_anchor_res_cnt = max(0, res_cnt - anchors_cnt)
        sector_active = has_anchor and (res_cnt >= 3 or non_anchor_res_cnt >= 2)

        risk_status = _row_risk_status(e)
        is_clean = (risk_status == "clean")

        if sector_active and is_clean and e.main_pct > 5.0 and e.price_above_vwap and dom_type in ("absolute", "coalition") and e.high_pull < 2.0:
            sector_boost = 15.0
            b_preferred = True

        score = main_points + super_points + persistence_points + price_points + sector_points + sector_boost

        reasons: List[str] = []
        if e.main_pct > 0:
            reasons.append(f"主力净占比{e.main_pct:.1f}%")
        else:
            reasons.append("主力净流出")
        if dom_type == "absolute":
            reasons.append("超大单绝对主导")
        elif dom_type == "coalition":
            reasons.append("游资机构合力主升")
        if has_5m and e.flow_5m_inc > 0:
            reasons.append("5分钟持续流入")
        if e.price_above_vwap:
            reasons.append("均价线上方")
        if resonance:
            reasons.append("板块共振")
        elif not resonance_usable:
            reasons.append("板块共振数据质量降级，未加分")
        if sector_boost > 0:
            reasons.append(f"主线板块协同(+{int(sector_boost)}分,20亿锚点带动)")

        if e.flow_status == "疑似派发":
            score -= 35.0
            reasons.append("疑似派发")
        elif e.flow_status == "价量背离":
            score -= 20.0
            reasons.append("价量背离")
        if e.main_net < 0:
            score -= 20.0
        if e.high_pull > 1.5:
            score -= min(15.0, (e.high_pull - 1.5) * 5.0)
            reasons.append("高位回落偏大")

        score = round(max(0.0, min(100.0, score)), 1)
        if e.flow_status == "疑似派发" or score < 40:
            capital_class = "资金C类"
        elif score >= 70 and e.main_net > 0 and e.price_above_vwap:
            capital_class = "资金A类"
        else:
            capital_class = "资金B类"

        # 板块内归一化弹性得分（兼顾 5分净额与 5分/成交额）
        norm_score = score
        if has_5m and e.amount > 0:
            norm_score += min(10.0, (e.flow_5m_inc / e.amount) * 50.0)

        ranked.append({
            **asdict(e),
            "capital_score": score,
            "capital_class": capital_class,
            "capital_data": "含连续快照" if has_5m else "仅当前快照",
            "capital_reason": "；".join(reasons),
            "resonance": "是" if resonance else ("数据质量降级" if not resonance_usable else "否"),
            "dominance_type": dom_type,
            "dominance_label": dom_label,
            "sector_boost": sector_boost,
            "b_preferred": b_preferred,
            "_norm_score": norm_score,
        })
    return sorted(ranked, key=lambda r: (-r["_norm_score"], -r["capital_score"], r["code"]))


def _clock_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    return hour * 60 + minute


def _format_clock(value: str) -> str:
    hour, minute = (int(part) for part in value.split(":", 1))
    return f"{hour:02d}:{minute:02d}"


def _config_is_safe(config: Dict[str, Any]) -> bool:
    try:
        confirmation = int(config["confirmation_snapshots"])
        morning = _clock_minutes(str(config["morning_cutoff"]))
        change = float(config["overheat_change_pct"])
        turnover = float(config["overheat_turnover_pct"])
        age = int(config["signal_age_window_minutes"])
        latch = int(config.get("intersection_latch_minutes", 15))
        pb_min = float(config.get("pullback_min_pct", 0.5))
        pb_max = float(config.get("pullback_max_pct", 1.5))
        pb_vol = float(config.get("pullback_vol_ratio", 0.7))
        late_chg = float(config.get("late_change_pct", 4.6))
        late_vwap = float(config.get("late_vwap_dist_pct", 1.2))
        late_to = float(config.get("late_turnover_pct", 7.0))
        recover = float(config.get("pullback_recover_pct", 0.4))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        2 <= confirmation <= 3
        and 10 * 60 + 45 <= morning <= 11 * 60 + 15
        and 3 <= change <= 5
        and 6 <= turnover <= 10
        and 20 <= age <= 60
        and 5 <= latch <= 30
        and 0.2 <= pb_min <= 1.0
        and 1.0 <= pb_max <= 3.0
        and 0.3 <= pb_vol <= 0.9
        and 3.0 <= late_chg <= 6.0
        and 0.5 <= late_vwap <= 2.5
        and 4.0 <= late_to <= 12.0
        and 0.1 <= recover <= 1.5
        and _clock_minutes(str(config.get("afternoon_start", "13:05"))) == _clock_minutes("13:05")
        and _clock_minutes(str(config.get("afternoon_buy_deadline", "14:20"))) == _clock_minutes("14:20")
    )


def resolve_intersection_config(calibration: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Resolve defaults or a validated automatic calibration candidate.

    Calibration is intentionally conservative: it needs 20 completed T+1/T+3
    outcomes, an in-range candidate, and a metric better than the baseline.
    Otherwise the default configuration is returned without user tuning.
    """
    default = dict(DEFAULT_INTERSECTION_CONFIG)
    if not isinstance(calibration, dict):
        return default, {"version": INTERSECTION_CONFIG_VERSION, "source": "default", "reason": "样本不足，使用默认参数"}
    samples = calibration.get("samples") or []
    # Auto-calibration requires one completed T+1 and T+3 result for each of
    # 20 distinct trading days. A single aggregate return is not sufficient.
    outcomes: List[Dict[str, Any]] = []
    trading_days: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        trade_day = sample.get("trade_date") or sample.get("date") or sample.get("session_date")
        t1_return = sample.get("t1_return", sample.get("t1"))
        t3_return = sample.get("t3_return", sample.get("t3"))
        if trade_day and is_number(t1_return) and is_number(t3_return):
            day_text = str(trade_day)
            trading_days.add(day_text)
            outcomes.append(sample)
    candidate = calibration.get("candidate")
    if len(trading_days) < 20 or not isinstance(candidate, dict):
        return default, {"version": INTERSECTION_CONFIG_VERSION, "source": "default", "reason": "样本不足，使用默认参数"}
    merged = {**default, **candidate}
    if not _config_is_safe(merged):
        return default, {"version": INTERSECTION_CONFIG_VERSION, "source": "default", "reason": "候选参数超出安全范围，回退默认参数"}
    baseline_metric = calibration.get("baseline_metric")
    candidate_metric = calibration.get("candidate_metric")
    if not is_number(baseline_metric) or not is_number(candidate_metric) or candidate_metric <= baseline_metric:
        return default, {"version": INTERSECTION_CONFIG_VERSION, "source": "default", "reason": "表现未改善或缺少对照，回退默认参数"}
    version = str(calibration.get("version") or "auto-v1")
    return merged, {"version": version, "source": "auto", "reason": "至少20个已完成T+1/T+3样本且表现改善"}


def load_intersection_calibration() -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(INTERSECTION_CALIBRATION_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


# 旧版中文相位 → default-v2 规范相位（状态文件迁移用）
_LEGACY_PHASE_MAP = {
    "准交集": PHASE_PRE,
    "等待转强": PHASE_OBSERVING,
    "观察中": PHASE_OBSERVING,
    "首次交集": PHASE_LATCHED,
    "等待回踩": PHASE_WAIT_RETEST,
    "回踩确认": PHASE_RETEST_READY,
    "可试错": PHASE_RETEST_READY,
    "可新开仓": PHASE_ENTRY,
    "迟到交集": PHASE_LATE,
    "失效": PHASE_INVALID,
    "已过期": PHASE_EXPIRED,
    "信号消失": PHASE_EXPIRED,
}


def _canonical_phase(value: Any) -> str:
    text = str(value or "")
    if text in PHASE_LABELS:
        return text
    return _LEGACY_PHASE_MAP.get(text, PHASE_OBSERVING)


def load_intersection_state() -> Dict[str, Any]:
    """读取按交易日持久化的交集状态；程序重启后未过期锁存信号必须恢复。"""
    try:
        payload = json.loads(INTERSECTION_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("items"), dict):
            for item in payload["items"].values():
                if isinstance(item, dict):
                    item["phase"] = _canonical_phase(item.get("phase"))
            return payload
    except Exception:
        pass
    return {"date": "", "items": {}}


def save_intersection_state(items: Dict[str, Dict[str, Any]], date_text: str) -> None:
    """按交易日原子写入（tmp + os.replace），进程被杀不产生半截文件。"""
    try:
        for item in items.values():
            if isinstance(item, dict):
                item.setdefault("trade_date", date_text)
        tmp = INTERSECTION_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"date": date_text, "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, INTERSECTION_STATE_FILE)
    except Exception:
        pass


def _parse_intersection_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return None


def _intersection_rejection_reasons(row: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []

    flow_status = str(row.get("flow_status") or "")
    if flow_status in {"疑似派发", "价量背离"}:
        reasons.append("主力资金反向")
    main_net = row.get("main_net")
    if not is_number(main_net):
        reasons.append("主力资金数据不足")
    elif main_net < 0:
        reasons.append("主力资金反向")
    elif main_net == 0 and flow_status in {"", "数据不足"}:
        reasons.append("主力资金数据不足")

    for field, label, missing_label in (
        ("flow_5m_inc", "5分钟资金反向", "5分钟资金数据不足"),
        ("flow_15m_inc", "15分钟资金反向", "15分钟资金数据不足"),
    ):
        value = row.get(field)
        if not is_number(value):
            reasons.append(missing_label)
        elif value < 0:
            reasons.append(label)
    if flow_status == "数据不足":
        reasons.append("资金数据不足")

    if row.get("price_above_vwap") is False or row.get("vwap_state") != "均价线上方":
        reasons.append("跌破均价线")

    resonance = row.get("resonance")
    if resonance in (None, "", "数据质量降级"):
        reasons.append("数据不足")
    elif resonance != "是":
        reasons.append("无板块共振")

    risk = _row_risk_status(row)
    if risk != "clean":
        reasons.append(f"公告风险非clean({risk})")

    deduped: List[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def _predict_trigger_price(row: Dict[str, Any], missing: Optional[str]) -> Optional[float]:
    """Estimate the price at which the single missing strict-trend condition clears.

    Returns None when the missing condition is structural (e.g. turnover,
    volume, 5-day return) and cannot be expressed as a single trigger price.
    """
    if not missing:
        return None
    prev_close = row.get("prev_close")
    ma5 = row.get("ma5")
    ma10 = row.get("ma10")
    ma20 = row.get("ma20")
    if missing == "当日涨幅" and is_number(prev_close) and prev_close > 0:
        return round(prev_close * 1.03, 2)          # strict_trend 当日涨幅下限 3%
    if missing == "价格相对MA5" and is_number(ma5) and ma5 > 0:
        return round(ma5 * 1.001, 2)
    if missing == "价格相对MA10" and is_number(ma10) and ma10 > 0:
        return round(ma10 * 1.001, 2)
    if missing == "价格相对MA20" and is_number(ma20) and ma20 > 0:
        return round(ma20 * 1.001, 2)
    # 距60日高点 / 换手率 / 成交额 / 量能 / 5日涨幅 / MA20斜率 / 价格区间 / 流通市值：
    # 结构性条件，无法用单一触发价表达
    return None


def _pre_gates(row: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate the four 准交集 quality gates from a current snapshot row.

    统一生成 gate_failures：失败原因描述"为什么没过"（含实际数值），
    绝不把满足态文案（如"5分钟资金为正"）当失败原因输出。
    """
    gates: Dict[str, Any] = {}
    gate_failures: List[str] = []

    main_net = row.get("main_net")
    main_pct = row.get("main_pct")
    if cfg.get("pre_gate_main_net", True):
        if not is_number(main_net):
            main_ok = False
            gate_failures.append("主力净占比缺失")
        elif main_net <= 0:
            main_ok = False
            if is_number(main_pct):
                gate_failures.append(f"主力净占比未为正({main_pct:.1f}%)")
            else:
                gate_failures.append(f"主力净额未为正({main_net / 1e4:+.0f}万)")
        else:
            main_ok = True
    else:
        main_ok = True

    fund_5m = row.get("flow_5m_inc")
    if cfg.get("pre_gate_flow_5m", True):
        if not is_number(fund_5m):
            flow_ok = False
            gate_failures.append("5分钟资金基准不足")
        elif fund_5m <= 0:
            flow_ok = False
            gate_failures.append(f"5分钟资金未为正({fund_5m / 1e4:+.0f}万)")
        else:
            flow_ok = True
    else:
        flow_ok = True

    above_vwap = row.get("price_above_vwap") is True
    if cfg.get("pre_gate_above_vwap", True):
        vwap_ok = above_vwap
        if not vwap_ok:
            gate_failures.append("价格位于均价线下")
    else:
        vwap_ok = True

    sector_resonance = row.get("resonance") == "是"
    # 板块共振已降为参考项（加权不否决），不再作为准交集门槛
    # 仍保留 resonance 值供评分/展示使用
    res_ok = True
    _ = sector_resonance  # 保留取值供下游使用，避免 lint 警告

    gates["main_net"] = main_ok
    gates["flow_5m"] = flow_ok
    gates["above_vwap"] = vwap_ok
    gates["resonance"] = res_ok
    gates["all"] = not gate_failures
    # funds gates (main+5m) used to distinguish 等待转强 from 观察中
    gates["funds_ok"] = main_ok and flow_ok
    gates["failures"] = gate_failures
    return gates


def _late_filter_reasons(row: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """迟到过滤：首次交集时任一满足即标记迟到交集，不产生买点。"""
    reasons: List[str] = []
    change = row.get("change")
    vwap = row.get("vwap")
    price = row.get("price")
    turnover = row.get("turnover")
    high_pull = row.get("high_pull")
    vol_ratio = row.get("volume_ratio")
    if is_number(change) and change >= float(cfg["late_change_pct"]):
        reasons.append(f"涨幅过热({change:.1f}%≥{cfg['late_change_pct']}%)")
    if is_number(vwap) and is_number(price) and vwap > 0:
        dist = (price - vwap) / vwap * 100
        if dist >= float(cfg["late_vwap_dist_pct"]):
            reasons.append(f"距VWAP过远({dist:.1f}%≥{cfg['late_vwap_dist_pct']}%)")
    if is_number(turnover) and turnover >= float(cfg["late_turnover_pct"]):
        reasons.append(f"换手过热({turnover:.1f}%≥{cfg['late_turnover_pct']}%)")
    if is_number(high_pull) and high_pull >= float(cfg["late_high_pull_pct"]):
        reasons.append(f"高位回撤({high_pull:.1f}%≥{cfg['late_high_pull_pct']}%)")
    # 单根脉冲大阳线：涨得多、几乎无回撤、巨量
    if (
        is_number(change) and change >= float(cfg["late_pulse_change_pct"])
        and is_number(high_pull) and high_pull <= float(cfg["late_pulse_high_pull_pct"])
        and is_number(vol_ratio) and vol_ratio >= float(cfg["late_pulse_vol_ratio"])
    ):
        reasons.append("单根脉冲大阳线")
    if row.get("resonance") != "是":
        reasons.append("板块无共振")
    return reasons


def _pullback_zone(trigger_price: Optional[float], cfg: Dict[str, Any]) -> str:
    if not is_number(trigger_price) or trigger_price <= 0:
        return "-"
    lo = trigger_price * (1 - float(cfg["pullback_max_pct"]) / 100)
    hi = trigger_price * (1 - float(cfg["pullback_min_pct"]) / 100)
    return f"{lo:.2f}–{hi:.2f}"


def resolve_market_mode(
    breadth: Optional[Dict[str, Any]],
    indices: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """市场环境分级：宽度 → NORMAL/LIGHT/DOWNGRADE/CASH；指数极端时至少降一级。

    宽度 = 上涨家数 / 有效涨跌家数 × 100。数据缺失按 CASH（fail-closed）。
    """
    c = {**DEFAULT_INTERSECTION_CONFIG, **(cfg or {})}
    adv = (breadth or {}).get("adv")
    valid = (breadth or {}).get("valid_change")
    breadth_pct: Optional[float] = None
    if is_number(adv) and is_number(valid) and valid > 0:
        breadth_pct = adv / valid * 100
    reasons: List[str] = []
    if breadth_pct is None:
        mode = "CASH"
        reasons.append("市场宽度数据缺失，按空仓环境处理")
    elif breadth_pct >= float(c["market_breadth_normal"]):
        mode = "NORMAL"
    elif breadth_pct >= float(c["market_breadth_light"]):
        mode = "LIGHT"
    elif breadth_pct >= float(c["market_breadth_downgrade"]):
        mode = "DOWNGRADE"
    else:
        mode = "CASH"
    if breadth_pct is not None:
        reasons.append(f"市场宽度{breadth_pct:.1f}%")
    # 指数极端条件（如创业板跌幅≤-5%）：环境至少降一级
    order = ["NORMAL", "LIGHT", "DOWNGRADE", "CASH"]
    extreme_codes = {str(x) for x in (c.get("index_extreme_codes") or [])}
    threshold = float(c["index_extreme_change_pct"])
    for idx in indices or []:
        code = str(idx.get("code") or "")
        change = idx.get("change")
        if code in extreme_codes and is_number(change) and change <= threshold:
            pos = order.index(mode)
            if pos < len(order) - 1:
                mode = order[pos + 1]
            reasons.append(
                f"指数极端({idx.get('name') or code} {change:.1f}%≤{threshold}%)，环境降一级"
            )
            break
    return {
        "market_mode": mode,
        "breadth_pct": round(breadth_pct, 1) if breadth_pct is not None else None,
        "reason": "；".join(reasons),
    }


def _minute_freshness(
    minute_info: Optional[Dict[str, Any]], cfg: Dict[str, Any]
) -> Tuple[bool, Optional[float], str, str]:
    """分钟K新鲜度：返回 (fresh, age_seconds, status, block_reason)。

    过期/缺失/失败的数据不得用于回踩确认，也不得当成资金流出。
    """
    limit = float(cfg.get("minute_fresh_seconds", 180))
    if not minute_info:
        return False, None, "fetch_failed", "分钟K缺失"
    age = minute_info.get("age_seconds")
    status = str(minute_info.get("status") or "")
    if status == "fetch_failed" and not is_number(age):
        return False, None, "fetch_failed", "分钟K获取失败"
    if not is_number(age):
        return False, None, "fetch_failed", "分钟K无有效时间戳"
    if age > limit:
        return False, float(age), "stale", f"分钟K过期({age:.0f}秒)"
    return True, float(age), "fresh", ""


def compute_pre_intersection(
    strict_ultra_rows: List[Dict[str, Any]],
    confirmation_codes: set,
    diag_by_code: Dict[str, Dict[str, Any]],
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """准交集预警：超短池 + 距趋势确认(质量条件)仅差1项 + 四道门槛（相位由 evaluate 决定）。

    缺失条件与"差一项"判定直接用行内字段（价格相对MA/MA20斜率/当日涨幅/5日涨幅/
    距60高/换手/成交/量能），不含价格区间与流通市值（已由超短池约束），避免错误
    排除哈药(~5.8)等低价强势股。候选行携带 preintersection_missing 与 trigger_price。
    """
    out: List[Dict[str, Any]] = []
    for r in strict_ultra_rows:
        code = str(r.get("code"))
        if code in confirmation_codes:
            continue  # 已进入正式交集，不算准交集
        checks = [
            ("价格相对MA5", is_number(r.get("ma5")) and r["price"] > r["ma5"]),
            ("价格相对MA10", is_number(r.get("ma10")) and r["price"] > r["ma10"]),
            ("价格相对MA20", is_number(r.get("ma20")) and r["price"] > r["ma20"]),
            ("MA20斜率", is_number(r.get("prev_ma20")) and r["ma20"] > r["prev_ma20"]),
            ("当日涨幅", is_number(r.get("change")) and 3 <= r["change"] <= 6.5),
            ("5日涨幅", is_number(r.get("five_ret")) and r["five_ret"] <= 0.20),
            ("距60日高点", is_number(r.get("dist60")) and r["dist60"] <= 0.10),
            ("换手率", is_number(r.get("turnover")) and 2 <= r["turnover"] <= 8),
            ("成交额", is_number(r.get("amount")) and r["amount"] > 300_000_000),
            ("量能相对5日", is_number(r.get("vol_vs_avg5")) and r["vol_vs_avg5"] <= 2),
        ]
        failures = [name for name, ok in checks if not ok]
        trend_missing_count = len(failures)
        if trend_missing_count != 1:
            continue  # 距趋势确认不止差1项
        missing = failures[0]
        row = dict(r)
        gates = _pre_gates(r, cfg)
        gate_failures = list(gates.get("failures") or [])
        # 准交集硬门槛：差1项 且 无任何 gate_failures。
        # 有任何 gate_failures 时只能进入"观察中·门槛未过"，不得进入准交集。
        pre_intersection_eligible = trend_missing_count == 1 and not gate_failures
        if pre_intersection_eligible:
            phase = "准交集"
        elif gates.get("funds_ok") and not gates.get("above_vwap"):
            phase = "等待转强"   # 资金+共振已确认，仅价格低于VWAP（仍属观察，不预警买点）
        else:
            phase = "观察中"     # 前置门槛未过，不进入预警
        risk = _row_risk_status(r)
        row["intersection_phase"] = phase
        row["pre_intersection_eligible"] = pre_intersection_eligible
        row["preintersection_missing"] = missing
        row["trigger_price"] = _predict_trigger_price(r, missing)
        row["_gates"] = gates
        row["gate_failures"] = gate_failures
        row["gate_failure_text"] = "；".join(gate_failures) if gate_failures else "全部通过"
        row["risk_note"] = (
            f"公告风险仅观察({risk})" if (phase in ("准交集", "等待转强") and risk != "clean") else ""
        )
        out.append(row)
    return out


def evaluate_intersection_states(
    intersection_rows: List[Dict[str, Any]],
    pre_rows: List[Dict[str, Any]],
    previous_state: Dict[str, Dict[str, Any]],
    now: datetime,
    config: Optional[Dict[str, Any]] = None,
    snapshot_id: Optional[str] = None,
    risk_map: Optional[Dict[str, str]] = None,
    minute_map: Optional[Dict[str, Dict[str, Any]]] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """default-v2 严格顺序状态机。

    OBSERVING → PRE_INTERSECTION → INTERSECTION_LATCHED → WAIT_RETEST
      → RETEST_READY → ENTRY_ELIGIBLE
    任意阶段 → INVALID；任意锁存阶段 → EXPIRED；首次交集过热 → LATE_INTERSECTION。
    禁止同一轮快照跳级（每快照最多前进一级）。

    - snapshot_id：独立快照标识（报告数据时间/行情批次），连续确认按不同
      snapshot_id 计数，同一份数据不得重复计数。
    - risk_map：按代码索引的统一公告风险，状态机不重新查询/解析风险。
    - minute_map：code -> {age_seconds,status,last_bar_at,close_5m,vwap_5m,vol_5m}；
      分钟K过期(>minute_fresh_seconds)是回踩确认硬否决，但不推断资金流出。
    - market_context：{market_mode, breadth_pct, reason}；CASH 一律禁止新开仓。
    每个快照只读取"当前可见字段 + 已持久化历史"，不使用任何未来数据。
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    cfg = {**DEFAULT_INTERSECTION_CONFIG, **(config or {})}
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    date_text = now.strftime("%Y-%m-%d")
    latch_window = int(cfg["intersection_latch_minutes"])
    pre_need = max(1, int(cfg.get("pre_confirm_snapshots", 2)))
    retest_need = max(1, int(cfg.get("retest_confirm_snapshots", 2)))
    deadline_seconds = _clock_minutes(str(cfg["afternoon_buy_deadline"])) * 60
    now_seconds = now.hour * 3600 + now.minute * 60 + now.second
    past_deadline = now_seconds > deadline_seconds

    snap_id = str(snapshot_id or now_text)
    risk_map = risk_map or {}
    minute_map = minute_map or {}
    mctx = market_context or {}
    market_mode = str(mctx.get("market_mode") or "NORMAL")
    breadth_pct = mctx.get("breadth_pct")

    current_inter: Dict[str, Dict[str, Any]] = {str(r.get("code")): r for r in intersection_rows}
    current_pre: Dict[str, Dict[str, Any]] = {str(r.get("code")): r for r in pre_rows}

    next_state: Dict[str, Dict[str, Any]] = {
        code: dict(value) for code, value in (previous_state or {}).items()
    }
    results: List[Dict[str, Any]] = []

    LATCHED_FAMILY = {PHASE_LATCHED, PHASE_WAIT_RETEST, PHASE_RETEST_READY, PHASE_ENTRY, PHASE_LATE}

    def _risk_for(code: str, row: Dict[str, Any]) -> str:
        # 需求2：所有模块从同一份按代码索引的公告风险结果读取；缺失=unknown
        if code in risk_map:
            status = str(risk_map[code])
            return status if status else "unknown"
        return _row_risk_status(row)

    def _entry_allowed(risk: str) -> Tuple[bool, str]:
        """新开仓资格 = 公告clean + 未过14:20 + 市场环境非CASH。

        watch_risk 可预警可观察但不得新开仓；avoid/unknown 均否决。
        DOWNGRADE 环境的附加条件（clean+共振+回踩确认）已是 ENTRY_ELIGIBLE
        的必要条件；CASH 一律禁止。
        """
        if risk != "clean":
            return False, f"公告风险否决({risk})"
        if past_deadline:
            return False, "已过新开仓截止，仅供明日观察"
        if market_mode == "CASH":
            return False, f"市场环境CASH(宽度{breadth_pct}%)，暂停新开仓"
        return True, ""

    def _count_confirm(item: Dict[str, Any], count_key: str, snap_key: str, passed: bool) -> int:
        """连续确认按不同快照计数：同一 snapshot_id 不重复计数（需求6）。"""
        if item.get(snap_key) == snap_id:
            return int(item.get(count_key) or 0)
        item[snap_key] = snap_id
        item[count_key] = (int(item.get(count_key) or 0) + 1) if passed else 0
        return item[count_key]

    def _expires_text(base: datetime) -> str:
        return (base + timedelta(minutes=latch_window)).strftime("%Y-%m-%d %H:%M:%S")

    def _emit(row: Dict[str, Any], item: Dict[str, Any], display_label: Optional[str] = None) -> None:
        code = str(row.get("code"))
        phase_code = _canonical_phase(item.get("phase"))
        label = display_label or PHASE_LABELS.get(phase_code, phase_code)
        risk = item.get("risk_status") or _risk_for(code, row)
        risk_clean = risk == "clean"
        first = _parse_intersection_datetime(item.get("first_intersection_at")) or now
        age = round(max(0.0, (now - first).total_seconds() / 60), 1)
        minute_info = minute_map.get(code)
        fresh, m_age, m_status, _m_block = _minute_freshness(minute_info, cfg)
        allowed, block = _entry_allowed(risk)
        eligible = phase_code == PHASE_ENTRY and allowed
        entry_block = item.get("entry_block_reason") or ("" if allowed else block)
        row.update({
            "intersection_phase": label,
            "phase_code": phase_code,
            "intersection_state": label,            # 兼容旧前端字段
            "signal_age_minutes": age,
            "signal_age": f"{age:.1f}分钟",
            "first_pre_at": item.get("first_pre_at"),
            "first_intersection_at": item.get("first_intersection_at"),
            "preintersection_at": item.get("first_pre_at") or item.get("preintersection_at"),
            "preintersection_missing": item.get("preintersection_missing") or row.get("preintersection_missing"),
            "trigger_price": item.get("trigger_price") if item.get("trigger_price") is not None else row.get("trigger_price"),
            "trigger_vwap": item.get("trigger_vwap"),
            "trigger_high": item.get("trigger_high"),
            "trigger_flow_5m": item.get("trigger_flow_5m"),
            "trigger_sector_strength": item.get("trigger_sector_strength"),
            "expires_at": item.get("expires_at"),
            "consecutive_confirmations": int(item.get("consecutive_confirmations") or 0),
            "pullback_pct": item.get("pullback_pct"),
            "late_flag": bool(item.get("late_flag")),
            "late_reasons": item.get("late_reasons") or [],
            "late_reason": "；".join(item.get("late_reasons") or []) or "无",
            "invalid_reason": item.get("invalid_reason"),
            "failure_reason": item.get("invalid_reason") or item.get("failure_reason") or "-",
            "data_block_reason": item.get("data_block_reason") or "",
            "pullback_zone": _pullback_zone(item.get("trigger_high") or item.get("trigger_price"), cfg),
            "actionable": eligible,
            "new_open_eligible": eligible,
            "new_entry_allowed": allowed,
            "entry_block_reason": entry_block,
            "risk_note": "" if risk_clean else f"公告风险仅观察({risk})",
            "announcement_risk": risk,
            "risk_status": risk,
            "minute_data_fresh": fresh,
            "minute_data_stale": m_status != "fresh",
            "minute_status": m_status,
            "minute_age_seconds": m_age,
            "minute_last_bar_at": (minute_info or {}).get("last_bar_at"),
            "market_mode": market_mode,
            "market_breadth_pct": breadth_pct,
            "past_entry_deadline": past_deadline,
            "deadline_note": "已过新开仓截止，仅供明日观察" if past_deadline else "",
            "gate_failures": row.get("gate_failures") or [],
            "gate_failure_text": row.get("gate_failure_text") or "全部通过",
            "intersection_config_version": str(cfg.get("version", INTERSECTION_CONFIG_VERSION)),
            "intersection_config_source": str(cfg.get("source", "default")),
        })
        results.append(row)

    def _step_retest(item: Dict[str, Any], row: Dict[str, Any], code: str, risk: str) -> None:
        """已锁存(非迟到)信号的严格顺序推进：LATCHED→WAIT_RETEST→RETEST_READY→ENTRY_ELIGIBLE。

        每快照最多前进一级；失效条件命中立即 INVALID。
        分钟K过期时不得推进到 RETEST_READY/ENTRY_ELIGIBLE，也不推断资金流出。
        """
        phase = _canonical_phase(item.get("phase"))
        price = row.get("price")
        vwap = row.get("vwap")
        flow5 = row.get("flow_5m_inc")
        flow15 = row.get("flow_15m_inc")
        resonance = row.get("resonance") == "是"

        # 更新触发后高点（回踩幅度基准）
        trigger_high = item.get("trigger_high") or item.get("trigger_price")
        for candidate_high in (row.get("high"), price):
            if is_number(candidate_high) and is_number(trigger_high):
                trigger_high = max(trigger_high, candidate_high)
            elif is_number(candidate_high) and trigger_high is None:
                trigger_high = candidate_high
        item["trigger_high"] = trigger_high

        minute_info = minute_map.get(code)
        fresh, _m_age, m_status, m_block = _minute_freshness(minute_info, cfg)
        close_5m = (minute_info or {}).get("close_5m")
        vwap_5m = (minute_info or {}).get("vwap_5m")
        vol_5m = (minute_info or {}).get("vol_5m")
        impulse = item.get("impulse_5m_volume")
        if not is_number(impulse) and is_number(vol_5m) and fresh:
            item["impulse_5m_volume"] = vol_5m   # 迟补启动量基准（首次可用的新鲜分钟量）
            impulse = vol_5m

        pullback_pct = None
        if is_number(trigger_high) and trigger_high > 0 and is_number(price):
            pullback_pct = (trigger_high - price) / trigger_high * 100
        item["pullback_pct"] = round(pullback_pct, 2) if pullback_pct is not None else None

        # ── 立即失效条件（需求7）──
        below_vwap_snap = (row.get("price_above_vwap") is False) or (
            is_number(vwap) and is_number(price) and vwap > 0 and price < vwap
        )
        # 分钟K新鲜时用 close_5m/vwap_5m 判定；过期/缺失时退回快照VWAP，
        # 绝不把分钟数据缺失当成资金流出。
        if fresh and is_number(close_5m) and is_number(vwap_5m):
            below_vwap = close_5m < vwap_5m
        else:
            below_vwap = below_vwap_snap
        if below_vwap and is_number(flow5) and flow5 <= 0:
            item["phase"] = PHASE_INVALID
            item["invalid_reason"] = "跌破VWAP且5分钟资金转负"
            return
        if pullback_pct is not None and pullback_pct > float(cfg["pullback_max_pct"]):
            item["phase"] = PHASE_INVALID
            item["invalid_reason"] = f"回撤超限({pullback_pct:.1f}%>{cfg['pullback_max_pct']}%)"
            return
        if not resonance:
            item["phase"] = PHASE_INVALID
            item["invalid_reason"] = "板块共振消失"
            return

        # 首次交集只记录启动事件：下一快照才进入等待回踩（禁止同轮跳级）
        if phase == PHASE_LATCHED:
            item["phase"] = PHASE_WAIT_RETEST
            item["consecutive_confirmations"] = 0
            item["last_confirm_snapshot_id"] = None
            return

        # ── 回踩确认条件（需求3/7）──
        volume_contracted = (
            is_number(vol_5m) and is_number(impulse) and impulse > 0
            and vol_5m <= impulse * float(cfg["pullback_vol_ratio"])
        )
        retest_structure_ok = (
            pullback_pct is not None
            and float(cfg["pullback_min_pct"]) <= pullback_pct <= float(cfg["pullback_max_pct"])
            and volume_contracted
            and fresh                      # 分钟K过期 → 硬否决回踩确认
        )
        retest_confirmed = (
            retest_structure_ok
            and is_number(close_5m) and is_number(vwap_5m) and close_5m >= vwap_5m
            and is_number(flow5) and flow5 > 0
            and is_number(flow15) and flow15 > 0
            and resonance
            and risk == "clean"
        )
        if not fresh:
            retest_confirmed = False
            item["data_block_reason"] = m_block
        else:
            item["data_block_reason"] = ""
        item["retest_confirmed"] = bool(retest_confirmed)

        count = _count_confirm(item, "consecutive_confirmations", "last_confirm_snapshot_id", retest_confirmed)

        if phase == PHASE_WAIT_RETEST:
            if retest_confirmed and count >= 1:
                item["phase"] = PHASE_RETEST_READY   # 第1次确认
            return
        if phase == PHASE_RETEST_READY:
            if not retest_confirmed:
                item["phase"] = PHASE_WAIT_RETEST    # 确认中断，回到等待并重新计数
                return
            if count >= retest_need:                 # 连续第2次确认（不同快照）
                allowed, block = _entry_allowed(risk)
                if allowed and not past_deadline:    # 两次确认都必须在14:20前完成
                    item["phase"] = PHASE_ENTRY
                    item["entry_confirmed_at"] = now_text
                    item["entry_block_reason"] = ""
                else:
                    item["entry_block_reason"] = block
            return
        if phase == PHASE_ENTRY:
            # 维持资格需条件持续成立；结构失守但未触发失效 → 退回等待回踩
            if not retest_confirmed:
                item["phase"] = PHASE_WAIT_RETEST
                item["invalid_reason"] = None
            return

    def _new_item(code: str) -> Dict[str, Any]:
        return {
            "trade_date": date_text,
            "code": code,
            "phase": PHASE_OBSERVING,
            "first_pre_at": None,
            "first_intersection_at": None,
            "trigger_price": None,
            "trigger_vwap": None,
            "trigger_high": None,
            "expires_at": None,
            "consecutive_confirmations": 0,
            "last_snapshot_at": None,
            "last_minute_bar_at": None,
            "invalid_reason": None,
        }

    # ── 1) 当前正式交集行（锁存事件 + 迟到过滤 + 严格顺序推进）──
    for code, row in current_inter.items():
        old = next_state.get(code)
        item = dict(old) if old else _new_item(code)
        cur_phase = _canonical_phase(item.get("phase"))
        item["trade_date"] = date_text
        item["active"] = True
        item["last_seen_at"] = now_text
        item["last_snapshot_at"] = now_text
        item["name"] = row.get("name") or item.get("name")
        risk = _risk_for(code, row)
        item["risk_status"] = risk
        minute_info = minute_map.get(code)
        if (minute_info or {}).get("last_bar_at"):
            item["last_minute_bar_at"] = minute_info["last_bar_at"]

        if cur_phase == PHASE_EXPIRED:
            # 过期后重新出现在交集：视为全新启动事件
            item = _new_item(code)
            item["active"] = True
            item["last_seen_at"] = now_text
            item["last_snapshot_at"] = now_text
            item["name"] = row.get("name")
            item["risk_status"] = risk
            cur_phase = PHASE_OBSERVING

        if cur_phase == PHASE_INVALID:
            # 当日失效信号不复活，不产生买点
            _emit(row, item)
            next_state[code] = item
            continue

        if not item.get("first_intersection_at"):
            # 首次交集事件：锁存触发信息 + 迟到过滤（只记录启动，不产生买点）
            item["first_intersection_at"] = now_text
            item["trigger_price"] = row.get("price")
            item["trigger_vwap"] = row.get("vwap")
            item["trigger_high"] = row.get("high") if is_number(row.get("high")) else row.get("price")
            item["trigger_flow_5m"] = row.get("flow_5m_inc")
            item["trigger_sector_strength"] = row.get("resonance")
            item["impulse_5m_volume"] = (minute_info or {}).get("vol_5m")
            item["expires_at"] = _expires_text(now)
            late_reasons = _late_filter_reasons(row, cfg)
            if late_reasons:
                item["late_flag"] = True
                item["late_reasons"] = late_reasons
                item["phase"] = PHASE_LATE          # 首次交集过热 → 迟到交集
            else:
                item["late_flag"] = False
                item["late_reasons"] = []
                item["phase"] = PHASE_LATCHED       # 启动事件，不直接产生买点
                item["consecutive_confirmations"] = 0
                item["last_confirm_snapshot_id"] = None
        else:
            item["expires_at"] = _expires_text(now)  # 仍在交集内，刷新锁存期
            if item.get("late_flag"):
                item["phase"] = PHASE_LATE
            else:
                _step_retest(item, row, code, risk)
        _emit(row, item)
        next_state[code] = item

    # ── 2) 准交集候选（未同时在正式交集）──
    for code, row in current_pre.items():
        if code in current_inter:
            continue
        old = next_state.get(code)
        item = dict(old) if old else _new_item(code)
        cur_phase = _canonical_phase(item.get("phase"))
        item["trade_date"] = date_text
        item["active"] = True
        item["last_seen_at"] = now_text
        item["last_snapshot_at"] = now_text
        item["name"] = row.get("name") or item.get("name")
        risk = _risk_for(code, row)
        item["risk_status"] = risk

        # 已锁存过交集的股票掉回准交集候选：保持锁存相位，检查过期
        if item.get("first_intersection_at") and cur_phase in LATCHED_FAMILY | {PHASE_INVALID}:
            expires = _parse_intersection_datetime(item.get("expires_at"))
            if cur_phase != PHASE_INVALID and expires and now > expires:
                item["phase"] = PHASE_EXPIRED
                item["invalid_reason"] = item.get("invalid_reason") or "锁存期满"
                item["active"] = False
            _emit(row, item)
            next_state[code] = item
            continue

        gate_failures = row.get("gate_failures") or []
        eligible = bool(row.get("pre_intersection_eligible"))
        count = _count_confirm(item, "pre_confirmations", "last_pre_snapshot_id", eligible)

        display: Optional[str] = None
        if eligible and count >= pre_need:
            if not item.get("first_pre_at"):
                item["first_pre_at"] = now_text
                item["preintersection_at"] = now_text
            item["phase"] = PHASE_PRE
            item["preintersection_missing"] = row.get("preintersection_missing")
            if item.get("trigger_price") is None:
                item["trigger_price"] = row.get("trigger_price")
            item["invalid_reason"] = None
            item["pre_note"] = ""
        elif eligible:
            item["phase"] = PHASE_OBSERVING
            item["pre_note"] = f"准交集候选·连续确认{count}/{pre_need}"
            display = "观察中"
        else:
            # 有任何 gate_failures：只能"观察中·门槛未过"，不得进入准交集
            if cur_phase == PHASE_PRE:
                item["pre_exit_reason"] = "；".join(gate_failures) or "门槛回落"
                item["pre_exit_at"] = now_text
            item["phase"] = PHASE_OBSERVING
            item["pre_note"] = "观察中·门槛未过" if gate_failures else "观察中"
            # 等待转强：资金+共振已过、仅VWAP下（仍属观察，不预警买点）
            row_label = row.get("intersection_phase")
            display = row_label if row_label == "等待转强" else "观察中"
        row["pre_note"] = item.get("pre_note") or ""
        row["pre_exit_reason"] = item.get("pre_exit_reason") or ""
        _emit(row, item, display)
        next_state[code] = item

    # ── 3) 本快照掉出候选的锁存信号：latch 窗口内保留，超期 EXPIRED ──
    for code, item in list(next_state.items()):
        if code in current_inter or code in current_pre:
            continue
        cur_phase = _canonical_phase(item.get("phase"))
        if not item.get("first_intersection_at"):
            # 从未锁存：准交集/观察状态掉出候选 → 回到观察，重置确认计数
            if cur_phase == PHASE_PRE:
                item["phase"] = PHASE_OBSERVING
                item["pre_exit_reason"] = "跌出准交集候选"
                item["pre_exit_at"] = now_text
            item["pre_confirmations"] = 0
            item["active"] = False
            continue
        expires = _parse_intersection_datetime(item.get("expires_at"))
        first = _parse_intersection_datetime(item.get("first_intersection_at"))
        deadline_dt = expires or (first + timedelta(minutes=latch_window) if first else None)
        if deadline_dt and now > deadline_dt:
            if cur_phase not in (PHASE_INVALID, PHASE_EXPIRED):
                item["phase"] = PHASE_EXPIRED
                item["invalid_reason"] = item.get("invalid_reason") or "锁存期满"
            item["active"] = False
            continue
        # 仍在锁存窗口内：保留最后相位（无当前快照，不推进、不失效）
        synth = {
            "code": code,
            "name": item.get("name") or code,
            "price": item.get("trigger_price"),
            "change": None,
            "risk_status": item.get("risk_status") or "unknown",
            "latched_hold": True,
        }
        _emit(synth, item)

    return results, next_state


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively replace NaN/Inf with None for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


WATCHLIST_BREAKOUT_STATE_PATH = Path(__file__).resolve().parent / "watchlist_breakout_state.json"


def load_watchlist_breakout_state() -> Dict[str, Dict[str, Any]]:
    """加载观察池突破状态机跨快照持久化数据。"""
    try:
        if WATCHLIST_BREAKOUT_STATE_PATH.exists():
            data = json.loads(WATCHLIST_BREAKOUT_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_watchlist_breakout_state(items: Dict[str, Dict[str, Any]], date_str: str) -> None:
    """持久化保存观察池突破状态机。"""
    try:
        payload = {"date": date_str, "items": items}
        WATCHLIST_BREAKOUT_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def evaluate_watchlist_breakout_states(
    watchlist_items: List[Dict[str, Any]],
    enriched_by_code: Dict[str, Enriched],
    stats: Dict[str, Dict[str, Any]],
    flow_history: Optional[Dict[str, List[Dict[str, Any]]]],
    previous_state: Dict[str, Dict[str, Any]],
    now: datetime,
    risk_map: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    明日观察池突破升级状态机（来源：选股框架.md 8/21 权威定版）：
    流转路径: WATCHING → TRIGGERED → CONFIRMED → B_BREAKOUT → A_STRICT
    - 跨交易日合并：将昨日已持久化的观察池标的与今日新候选求并集，确保昨日标的在次日开盘能无缝流转
    - 09:30–10:10 原则上只观察（不可直升 B_BREAKOUT/A_STRICT）
    - 至少 2 次快照确认
    - avoid 一票否决
    - 超过追高禁区、跌回触发价或 5 分钟转负立即降级重置
    """
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    is_morning_observe = now.hour == 9 or (now.hour == 10 and now.minute <= 10)
    next_state: Dict[str, Dict[str, Any]] = {}
    evaluated_watchlist: List[Dict[str, Any]] = []

    # 1. 跨日观察池合并：将昨日持久化标的与今日新候选合并（昨日标的保留其触发价基准）
    merged_items_map: Dict[str, Dict[str, Any]] = {}
    for p_code, p_item in (previous_state or {}).items():
        if p_code:
            merged_items_map[p_code] = {
                "code": p_code,
                "name": p_item.get("name", p_code),
                "trigger": p_item.get("trigger_price", p_item.get("trigger", 0)),
                "buy_zone": p_item.get("buy_zone", "-"),
                "invalid": p_item.get("invalid", 0),
                "no_chase": p_item.get("no_chase", f">{float(p_item.get('trigger_price', 0))*1.025:.2f}不追"),
                "industry": p_item.get("industry", "-"),
                "structure": p_item.get("structure", "观察池追踪"),
                "reason": p_item.get("reason", "昨日观察池延续"),
            }
    for item in watchlist_items:
        code = item.get("code", "")
        if code:
            merged_items_map[code] = {**merged_items_map.get(code, {}), **item}

    for code, item in merged_items_map.items():
        trigger_price = float(item.get("trigger", 0))
        no_chase_str = str(item.get("no_chase", ""))
        m_chase = re.search(r">([\d.]+)不追", no_chase_str)
        no_chase_price = float(m_chase.group(1)) if m_chase else trigger_price * 1.025

        e = enriched_by_code.get(code)
        prev = (previous_state or {}).get(code) or {}
        prev_phase = prev.get("phase", "WATCHING")
        confirm_count = prev.get("confirm_count", 0)

        p_val = getattr(e, "price", None)
        cur_price = float(p_val) if isinstance(p_val, (int, float)) else float(item.get("price", 0))
        f5_val = getattr(e, "flow_5m_inc", None)
        flow_5m = float(f5_val) if isinstance(f5_val, (int, float)) else 0.0
        above_vwap = bool(getattr(e, "price_above_vwap", False))
        dom_type, dom_label = evaluate_dominance_type(e, flow_history) if e else ("none", "✗")
        res = has_resonance(e, stats) if e else False

        # 公告风控解析（优先从 risk_map 获取，次选 _row_risk_status）
        risk = "unknown"
        if risk_map and code in risk_map:
            risk = risk_map[code]
        elif e:
            risk = _row_risk_status(e)
        else:
            risk = _row_risk_status(item)

        # 状态机判定
        cur_phase = "WATCHING"
        breakout_class = "WATCHING"
        status_note = "处于触发价下方观察"

        if risk == "avoid":
            cur_phase = "INVALID"
            breakout_class = "INVALID"
            confirm_count = 0
            status_note = "公告风控 avoid 否决"
        elif cur_price > no_chase_price:
            cur_phase = "OVER_CHASE"
            breakout_class = "WATCHING"
            confirm_count = 0
            status_note = f"超过追高禁区({cur_price:.2f} > {no_chase_price:.2f})"
        elif cur_price < trigger_price:
            # 跌回触发价下方立即降级重置
            confirm_count = 0
            cur_phase = "WATCHING"
            breakout_class = "WATCHING"
            status_note = "处于触发价下方观察"
        elif flow_5m < 0:
            # 5分钟增量转负立即降级重置
            confirm_count = 0
            cur_phase = "WATCHING"
            breakout_class = "WATCHING"
            status_note = f"5分钟增量转负({flow_5m/10000:.0f}万 < 0)立即降级"
        elif cur_price >= trigger_price:
            # 价格突破触发价且 5分增量 >= 500万
            if flow_5m >= 5_000_000.0:
                if prev_phase in ("TRIGGERED", "CONFIRMED", "B_BREAKOUT", "A_STRICT"):
                    # 连续确认
                    confirm_count += 1
                    # 突破确认门槛：站稳 >= 2 期 + VWAP 上方 + 超单主导 (A或B) + 板块共振
                    if confirm_count >= 2 and above_vwap and dom_type in ("absolute", "coalition") and res:
                        cur_phase = "CONFIRMED"
                        if is_morning_observe:
                            breakout_class = "CONFIRMED"
                            status_note = f"09:30-10:10 观察期锁定(已站稳{confirm_count}期·板块共振)"
                        else:
                            # 10:10 后根据资金强度评定 A_STRICT 或 B_BREAKOUT
                            mp_val = getattr(e, "main_pct", 0) if e else 0
                            main_pct = float(mp_val) if isinstance(mp_val, (int, float)) else 0.0
                            hp_val = getattr(e, "high_pull", 0) if e else 0
                            high_pull = float(hp_val) if isinstance(hp_val, (int, float)) else 0.0
                            br_val = getattr(e, "buy_ratio", None) if e else None
                            buy_ratio = float(br_val) if (isinstance(br_val, (int, float)) and not math.isnan(br_val)) else None

                            # A_STRICT 严格要求：绝对主导 + clean + 主力>=5% + 回落<1.5% + 主买比>=1.5（严禁缺数据放行）
                            is_a_strict = (
                                dom_type == "absolute" and
                                risk == "clean" and
                                main_pct >= 5.0 and
                                high_pull < 1.5 and
                                buy_ratio is not None and buy_ratio >= 1.5
                            )
                            if is_a_strict:
                                cur_phase = "A_STRICT"
                                breakout_class = "A_STRICT"
                                status_note = f"A类突破主升·站稳{confirm_count}期·超单绝对主导·板块共振·低回落"
                            else:
                                cur_phase = "B_BREAKOUT"
                                breakout_class = "B_BREAKOUT"
                                status_note = f"B类突破候选·已站稳{confirm_count}期·超单主导·板块共振"
                    else:
                        cur_phase = "TRIGGERED"
                        breakout_class = "TRIGGERED"
                        missing_notes = []
                        if not above_vwap: missing_notes.append("未站上VWAP")
                        if dom_type not in ("absolute", "coalition"): missing_notes.append("超单未主导")
                        if not res: missing_notes.append("板块无共振")
                        status_note = f"突破触发·等待确认({confirm_count}/2期, 待满足:{','.join(missing_notes)})"
                else:
                    cur_phase = "TRIGGERED"
                    breakout_class = "TRIGGERED"
                    confirm_count = 1
                    status_note = "初次放量突破触发价"
            else:
                cur_phase = "WATCHING"
                breakout_class = "WATCHING"
                confirm_count = 0
                status_note = f"突破但5分量能不足({flow_5m/10000:.0f}万 < 500万)"

        # 更新状态持久化
        item_state = {
            "code": code,
            "name": item.get("name"),
            "phase": cur_phase,
            "breakout_class": breakout_class,
            "confirm_count": confirm_count,
            "trigger_price": trigger_price,
            "no_chase_price": no_chase_price,
            "buy_zone": item.get("buy_zone", "-"),
            "invalid": item.get("invalid", 0),
            "no_chase": item.get("no_chase", ""),
            "industry": item.get("industry", "-"),
            "structure": item.get("structure", ""),
            "reason": item.get("reason", ""),
            "last_price": cur_price,
            "flow_5m": flow_5m,
            "dominance_type": dom_type,
            "above_vwap": above_vwap,
            "risk_status": risk,
            "status_note": status_note,
            "last_updated": now_text,
        }
        next_state[code] = item_state

        item_copy = dict(item)
        item_copy.update({
            "price": cur_price,
            "change": getattr(e, "change", item.get("change", 0)) if e else item.get("change", 0),
            "breakout_phase": cur_phase,
            "breakout_class": breakout_class,
            "confirm_count": confirm_count,
            "dominance_label": dom_label,
            "risk_status": risk,
            "status_note": status_note,
        })
        evaluated_watchlist.append(item_copy)

    return evaluated_watchlist, next_state


def build_watchlist(items: List[Enriched], stats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for e in items:
        if not (5 <= e.price <= 45 and 3_000_000_000 <= e.float_mv <= 25_000_000_000):
            continue
        if not (e.price >= e.ma10 or e.price >= 0.97 * e.ma10):
            continue
        if e.five_ret > 0.12 or e.dist60 > 0.15:
            continue
        if not has_resonance(e, stats):
            continue
        if e.change > 5.2 or e.turnover > 9:
            continue
        trigger = max(e.prior_high, e.high)
        buy_low = max(e.ma5 * 0.995, e.ma10 * 0.99)
        buy_high = max(e.ma5 * 1.01, e.ma10 * 1.005)
        invalid = min(e.ma10 * 0.985, e.prior_low * 0.99)
        structure = "趋势低吸" if e.price > e.ma5 > e.ma10 and e.ma10 >= e.ma20 else "突破前观察"
        score = 100 - abs(e.change - 3.2) * 8 - max(0, e.five_ret * 100 - 8) * 2 - max(0, e.dist60 * 100 - 8)
        rows.append({
            "score": round(score, 1),
            "code": e.code,
            "name": e.name,
            "price": e.price,
            "change": e.change,
            "industry": e.industry,
            "structure": structure,
            "trigger": round(trigger, 2),
            "buy_zone": f"{buy_low:.2f}-{buy_high:.2f}",
            "invalid": round(invalid, 2),
            "no_chase": f">{trigger * 1.025:.2f}不追",
            "reason": f"{e.industry}; 5日{e.five_ret*100:.1f}%; 距60高{e.dist60*100:.1f}%",
        })
    return sorted(rows, key=lambda r: r["score"], reverse=True)[:10]


def markdown_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "无"
    align = ["---"] + ["---:" if h in {"涨幅", "换手率", "成交额", "量比", "高位回落", "近5日", "距20日线", "评分", "涨跌", "涨跌%", "主力净占比", "5分钟增量", "主力净额", "超大单", "大单", "中单", "小单", "15分钟增量", "板块内候选"} else "---" for h in headers[1:]]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(lines)


def render_markdown(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    meta = result["meta"]
    lines.append(f"数据时间：{meta['timestamp']}，状态：{meta['status']}，耗时 {meta.get('elapsed_seconds', '?')}s。来源：{meta['source']}。")
    b = result["breadth"]
    fetch_status = result.get("market_fetch_status") or {}
    if fetch_status.get("source") == "sina_fallback":
        lines.append(
            "行情快照：已切换新浪备用源。该源缺少兼容量比、主力资金和行业字段；"
            "趋势结果仅供观察，超短池、双池交集、资金优选和低吸买点均已关闭。"
        )
    elif fetch_status.get("complete") is False:
        lines.append(
            "行情快照：不完整（"
            f"已取 {fetch_status.get('received_pages', '?')}/{fetch_status.get('expected_pages', '?')} 页，"
            f"失败页 {','.join(map(str, fetch_status.get('failed_pages') or [])) or '无'}）。"
        )
    provider_total_label = b.get("provider_total") if b.get("provider_total") is not None else "不可校验"
    adv = b.get("adv", 0)
    dec = b.get("dec", 0)
    flat = b.get("flat", 0)
    valid_chg = b.get("valid_change", adv + dec + flat)
    lines.append(
        "市场宽度："
        f"A股样本 {b.get('total_rows', '?')} / 原始服务端 {provider_total_label}；"
        f"有效涨跌 {valid_chg}；"
        f"上涨 {adv} / 下跌 {dec} / 平盘 {flat} / 无效 {b.get('invalid_change', 0)}；"
        f"主板涨停 {b.get('main_limit_up', 0)}，跌停 {b.get('main_limit_down', 0)}。"
    )
    if b.get("degraded"):
        lines.append(f"数据质量降级：{b.get('quality_reason') or '市场宽度不完整'}；板块共振不参与加分或筛选。")
    idx = "；".join(f"{x['name']} {x['price']} ({x['change']}%)" for x in result["indices"])
    lines.append(f"指数：{idx}")
    if result.get("errors"):
        lines.append(f"K线失败：{len(result['errors'])} 只；结果已按可验证数据输出。")
    if result.get("announcement_check_available") is False:
        unknown_count = len(result.get("announcement_unknown_codes") or [])
        lines.append(
            f'公告检查不可用：候选 {result.get("announcement_total_count", "?")} 只；'
            f'缓存 {result.get("announcement_cached_count", 0)} 只；'
            f'本次请求 {result.get("announcement_requested_count", "?")} 只；'
            f'{len(result.get("announcement_errors") or [])} 只请求失败；unknown {unknown_count} 只，'
            '仅显示观察/数据不足，未进入双池交集、趋势确认和资金优选。'
        )
    elif result.get("announcement_errors"):
        lines.append(f"公告失败：{len(result['announcement_errors'])} 只；相关个股标记为 unknown。")
    elif "announcement_total_count" in result:
        lines.append(
            f'公告检查：候选 {result.get("announcement_total_count", 0)} 只；'
            f'缓存命中 {result.get("announcement_cached_count", 0)} 只；'
            f'本次请求 {result.get("announcement_requested_count", 0)} 只。'
        )
    for warning in result.get("warnings") or []:
        lines.append(f"警告：{warning}")
    if result.get("strict_enabled"):
        raw_dual_pool = result.get("dual_pool_raw") if "dual_pool_raw" in result else result.get("dual_pool") or []
        state_rows = result.get("intersection_states") or []
        eligible_count = sum(1 for row in state_rows if row.get("new_open_eligible"))
        lines.append(
            "双池运行："
            f"超短池 {len(result.get('strict_ultra') or [])} 只；"
            f"趋势观察池 {len(result.get('trend_observation') or [])} 只；"
            f"趋势确认池 {len(result.get('strict_trend') or [])} 只；"
            f"交集 {len(raw_dual_pool)} 只。"
        )
        lines.append(f"交集状态机：可新开仓 {eligible_count} 只；其余为观察、过热或拒绝状态。")

    if result.get("capital_rank"):
        rows = [[
            r["capital_class"], r.get("pool_source", ""), r["code"], r["name"], _fmt(r.get("price")),
            pct(r.get("change")), f"{_fmt(r.get('capital_score'), 1)}",
            flow_amount_str(r.get("main_net", 0)), f"{r.get('main_pct', 0):.1f}%",
            flow_amount_str(r.get("super_net", 0)),
            flow_amount_str(r.get("flow_5m_inc", float('nan'))),
            r.get("vwap_state", ""), r.get("resonance", "否"),
            r.get("capital_data", ""), r.get("capital_reason", ""),
        ] for r in result["capital_rank"]]
        lines += ["", "## 主力资金优选（候选池二次排序）", markdown_table(
            ["资金类", "原始来源", "代码", "名称", "现价", "涨幅", "资金评分", "主力净额",
             "主力净占比", "超大单", "5分钟增量", "均价线", "板块共振",
             "数据完整度", "评分依据"], rows)]

    raw_dual_pool = result.get("dual_pool_raw") if "dual_pool_raw" in result else result.get("dual_pool") or []
    if raw_dual_pool:
        rows = [[r["code"], r["name"], _fmt(r.get("price")), pct(r["change"]), pct(r["turnover"]), amount_yi(r["amount"]), r["industry"], f"{r.get('main_pct',0):.1f}%", r.get("flow_status", ""), announcement_label(r)] for r in raw_dual_pool]
        lines += ["", "## 双池交集（超短池 ∩ 趋势确认池，启动事件）", "注：交集仅代表启动确认，不是买点；真正买点由交集后的缩量回踩产生（见下方状态机）。", markdown_table(["代码", "名称", "现价", "涨幅", "换手率", "成交额", "板块", "主力净占比", "资金状态", "公告风险"], rows)]
    elif "dual_pool" in result:
        lines += ["", "## 双池交集（超短池 ∩ 趋势确认池，启动事件）", "无"]

    if result.get("strict_enabled"):
        state_rows = result.get("intersection_states") or []
        pre_rows_md = result.get("pre_intersection") or []
        config = result.get("intersection_config") or {}
        config_meta = result.get("intersection_config_meta") or {}
        version = config.get("version") or config_meta.get("version") or INTERSECTION_CONFIG_VERSION
        source = config.get("source") or config_meta.get("source") or "default"
        lines += [
            "",
            "## 交集四阶段状态机（准交集 → 首次交集锁存 → 等待回踩 → 回踩确认/失效）",
            (
                f"参数版本：{version}；来源：{source}；确认快照 {config.get('confirmation_snapshots', DEFAULT_INTERSECTION_CONFIG['confirmation_snapshots'])} 次；"
                f"上午截止 {config.get('morning_cutoff', DEFAULT_INTERSECTION_CONFIG['morning_cutoff'])}；"
                f"午后买点截止 {config.get('afternoon_buy_deadline', DEFAULT_INTERSECTION_CONFIG['afternoon_buy_deadline'])}；"
                f"信号锁存 {config.get('intersection_latch_minutes', DEFAULT_INTERSECTION_CONFIG['intersection_latch_minutes'])} 分钟；"
                f"迟到涨幅阈值 {config.get('late_change_pct', DEFAULT_INTERSECTION_CONFIG['late_change_pct'])}%。"
            ),
        ]
        # ── 准交集预警 ──
        if pre_rows_md:
            pre_table = [[
                r.get("code", ""), r.get("name", ""), f"{r.get('price', 0):.2f}", pct(r.get("change")),
                r.get("intersection_phase", ""),
                r.get("preintersection_missing", "-"),
                (f"{r['trigger_price']:.2f}" if is_number(r.get("trigger_price")) else "—"),
                f"{r.get('main_pct', 0):.1f}%", r.get("flow_status", ""), r.get("resonance", "否"),
                r.get("risk_note") or announcement_label(r),
            ] for r in pre_rows_md if r.get("intersection_phase") in ("准交集", "等待转强")]
            if pre_table:
                lines += ["", "### 【准交集预警】", "距趋势确认仅差1项 + 四道门槛（主力净流入/5分资金正/均价线上/板块共振）全部通过。公告风险非clean者仅观察，不给新开仓资格。", markdown_table(
                    ["代码", "名称", "现价", "涨幅", "相位", "缺失条件", "预计触发价", "主力净占比", "资金状态", "板块共振", "公告风险"], pre_table)]
            # 被前置门槛排除的标的：透明输出未通过门槛，便于核对
            excluded_table = [[
                r.get("code", ""), r.get("name", ""), f"{r.get('price', 0):.2f}", pct(r.get("change")),
                r.get("preintersection_missing", "-"),
                r.get("gate_failure_text", "-"),
            ] for r in pre_rows_md if r.get("intersection_phase") == "观察中"]
            if excluded_table:
                lines += ["", "### 【观察中·门槛未过】", "趋势条件只差1项但四道前置门槛未全过，不进入准交集预警。", markdown_table(
                    ["代码", "名称", "现价", "涨幅", "缺失趋势条件", "未通过前置门槛"], excluded_table)]
        # ── 已触发·等待回踩 ──
        active_states = [r for r in state_rows if r.get("late_flag") is not True and r.get("intersection_phase") in ("首次交集", "等待回踩", "回踩确认", "可试错")]
        if active_states:
            act_table = [[
                r.get("code", ""), r.get("name", ""), r.get("first_intersection_at", ""),
                (f"{r['trigger_price']:.2f}" if is_number(r.get("trigger_price")) else "—"),
                (f"{r['trigger_vwap']:.2f}" if is_number(r.get("trigger_vwap")) else "—"),
                flow_amount_str(r.get("trigger_flow_5m")) if is_number(r.get("trigger_flow_5m")) else "—",
                r.get("pullback_zone", "-"), r.get("intersection_phase", ""),
                "有效" if r.get("failure_reason") in (None, "-") else "失效",
                r.get("failure_reason", "-"),
            ] for r in active_states]
            lines += ["", "### 【已触发·等待回踩】", "交集后不追，等待缩量回踩至回踩区且5分资金仍正，方为买点。", markdown_table(
                ["代码", "名称", "交集时间", "触发价", "当时VWAP", "5分资金", "回踩观察区", "相位", "有效性", "失效原因"], act_table)]
        # ── 迟到交集 ──
        late_states = [r for r in state_rows if r.get("late_flag") is True]
        if late_states:
            late_table = [[
                r.get("code", ""), r.get("name", ""), pct(r.get("change")),
                "；".join(r.get("late_reasons", []) or []) or "无",
            ] for r in late_states]
            lines += ["", "### 【迟到交集·不追】", "首次交集即过热/无共振，已标记迟到，不提供买点。", markdown_table(
                ["代码", "名称", "涨幅", "迟到原因"], late_table)]
        if not (pre_rows_md or active_states or late_states):
            lines.append("无（当前无准交集预警，也无正式交集；不为了制造信号而放宽原池条件）")

    if result.get("strict_enabled"):
        rows = [[r["code"], r["name"], _fmt(r.get("price")), pct(r["change"]), pct(r["turnover"]), amount_yi(r["amount"]), _fmt(r.get("volume_ratio")), r["industry"], f"{r.get('main_pct',0):.1f}%", flow_amount_str(r.get("flow_5m_inc",0)), r.get("flow_status",""), announcement_label(r)] for r in result["strict_ultra"]]
        content = markdown_table(["代码", "名称", "现价", "涨幅", "换手率", "成交额", "量比", "板块", "主力净占比", "5分钟增量", "资金状态", "公告风险"], rows)
        if not rows:
            content = "无（当前没有满足超短池硬条件的标的）"
        lines += ["", "## 超短池", content]
    if result.get("strict_enabled"):
        rows = [[r["code"], r["name"], _fmt(r.get("price")), pct(r["change"]), pct(r["turnover"]), amount_yi(r["amount"]), r["industry"], r.get("ma_state", ""), f"{r.get('main_pct',0):.1f}%", flow_amount_str(r.get("flow_5m_inc",0)), r.get("flow_status",""), announcement_label(r)] for r in result.get("trend_observation") or []]
        content = markdown_table(["代码", "名称", "现价", "涨幅", "换手率", "成交额", "板块", "均线状态", "主力净占比", "5分钟增量", "资金状态", "公告风险"], rows)
        if not rows:
            content = "无（当前没有满足趋势观察条件的标的）\n\n说明：不以放宽趋势确认来凑数。"
        lines += ["", "## 趋势观察池", content]
    if result.get("strict_enabled"):
        rows = [[r["code"], r["name"], _fmt(r.get("price")), pct(r["change"]), pct(r["turnover"]), amount_yi(r["amount"]), r["industry"], f"{r.get('main_pct',0):.1f}%", flow_amount_str(r.get("flow_5m_inc",0)), r.get("flow_status",""), announcement_label(r)] for r in result["strict_trend"]]
        content = markdown_table(["代码", "名称", "现价", "涨幅", "换手率", "成交额", "板块", "主力净占比", "5分钟增量", "资金状态", "公告风险"], rows)
        if not rows:
            content = "无（当前没有满足趋势确认硬条件的标的）"
        lines += ["", "## 趋势确认池", content]
    if result.get("strict_enabled"):
        diagnostics = result.get("trend_diagnostics") or []
        near_match = sum(1 for row in diagnostics if row.get("near_match"))
        lines += ["", "## 超短候选的趋势条件淘汰诊断", f"趋势确认诊断：超短候选 {len(diagnostics)} 只，近似命中/只差一项 {near_match} 只。“观察淘汰”与“确认淘汰”分别对应两套规则。"]
        diag_rows = [[
            r.get("code", ""), r.get("name", ""), r.get("ma_state", ""), r.get("ma20_slope", ""),
            r.get("day_change", ""), r.get("five_day", ""), r.get("dist60", ""),
            r.get("turnover_amount", ""), r.get("sector_condition", ""), announcement_label(r),
            r.get("observation_first_failure", ""), r.get("observation_all_failures", ""),
            r.get("first_failure", ""), r.get("all_failures", ""), r.get("upgrade_status", ""),
        ] for r in diagnostics]
        lines.append(markdown_table(
            ["代码", "名称", "均线", "MA20斜率", "当日涨幅", "5日涨幅", "距60高", "换手/成交额", "板块条件", "公告风险", "观察首个淘汰", "观察全部淘汰", "确认首个淘汰", "确认全部淘汰", "结论"], diag_rows
        ))
    if result.get("low_ultra"):
        ind_counts = Counter(r.get("industry", "") for r in result["low_ultra"])
        rows = [[
            r["class"], r["code"], r["name"], _fmt(r.get("price")), pct(r["change"]), pct(r["turnover"]),
            amount_yi(r["amount"]), _fmt(r.get("volume_ratio")), r["industry"], ind_counts.get(r.get("industry", ""), 0),
            r["resonance"], f"{_fmt(r.get('high_pull'))}pct", r["vwap_state"], f"{r.get('main_pct',0):.1f}%",
            flow_amount_str(r.get("super_net", 0)),
            r.get("dominance_label") or r.get("super_lead") or "未计算",
            flow_amount_str(r.get("flow_5m_inc", 0)), r.get("flow_status", ""), r["risk"], announcement_label(r),
        ] for r in result["low_ultra"]]
        lines += ["", "## 低吸超短线 A/B/C", markdown_table(["类", "代码", "名称", "现价", "涨幅", "换手率", "成交额", "量比", "板块", "板块内候选", "共振", "高位回落", "均价线", "主力净占比", "超大单", "超单主导", "5分钟增量", "资金状态", "风险", "公告风险"], rows)]
    if result.get("low_trend"):
        rows = [[r["class"], r["code"], r["name"], _fmt(r.get("price")), pct(r["change"]), pct(r["turnover"]), amount_yi(r["amount"]), r["industry"], r["ma_state"], pct((r.get("five_ret") or 0) * 100), pct((r.get("ma20_dist") or 0) * 100), f"{_fmt(r.get('high_pull'))}pct", f"{r.get('main_pct',0):.1f}%", flow_amount_str(r.get("flow_5m_inc",0)), r.get("flow_status",""), r["risk"], announcement_label(r)] for r in result["low_trend"]]
        lines += ["", "## 低吸短线趋势 A/B/C", markdown_table(["类", "代码", "名称", "现价", "涨幅", "换手率", "成交额", "板块", "均线状态", "近5日", "距20日线", "高位回落", "主力净占比", "5分钟增量", "资金状态", "风险", "公告风险"], rows)]
    if result.get("low_open_wash"):
        rows = [[
            r["code"], r["name"],
            _fmt(r.get("open")), _fmt(r.get("prev_close")),
            f"{r.get('low_open_pct', 0):.2f}%",
            f"{_fmt(r.get('price'))} / {pct(r.get('change'))}",
            f"{r.get('main_pct', 0):.1f}%",
            flow_amount_str(r.get("flow_5m_inc", 0)),
            r.get("vwap_state", ""),
            flow_amount_str(r.get("persistent_net", 0)),
            r.get("industry", ""),
            announcement_label(r),
        ] for r in result["low_open_wash"]]
        lines += ["", "## 低开洗盘（实验性观察补充 · 不计入主/次级信号 · 永不自动出手）",
                   "> ⚠️ 不在 7/30 收敛核心框架内。条件：低开≥2% + 翻红 + 站上均价线 + 当日主力净流入正 + 20日持续净流入。匹配即进观察，不参与主/次级判定，也绝不自动出手。",
                   markdown_table(["代码", "名称", "今开", "昨收", "低开%", "现价/涨幅", "主力净占比", "5分钟增量", "均价线", "20日累计净流入", "板块", "公告风险"], rows)]
    if result.get("watchlist"):
        rows = [[
            r["code"], r["name"], _fmt(r.get("price")), pct(r.get("change")), r["industry"], r["structure"],
            r["trigger"], r.get("breakout_phase", "WATCHING"), f"{r.get('confirm_count', 0)}次",
            r.get("dominance_label", "✗"), r["buy_zone"], r["invalid"], r["no_chase"],
            r.get("status_note", ""), announcement_label(r)
        ] for r in result["watchlist"]]
        lines += ["", "## 明日观察池（含突破升级状态机）", markdown_table(["代码", "名称", "当前价", "涨幅", "板块", "结构", "触发价", "突破状态", "确认次数", "超单主导", "低吸区", "失效", "追高禁区", "状态说明", "公告风险"], rows)]
    if result.get("sector_indices"):
        sectors = sorted(result["sector_indices"], key=lambda s: s["change"], reverse=True)
        rows = [[s["name"], pct(s["change"]), f"{s['price']:.2f}" if s.get("price") else "-",
                 f"{s['up_count']}↑{s['down_count']}↓",
                 s.get("source", "")] for s in sectors]
        lines += ["", "## 相关板块指数", markdown_table(["板块", "涨跌%", "现价", "涨/跌", "来源"], rows)]
    if result.get("flow_detail"):
        details = result["flow_detail"]
        rows = []
        for r in details:
            vwap_label = "上方" if r.get("price_above_vwap", True) else "下方"
            rows.append([
                r.get("code", ""),
                r.get("name", ""),
                flow_amount_str(r.get("main_net", 0)),
                f"{r.get('main_pct', 0):.1f}%",
                flow_amount_str(r.get("super_net", 0)),
                flow_amount_str(r.get("big_net", 0)),
                flow_amount_str(r.get("mid_net", 0)),
                flow_amount_str(r.get("small_net", 0)),
                flow_amount_str(r.get("flow_5m_inc", 0)),
                flow_amount_str(r.get("flow_15m_inc", 0)),
                vwap_label,
                r.get("industry", ""),
                r.get("flow_status", ""),
            ])
        lines += ["", "## 重点候选资金追踪", markdown_table(
            ["代码", "名称", "主力净额", "主力净占比", "超大单", "大单", "中单", "小单",
             "5分钟增量", "15分钟增量", "均价线", "板块", "结论"], rows)]
    lines.append("\n备注：脚本只负责查询和分层；最终买卖、仓位、止损需要按 trading-rules 再分析。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="A-share screening query helper for daily-stock-analysis")
    parser.add_argument("--mode", nargs="+", choices=["all", "strict", "low", "watchlist"], default=["strict"], help="screening modules to output (default: original dual screen)")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="output format")
    parser.add_argument("--workers", type=int, default=6, help="concurrent K-line workers")
    parser.add_argument("--top", type=int, default=10, help="max rows per section")
    parser.add_argument("--save", type=Path, help="also save output to file")
    parser.add_argument("--skip-announcements", action="store_true", help="skip latest-announcement risk check")
    parser.add_argument("--skip-capital-ranking", action="store_true", help="skip capital-flow secondary ranking")
    parser.add_argument("--announcement-page-size", type=int, default=8, help="latest announcement rows per stock")
    parser.add_argument("--network-mode", choices=["auto", "direct", "proxy"], default="auto",
                        help="connection strategy: auto (proxy then direct), direct, or proxy")
    args = parser.parse_args()
    set_network_mode(args.network_mode)
    MARKET_WARNINGS.clear()

    # Normalize mode list: "all" expands to all three individual modules
    modes = set(args.mode)
    if "all" in modes:
        modes = {"strict", "low", "watchlist"}

    t0 = time.time()
    try:
        market, total = fetch_market()
    except NetworkUnavailable as exc:
        print(format_network_failure(exc), file=sys.stderr)
        return 2
    t_market = time.time() - t0
    print(f"[计时] 行情分页: {t_market:.1f}s  ({len(market)} 条)", file=sys.stderr)
    market_fetch_status = get_market_fetch_status()
    fallback_snapshot = market_fetch_status.get("source") == "sina_fallback"
    if fallback_snapshot:
        indices_raw = []
        sector_boards = []
        MARKET_WARNINGS.append("备用行情模式跳过东方财富指数和板块接口，避免主源故障导致二次等待")
    else:
        indices_raw = fetch_indices()
        sector_boards = fetch_sector_indices()
    prefetch = filter_prefetch(market, args.mode)
    enriched, errors = enrich_all(prefetch, max(1, args.workers))
    t_enrich = time.time() - t0 - t_market
    print(f"[计时] K线增强: {t_enrich:.1f}s  ({len(enriched)} 只)", file=sys.stderr)
    if fallback_snapshot:
        MARKET_WARNINGS.append(
            "备用行情缺少兼容量比、主力资金和行业字段：趋势池仅供观察；"
            "超短池、双池交集、资金优选和低吸买点不会输出。"
        )
    breadth = market_summary(market, total, market_fetch_status)
    stats = sector_stats(market, breadth)

    # --- capital flow: load history, compute increments, classify ---
    flow_history = load_flow_history()
    has_snapshot = bool(flow_history)
    apply_flow_increments(enriched, flow_history)
    for e in enriched:
        e.flow_status = classify_flow(e, stats, has_snapshot)
    save_flow_history(enriched, flow_history)
    print(f"[计时] 资金流向: 历史{'有' if has_snapshot else '无'} ({len(flow_history)} 只)", file=sys.stderr)

    latest_ts = max([r.get("f124") or 0 for r in market] or [0])
    ts = datetime.fromtimestamp(latest_ts, TZ) if latest_ts else datetime.now(TZ)
    after_1420 = ts.hour > 14 or (ts.hour == 14 and ts.minute >= 20)

    strict_ultra_all = [] if fallback_snapshot else sorted([e for e in enriched if strict_ultra(e)], key=lambda e: e.change, reverse=True)
    strict_ultra_items = strict_ultra_all[:args.top]
    trend_observation_items = sorted(
        [e for e in enriched if trend_observation(e)],
        key=lambda e: (not strict_trend(e), -e.change, e.dist60),
    )[:args.top]
    strict_trend_items = sorted([e for e in enriched if strict_trend(e)], key=lambda e: e.change, reverse=True)[:args.top]

    class_order = {"A": 0, "B": 1, "C": 2}
    low_ultra_rows = []
    low_trend_rows = []
    if "low" in modes and not fallback_snapshot:
        for e in enriched:
            exclude, reason = _should_exclude_from_low_absorb(e, flow_history)
            if exclude:
                continue  # 硬黑名单或高位派发降权，不进入低吸候选
            cls, tags, score = low_ultra_class(e, stats, after_1420)
            dom_type, dom_label = evaluate_dominance_type(e, flow_history)
            if cls != "C" or (e.change >= 2.2 and (e.turnover > 10 or e.change > 5.2 or e.volume_ratio > 6)):
                low_ultra_rows.append({
                    **asdict(e),
                    "class": cls,
                    "risk": "/".join(tags) if tags else "无",
                    "score": score,
                    "resonance": "是" if has_resonance(e, stats) else "否",
                    "dominance_type": dom_type,
                    "dominance_label": dom_label,
                    "super_lead": dom_label,
                })
            cls2, tags2, score2 = low_trend_class(e, stats, after_1420)
            if cls2 != "C" or (e.change >= 2.5 and (e.change > 6 or e.turnover > 9 or e.ma20_dist > 0.15)):
                low_trend_rows.append({
                    **asdict(e),
                    "class": cls2,
                    "risk": "/".join(tags2) if tags2 else "无",
                    "score": score2,
                    "ma_state": f"MA5/10/20上方,5日{'上行' if e.ma5 > e.prev_ma5 else '未上行'},10日{'走平上行' if e.ma10 >= e.prev_ma10 else '下行'}",
                    "dominance_type": dom_type,
                    "dominance_label": dom_label,
                    "super_lead": dom_label,
                })
        low_ultra_rows = sorted(low_ultra_rows, key=lambda r: (class_order[r["class"]], -r["score"], abs(r["change"] - 3.4)))[: max(args.top, 15)]
        low_trend_rows = sorted(low_trend_rows, key=lambda r: (class_order[r["class"]], -r["score"], abs(r["change"] - 3.8)))[: max(args.top, 15)]

    strict_ultra_rows = [
        {**asdict(e), "resonance": "是" if has_resonance(e, stats) else "否"}
        for e in strict_ultra_items
    ] if "strict" in modes else []
    trend_observation_rows = [
        {**asdict(e), "ma_state": "MA5/10/20上方" if e.price > e.ma5 and e.price > e.ma10 and e.price > e.ma20 else "MA20上方，短均线修复中"}
        for e in trend_observation_items
    ] if "strict" in modes else []
    strict_trend_rows = [asdict(e) for e in strict_trend_items] if "strict" in modes else []
    trend_diagnostics = [trend_condition_diagnosis(e, stats) for e in strict_ultra_all] if "strict" in modes else []
    ultra_codes = {r["code"] for r in strict_ultra_rows}
    observation_codes = {r["code"] for r in trend_observation_rows}
    confirmation_codes = {r["code"] for r in strict_trend_rows}
    relaxed_confirm_codes = {e.code for e in enriched if trend_confirm_relaxed(e)}
    enriched_by_code_for_pool = {e.code: e for e in enriched}

    # 双池交集基准：超短池 ∩ 趋势确认池(strict_trend)。交集即启动事件。
    board_by_code_for_pool = {b["name"]: b for b in sector_boards if b.get("name")}
    dual_pool_rows = [
        {
            **r,
            "resonance": "是" if has_resonance(enriched_by_code_for_pool[r["code"]], stats) else "否",
            "sector_change": (board_by_code_for_pool.get(r.get("industry"), {}) or {}).get("change"),
        }
        for r in strict_ultra_rows
        if r["code"] in relaxed_confirm_codes
    ]
    dual_pool_raw_rows = [dict(r) for r in dual_pool_rows]

    # --- sector indices for relevant industries ---
    relevant_industries: set = set()
    for r in strict_ultra_rows + trend_observation_rows + strict_trend_rows:
        ind = r.get("industry", "")
        if ind and ind != "-":
            relevant_industries.add(ind)
    board_by_name = {b["name"]: b for b in sector_boards}
    sector_indices: List[Dict[str, Any]] = []
    for ind in sorted(relevant_industries):
        board = board_by_name.get(ind)
        if not board:
            for bn, bd in board_by_name.items():
                if ind in bn or bn in ind:
                    board = bd
                    break
        if board:
            sector_indices.append({
                "name": board["name"],
                "change": board["change"],
                "price": board["price"],
                "up_count": board["up_count"],
                "down_count": board["down_count"],
                "turnover": board["turnover"],
                "source": "筛选",
            })

    intersection_config, intersection_config_meta = resolve_intersection_config(load_intersection_calibration())
    intersection_runtime_config = {
        **intersection_config,
        "version": intersection_config_meta["version"],
        "source": intersection_config_meta["source"],
    }

    diag_by_code = {d["code"]: d for d in trend_diagnostics} if "strict" in modes else {}
    pre_intersection_rows = (
        compute_pre_intersection(strict_ultra_rows, relaxed_confirm_codes, diag_by_code, intersection_runtime_config)
        if "strict" in modes else []
    )

    raw_watchlist = build_watchlist(enriched, stats) if "watchlist" in modes else []

    result = {
        "meta": {
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "status": current_status(ts),
            "source": (
                "新浪财经实时备用快照（字段降级）"
                if fallback_snapshot else "东方财富push2实时/快照"
            ) + " + 腾讯/东方财富日K" + ("" if args.skip_announcements else " + 东方财富公告"),
            "total_rows": len(market),
            "provider_total": total,
            "market_fetch_complete": market_fetch_status.get("complete"),
            "prefetch_rows": len(prefetch),
            "enriched_rows": len(enriched),
            "elapsed_seconds": round(time.time() - t0, 1),
            "market_data_degraded": fallback_snapshot,
        },
        "breadth": breadth,
        "market_fetch_status": market_fetch_status,
        "indices": [{"code": x.get("f12"), "name": x.get("f14"), "price": x.get("f2"), "change": x.get("f3")} for x in indices_raw],
        "errors": errors,
        "warnings": MARKET_WARNINGS,
        "announcement_errors": [],
        "strict_enabled": "strict" in modes,
        "strict_ultra": strict_ultra_rows,
        "trend_observation": trend_observation_rows,
        "strict_trend": strict_trend_rows,
        "trend_diagnostics": trend_diagnostics,
        "dual_pool": dual_pool_rows,
        "dual_pool_raw": dual_pool_raw_rows,
        "pre_intersection": pre_intersection_rows,
        "intersection_states": [],
        "intersection_config": intersection_runtime_config,
        "intersection_config_meta": intersection_config_meta,
        "capital_rank": [],
        "low_ultra": low_ultra_rows if "low" in modes and not fallback_snapshot else [],
        "low_trend": low_trend_rows if "low" in modes and not fallback_snapshot else [],
        "watchlist": raw_watchlist,
        "sector_indices": sector_indices,
        "has_snapshot": has_snapshot,
        "low_open_wash": low_open_wash_rows(enriched, flow_history),
    }

    # --- flow detail for key candidates (strict top 5 + low A-class top 5 + holdings) ---
    detail_codes: set = set()
    flow_detail: List[Dict[str, Any]] = []
    for e in (strict_ultra_items[:5] + trend_observation_items[:5] + strict_trend_items[:5]):
        if e.code not in detail_codes:
            detail_codes.add(e.code)
            flow_detail.append(asdict(e))
    for r in low_ultra_rows if "low" in modes else []:
        if r.get("class") == "A" and r["code"] not in detail_codes:
            detail_codes.add(r["code"])
            flow_detail.append(r)
            if len([d for d in flow_detail if d.get("class") == "A"]) >= 5:
                break
    # Add holdings to flow detail
    holdings_file = SCRIPT_DIR / "holdings.json"
    enriched_by_code = {e.code: e for e in enriched}
    try:
        if holdings_file.exists():
            holdings_data = json.loads(holdings_file.read_text(encoding="utf-8"))
            for h in holdings_data:
                code = h.get("code", "")
                if code and code not in detail_codes:
                    detail_codes.add(code)
                    e = enriched_by_code.get(code)
                    if e:
                        d = asdict(e)
                        d["_holding"] = True
                        flow_detail.append(d)
    except Exception:
        pass
    result["flow_detail"] = flow_detail

    # --- 公告风控查询与统一门槛处理 ---
    if not args.skip_announcements:
        t_ann_start = time.time()
        def announcement_progress(done: int, total: int, code: str, status: str, source: str) -> None:
            source_label = "缓存" if source == "cache" else "查询"
            print(f"[公告] {done}/{total} {code} {source_label}={status}", file=sys.stderr, flush=True)

        result["announcement_errors"] = attach_announcement_risks(
            result,
            args.announcement_page_size,
            args.workers,
            progress_callback=announcement_progress,
        )
        apply_announcement_pool_gates(result)
        t_ann = time.time() - t_ann_start
        print(f"[计时] 公告检查: {t_ann:.1f}s", file=sys.stderr)
    else:
        result["announcement_check_available"] = False
        result["announcement_unknown_codes"] = sorted({
            str(row.get("code"))
            for section in (
                "strict_ultra", "trend_observation", "strict_trend", "dual_pool", "dual_pool_raw",
                "trend_diagnostics", "low_ultra", "low_trend", "watchlist",
            )
            for row in (result.get(section) or [])
            if _row_risk_status(row) == "unknown"
        })
        apply_announcement_pool_gates(result)

    # 汇总统一公告风控字典。公告查询结果是唯一权威来源；其它池子只
    # 用来补齐查询结果缺失的代码，且缺失状态一律 unknown（fail-closed）。
    valid_risk_statuses = {"clean", "watch_risk", "avoid", "unknown"}
    risk_map: Dict[str, str] = {
        str(code): (status if isinstance(status, str) and status in valid_risk_statuses else "unknown")
        for code, status in (result.get("announcement_risk_map") or {}).items()
        if code
    }
    for section in (
        "watchlist", "strict_ultra", "trend_observation", "strict_trend", "dual_pool",
        "dual_pool_raw", "pre_intersection", "capital_rank", "trend_diagnostics",
        "low_ultra", "low_trend", "low_open_wash",
    ):
        for row in (result.get(section) or []):
            code = str(row.get("code") or "")
            if code:
                risk_map.setdefault(code, _row_risk_status(row))

    # 将确切的公告风控状态同步赋给 Enriched 实例
    for e in enriched:
        e.risk_status = risk_map.get(e.code, "unknown")

    # --- 在公告检查之后统一执行主力资金优选排序（确保 clean 硬门槛生效） ---
    strict_candidate_codes = {
        r["code"]
        for r in (result.get("strict_ultra") or []) + (result.get("trend_observation") or [])
        if _row_risk_status(r) not in {"avoid", "unknown"}
    }
    capital_rank = [] if args.skip_capital_ranking or fallback_snapshot else rank_capital_candidates(
        [e for e in enriched if e.code in strict_candidate_codes], stats, flow_history
    )[:args.top]
    for row in capital_rank:
        in_ultra = row["code"] in ultra_codes
        in_observation = row["code"] in observation_codes
        in_confirmation = row["code"] in confirmation_codes
        if in_ultra and in_observation:
            row["pool_source"] = "双池交集 + 趋势确认" if in_confirmation else "双池交集"
        elif in_ultra:
            row["pool_source"] = "超短池"
        else:
            row["pool_source"] = "趋势确认池" if in_confirmation else "趋势观察池"
    result["capital_rank"] = capital_rank
    # capital_rank 是公告查询完成后新生成的派生池，必须再次走同一套
    # fail-closed 门槛，避免 avoid/unknown 通过重新排名重新出现。
    apply_announcement_pool_gates(result)

    # 观察池构建与突破状态机评估（在公告检查之后执行，接入真实风控）
    wl_state_payload = load_watchlist_breakout_state()
    prev_wl_items = wl_state_payload.get("items") or {}
    is_today = (wl_state_payload.get("date") == ts.strftime("%Y-%m-%d"))
    if not is_today and prev_wl_items:
        # 跨日：保留昨日设定的触发价基准，重置日内确认计数为0
        for code, itm in prev_wl_items.items():
            itm["confirm_count"] = 0
            itm["phase"] = "WATCHING"
            itm["breakout_class"] = "WATCHING"
    watchlist_evaluated, next_wl_items = evaluate_watchlist_breakout_states(
        raw_watchlist, enriched_by_code_for_pool, stats, flow_history, prev_wl_items, ts, risk_map
    )
    result["watchlist"] = watchlist_evaluated
    save_watchlist_breakout_state(next_wl_items, ts.strftime("%Y-%m-%d"))

    if result.get("strict_enabled"):
        state_payload = load_intersection_state()
        previous_items = state_payload.get("items") or {}
        if state_payload.get("date") != ts.strftime("%Y-%m-%d"):
            previous_items = {}
        state_rows, next_items = evaluate_intersection_states(
            result.get("dual_pool_raw") or [],
            result.get("pre_intersection") or [],
            previous_items,
            ts,
            intersection_runtime_config,
            risk_map=risk_map,
        )
        result["intersection_states"] = state_rows
        save_intersection_state(next_items, ts.strftime("%Y-%m-%d"))

    t_total = time.time() - t0
    print(f"[计时] 总计: {t_total:.1f}s", file=sys.stderr)

    if args.format == "json":
        output = json.dumps(_sanitize_for_json(result), ensure_ascii=False, indent=2)
    else:
        output = render_markdown(result)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(output, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
