#!/usr/bin/env python3
"""Real-time A-share screening dashboard server.

Uses the cached engine (realtime_engine.py) to run screening in a background
thread during trading hours, and serves a web dashboard for monitoring.

No external dependencies — uses only Python standard library.
Run: python3 realtime_dashboard.py  then open http://localhost:8765
"""
from __future__ import annotations

import json
import math
import os
import sys
import subprocess
import re
import threading
import time
import urllib.request
import ssl
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Auto-detect system proxy (bypasses IP bans on East Money API)
def _list_proxy_candidates() -> list[str]:
    """Collect candidate proxy URLs (without testing connectivity).

    Sources, in priority order:
      1. Environment HTTP(S)_PROXY (format-validated)
      2. macOS system proxy from scutil
      3. Listening ports of known proxy processes (clash/privoxy/ss-local/...)
    Covers stale-scutil and proxy-port-drift cases after a Clash/SS restart.
    """
    candidates: list[str] = []

    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        try:
            p = urlparse(env_proxy)
            ok = (
                p.scheme in ("http", "https")
                and bool(p.hostname)
                and (":" not in p.netloc or p.port is not None)
            )
        except Exception:
            ok = False
        if ok:
            candidates.append(env_proxy)
        else:
            print(f"[dashboard] env proxy malformed ({env_proxy}); ignored", file=sys.stderr)

    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
            host = port = None
            for line in out.splitlines():
                s = line.strip()
                if s.startswith("HTTPProxy :") or s.startswith("HTTPSProxy :"):
                    host = s.split(":", 1)[1].strip()
                elif s.startswith("HTTPPort :") or s.startswith("HTTPSPort :"):
                    port = s.split(":", 1)[1].strip()
            if host and port:
                candidates.append(f"http://{host}:{port}")
        except Exception:
            pass

    proxy_names = (
        "clash", "clash-ver", "mihomo", "privoxy", "ss-local", "sslocal",
        "shadowsocks", "v2ray", "xray", "surge", "trojan", "sing-box", "v2rayn",
    )
    try:
        pg = subprocess.run(["pgrep", "-i", "|".join(proxy_names)],
                            capture_output=True, text=True, timeout=5).stdout
        for pid in {p for p in pg.split() if p.strip().isdigit()}:
            try:
                ls = subprocess.run(["lsof", "-p", pid, "-i", "-P", "-n"],
                                    capture_output=True, text=True, timeout=5).stdout
                for line in ls.splitlines():
                    m = re.search(r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\*):(\d+) \(LISTEN\)", line)
                    if m:
                        candidates.append(f"http://127.0.0.1:{m.group(1)}")
            except Exception:
                pass
    except Exception:
        pass

    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _test_proxy(proxy_url: str, timeout: int = 5) -> bool:
    """Return True only if the proxy really proxies East Money (stdlib urllib only).

    We require the response to be valid East Money JSON (not just HTTP 200),
    otherwise a local HTTP server that happens to answer 200 would be mistaken
    for a working proxy. We also never trust the dashboard's own port.
    """
    try:
        p = urlparse(proxy_url)
        if p.scheme not in ("http", "https"):
            return False
        if p.hostname in ("127.0.0.1", "localhost", "::1") and p.port == PORT:
            return False
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        https_handler = urllib.request.HTTPSHandler(context=ctx)
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler, https_handler)
        req = urllib.request.Request(
            # 2026-07-30: push2 对海外出口间歇 502 且 302 到 push2delay；
            # 用 push2delay 做健康检查（A股实时，100%稳定），避免误判"代理不可用"。
            "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=1&fs=m:1+t:2",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json",
            },
        )
        resp = opener.open(req, timeout=timeout)
        if resp.status != 200:
            return False
        try:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        # East Money's clist/get returns {"rc":0,"rt":...,"data":{...}} or {"data":...}
        if "rc" not in data and "data" not in data:
            return False
        return True
    except Exception:
        return False


_last_working_proxy: str | None = None


