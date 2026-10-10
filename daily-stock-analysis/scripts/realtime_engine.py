#!/usr/bin/env python3
"""Modified screening engine for real-time dashboard.

Key optimizations over the original a_share_daily_screen:
- K-line caching: daily K-line data barely changes intraday (MAs move slowly),
  so cache it with a 30-min TTL instead of re-fetching every pass.
- Adaptive rate limiting: no fixed delay — start fast, back off exponentially
  only when Eastmoney actually rate-limits us (connection error / HTTP error).
- Pre-warm: separate K-line prefetch step so the first screening is fast.
- run_screening() returns result dict (no printing).
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from dataclasses import asdict
from typing import Any, Dict, List, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import a_share_daily_screen as screen
import dashboard_settings  # 观察模式取值单一来源（strict / observe）
import runtime_paths  # 运行状态路径唯一来源（A_SHARE_STATE_DIR 可定向到临时目录）
import state_commit as state_commit_mod  # 回合状态提交门（超时轮不得提交运行状态）

TZ = screen.TZ
REALTIME_CONFIG = screen.RULE_CONFIG["realtime"]
EM_TRENDS_URL = "https://push2.eastmoney.com/webguest/api/qt/stock/trends2/get"

KLINE_CACHE_FILE = runtime_paths.state_file(".kline_cache.json")
KLINE_CACHE_TTL = 1800  # 30 min — MAs are slow-moving, don't need tick-level freshness

# 每个条目单独记抓取时间：code -> {"fetched_at": ts, "data": result}
# 2026-09-26 修正：原实现用"整份缓存的保存时间"做 TTL，而保存函数每轮筛选结束都会被调用，
# 于是计时器每轮被重置、缓存整天不过期——一只股票当天第一次抓到什么就全天沿用，
# 注释承诺的 30 分钟刷新从未生效。
_kline_cache: Dict[str, Dict[str, Any]] = {}
_kline_cache_date: str = ""      # 缓存文件属于哪一天；跨日整体丢弃
_kline_fetch_count = 0
_kline_cache_hit_count = 0
_kline_fail_count = 0


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── Adaptive rate limiter ────────────────────────────

class AdaptiveRateLimiter:
    """No fixed delay. Backs off on failure, recovers on success."""

    def __init__(self) -> None:
        self._delay = 0.05  # start tiny
        self._success_streak = 0
        self._lock = threading.Lock()

    def on_success(self) -> None:
        with self._lock:
            self._success_streak += 1
            if self._success_streak >= 15:
                self._delay = max(0.0, self._delay - 0.05)
                self._success_streak = 0

    def on_failure(self) -> None:
        with self._lock:
            self._delay = min(2.0, self._delay + 0.3)
            self._success_streak = 0

    def wait(self) -> None:
        d = self._delay
        if d > 0:
            time.sleep(d + random.uniform(0, 0.03))  # jitter


_rate_limiter = AdaptiveRateLimiter()


# ── Cache persistence ────────────────────────────────

def _load_kline_cache() -> None:
    """读取当日缓存文件；旧格式（整条即结果）按文件保存时间补 fetched_at，随后自然过期。"""
    global _kline_cache, _kline_cache_date
    _kline_cache = {}
    _kline_cache_date = ""
    try:
        if not KLINE_CACHE_FILE.exists():
            return
        data = json.loads(KLINE_CACHE_FILE.read_text(encoding="utf-8"))
        today = _today()
        if data.get("date") != today:
            return
        file_ts = float(data.get("timestamp") or 0)
        restored: Dict[str, Dict[str, Any]] = {}
        for code, entry in (data.get("data") or {}).items():
            if isinstance(entry, dict) and "data" in entry:
                fetched_at = float(entry.get("fetched_at") or file_ts or 0)
                restored[code] = {"fetched_at": fetched_at, "data": entry["data"]}
            else:
                restored[code] = {"fetched_at": file_ts, "data": entry}
        _kline_cache = restored
        _kline_cache_date = today
        print(f"[kline-cache] loaded {len(_kline_cache)} entries from {today}", file=sys.stderr)
    except Exception:
        _kline_cache = {}
        _kline_cache_date = ""


def _save_kline_cache(commit: "state_commit_mod.RoundCommit | None" = None) -> None:
    """写出缓存。**不得改写条目的 fetched_at**，否则 TTL 会被每轮重置（2026-09-26 修正点）。

    ``commit`` 非空时只暂存：看板超时/失败的一轮不得提交 K 线缓存。
    """
    global _kline_cache_date
    today = _today()
    _kline_cache_date = today

    def _writer() -> None:
        data = {"date": today, "timestamp": time.time(), "data": _kline_cache}
        state_commit_mod.atomic_write_text(KLINE_CACHE_FILE, json.dumps(data, ensure_ascii=False))

    if commit is not None:
        commit.stage(KLINE_CACHE_FILE, _writer)
        return
    try:
        _writer()
    except Exception:
        pass


def _is_entry_fresh(entry: Optional[Dict[str, Any]]) -> bool:
    """单条缓存是否仍在 TTL 内。

    只看抓取时间，不看缓存文件日期：条目是本进程写入的，天然属于当日；
    跨日文件在 `_load_kline_cache` 已被整体丢弃。2026-09-27 修正：
    原先这里额外要求 `_kline_cache_date == today`，而该变量只在"载入同日文件"时才被设置，
    导致**没有同日缓存文件的进程里，所有条目永远不新鲜、缓存完全不命中**——
    看板每轮会把全部 K 线重新拉一遍，缓存形同虚设。
    """
    if not entry:
        return False
    fetched_at = float(entry.get("fetched_at") or 0)
    return fetched_at > 0 and (time.time() - fetched_at) <= KLINE_CACHE_TTL


def _is_cache_valid() -> bool:
    """整份缓存是否可用：当日写入且至少有内容（用于看板判断是否需要预热）。"""
    return bool(_kline_cache) and _kline_cache_date == _today()


def _fresh_entry_count() -> int:
    return sum(1 for entry in _kline_cache.values() if _is_entry_fresh(entry))


# ── Monkey-patch fetch_kline with cache + adaptive limiting ──

_original_fetch_kline = screen.fetch_kline


def _cached_fetch_kline(code: str, *args, **kwargs):
    """Cached + adaptively rate-limited version of fetch_kline."""
    global _kline_fetch_count, _kline_cache_hit_count, _kline_fail_count, _kline_cache_date
    _kline_cache_date = _today()   # 本进程写入的条目属于当日，缓存文件日期随之为当日

    # 1. 命中当日且未过 TTL 的条目 — 立即返回，不占限速
    entry = _kline_cache.get(code)
    if _is_entry_fresh(entry):
        _kline_cache_hit_count += 1
        return entry["data"]

    # 2. 未命中或已过期 — 重新抓取并写入本条的抓取时间
    _kline_fetch_count += 1
    _rate_limiter.wait()

    try:
        result = _original_fetch_kline(code, *args, **kwargs)
        if result:
            _kline_cache[code] = {"fetched_at": time.time(), "data": result}
        _rate_limiter.on_success()
        return result
    except Exception as e:
        _kline_fail_count += 1
        _rate_limiter.on_failure()
        raise


# 注意：这里**不再** monkey-patch ``screen.fetch_kline``。缓存版通过
# ``run_screening(fetch_kline_fn=...)`` 显式注入核心，避免"导入即改写全局函数"
# 让所有调用点（含单测）的含义随导入顺序漂移。
_load_kline_cache()


# ── Public API ───────────────────────────────────────

def get_cache_stats() -> Dict[str, Any]:
    return {
        "cache_size": len(_kline_cache),
        "cache_valid": _is_cache_valid(),
        "fresh_entries": _fresh_entry_count(),
        "fetch_count": _kline_fetch_count,
        "cache_hit_count": _kline_cache_hit_count,
        "fail_count": _kline_fail_count,
        "rate_limit_delay": round(_rate_limiter._delay, 2),
        "cache_date": _kline_cache_date,
    }


def prewarm_kline_cache(
    workers: int = 6, progress_callback=None, boards: Any = (screen.BOARD_MAIN,)
) -> Dict[str, Any]:
    """Pre-fetch K-line for all stocks that pass the prefetch filter.

    Call this before the first screening to warm the cache.
    Returns stats dict.
    """
    global _kline_fetch_count, _kline_cache_hit_count, _kline_fail_count
    _kline_fetch_count = 0
    _kline_cache_hit_count = 0
    _kline_fail_count = 0

    t0 = time.time()
    screen.set_network_mode("auto")
    screen.MARKET_WARNINGS.clear()

    try:
        market, total = screen.fetch_market()
    except screen.NetworkUnavailable as exc:
        return {"error": str(exc), "elapsed": round(time.time() - t0, 1)}

    prefetch = screen.filter_prefetch(market, ["all"], boards=boards)
    # 2026-09-26 修正：按"条目是否仍在 TTL 内"逐个判断。原先只判断"代码是否在缓存里"，
    # 于是当天已过期（>30 分钟）的条目不会被预热刷新，预热统计也失真。
    codes_to_fetch = [
        r["f12"] for r in prefetch if not _is_entry_fresh(_kline_cache.get(str(r.get("f12", ""))))
    ]

    total_codes = len(codes_to_fetch)
    print(f"[prewarm] {total_codes} K-line to fetch ({len(prefetch)} total, {len(_kline_cache)} cached)", file=sys.stderr)

    if total_codes == 0:
        return {
            "fetched": 0,
            "cached": len(_kline_cache),
            "elapsed": round(time.time() - t0, 1),
            "cache_stats": get_cache_stats(),
        }

    import concurrent.futures as futures

    counters = {"done": 0, "failed": 0}
    lock = threading.Lock()

    def _fetch_one(code: str):
        try:
            _cached_fetch_kline(code, 90)
        except Exception:
            with lock:
                counters["failed"] += 1
        with lock:
            counters["done"] += 1
        if progress_callback:
            progress_callback(counters["done"], total_codes, code, counters["failed"])

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_fetch_one, codes_to_fetch))

    done = counters["done"]
    failed = counters["failed"]

    _save_kline_cache()
    elapsed = time.time() - t0
    print(f"[prewarm] done: {done} fetched, {failed} failed, {elapsed:.1f}s, delay={_rate_limiter._delay:.2f}s", file=sys.stderr)

    return {
        "fetched": done,
        "failed": failed,
        "cached": len(_kline_cache),
        "elapsed": round(elapsed, 1),
        "cache_stats": get_cache_stats(),
    }


# ── 大盘温度计 ──────────────────────────────────────────────

def _build_market_thermometer(breadth: dict, indices_raw: list) -> dict:
    """Assess overall market risk from breadth + index data.

    Returns risk_level: 'strong' | 'normal' | 'caution' | 'danger'
    with detailed metrics for the user to adjust thresholds.
    """
    adv = breadth.get("adv", 0) if breadth else 0
    dec = breadth.get("dec", 0) if breadth else 0
    limit_up = breadth.get("main_limit_up", 0) if breadth else 0
    limit_down = breadth.get("main_limit_down", 0) if breadth else 0
    total_valid = breadth.get("valid_change", 0) if breadth else 0

    adv_dec_ratio = round(adv / dec, 2) if dec > 0 else float("inf") if adv > 0 else 0

    # Index trend: check if major indices are up or down
    idx_up = 0
    idx_down = 0
    for x in indices_raw:
        # 兼容两种入参：原始东财行（f3）与结果里归一化后的指数行（change）。
        chg = x.get("f3", x.get("change"))
        if isinstance(chg, (int, float)):
            if chg > 0:
                idx_up += 1
            elif chg < 0:
                idx_down += 1

    risk_cfg = REALTIME_CONFIG["market_thermometer"]
    # Risk assessment thresholds are registered in tools/rule_config.py.
    #   danger:  limit_up < 5  OR  adv_dec_ratio < 0.5 (with enough samples)
    #   caution: limit_up < 15 OR  adv_dec_ratio < 0.8
    #   strong:  limit_up >= 30 AND adv_dec_ratio >= 1.5
    #   normal:  everything else
    if (
        limit_up < int(risk_cfg["danger_limit_up_max_exclusive"])
        or (
            adv_dec_ratio < float(risk_cfg["danger_adv_dec_ratio_max_exclusive"])
            and total_valid > int(risk_cfg["danger_total_valid_min_exclusive"])
        )
    ):
        risk_level = "danger"
        risk_msg = "市场弱势，涨停稀少且跌多涨少，建议观望"
    elif (
        limit_up < int(risk_cfg["caution_limit_up_max_exclusive"])
        or adv_dec_ratio < float(risk_cfg["caution_adv_dec_ratio_max_exclusive"])
    ):
        risk_level = "caution"
        risk_msg = "市场偏弱，注意控制仓位和止损"
    elif (
        limit_up >= int(risk_cfg["strong_limit_up_min_inclusive"])
        and adv_dec_ratio >= float(risk_cfg["strong_adv_dec_ratio_min_inclusive"])
    ):
        risk_level = "strong"
        risk_msg = "市场强势，涨停家数多且涨多跌少，适合操作"
    else:
        risk_level = "normal"
        risk_msg = "市场中性，按正常策略操作"

    return {
        "risk_level": risk_level,
        "risk_msg": risk_msg,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "adv": adv,
        "dec": dec,
        "adv_dec_ratio": adv_dec_ratio,
        "index_up": idx_up,
        "index_down": idx_down,
        "total_valid": total_valid,
    }


# ── 资金交叉验证 + 进出场建议 ───────────────────────────────

# Sections in result that contain stock rows needing enrichment
_RESULT_ROW_SECTIONS = (
    "strict_ultra", "trend_observation", "strict_trend",
    "capital_rank", "flow_detail",
    "low_ultra", "low_trend", "watchlist",
)


def _enrich_result_rows(result: dict, enriched_by_code: dict) -> None:
    """Add cross-validation warnings and entry/exit suggestions to each stock row."""
    for section in _RESULT_ROW_SECTIONS:
        rows = result.get(section)
        if not rows or not isinstance(rows, list):
            continue
        for row in rows:
            code = str(row.get("code", ""))
            e = enriched_by_code.get(code)
            if e:
                _add_cross_validation(row, e)
                _add_entry_exit(row, e)
            else:
                # Try to use row's own fields if no enriched object
                _add_cross_validation_from_row(row)
                _add_entry_exit_from_row(row)


def _add_cross_validation(row: dict, e) -> None:
    """Flag suspicious patterns: volume up but capital out, or capital in but volume low."""
    vol_ratio = getattr(e, "volume_ratio", 0) or 0
    main_net = getattr(e, "main_net", 0) or 0
    price = getattr(e, "price", 0) or 0
    chg = getattr(e, "change", 0) or 0

    cfg = REALTIME_CONFIG["cross_validation"]
    warns = []

    # 量比高但主力净流出 → 疑似诱多
    if vol_ratio > float(cfg["volume_ratio_high_min_exclusive"]) and main_net < 0:
        warns.append("疑似诱多：放量但主力净流出")

    # 主力净流入但量比低 → 疑似拆单进场
    if main_net > 0 and vol_ratio < float(cfg["volume_ratio_low_max_exclusive"]):
        warns.append("疑似拆单进场：主力流入但缩量")

    # 涨幅大但主力流出 → 警惕出货
    if chg > float(cfg["large_change_min_exclusive"]) and main_net < 0:
        warns.append("警惕出货：涨幅较大但主力净流出")

    # 冲高回落：现价离最高价很远
    high = getattr(e, "high", 0) or 0
    if high > 0 and price > 0:
        pullback_pct = round((high - price) / high * 100, 2)
        if pullback_pct > float(cfg["pullback_max_exclusive"]):
            warns.append(f"冲高回落：从最高价回落{pullback_pct}%")

    if warns:
        row["warn"] = "；".join(warns)
    else:
        row["warn"] = ""


def _add_cross_validation_from_row(row: dict) -> None:
    """Fallback: use row fields directly when no Enriched object available."""
    vol_ratio = row.get("vol_ratio") or row.get("volume_ratio") or 0
    main_net = row.get("main_net") or 0
    chg = row.get("chg") or row.get("change") or 0

    cfg = REALTIME_CONFIG["cross_validation"]
    warns = []
    if vol_ratio > float(cfg["volume_ratio_high_min_exclusive"]) and main_net < 0:
        warns.append("疑似诱多：放量但主力净流出")
    if main_net > 0 and vol_ratio < float(cfg["volume_ratio_low_max_exclusive"]):
        warns.append("疑似拆单进场：主力流入但缩量")
    if chg > float(cfg["large_change_min_exclusive"]) and main_net < 0:
        warns.append("警惕出货：涨幅较大但主力净流出")

    row["warn"] = "；".join(warns) if warns else ""


def _add_entry_exit(row: dict, e) -> None:
    """Add framework-aligned reference levels, estimated from the current price."""
    price = getattr(e, "price", 0) or 0

    if price <= 0:
        return

    entry_exit_cfg = REALTIME_CONFIG["entry_exit"]
    stop_loss = round(price * (1 - float(entry_exit_cfg["stop_loss_pct"]) / 100), 2)
    take_profit = round(price * (1 + float(entry_exit_cfg["take_profit_pct"]) / 100), 2)
    risk = price - stop_loss
    reward = take_profit - price
    rr_ratio = round(reward / risk, 2) if risk > 0 else None

    row["stop_loss"] = round(stop_loss, 2)
    row["stop_loss_pct"] = round((price - stop_loss) / price * 100, 2)
    row["take_profit_1"] = take_profit
    row["take_profit_2"] = None
    row["rr_ratio"] = rr_ratio
    row["entry_reference_price"] = round(price, 2)
    row["exit_reference_basis"] = "按现价估算；实际计划按买点价重算"


def _add_entry_exit_from_row(row: dict) -> None:
    """Fallback: add the same framework-aligned current-price reference."""
    price = row.get("price", 0) or 0

    if price <= 0:
        return

    entry_exit_cfg = REALTIME_CONFIG["entry_exit"]
    stop_loss = round(price * (1 - float(entry_exit_cfg["stop_loss_pct"]) / 100), 2)
    take_profit = round(price * (1 + float(entry_exit_cfg["take_profit_pct"]) / 100), 2)
    row["stop_loss"] = stop_loss
    row["stop_loss_pct"] = round((price - stop_loss) / price * 100, 2)
    row["take_profit_1"] = take_profit
    row["take_profit_2"] = None
    risk = price - stop_loss
    reward = take_profit - price
    row["rr_ratio"] = round(reward / risk, 2) if risk > 0 else None
    row["entry_reference_price"] = round(price, 2)
    row["exit_reference_basis"] = "按现价估算；实际计划按买点价重算"


# ── 5分钟量能模块（1分钟K滚动合成）────────────────────────────
#
# 数据源：东财 trends2 接口，一次请求返回全天 1 分钟K（时间,开,收,高,低,量[手],额[元],全天均价）。
# 已验证（2026-07-27 600584）：
#   - 09:25 竞价不单独成根（并入 09:30），午休无空根；但仍做防御性过滤。
#   - VWAP = Σ额 ÷ (Σ量×100)，反算全天 81.157 vs 接口均价线 81.156，单位换算正确。
# 约束：这是每股一次的真实新增请求 → 只覆盖交集/超短/低吸A/B，去重，
#       缓存60秒，单轮上限20只，失败保留旧缓存并暴露数据年龄。

MIN5_CACHE_TTL = 60          # 秒。分钟线缓存
MIN5_STALE_LIMIT = 180       # 秒。超过则标记失效
MIN5_MAX_PER_ROUND = 20      # 单轮最多请求股票数
MIN5_WORKERS = 4
STICKY_TTL = 900             # 秒。候选黏性：进入过超短池/自选的股票，退出后继续跟踪15分钟

_min5_cache: Dict[str, Dict[str, Any]] = {}   # code -> {"time": ts, "data": {...}}
_min5_lock = threading.Lock()

# 候选黏性集合：code -> {"last_seen": ts, "source": "超短池"/"自选"/"关注", "info": {...行情快照}}
_sticky: Dict[str, Dict[str, Any]] = {}
# 人工关注代码（长期跟踪，直到手动移除；受 MIN5_MAX_PER_ROUND 上限约束）
_manual_focus: set = set()
_sticky_lock = threading.Lock()

# 进入候选池时快照的行情字段（用于退出后仍在跟踪期时展示现价/涨幅等）
_STICKY_INFO_FIELDS = (
    "code", "name", "price", "change", "open", "high", "low", "prev_close",
    "turnover", "amount", "volume_ratio", "industry",
)


def _update_sticky(result: dict, now: float) -> None:
    """把本轮进入超短池/自选的股票写入黏性集合（带行情快照），并清理过期项。"""
    with _sticky_lock:
        for key in ("strict_ultra", "watchlist"):
            src = "超短池" if key == "strict_ultra" else "自选"
            for r in result.get(key) or []:
                c = str(r.get("code", ""))
                if not c or c in _manual_focus:
                    continue
                _sticky[c] = {
                    "last_seen": now,
                    "source": src,
                    "info": {k: r.get(k) for k in _STICKY_INFO_FIELDS if k in r},
                }
        # 清理超过黏性期的自动项（人工关注不受影响）
        expired = [c for c, v in _sticky.items()
                   if c not in _manual_focus and now - v["last_seen"] > STICKY_TTL]
        for c in expired:
            del _sticky[c]


def add_manual_focus(code: str, name: str | None = None) -> None:
    """人工关注：长期纳入分钟线拉取范围，直到 remove_manual_focus。"""
    code = str(code)
    if not code:
        return
    with _sticky_lock:
        _manual_focus.add(code)
        if code not in _sticky:
            _sticky[code] = {
                "last_seen": time.time(),
                "source": "关注",
                "info": {"code": code, "name": name},
            }


def remove_manual_focus(code: str) -> None:
    code = str(code)
    with _sticky_lock:
        _manual_focus.discard(code)
        if _sticky.get(code, {}).get("source") == "关注":
            _sticky.pop(code, None)


def get_sticky_debug() -> dict:
    return {
        "sticky": {c: {"source": v["source"], "remaining": int(STICKY_TTL - (time.time() - v["last_seen"]))}
                   for c, v in _sticky.items()},
        "manual_focus": list(_manual_focus),
    }



def _is_valid_minute(hhmm: str) -> bool:
    """仅保留连续竞价时段：09:30–11:30、13:00–15:00（防御性过滤竞价/午休）。"""
    return ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00")


def _parse_minute_asof(value: Any) -> Optional[datetime]:
    """Parse a source bar timestamp, retaining its trading date and China timezone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _minute_snapshot_freshness(
    snapshot: Dict[str, Any], now: Optional[datetime] = None
) -> Tuple[Optional[float], bool]:
    """Use the source bar time, not the download time, to decide freshness."""
    moment = now or datetime.now(TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    moment = moment.astimezone(TZ)
    asof = _parse_minute_asof(snapshot.get("bar_end_asof") or snapshot.get("bar_end"))
    if asof is None:
        return None, False
    age = (moment - asof).total_seconds()
    fresh = asof.date() == moment.date() and 0 <= age <= MIN5_STALE_LIMIT
    return age, fresh


def _fetch_minute_trends(code: str) -> List[Tuple[str, float, float, float, float, float, float, str]]:
    """拉取当日1分钟K，保留行情日期与时间。"""
    params = {
        "secid": screen.secid_for(code),
        "fields1": "f1,f2,f3,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": 1,
        "iscr": 0,
    }
    data = screen.fetch_json(EM_TRENDS_URL, params, timeout=6)
    trends = ((data or {}).get("data") or {}).get("trends") or []
    bars = []
    for line in trends:
        p = line.split(",")
        if len(p) < 8:
            continue
        source_asof = _parse_minute_asof(p[0])
        if source_asof is None:
            continue
        hhmm = source_asof.strftime("%H:%M")
        if not _is_valid_minute(hhmm):
            continue
        try:
            bars.append((
                hhmm, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[6]),
                source_asof.isoformat(timespec="minutes"),
            ))
        except ValueError:
            continue
    return bars


