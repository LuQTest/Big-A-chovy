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

KLINE_CACHE_FILE = SCRIPT_DIR / ".kline_cache.json"
KLINE_CACHE_TTL = 1800  # 30 min — MAs are slow-moving, don't need tick-level freshness

_kline_cache: Dict[str, Any] = {}
_kline_cache_time: float = 0
_kline_cache_date: str = ""
_kline_fetch_count = 0
_kline_cache_hit_count = 0
_kline_fail_count = 0


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
    global _kline_cache, _kline_cache_time, _kline_cache_date
    try:
        if KLINE_CACHE_FILE.exists():
            data = json.loads(KLINE_CACHE_FILE.read_text(encoding="utf-8"))
            today = datetime.now().strftime("%Y-%m-%d")
            if data.get("date") == today:
                _kline_cache = data.get("data", {})
                # Reset TTL clock on load — K-line is daily data,
                # same-day cache is always valid regardless of when it was saved
                _kline_cache_time = time.time()
                _kline_cache_date = data.get("date", "")
                print(f"[kline-cache] loaded {len(_kline_cache)} entries from {today}", file=sys.stderr)
    except Exception:
        pass


def _save_kline_cache() -> None:
    global _kline_cache_time, _kline_cache_date
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        ts = time.time()
        _kline_cache_time = ts
        _kline_cache_date = today
        data = {"date": today, "timestamp": ts, "data": _kline_cache}
        KLINE_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _is_cache_valid() -> bool:
    if not _kline_cache or not _kline_cache_time:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    if _kline_cache_date != today:
        return False
    if time.time() - _kline_cache_time > KLINE_CACHE_TTL:
        return False
    return True


def _invalidate_kline_cache() -> None:
    global _kline_cache, _kline_cache_time, _kline_cache_date
    _kline_cache = {}
    _kline_cache_time = 0
    _kline_cache_date = ""


# ── Monkey-patch fetch_kline with cache + adaptive limiting ──

_original_fetch_kline = screen.fetch_kline


def _cached_fetch_kline(code: str, *args, **kwargs):
    """Cached + adaptively rate-limited version of fetch_kline."""
    global _kline_fetch_count, _kline_cache_hit_count, _kline_fail_count

    # 1. Cache hit — instant, no delay
    if code in _kline_cache and _is_cache_valid():
        _kline_cache_hit_count += 1
        return _kline_cache[code]

    # 2. Cache miss — fetch with adaptive rate limiting
    _kline_fetch_count += 1
    _rate_limiter.wait()

    try:
        result = _original_fetch_kline(code, *args, **kwargs)
        if result:
            _kline_cache[code] = result
        _rate_limiter.on_success()
        return result
    except Exception as e:
        _kline_fail_count += 1
        _rate_limiter.on_failure()
        raise


screen.fetch_kline = _cached_fetch_kline
_load_kline_cache()


# ── Public API ───────────────────────────────────────

def get_cache_stats() -> Dict[str, Any]:
    return {
        "cache_size": len(_kline_cache),
        "cache_valid": _is_cache_valid(),
        "fetch_count": _kline_fetch_count,
        "cache_hit_count": _kline_cache_hit_count,
        "fail_count": _kline_fail_count,
        "rate_limit_delay": round(_rate_limiter._delay, 2),
        "cache_date": _kline_cache_date,
    }