def _detect_proxy() -> str | None:
    """Detect a *working* HTTP proxy by verifying connectivity to East Money.

    Scans environment, system preference and live proxy-process ports, then
    returns the first one that can actually fetch East Money. Self-heals when
    the proxy port changes (Clash/SS restart) or scutil reports a stale port.
    Returns None if no candidate works.
    """
    global _last_working_proxy
    if _last_working_proxy and _test_proxy(_last_working_proxy):
        return _last_working_proxy
    _last_working_proxy = None

    # 候选端口可能很多（pgrep 匹配到的进程会带出一堆 LISTEN 端口），
    # 串行逐个探测在全部失败时可能上百秒。改为并发探测 + 短超时，整轮 ≤ ~4s。
    cands = _list_proxy_candidates()
    results: dict[str, bool] = {}

    def _probe(u: str) -> None:
        try:
            results[u] = _test_proxy(u, timeout=3)
        except Exception:
            results[u] = False

    threads = [threading.Thread(target=_probe, args=(u,), daemon=True) for u in cands]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=4)

    for u in cands:
        if results.get(u):
            _last_working_proxy = u
            # 不固化 env：让 REQUESTS_SESSION(trust_env=True) 每次从 scutil 实时读取代理，
            # 配合 keep_proxy_alive 守护，代理被重置后可自动恢复，无需重启看板。
            print(f"[dashboard] proxy OK (verified): {u}", file=sys.stderr)
            return u
    print("[dashboard] no working proxy found (East Money unreachable via any candidate)", file=sys.stderr)
    return None

_detected_proxy_at_startup = _detect_proxy()
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
STATIC_DIR = SCRIPT_DIR / "realtime_static"
PORT = 8765
# 单次筛选硬超时（秒）。健康刷新通常 5~10s（K线走缓存）；若代理在筛选中途掉线，
# 引擎会在超时附近空耗，这里兜底中止该轮，标记代理不可用并保留快照，
# 避免前端一直停在「筛选中」。
SCREENING_TIMEOUT = 120
# 代理断开时，用更短的轮询间隔探测恢复（正常刷新间隔是 settings["interval"]=90s）。
# 你一旦把代理弄通，看板约 20s 内自动恢复，不用干等一整轮。
PROXY_RECOVERY_INTERVAL = 15
MD_OUTPUT_DIR = Path("/Users/luqiang/Documents/Others/股票/筛选结果")
# 持久化最近一次「有效完整」结果，供非交易时段保留快照 / 跨重启恢复
LAST_VALID_RESULT_PATH = SCRIPT_DIR / "last_valid_result.json"

TRADING_SESSIONS = [
    (9, 15, 11, 35),
    (12, 55, 15, 5),
]


def is_trading_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.hour * 60 + now.minute
    for h1, m1, h2, m2 in TRADING_SESSIONS:
        if h1 * 60 + m1 <= current <= h2 * 60 + m2:
            return True
    return False


def _inject_proxy_to_session() -> None:
    """Re-detect proxy from scutil each time and inject into REQUESTS_SESSION.
    This ensures proxy changes (turned on/off mid-session) are picked up."""
    proxy_url = _detect_proxy()  # fresh detection every run
    if not proxy_url:
        # No proxy available — clear any stale proxy from sessions
        try:
            import a_share_daily_screen as screen
            if screen.REQUESTS_SESSION is not None and screen.REQUESTS_SESSION.proxies:
                screen.REQUESTS_SESSION.proxies = {}
                print("[dashboard] cleared proxy from REQUESTS_SESSION (no system proxy)", file=sys.stderr)
        except Exception:
            pass
        return
    try:
        import a_share_daily_screen as screen
        if screen.REQUESTS_SESSION is not None:
            screen.REQUESTS_SESSION.proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }
            # Never fall back to (possibly malformed) environment proxies
            screen.REQUESTS_SESSION.trust_env = False
            print(f"[dashboard] injected proxy {proxy_url} into REQUESTS_SESSION", file=sys.stderr)
        # Don't inject into DIRECT_SESSION — that session is meant for direct connections
    except Exception as e:
        print(f"[dashboard] proxy injection error: {e}", file=sys.stderr)