def _agg_window(bars: list) -> Dict[str, Any] | None:
    """把若干根1分钟K聚合成一个窗口：OHLC/量/额/VWAP。量单位=手，VWAP=Σ额÷(Σ量×100)。"""
    if not bars:
        return None
    vol = sum(b[5] for b in bars)
    amt = sum(b[6] for b in bars)
    return {
        "open": bars[0][1],
        "close": bars[-1][2],
        "high": max(b[3] for b in bars),
        "low": min(b[4] for b in bars),
        "vol": vol,                                        # 手
        "amount": amt,                                     # 元
        "vwap": round(amt / (vol * 100), 3) if vol > 0 else None,
        "bars": len(bars),
        "start": bars[0][0],
        "end": bars[-1][0],
    }


def _build_min5_snapshot(code: str) -> Dict[str, Any] | None:
    """从1分钟K合成5分钟量能指标。

    closed_5m：只用已收完的1分钟K（丢弃最后一根未完成K），用于交易判定。
    live_5m：含最后一根未完成K，仅供看板提示。
    同一交易节内取窗，不跨午休拼接（bars 已过滤午休，最近5根若跨
    11:30→13:00 边界，因分钟序列不连续属于跨节；上午收完的根在下午
    开盘初期会被自然排除——通过检查窗口首尾是否同节）。
    """
    bars = _fetch_minute_trends(code)
    if len(bars) < 2:
        return None

    now = datetime.now(TZ)
    cur_hhmm = now.strftime("%H:%M")
    # 最后一根若是"当前分钟"则视为未完成
    live_bars = bars
    closed_bars = bars[:-1] if bars[-1][0] >= cur_hhmm else bars

    def _same_session(win: list) -> list:
        """窗口内只保留与最后一根同交易节的根（不跨午休）。"""
        if not win:
            return win
        last_pm = win[-1][0] >= "13:00"
        return [b for b in win if (b[0] >= "13:00") == last_pm]

    def _calc(src: list) -> Dict[str, Any] | None:
        if len(src) < 1:
            return None
        cur5 = _agg_window(_same_session(src[-5:]))
        prev5 = _agg_window(_same_session(src[-10:-5]))
        # 近30分钟平均5分钟量：取同节最近30根，按每5根一组
        recent30 = _same_session(src[-30:])
        avg5_vol = None
        if len(recent30) >= 5:
            total_vol = sum(b[5] for b in recent30)
            avg5_vol = total_vol / (len(recent30) / 5)
        out = {
            "cur": cur5,
            "prev_vol": prev5["vol"] if prev5 else None,
            "avg5_vol_30m": round(avg5_vol, 0) if avg5_vol else None,
        }
        if cur5 and avg5_vol and avg5_vol > 0:
            out["vol_ratio_5m"] = round(cur5["vol"] / avg5_vol, 2)
        else:
            out["vol_ratio_5m"] = None
        return out

    closed = _calc(closed_bars)
    live = _calc(live_bars)
    if not closed and not live:
        return None
    return {
        "closed_5m": closed,   # 交易判定用
        "live_5m": live,       # 仅看板提示
        "fetched_at": time.time(),
        "bar_end": closed_bars[-1][0] if closed_bars else None,
        "bar_end_asof": closed_bars[-1][7] if closed_bars and len(closed_bars[-1]) > 7 else None,
    }