def prewarm_kline_cache(workers: int = 6, progress_callback=None) -> Dict[str, Any]:
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

    prefetch = screen.filter_prefetch(market, ["all"])
    codes_to_fetch = [
        r["f12"] for r in prefetch
        if str(r.get("f12", "")) not in _kline_cache or not _is_cache_valid()
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
        chg = x.get("f3")
        if isinstance(chg, (int, float)):
            if chg > 0:
                idx_up += 1
            elif chg < 0:
                idx_down += 1

    # Risk assessment (initial conservative thresholds — user can adjust)
    #   danger:  limit_up < 5  OR  adv_dec_ratio < 0.5
    #   caution: limit_up < 15 OR  adv_dec_ratio < 0.8
    #   strong:  limit_up >= 30 AND adv_dec_ratio >= 1.5
    #   normal:  everything else
    if limit_up < 5 or (adv_dec_ratio < 0.5 and total_valid > 100):
        risk_level = "danger"
        risk_msg = "市场弱势，涨停稀少且跌多涨少，建议观望"
    elif limit_up < 15 or adv_dec_ratio < 0.8:
        risk_level = "caution"
        risk_msg = "市场偏弱，注意控制仓位和止损"
    elif limit_up >= 30 and adv_dec_ratio >= 1.5:
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

    warns = []

    # 量比高但主力净流出 → 疑似诱多
    if vol_ratio > 1.2 and main_net < 0:
        warns.append("疑似诱多：放量但主力净流出")

    # 主力净流入但量比低 → 疑似拆单进场
    if main_net > 0 and vol_ratio < 1.0:
        warns.append("疑似拆单进场：主力流入但缩量")

    # 涨幅大但主力流出 → 警惕出货
    if chg > 4 and main_net < 0:
        warns.append("警惕出货：涨幅较大但主力净流出")

    # 冲高回落：现价离最高价很远
    high = getattr(e, "high", 0) or 0
    if high > 0 and price > 0:
        pullback_pct = round((high - price) / high * 100, 2)
        if pullback_pct > 3:
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

    warns = []
    if vol_ratio > 1.2 and main_net < 0:
        warns.append("疑似诱多：放量但主力净流出")
    if main_net > 0 and vol_ratio < 1.0:
        warns.append("疑似拆单进场：主力流入但缩量")
    if chg > 4 and main_net < 0:
        warns.append("警惕出货：涨幅较大但主力净流出")

    row["warn"] = "；".join(warns) if warns else ""


def _add_entry_exit(row: dict, e) -> None:
    """Calculate suggested stop-loss and take-profit levels from MA/recent low."""
    price = getattr(e, "price", 0) or 0
    ma5 = getattr(e, "ma5", 0) or 0
    low = getattr(e, "low", 0) or 0
    prev_low = getattr(e, "prior_low", 0) or 0

    if price <= 0:
        return

    # Stop loss: below MA5 or today's low, whichever is tighter
    stop_candidates = [x for x in [ma5, low, prev_low] if x > 0]
    if not stop_candidates:
        return
    stop_loss = min(stop_candidates)

    # Take profit: +3% and +5% from current price
    tp1 = round(price * 1.03, 2)
    tp2 = round(price * 1.05, 2)

    # Risk-reward ratio
    risk = price - stop_loss
    reward = tp2 - price
    rr_ratio = round(reward / risk, 2) if risk > 0 else None

    row["stop_loss"] = round(stop_loss, 2)
    row["stop_loss_pct"] = round((price - stop_loss) / price * 100, 2)
    row["take_profit_1"] = tp1
    row["take_profit_2"] = tp2
    row["rr_ratio"] = rr_ratio


def _add_entry_exit_from_row(row: dict) -> None:
    """Fallback: use row fields directly."""
    price = row.get("price", 0) or 0
    ma5 = row.get("ma5", 0) or 0
    low = row.get("low", 0) or 0

    if price <= 0:
        return

    stop_candidates = [x for x in [ma5, low] if x > 0]
    if not stop_candidates:
        return
    stop_loss = min(stop_candidates)

    row["stop_loss"] = round(stop_loss, 2)
    row["stop_loss_pct"] = round((price - stop_loss) / price * 100, 2)
    row["take_profit_1"] = round(price * 1.03, 2)
    row["take_profit_2"] = round(price * 1.05, 2)
    risk = price - stop_loss
    reward = price * 1.05 - price
    row["rr_ratio"] = round(reward / risk, 2) if risk > 0 else None


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


def _fetch_minute_trends(code: str) -> List[Tuple[str, float, float, float, float, float, float]]:
    """拉取当日1分钟K。返回 [(hhmm, open, close, high, low, vol_hand, amount_yuan), ...]"""
    url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    params = {
        "secid": screen.secid_for(code),
        "fields1": "f1,f2,f3,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": 1,
        "iscr": 0,
    }
    data = screen.fetch_json(url, params, timeout=6)
    trends = ((data or {}).get("data") or {}).get("trends") or []
    bars = []
    for line in trends:
        p = line.split(",")
        if len(p) < 8:
            continue
        hhmm = p[0][-5:]
        if not _is_valid_minute(hhmm):
            continue
        try:
            bars.append((hhmm, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[6])))
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

    now = datetime.now()
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
            age = int(now - ent["time"])
            snap = dict(ent["data"])
            snap["age_seconds"] = age
            snap["stale"] = age > MIN5_STALE_LIMIT
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
            age = int(now - ent["time"])
            snap = dict(ent["data"])
            snap["age_seconds"] = age
            snap["stale"] = age > MIN5_STALE_LIMIT
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
            age = int(now - ent["time"])
            snap = dict(ent["data"])
            snap["age_seconds"] = age
            snap["stale"] = age > MIN5_STALE_LIMIT
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
            age = now - ent["time"]
            snap = ent["data"]
            cur = ((snap.get("closed_5m") or {}).get("cur")) or {}
            minute_map[c] = {
                "status": "stale" if age > MIN5_STALE_LIMIT else "fresh",
                "age_seconds": round(age, 1),
                "last_bar_at": snap.get("bar_end"),
                "close_5m": cur.get("close"),
                "vwap_5m": cur.get("vwap"),
                "vol_5m": cur.get("vol"),
                "fetch_status": "cached_after_fail" if c in errors else "ok",
                "last_success_at": datetime.fromtimestamp(ent["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                "error": errors.get(c),
            }
    return minute_map


def run_screening(
    modes: Set[str] | None = None,
    workers: int = 6,
    top: int = 15,
    skip_announcements: bool = False,
    skip_capital_ranking: bool = False,
    network_mode: str = "auto",
    announcement_page_size: int = 8,
) -> Dict[str, Any]:
    """Run a single screening pass and return the result dict.

    Mirrors a_share_daily_screen.main() but returns data instead of printing.
    """
    global _kline_fetch_count, _kline_cache_hit_count, _kline_fail_count
    _kline_fetch_count = 0
    _kline_cache_hit_count = 0
    _kline_fail_count = 0

    if modes is None:
        modes = {"strict"}
    if "all" in modes:
        modes = {"strict", "low", "watchlist"}

    screen.set_network_mode(network_mode)
    screen.MARKET_WARNINGS.clear()

    t0 = time.time()

    try:
        market, total = screen.fetch_market()
    except screen.NetworkUnavailable as exc:
        return {"error": screen.format_network_failure(exc)}

    market_fetch_status = screen.get_market_fetch_status()
    fallback_snapshot = market_fetch_status.get("source") == "sina_fallback"

    if fallback_snapshot:
        indices_raw: list = []
        sector_boards: list = []
        screen.MARKET_WARNINGS.append(
            "备用行情模式跳过东方财富指数和板块接口，避免主源故障导致二次等待"
        )
    else:
        indices_raw = screen.fetch_indices()
        sector_boards = screen.fetch_sector_indices()

    if modes == {"strict", "low", "watchlist"}:
        mode_list = ["all"]
    else:
        mode_list = list(modes)

    prefetch = screen.filter_prefetch(market, mode_list)
    enriched, errors = screen.enrich_all(prefetch, max(1, workers))

    _save_kline_cache()

    if fallback_snapshot:
        screen.MARKET_WARNINGS.append(
            "备用行情缺少兼容量比、主力资金和行业字段：趋势池仅供观察；"
            "超短池、双池交集、资金优选和低吸买点不会输出。"
        )

    breadth = screen.market_summary(market, total, market_fetch_status)
    stats = screen.sector_stats(market, breadth)

    flow_history = screen.load_flow_history()
    has_snapshot = bool(flow_history)
    screen.apply_flow_increments(enriched, flow_history)
    for e in enriched:
        e.flow_status = screen.classify_flow(e, stats, has_snapshot)
    screen.save_flow_history(enriched, flow_history)

    latest_ts = max([r.get("f124") or 0 for r in market] or [0])
    ts = datetime.fromtimestamp(latest_ts, screen.TZ) if latest_ts else datetime.now(screen.TZ)
    after_1420 = ts.hour > 14 or (ts.hour == 14 and ts.minute >= 20)

    strict_ultra_all = (
        []
        if fallback_snapshot
        else sorted([e for e in enriched if screen.strict_ultra(e)], key=lambda e: e.change, reverse=True)
    )
    strict_ultra_items = strict_ultra_all[:top]
    trend_observation_items = sorted(
        [e for e in enriched if screen.trend_observation(e)],
        key=lambda e: (not screen.strict_trend(e), -e.change, e.dist60),
    )[:top]
    strict_trend_items = sorted(
        [e for e in enriched if screen.strict_trend(e)], key=lambda e: e.change, reverse=True
    )[:top]

    class_order = {"A": 0, "B": 1, "C": 2}
    low_ultra_rows: list = []
    low_trend_rows: list = []
    if "low" in modes and not fallback_snapshot:
        for e in enriched:
            cls, tags, score = screen.low_ultra_class(e, stats, after_1420)
            if cls != "C" or (e.change >= 2.2 and (e.turnover > 10 or e.change > 5.2 or e.volume_ratio > 6)):
                low_ultra_rows.append(
                    {**asdict(e), "class": cls, "risk": "/".join(tags) if tags else "无", "score": score, "resonance": "是" if screen.has_resonance(e, stats) else "否"}
                )
            cls2, tags2, score2 = screen.low_trend_class(e, stats, after_1420)
            if cls2 != "C" or (e.change >= 2.5 and (e.change > 6 or e.turnover > 9 or e.ma20_dist > 0.15)):
                low_trend_rows.append(
                    {**asdict(e), "class": cls2, "risk": "/".join(tags2) if tags2 else "无", "score": score2, "ma_state": f"MA5/10/20上方,5日{'上行' if e.ma5 > e.prev_ma5 else '未上行'},10日{'走平上行' if e.ma10 >= e.prev_ma10 else '下行'}"}
                )
        low_ultra_rows = sorted(low_ultra_rows, key=lambda r: (class_order[r["class"]], -r["score"], abs(r["change"] - 3.4)))[: max(top, 15)]
        low_trend_rows = sorted(low_trend_rows, key=lambda r: (class_order[r["class"]], -r["score"], abs(r["change"] - 3.8)))[: max(top, 15)]

    watchlist = screen.build_watchlist(enriched, stats)[:top] if "watchlist" in modes else []

    strict_ultra_rows = [
        {**asdict(e), "resonance": "是" if screen.has_resonance(e, stats) else "否"}
        for e in strict_ultra_items
    ] if "strict" in modes else []
    trend_observation_rows = [
        {**asdict(e), "ma_state": "MA5/10/20上方" if e.price > e.ma5 and e.price > e.ma10 and e.price > e.ma20 else "MA20上方，短均线修复中"}
        for e in trend_observation_items
    ] if "strict" in modes else []
    strict_trend_rows = [asdict(e) for e in strict_trend_items] if "strict" in modes else []
    trend_diagnostics = [screen.trend_condition_diagnosis(e, stats) for e in strict_ultra_all] if "strict" in modes else []

    ultra_codes = {r["code"] for r in strict_ultra_rows}
    observation_codes = {r["code"] for r in trend_observation_rows}
    confirmation_codes = {r["code"] for r in strict_trend_rows}
    # 交集状态机基准：超短池 ∩ 趋势确认(质量条件，不含价格区间/流通市值)
    relaxed_confirm_codes = {e.code for e in enriched if screen.trend_confirm_relaxed(e)}
    enriched_by_code_for_pool = {e.code: e for e in enriched}
    # 双池交集基准：超短池 ∩ 趋势确认(质量条件)。交集即"启动事件"，
    # 真正买点由交集后的缩量回踩产生（见 evaluate_intersection_states 四阶段状态机）。
    board_by_code_for_pool = {}
    for b in sector_boards:
        if b.get("name"):
            board_by_code_for_pool[b["name"]] = b
    dual_pool_rows = [
        {
            **r,
            "resonance": "是" if screen.has_resonance(enriched_by_code_for_pool[r["code"]], stats) else "否",
            "sector_change": (board_by_code_for_pool.get(r.get("industry"), {}) or {}).get("change"),
        }
        for r in strict_ultra_rows
        if r["code"] in relaxed_confirm_codes
    ]
    dual_pool_raw_rows = [dict(r) for r in dual_pool_rows]
    strict_candidate_codes = {r["code"] for r in strict_ultra_rows + trend_observation_rows}
    capital_rank = (
        []
        if skip_capital_ranking or fallback_snapshot
        else screen.rank_capital_candidates(
            [e for e in enriched if e.code in strict_candidate_codes], stats
        )[:top]
    )
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
            sector_indices.append(
                {
                    "name": board["name"],
                    "change": board["change"],
                    "price": board["price"],
                    "up_count": board["up_count"],
                    "down_count": board["down_count"],
                    "turnover": board["turnover"],
                    "source": "筛选",
                }
            )

    intersection_config, intersection_config_meta = screen.resolve_intersection_config(
        screen.load_intersection_calibration()
    )
    intersection_runtime_config = {
        **intersection_config,
        "version": intersection_config_meta["version"],
        "source": intersection_config_meta["source"],
    }

    # 准交集候选：超短池 + 距趋势确认仅差1项 + 四道门槛（相位由状态机判定）
    diag_by_code = {d["code"]: d for d in trend_diagnostics} if "strict" in modes else {}
    pre_intersection_rows = (
        screen.compute_pre_intersection(
            strict_ultra_rows, relaxed_confirm_codes, diag_by_code, intersection_runtime_config
        )
        if "strict" in modes
        else []
    )

    result = {
        "meta": {
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "status": screen.current_status(ts),
            "source": (
                "新浪财经实时备用快照（字段降级）"
                if fallback_snapshot
                else "东方财富push2实时/快照"
            )
            + " + 腾讯/东方财富日K"
            + ("" if not skip_announcements else " (公告已跳过)"),
            "total_rows": len(market),
            "provider_total": total,
            "market_fetch_complete": market_fetch_status.get("complete"),
            "prefetch_rows": len(prefetch),
            "enriched_rows": len(enriched),
            "elapsed_seconds": round(time.time() - t0, 1),
            "market_data_degraded": fallback_snapshot,
            "kline_cache_stats": get_cache_stats(),
        },
        "breadth": breadth,
        "market_fetch_status": market_fetch_status,
        "indices": [
            {"code": x.get("f12"), "name": x.get("f14"), "price": x.get("f2"), "change": x.get("f3")}
            for x in indices_raw
        ],
        "errors": errors,
        "warnings": list(screen.MARKET_WARNINGS),
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
        "capital_rank": capital_rank,
        "low_ultra": low_ultra_rows if "low" in modes and not fallback_snapshot else [],
        "low_trend": low_trend_rows if "low" in modes and not fallback_snapshot else [],
        "watchlist": watchlist,
        "sector_indices": sector_indices,
        "has_snapshot": has_snapshot,
        # 低开洗盘模块：低开≥2% + 翻红 + 站上均价线 + 当日主力净流入 + 20日持续净流入
        "low_open_wash": screen.low_open_wash_rows(enriched, flow_history),
    }

    detail_codes: set = set()
    flow_detail: list = []
    for e in strict_ultra_items[:5] + trend_observation_items[:5] + strict_trend_items[:5]:
        if e.code not in detail_codes:
            detail_codes.add(e.code)
            flow_detail.append(asdict(e))
    for r in low_ultra_rows if "low" in modes else []:
        if r.get("class") == "A" and r["code"] not in detail_codes:
            detail_codes.add(r["code"])
            flow_detail.append(r)
            if len([d for d in flow_detail if d.get("class") == "A"]) >= 5:
                break
    holdings_file = screen.SCRIPT_DIR / "holdings.json"
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

    if not skip_announcements:
        def _noop_progress(done, total, code, status, source):
            pass

        result["announcement_errors"] = screen.attach_announcement_risks(
            result, announcement_page_size, workers, progress_callback=_noop_progress
        )
        screen.apply_announcement_pool_gates(result)
    else:
        result["announcement_check_available"] = False
        result["announcement_unknown_codes"] = sorted(
            {
                str(row.get("code"))
                for section in (
                    "strict_ultra", "trend_observation", "strict_trend", "dual_pool", "dual_pool_raw",
                    "capital_rank", "trend_diagnostics", "low_ultra", "low_trend", "watchlist",
                )
                for row in (result.get(section) or [])
                if screen._row_risk_status(row) == "unknown"
            }
        )
        screen.apply_announcement_pool_gates(result)

    if result.get("strict_enabled"):
        state_payload = screen.load_intersection_state()
        previous_items = state_payload.get("items") or {}
        if state_payload.get("date") != ts.strftime("%Y-%m-%d"):
            previous_items = {}
        # 需求10：状态机前按优先级拉取分钟线（准交集→锁存/等待回踩→双池交集→低吸A）
        minute_map: Dict[str, Dict[str, Any]] = {}
        if not fallback_snapshot:
            try:
                minute_map = build_minute_map(result, previous_items)
            except Exception as exc:
                result.setdefault("warnings", []).append(f"状态机分钟线拉取失败：{exc}")
        # 需求8：市场环境分级（宽度/指数极端），CASH 一律禁止新开仓
        market_context = screen.resolve_market_mode(
            breadth, result.get("indices") or [], intersection_runtime_config
        )
        result["market_context"] = market_context
        state_rows, next_items = screen.evaluate_intersection_states(
            result.get("dual_pool_raw") or [],
            result.get("pre_intersection") or [],
            previous_items,
            ts,
            intersection_runtime_config,
            snapshot_id=ts.strftime("%Y-%m-%d %H:%M:%S"),
            risk_map=result.get("announcement_risk_map") or {},
            minute_map=minute_map,
            market_context=market_context,
        )
        result["intersection_states"] = state_rows
        result["minute_fetch_log"] = minute_map
        screen.save_intersection_state(next_items, ts.strftime("%Y-%m-%d"))

    # ── 1. 大盘温度计 ──────────────────────────────────────
    result["market_thermometer"] = _build_market_thermometer(breadth, indices_raw)

    # ── 2. 资金交叉验证 + 3. 进出场建议 ────────────────────
    _enrich_result_rows(result, enriched_by_code)

    # ── 4. 5分钟量能（交集/超短/低吸A/B，≤20只，60s缓存）────
    if not fallback_snapshot:
        try:
            enrich_min5(result)
        except Exception as exc:  # 量能失败不影响主流程
            result.setdefault("warnings", []).append(f"5分钟量能获取失败：{exc}")

    _save_kline_cache()

    result["meta"]["elapsed_seconds"] = round(time.time() - t0, 1)
    result = screen._sanitize_for_json(result)
    return result