class ScreeningScheduler:
    def __init__(self) -> None:
        self.latest_result: dict | None = None
        self.last_run_time: datetime | None = None
        self.last_run_duration: float | None = None
        self.preserve_snapshot = False
        self.preserved_from: str | None = None
        self.last_degraded_attempt: datetime | None = None
        self.is_running = False
        self.is_prewarming = False
        self.prewarm_progress = {"done": 0, "total": 0, "failed": 0}
        self.latest_md_path: str | None = None
        self.proxy_unavailable = False
        self._screening_lock = threading.Lock()
        self._prewarm_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.settings = {
            "skip_announcements": False,
            "skip_capital_ranking": False,
            "network_mode": "proxy",
            "auto_refresh": True,
            "auto_shutdown": True,
            "interval": 90,
            "top": 15,
        }
        # 启动时尝试恢复最近有效结果（跨重启 / 非交易时段保留快照）
        try:
            restored = self._load_last_valid()
            if restored is not None:
                self.latest_result = restored
                self.preserve_snapshot = True
                self.preserved_from = (restored.get("meta") or {}).get("timestamp")
                print(f"[dashboard] restored last valid result ({self.preserved_from})", file=sys.stderr)
        except Exception as e:
            print(f"[dashboard] restore last valid failed: {e}", file=sys.stderr)

    def run_screening(self, force: bool = False) -> bool:
        if not self._screening_lock.acquire(blocking=False):
            return False
        try:
            self.is_running = True
            # Fast-fail when no working proxy: East Money blocks direct
            # connections, so screening without a proxy just hangs on timeouts
            # (~minute per run) for nothing. Preserve the last snapshot and
            # surface a clear "proxy unavailable" status instead.
            if not _detect_proxy():
                self.proxy_unavailable = True
                prev = self.latest_result
                prev_meta = (prev or {}).get("meta", {})
                if prev and "error" not in prev and not prev_meta.get("market_data_degraded"):
                    self.preserve_snapshot = True
                    self.preserved_from = prev_meta.get("timestamp")
                    self.last_run_time = datetime.now()
                    self.last_run_duration = 0.0
                    print("[dashboard] no working proxy; preserving last snapshot", file=sys.stderr)
                    return True
                print("[dashboard] no working proxy and no valid snapshot to preserve", file=sys.stderr)
                return False
            self.proxy_unavailable = False
            _inject_proxy_to_session()
            from realtime_engine import run_screening as _engine_run

            # 在守护线程中执行引擎调用，并加硬超时兜底：代理在筛选中途掉线时，
            # 引擎会空耗很久；超时后中止本轮、标记代理不可用并保留上次快照。
            ctx: dict = {}

            def _worker() -> None:
                try:
                    t0 = time.time()
                    ctx["result"] = _engine_run(
                        modes={"strict", "low", "watchlist"},
                        workers=6,
                        top=self.settings["top"],
                        skip_announcements=self.settings["skip_announcements"],
                        skip_capital_ranking=self.settings["skip_capital_ranking"],
                        network_mode=self.settings["network_mode"],
                    )
                    ctx["elapsed"] = time.time() - t0
                except Exception as e:  # noqa: BLE001
                    ctx["error"] = e

            _th = threading.Thread(target=_worker, daemon=True)
            _th.start()
            _th.join(timeout=SCREENING_TIMEOUT)

            if _th.is_alive() or "error" in ctx:
                # 代理掉线 / 引擎崩溃：不要覆盖已有有效数据，保留快照并提示。
                self.proxy_unavailable = True
                prev = self.latest_result
                prev_meta = (prev or {}).get("meta", {})
                if prev and "error" not in prev and not prev_meta.get("market_data_degraded"):
                    self.preserve_snapshot = True
                    self.preserved_from = prev_meta.get("timestamp")
                    self.last_run_time = datetime.now()
                    self.last_run_duration = SCREENING_TIMEOUT if _th.is_alive() else (ctx.get("elapsed") or 0.0)
                    print("[dashboard] screening timed out / errored; preserving last snapshot", file=sys.stderr)
                    return True
                print("[dashboard] screening timed out / errored; no valid snapshot to preserve", file=sys.stderr)
                return False

            result = ctx["result"]
            elapsed = ctx.get("elapsed", 0.0)

            if "error" not in result:
                meta = result.get("meta", {})
                degraded = bool(meta.get("market_data_degraded"))
                complete = meta.get("market_fetch_complete")
                is_incomplete = degraded or (complete in (False, None))
                now_trading = is_trading_hours()

                # 已有有效完整结果？
                prev = self.latest_result
                prev_meta = (prev or {}).get("meta", {})
                prev_valid = bool(
                    prev and "error" not in prev
                    and not prev_meta.get("market_data_degraded")
                    and prev_meta.get("market_fetch_complete") is not False
                )

                # 本次降级(东财 fallback 或部分页失败) + 已有有效快照 + 非强制 → 保留，不覆盖
                # 交易时段与盘后 alike：偶发降级时展示最近完整快照，避免池子被清空
                if is_incomplete and prev_valid and not force:
                    self.last_degraded_attempt = datetime.now()
                    self.last_run_time = datetime.now()
                    self.last_run_duration = elapsed
                    self.preserve_snapshot = True
                    self.preserved_from = prev_meta.get("timestamp")
                    print(
                        f"[dashboard] 行情降级数据，保留最近有效快照({prev_meta.get('timestamp')})",
                        file=sys.stderr,
                    )
                    return True

                # 正常更新
                self.latest_result = result
                self.last_run_time = datetime.now()
                self.last_run_duration = elapsed
                self.preserve_snapshot = bool((not now_trading) and is_incomplete)
                if (not now_trading) and is_incomplete:
                    self.preserved_from = meta.get("timestamp")
                self._save_markdown(result)
                # 仅「完整盘中结果」持久化为最近有效快照
                if (not degraded) and (complete is not False):
                    self._save_last_valid(result)
                    self.preserve_snapshot = False
                    self.preserved_from = None
                print(
                    f"[dashboard] screening done in {elapsed:.1f}s "
                    f"kline_cache={meta.get('kline_cache_stats', {})} degraded={degraded}",
                    file=sys.stderr,
                )
                return True
            else:
                # 失败也尽量保留已有有效结果
                self.proxy_unavailable = True
                prev = self.latest_result
                prev_meta = (prev or {}).get("meta", {})
                prev_valid = bool(
                    prev and "error" not in prev
                    and not prev_meta.get("market_data_degraded")
                    and prev_meta.get("market_fetch_complete") is not False
                )
                if prev_valid:
                    self.last_run_time = datetime.now()
                    self.last_run_duration = elapsed
                    print(f"[dashboard] screening error, kept previous valid result: {result.get('error', '')[:200]}", file=sys.stderr)
                    return False
                self.latest_result = result
                self.last_run_time = datetime.now()
                self.last_run_duration = elapsed
                print(f"[dashboard] screening error: {result.get('error', '')[:200]}", file=sys.stderr)
                return False
        except Exception as e:
            self.latest_result = {"error": f"{type(e).__name__}: {e}"}
            self.last_run_time = datetime.now()
            print(f"[dashboard] exception: {e}", file=sys.stderr)
            return False
        finally:
            self.is_running = False
            self._screening_lock.release()

    def prewarm(self) -> bool:
        """Pre-fetch K-line data to warm the cache before screening."""
        if not self._prewarm_lock.acquire(blocking=False):
            return False
        try:
            self.is_prewarming = True
            from realtime_engine import prewarm_kline_cache

            def progress_cb(done, total, code, failed):
                self.prewarm_progress = {"done": done, "total": total, "failed": failed}

            result = prewarm_kline_cache(workers=6, progress_callback=progress_cb)
            print(f"[dashboard] prewarm done: {result}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[dashboard] prewarm error: {e}", file=sys.stderr)
            return False
        finally:
            self.is_prewarming = False
            self._prewarm_lock.release()

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        threading.Thread(target=self._initial_run, daemon=True).start()

    def _initial_run(self) -> None:
        """At startup: prewarm if cache is cold, then run screening."""
        time.sleep(1)
        # Check if cache needs prewarming
        from realtime_engine import get_cache_stats
        stats = get_cache_stats()
        if not stats.get("cache_valid") or stats.get("cache_size", 0) < 50:
            print("[dashboard] cache cold, prewarming...", file=sys.stderr)
            self.prewarm()
        self.run_screening()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            # Auto-shutdown at 15:15 on weekdays after final screening
            if (
                self.settings.get("auto_shutdown", True)
                and now.weekday() < 5
                and now.hour == 15 and now.minute >= 15
                and not self.is_running
                and self.latest_result is not None
            ):
                print("[dashboard] market closed, auto-shutting down...", file=sys.stderr)
                self._archive_markdown()
                _shutdown_server()
                return
            if self.settings["auto_refresh"] and is_trading_hours():
                cycle_start = time.time()
                if not self.is_running and not self.is_prewarming:
                    self.run_screening()
                # 代理断开时缩短轮询间隔，尽快探测到恢复
                if self.proxy_unavailable:
                    self._stop_event.wait(PROXY_RECOVERY_INTERVAL)
                else:
                    # 间隔按「本轮开始时间」计：等待 = interval - 本轮耗时，
                    # 保证两轮开始时间稳定间隔 interval 秒（而非 interval+耗时）。
                    elapsed = time.time() - cycle_start
                    self._stop_event.wait(max(10.0, self.settings["interval"] - elapsed))
            else:
                self._stop_event.wait(30)

    def trigger_refresh(self, force: bool = False) -> bool:
        if self.is_running or self.is_prewarming:
            return False
        threading.Thread(target=self.run_screening, kwargs={"force": force}, daemon=True).start()
        return True

    def _save_last_valid(self, result: dict) -> None:
        """Persist a complete (non-degraded) screening result for snapshot use."""
        try:
            LAST_VALID_RESULT_PATH.write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            print(f"[dashboard] save last valid failed: {e}", file=sys.stderr)

    def _load_last_valid(self) -> dict | None:
        """Load the last persisted valid result, or None."""
        try:
            if LAST_VALID_RESULT_PATH.exists():
                return json.loads(LAST_VALID_RESULT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[dashboard] load last valid failed: {e}", file=sys.stderr)
        return None

    def trigger_prewarm(self) -> bool:
        if self.is_prewarming or self.is_running:
            return False
        threading.Thread(target=self.prewarm, daemon=True).start()
        return True

    @staticmethod
    def _render_min5_table(result: dict) -> str:
        """5分钟量能紧凑表（closed口径，交易判定用）。>180s 标记失效。"""
        rows_by_code: dict = {}
        for section in ("dual_pool_raw", "strict_ultra", "low_ultra"):
            for row in result.get(section) or []:
                code = str(row.get("code", ""))
                if code and code not in rows_by_code and row.get("min5"):
                    rows_by_code[code] = row
        if not rows_by_code:
            return ""
        lines = [
            "",
            "## 5分钟量能（1分钟K滚动合成，closed口径）",
            "",
            "| 代码 | 5分量 | 前5分量 | 30分均量 | 5分量比 | 5分OHLC | 5分VWAP | 数据时间 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]

        def _fmt_vol(v):
            if v is None:
                return "-"
            return f"{v/10000:.1f}万手" if v >= 10000 else f"{v:.0f}手"

        for code, row in rows_by_code.items():
            m = row["min5"]
            c = m.get("closed_5m") or {}
            cur = c.get("cur") or {}
            ohlc = (
                f"{cur.get('open','-')} / {cur.get('high','-')} / {cur.get('low','-')} / {cur.get('close','-')}"
                if cur else "-"
            )
            ratio = c.get("vol_ratio_5m")
            age = m.get("age_seconds", 0)
            stamp = f"{m.get('bar_end','-')}({age}s前)"
            if m.get("stale"):
                stamp += " ⚠️已失效"
            lines.append(
                f"| {code} | {_fmt_vol(cur.get('vol'))} | {_fmt_vol(c.get('prev_vol'))} "
                f"| {_fmt_vol(c.get('avg5_vol_30m'))} | {ratio if ratio is not None else '-'} "
                f"| {ohlc} | {cur.get('vwap','-')} | {stamp} |"
            )
        lines.append("")
        lines.append("> 量单位=手；VWAP=Σ额÷(Σ量×100)；仅用已收完1分钟K，不含当前未完成K；不跨午休拼窗。")
        return "\n".join(lines)

    def _save_markdown(self, result: dict) -> None:
        """Generate and save markdown report to root dir (flat, for Codex comparison)."""
        try:
            import a_share_daily_screen as screen
            md_content = screen.render_markdown(result)
            min5_table = self._render_min5_table(result)
            if min5_table:
                md_content += "\n" + min5_table + "\n"
            MD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            md_path = MD_OUTPUT_DIR / f"A股筛选结果_{stamp}.md"
            md_path.write_text(md_content, encoding="utf-8")
            self.latest_md_path = str(md_path)
            print(f"[dashboard] markdown saved: {md_path}", file=sys.stderr)
        except Exception as e:
            print(f"[dashboard] markdown save error: {e}", file=sys.stderr)

    def _archive_markdown(self) -> None:
        """Move today's timestamped MD files into a date subfolder at market close."""
        try:
            today = datetime.now().strftime("%Y%m%d")
            today_files = list(MD_OUTPUT_DIR.glob(f"A股筛选结果_{today}_????.md"))
            if not today_files:
                return
            day_dir = MD_OUTPUT_DIR / today
            day_dir.mkdir(parents=True, exist_ok=True)
            for f in today_files:
                target = day_dir / f.name
                if not target.exists():
                    f.rename(target)
            print(f"[dashboard] archived {len(today_files)} md files to {day_dir}/", file=sys.stderr)
        except Exception as e:
            print(f"[dashboard] md archive error: {e}", file=sys.stderr)

    def update_settings(self, updates: dict) -> dict:
        for k, v in updates.items():
            if k in self.settings:
                self.settings[k] = v
        return dict(self.settings)

    def get_status(self) -> dict:
        meta = (self.latest_result or {}).get("meta", {})
        mfs = (self.latest_result or {}).get("market_fetch_status", {})
        degraded = bool(meta.get("market_data_degraded"))
        em_in_cooldown = False
        try:
            import a_share_daily_screen as screen
            em_in_cooldown = bool(screen._em_in_cooldown())
        except Exception:
            pass
        # 下一次刷新时间
        next_refresh_time: str | None = None
        next_is_trading_open = False
        if self.is_running or self.is_prewarming:
            next_refresh_time = "运行中"
        elif self.settings.get("auto_refresh") and is_trading_hours():
            if self.last_run_time:
                # 间隔按「本轮开始」计：下轮开始 ≈ 本次结束 + (interval - 本次耗时)，
                # 下次数据就绪 ≈ 下轮开始 + 本次耗时 ≈ 本次结束 + interval
                dur = self.last_run_duration or 0
                wait = max(10.0, self.settings["interval"] - dur)
                nxt = self.last_run_time + timedelta(seconds=wait + dur)
                if nxt <= datetime.now():
                    nxt = datetime.now() + timedelta(seconds=5)
                next_refresh_time = nxt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                next_refresh_time = "即将"
        else:
            op = _next_trading_open()
            if op:
                next_refresh_time = op.strftime("%Y-%m-%d %H:%M:%S")
                next_is_trading_open = True
        return {
            "is_running": self.is_running,
            "is_prewarming": self.is_prewarming,
            "prewarm_progress": dict(self.prewarm_progress),
            "last_run_time": self.last_run_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_run_time else None,
            "last_run_duration": round(self.last_run_duration, 1) if self.last_run_duration else None,
            "is_trading_hours": is_trading_hours(),
            "settings": dict(self.settings),
            "has_result": self.latest_result is not None and "error" not in (self.latest_result or {}),
            "md_path": self.latest_md_path,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "preserve_snapshot": self.preserve_snapshot,
            "preserved_from": self.preserved_from,
            "market_data_degraded": degraded,
            "incomplete": bool(degraded) or (mfs.get("complete") is False),
            "data_mode": (
                "degraded" if (bool(degraded) or (mfs.get("complete") is False))
                else ("snapshot" if self.preserve_snapshot else "live")
            ),
            "em_in_cooldown": em_in_cooldown,
            "proxy_unavailable": self.proxy_unavailable,
            "next_refresh_time": next_refresh_time,
            "next_is_trading_open": next_is_trading_open,
            "market_fetch_complete": mfs.get("complete"),
            "failed_pages": mfs.get("failed_pages") or [],
        }


def _next_trading_open() -> datetime | None:
    """返回下一个交易时段开始时间（09:30 或 13:00），用于非交易时段提示。"""
    now = datetime.now()
    today = now.date()
    candidates = []
    for h, m in ((9, 30), (13, 0)):
        cand = datetime(today.year, today.month, today.day, h, m)
        if cand > now:
            candidates.append(cand)
    if candidates:
        return candidates[0]
    # 今天已过，找下一个工作日 09:30
    d = today
    while True:
        d += timedelta(days=1)
        if d.weekday() < 5:
            return datetime(d.year, d.month, d.day, 9, 30)


scheduler = ScreeningScheduler()
_server: ThreadingHTTPServer | None = None


def _shutdown_server() -> None:
    """Shutdown the HTTP server from a background thread."""
    if _server is not None:
        threading.Thread(target=_server.shutdown, daemon=True).start()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def _sanitize_json(obj):
    """Recursively replace NaN/Inf with None for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
        elif path == "/style.css":
            self._serve_static("style.css")
        elif path == "/app.js":
            self._serve_static("app.js")
        elif path == "/api/data":
            self._serve_json(scheduler.latest_result or {"error": "waiting for first screening..."})
        elif path == "/api/status":
            self._serve_json(scheduler.get_status())
        elif path == "/api/md":
            if scheduler.latest_md_path:
                try:
                    md = Path(scheduler.latest_md_path).read_text(encoding="utf-8")
                    self._serve_text(md, "text/markdown; charset=utf-8")
                except Exception:
                    self._serve_json({"error": "file not found"})
            else:
                self._serve_json({"error": "no markdown generated yet"})
        elif path == "/api/sticky/debug":
            from realtime_engine import get_sticky_debug
            self._serve_json(get_sticky_debug())
        else:
            self.send_error(404, "Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/refresh":
            qs = parse_qs(urlparse(self.path).query)
            force = "force" in qs
            ok = scheduler.trigger_refresh(force=force)
            self._serve_json({"status": "started" if ok else "already_running", "force": force})
        elif path == "/api/prewarm":
            ok = scheduler.trigger_prewarm()
            self._serve_json({"status": "started" if ok else "already_running"})
        elif path == "/api/clear_cooldown":
            try:
                import a_share_daily_screen as screen
                screen._em_clear_cooldown()
                self._serve_json({"status": "cleared", "em_in_cooldown": bool(screen._em_in_cooldown())})
            except Exception as e:
                self._serve_json({"status": "error", "error": str(e)})
        elif path == "/api/settings":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                updates = json.loads(body)
                new_settings = scheduler.update_settings(updates)
                self._serve_json({"status": "ok", "settings": new_settings})
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
        elif path == "/api/sticky/add":
            from realtime_engine import add_manual_focus
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code") or [""])[0].strip()
            name = (qs.get("name") or [""])[0].strip() or None
            if not code:
                self._serve_json({"status": "error", "error": "code required"})
            else:
                add_manual_focus(code, name)
                self._serve_json({"status": "ok", "code": code, "name": name})
        elif path == "/api/sticky/remove":
            from realtime_engine import remove_manual_focus
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code") or [""])[0].strip()
            if not code:
                self._serve_json({"status": "error", "error": "code required"})
            else:
                remove_manual_focus(code)
                self._serve_json({"status": "ok", "code": code})
        else:
            self.send_error(404, "Not found")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def _serve_static(self, filename: str) -> None:
        filepath = STATIC_DIR / filename
        if not filepath.exists():
            self.send_error(404, f"File not found: {filename}")
            return
        content = filepath.read_bytes()
        ext = filepath.suffix
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data: object) -> None:
        content = json.dumps(
            _sanitize_json(data),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _serve_text(self, text: str, content_type: str) -> None:
        content = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format: str, *args) -> None:
        pass


def main() -> int:
    global _server

    # Auto-kill any stale process occupying the port
    import subprocess
    try:
        stale = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
        if stale.stdout.strip():
            for pid in stale.stdout.strip().split("\n"):
                os.kill(int(pid), 9)
                print(f"[dashboard] killed stale process {pid} on port {PORT}", file=sys.stderr)
            time.sleep(1)
    except Exception:
        pass

    _server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    scheduler.start()

    import atexit
    atexit.register(scheduler._archive_markdown)

    url = f"http://localhost:{PORT}"
    print(f"[dashboard] server running at {url}")
    print(f"[dashboard] trading hours: {is_trading_hours()}")
    print(f"[dashboard] auto-refresh: every {scheduler.settings['interval']}s during trading")
    print("[dashboard] auto-shutdown at 15:15 on weekdays")
    print("[dashboard] press Ctrl+C to stop")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] shutting down...")
        scheduler._archive_markdown()
        _server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