def enrich_min5(result: dict) -> None:
    """给操作导向池注入5分钟量能，并维护「候选黏性」。

    拉取范围：交集→超短→低吸A→低吸B ∪ 黏性未过期股票 ∪ 人工关注，去重，单轮≤20只。
    黏性：进入过超短池/自选的股票，退出候选池后继续跟踪 STICKY_TTL(15分钟)，
          期间仍拉分钟线并可在前端「跟踪中」标签继续验证买墙后续。

    注入字段（挂在每行 row 上）：
      min5: 完整快照(closed_5m/live_5m/fetched_at/bar_end/age_seconds/stale)
      vol_ratio_5m / vwap_5m: 顶层便捷字段（closed口径），供前端列直接用
    另外写入 result["sticky_tracking"]：已退出但在黏性期/人工关注的股票列表。
    """
    now = time.time()
    # 0. 更新/清理黏性集合（基于本轮进入超短池/自选的股票）
    _update_sticky(result, now)

    # 1. 按优先级收集当前目标池代码（去重）
    ordered: List[str] = []
    seen: set = set()

    def _take(rows, pred=None):
        for r in rows or []:
            c = str(r.get("code", ""))
            if c and c not in seen and (pred is None or pred(r)):
                seen.add(c)
                ordered.append(c)

    _take(result.get("dual_pool_raw"))                                  # 交集
    _take(result.get("strict_ultra"))                                   # 超短
    _take(result.get("low_ultra"), lambda r: r.get("class") == "A")     # 低吸A
    _take(result.get("low_ultra"), lambda r: r.get("class") == "B")     # 低吸B

    target_set = set(ordered)

    # 1b. 黏性补足：当前目标池 ∪ 黏性未过期 ∪ 人工关注，去重截20
    with _sticky_lock:
        for c, v in _sticky.items():
            if now - v["last_seen"] < STICKY_TTL and c not in seen:
                seen.add(c)
                ordered.append(c)
        for c in _manual_focus:
            if c not in seen:
                seen.add(c)
                ordered.append(c)

    codes = ordered[:MIN5_MAX_PER_ROUND]
    if not codes:
        result["sticky_tracking"] = []
        result["min5_meta"] = {"requested": 0, "cached": 0, "covered": 0,
                               "limit": MIN5_MAX_PER_ROUND, "sticky_ttl": STICKY_TTL}
        return

    # 2. 缓存命中的直接用；未命中/过期的去拉
    to_fetch = []
    with _min5_lock:
        for c in codes:
            ent = _min5_cache.get(c)
            if not ent or now - ent["time"] > MIN5_CACHE_TTL:
                to_fetch.append(c)

    if to_fetch:
        import concurrent.futures as futures

        def _one(c):
            try:
                _rate_limiter.wait()
                snap = _build_min5_snapshot(c)
                if snap:
                    with _min5_lock:
                        _min5_cache[c] = {"time": snap["fetched_at"], "data": snap}
                _rate_limiter.on_success()
            except Exception:
                _rate_limiter.on_failure()   # 失败保留旧缓存

        with futures.ThreadPoolExecutor(max_workers=MIN5_WORKERS) as pool:
            list(pool.map(_one, to_fetch))

    # 3. 注入到所有含这些代码的池行
    with _min5_lock:
        snap_by_code = {c: _min5_cache[c] for c in codes if c in _min5_cache}

    now = time.time()
    for section in ("dual_pool", "dual_pool_raw", "strict_ultra", "low_ultra", "intersection_states"):
        for row in result.get(section) or []:
            c = str(row.get("code", ""))
            ent = snap_by_code.get(c)
            if not ent:
                continue
            snap = dict(ent["data"])
            bar_age, bar_fresh = _minute_snapshot_freshness(snap, datetime.fromtimestamp(now, TZ))
            snap["age_seconds"] = round(bar_age, 1) if bar_age is not None else None
            snap["stale"] = not bar_fresh
            row["min5"] = snap
            closed = snap.get("closed_5m") or {}
            row["vol_ratio_5m"] = closed.get("vol_ratio_5m")
            cur = closed.get("cur") or {}
            row["vwap_5m"] = cur.get("vwap")

    # 4. 构建 sticky_tracking：已退出候选池但仍值得跟踪的股票（供前端独立标签）
    tracking: List[dict] = []
    with _sticky_lock:
        for c, v in _sticky.items():
            if c in target_set:          # 仍在目标池，原池已显示，不重复
                continue
            ent = _min5_cache.get(c)
            if not ent:
                continue
            snap = dict(ent["data"])
            bar_age, bar_fresh = _minute_snapshot_freshness(snap, datetime.fromtimestamp(now, TZ))
            snap["age_seconds"] = round(bar_age, 1) if bar_age is not None else None
            snap["stale"] = not bar_fresh
            remaining = int(STICKY_TTL - (now - v["last_seen"]))
            info = v.get("info") or {}
            tracking.append({
                "code": c,
                "name": info.get("name"),
                "source": v.get("source"),
                "price": info.get("price"),
                "change": info.get("change"),
                "info": info or {"code": c},
                "min5": snap,
                "remaining": remaining,
            })
        # 人工关注但尚无黏性快照的（如未进过候选池），也补进跟踪
        for c in _manual_focus:
            if any(t["code"] == c for t in tracking):
                continue
            ent = _min5_cache.get(c)
            if not ent:
                continue
            snap = dict(ent["data"])
            bar_age, bar_fresh = _minute_snapshot_freshness(snap, datetime.fromtimestamp(now, TZ))
            snap["age_seconds"] = round(bar_age, 1) if bar_age is not None else None
            snap["stale"] = not bar_fresh
            mname = (_sticky.get(c) or {}).get("info", {}).get("name")
            tracking.append({
                "code": c,
                "name": mname,
                "source": "关注",
                "price": None,
                "change": None,
                "info": {"code": c, "name": mname},
                "min5": snap,
                "remaining": None,
            })

    result["sticky_tracking"] = tracking
    result["min5_meta"] = {
        "requested": len(to_fetch),
        "cached": len(codes) - len(to_fetch),
        "covered": len(snap_by_code),
        "limit": MIN5_MAX_PER_ROUND,
        "sticky_tracked": len(tracking),
        "sticky_ttl": STICKY_TTL,
    }


def build_minute_map(result: dict, prev_items: dict) -> Dict[str, Dict[str, Any]]:
    """需求10：状态机专用分钟线拉取（优先级 + 失败重试一次 + 降级用缓存并标记过期）。

    优先级：准交集候选 → 上一轮锁存/等待回踩/回踩就绪/买点 → 双池交集 → 低吸A类(前5)。
    每只股票记录 fetch_status/last_success_at/last_bar_at/age_seconds/error；
    拉取失败重试一次，仍失败时使用最后一次成功缓存，由 age 判定过期（不伪装新鲜）。
    返回 minute_map: code -> {status, age_seconds, last_bar_at, close_5m, vwap_5m,
    vol_5m, fetch_status, last_success_at, error}，供 evaluate_intersection_states 使用。
    """
    ordered: List[str] = []
    seen: set = set()

    def _take_codes(codes) -> None:
        for c in codes:
            c = str(c or "")
            if c and c not in seen:
                seen.add(c)
                ordered.append(c)

    latched_phases = {
        screen.PHASE_PRE, screen.PHASE_LATCHED, screen.PHASE_WAIT_RETEST,
        screen.PHASE_RETEST_READY, screen.PHASE_ENTRY,
    }
    _take_codes(r.get("code") for r in result.get("pre_intersection") or [])
    _take_codes(
        c for c, it in (prev_items or {}).items()
        if screen._canonical_phase(it.get("phase")) in latched_phases
    )
    _take_codes(r.get("code") for r in result.get("dual_pool_raw") or [])
    _take_codes(
        r.get("code")
        for r in (result.get("low_ultra") or [])[:20]
        if r.get("class") == "A"
    )
    codes = ordered[:MIN5_MAX_PER_ROUND]
    if not codes:
        return {}

    now = time.time()
    errors: Dict[str, str] = {}
    to_fetch: List[str] = []
    with _min5_lock:
        for c in codes:
            ent = _min5_cache.get(c)
            if not ent or now - ent["time"] > MIN5_CACHE_TTL:
                to_fetch.append(c)

    if to_fetch:
        import concurrent.futures as futures

        def _one(c: str) -> None:
            for _attempt in range(2):        # 失败重试一次
                try:
                    _rate_limiter.wait()
                    snap = _build_min5_snapshot(c)
                    if snap:
                        with _min5_lock:
                            _min5_cache[c] = {"time": snap["fetched_at"], "data": snap}
                        _rate_limiter.on_success()
                        errors.pop(c, None)
                        return
                    errors[c] = "分钟线返回空数据"
                except Exception as exc:
                    errors[c] = str(exc) or exc.__class__.__name__
                    _rate_limiter.on_failure()
            # 两次均失败：保留最后成功缓存（若有），由 age 判定过期

        with futures.ThreadPoolExecutor(max_workers=MIN5_WORKERS) as pool:
            list(pool.map(_one, to_fetch))

    minute_map: Dict[str, Dict[str, Any]] = {}
    now = time.time()
    with _min5_lock:
        for c in codes:
            ent = _min5_cache.get(c)
            if not ent:
                minute_map[c] = {
                    "status": "fetch_failed",
                    "age_seconds": None,
                    "last_bar_at": None,
                    "close_5m": None,
                    "vwap_5m": None,
                    "vol_5m": None,
                    "fetch_status": "failed",
                    "last_success_at": None,
                    "error": errors.get(c, "无缓存且拉取失败"),
                }
                continue
            snap = ent["data"]
            age, fresh = _minute_snapshot_freshness(
                snap, datetime.fromtimestamp(now, TZ)
            )
            cur = ((snap.get("closed_5m") or {}).get("cur")) or {}
            minute_map[c] = {
                "status": "fresh" if fresh else "stale",
                "age_seconds": round(age, 1) if age is not None else None,
                "last_bar_at": snap.get("bar_end_asof") or snap.get("bar_end"),
                "close_5m": cur.get("close"),
                "vwap_5m": cur.get("vwap"),
                "vol_5m": cur.get("vol"),
                "fetch_status": "cached_after_fail" if c in errors else "ok",
                "last_success_at": datetime.fromtimestamp(ent["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                "error": errors.get(c),
            }
    return minute_map


def build_negative_super_payload(
    enriched: List[Any],
    stats: Dict[str, Any],
    flow_history: Any,
    ts: Any,
    status: str,
    view: str,
) -> Tuple[int, List[Dict[str, Any]]]:
    """负超单观察的产出策略：严格模式只报数量，开启观察才构造行。

    观察行会进入公告查询列表（每只一次请求）。严格模式下列表是隐藏的，因此只
    统计数量，不把隐藏代码带进公告检查，避免白跑接口。
    """
    if status != "ok":
        return 0, []
    count = screen.count_negative_super_observations(enriched)
    rows = (
        screen.build_negative_super_observations(enriched, stats, flow_history, ts)
        if view == dashboard_settings.VIEW_OBSERVE
        else []
    )
    return count, rows


def run_screening(
    modes: Set[str] | None = None,
    workers: int = 6,
    top: int = 15,
    skip_announcements: bool = False,
    skip_capital_ranking: bool = False,
    network_mode: str = "auto",
    announcement_page_size: int = 8,
    settings_snapshot: Dict[str, Any] | None = None,
    state_commit: "state_commit_mod.RoundCommit | None" = None,
) -> Dict[str, Any]:
    """Run a single screening pass and return the result dict.

    核心规则计算全部委托给 ``screen.run_screening_core``（与 CLI 同一条生产链）；
    本函数只负责看板专有的依赖注入与附加展示字段：缓存版 K 线取数、负超单观察、
    状态机分钟线、市场环境分级、大盘温度计、资金交叉验证/进出场建议、5 分钟量能。

    ``settings_snapshot``（配置版本 + 观察模式 + 交易板范围）在一轮开始时固定，
    运行途中修改只作用于下一轮。

    ``state_commit``：看板/工作台传入自己的回合提交门，由调度器在确认本轮成功后
    统一提交、超时/失败则中止。为空时本函数自建并在返回前提交（命令行/单测行为）。
    """
    global _kline_fetch_count, _kline_cache_hit_count, _kline_fail_count
    _kline_fetch_count = 0
    _kline_cache_hit_count = 0
    _kline_fail_count = 0

    if modes is None:
        modes = {"strict"}
    if "all" in modes:
        modes = {"strict", "low", "watchlist"}

    snapshot = settings_snapshot or {}
    enabled_boards = screen.normalize_boards(snapshot.get("enabled_boards"))
    negative_super_view = str(snapshot.get("negative_super_view") or "strict")

    own_commit = state_commit is None
    commit = state_commit if state_commit is not None else state_commit_mod.RoundCommit("engine-standalone")

    t0 = time.time()
    captured: Dict[str, Any] = {}

    def _capture_enriched(enriched_by_code: Dict[str, Any]) -> None:
        captured["enriched_by_code"] = enriched_by_code

    def _build_extras(**kw: Any) -> Dict[str, Any]:
        """公告核验前并入负超单观察：与生产否决同源（super_net<0），独立成表。"""
        fallback = bool(kw["fallback_snapshot"])
        fetch_status = kw["market_fetch_status"] or {}
        if fallback:
            status = "degraded"
        elif fetch_status.get("complete") is False:
            status = "incomplete"
        else:
            status = "ok"
        count, rows = build_negative_super_payload(
            kw["enriched"], kw["stats"], kw["flow_history"], kw["ts"], status, negative_super_view
        )
        return {
            "negative_super_observations": rows,
            "negative_super_status": status,
            "negative_super_count": count,
        }

    def _minute_map_provider(result: Dict[str, Any], previous_items: Dict[str, Any]) -> Dict[str, Any]:
        return build_minute_map(result, previous_items)

    params = screen.ScreeningParams(
        modes=frozenset(modes),
        workers=workers,
        top=top,
        skip_announcements=skip_announcements,
        skip_capital_ranking=skip_capital_ranking,
        network_mode=network_mode,
        announcement_page_size=announcement_page_size,
        enabled_boards=tuple(enabled_boards),
    )
    hooks = screen.ScreeningHooks(
        state_commit=commit,
        fetch_kline_fn=_cached_fetch_kline,
        source_suffix=" (公告已跳过)" if skip_announcements else "",
        extra_meta=lambda: {
            # 本轮实际执行口径：公告检查是否被跳过。页面据此判断"这份快照能不能作为
            # 真实仓依据"，与当前设置无关。
            "announcement_check_skipped": bool(skip_announcements),
            "kline_cache_stats": get_cache_stats(),
            # 本轮开始时固定的配置快照：配置版本 + 观察模式。恢复旧快照时保留它
            # 原来的版本，不按当前开关重解释。
            "config_revision": snapshot.get("revision"),
            "negative_super_view": negative_super_view,
        },
        build_extras=_build_extras,
        save_kline_cache=lambda: _save_kline_cache(commit),
        minute_map_provider=_minute_map_provider,
        on_enriched=_capture_enriched,
    )

    try:
        result = screen.run_screening_core(params, hooks)
    except screen.NetworkUnavailable as exc:
        if own_commit:
            commit.abort()
        return {"error": screen.format_network_failure(exc)}

    enriched_by_code = captured.get("enriched_by_code") or {}

    # ── 1. 大盘温度计 ──────────────────────────────────────
    result["market_thermometer"] = _build_market_thermometer(
        result.get("breadth") or {}, result.get("indices") or []
    )

    # ── 2. 资金交叉验证 + 3. 进出场建议 ────────────────────
    _enrich_result_rows(result, enriched_by_code)

    # ── 4. 5分钟量能（交集/超短/低吸A/B，≤20只，60s缓存）────
    if not (result.get("meta") or {}).get("market_data_degraded"):
        try:
            enrich_min5(result)
        except Exception as exc:  # 量能失败不影响主流程
            result.setdefault("warnings", []).append(f"5分钟量能获取失败：{exc}")

    # K 线缓存最后再存一次（暂存则由提交门在确认本轮成功后落盘）
    _save_kline_cache(commit)

    # 每行标注交易板。核心已标注过；这里覆盖 min5/交叉验证新增或替换的行。
    screen.stamp_board_fields(result)

    if own_commit:
        # 独立调用（命令行/单测）：本轮到此确认成功，统一提交暂存状态。
        # 落盘失败必须可诊断，但不能把一轮有效筛选结果整个丢掉。
        try:
            commit.commit()
        except Exception as exc:  # noqa: BLE001
            result.setdefault("warnings", []).append(f"运行状态落盘失败：{exc}")
            print(f"[engine] state commit failed: {exc}", file=sys.stderr)

    result["meta"]["elapsed_seconds"] = round(time.time() - t0, 1)
    return screen._sanitize_for_json(result)
