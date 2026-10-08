#!/usr/bin/env python3
"""Real-time A-share screening dashboard server.

Uses the cached engine (realtime_engine.py) to run screening in a background
thread during trading hours, and serves a web dashboard for monitoring.

No external dependencies — uses only Python standard library.
Run: python3 realtime_dashboard.py  then open http://localhost:8765
Use --no-browser to suppress automatic browser opening.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import subprocess
import re
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import network_path  # 多路径实测延迟择优（直连+候选代理端口），软件无关
import tencent_kline  # 腾讯日 K 主机列表单一来源
import dashboard_settings  # 看板参数配置（唯一写入入口，含白名单校验与版本号）
import runtime_paths  # 运行状态路径唯一来源（A_SHARE_STATE_DIR 可定向到临时目录）
import state_commit  # 回合状态提交门：超时/失败的一轮不得提交运行状态
import tls_context  # TLS 校验上下文唯一来源（默认校验证书）
import update_check  # 版本更新提示（唯一对外请求；失败静默、不阻塞请求）

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
        ctx = tls_context.build_context()   # 校验证书：能代理数据的路径必须也能通过校验
        https_handler = urllib.request.HTTPSHandler(context=ctx)
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler, https_handler)
        req = urllib.request.Request(
            # 使用与筛选列表相同的 push2/webguest 健康入口，避免检测旧标准路径。
            "https://push2.eastmoney.com/webguest/api/qt/clist/get?pn=1&pz=1&fs=m:1+t:2",
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

_detected_proxy_at_startup = None
# 启动即实测所有路径（直连+候选代理端口）延迟并缓存，不再依赖系统代理检测
_startup_paths = network_path.warm_up()
if _startup_paths:
    print("[dashboard] network paths: " + ", ".join(
        f"{p['label']}({p['latency_ms']:.0f}ms)" for p in _startup_paths), file=sys.stderr)
else:
    print("[dashboard] no working network path found at startup", file=sys.stderr)
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from tools.data_sources.calendar import TradingCalendarService  # noqa: E402
from tools.data_sources.background import build_market_background  # noqa: E402
STATIC_DIR = SCRIPT_DIR / "realtime_static"
# 两个入口共用的导航与运行状态条：工作台 handler 继承本模块的静态路由，
# 因此只需在这里挂一次（见 do_GET 的 /common.css、/common.js）。
SHARED_STATIC_DIR = SCRIPT_DIR / "shared_static"
PORT = 8765
# 单次筛选硬超时（秒）。健康刷新通常 5~10s（K线走缓存）；若代理在筛选中途掉线，
# 引擎会在超时附近空耗，这里兜底中止该轮，标记代理不可用并保留快照，
# 避免前端一直停在「筛选中」。
SCREENING_TIMEOUT = 120
SINA_REACHABILITY_BUDGET_SECONDS = 3.0
SINA_REACHABILITY_REQUEST_TIMEOUT_SECONDS = 1.5
# 代理断开时，用更短的轮询间隔探测恢复（正常刷新间隔是 settings["interval"]=90s）。
# 你一旦把代理弄通，看板约 20s 内自动恢复，不用干等一整轮。
PROXY_RECOVERY_INTERVAL = 15
# Market background is evidence-only.  It refreshes after the core result is
# published and never extends the screening round's lock/timeout.
BACKGROUND_REFRESH_BUDGET_SECONDS = 8.0
# 报告归档目录：验证跑会按 A_SHARE_REPORT_DIR 改写，避免把验证产物混进当日的
# 报告序列（scan_reports / “继续看筛选”都读这个目录）。
MD_OUTPUT_DIR = runtime_paths.report_dir(PROJECT_ROOT / "筛选结果")
# 持久化最近一次「有效完整」结果，供非交易时段保留快照 / 跨重启恢复
LAST_VALID_RESULT_PATH = runtime_paths.state_file("last_valid_result.json")

TRADING_SESSIONS = [
    (9, 15, 11, 35),
    (12, 55, 15, 5),
]

# 当日是否交易日：优先使用深交所官方整月日历；官方源不可用时保留原上证指数
# 日K确认机制，最后才落到“工作日但未确认”的兼容状态。未确认状态不会开启自动筛选。
TRADING_DAY_RETRY_SECONDS = 15.0
_TRADING_DAY_CACHE = {"date": None, "value": True, "source": None, "checked_at": 0.0}
_TRADING_DAY_LOCK = threading.Lock()
_OFFICIAL_CALENDAR: TradingCalendarService | None = None


def _official_calendar() -> TradingCalendarService:
    global _OFFICIAL_CALENDAR
    if _OFFICIAL_CALENDAR is None:
        from tools.data_sources.http import project_http_client
        _OFFICIAL_CALENDAR = TradingCalendarService(client=project_http_client())
    return _OFFICIAL_CALENDAR


def _fetch_official_calendar_day(now: datetime) -> tuple[bool, str] | None:
    """Return (is_open, source) when the official calendar is conclusive."""
    try:
        result = _official_calendar().is_open(now.date())
        if result.status == "ok" and isinstance(result.data, dict):
            return bool(result.data.get("is_open")), "szse_official"
    except Exception:
        pass
    return None


def _fetch_index_kline_dates() -> list:
    """拉上证指数最近几日K线日期（标准库直连，失败返回空列表）。

    2026-09-26：改走共用的腾讯主机列表。原先写死 web.ifzq.gtimg.cn，该主机已被 WAF 拦截，
    交易日判定因此长期取不到数据、退化成"保守视为交易日"。
    """
    try:
        payload, _ = tencent_kline.fetch_kline_json("sh000001", 4, timeout=5)
        return [row[0] for row in tencent_kline.kline_rows(payload, "sh000001")]
    except Exception:
        return []


def is_trading_day(now: datetime | None = None) -> bool:
    """当日是否 A 股交易日，且保留来源/确认状态供调度器使用。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    key = now.strftime("%Y-%m-%d")
    before_open = now.hour * 60 + now.minute < 9 * 60 + 30
    with _TRADING_DAY_LOCK:
        if _TRADING_DAY_CACHE["date"] == key:
            # A pre-open guess is only provisional. Recheck it once the market
            # has had a chance to publish today's index bar.
            cached_source = _TRADING_DAY_CACHE.get("source")
            if cached_source == "szse_official" or cached_source == "index":
                return _TRADING_DAY_CACHE["value"]
            if cached_source == "pending" and before_open:
                return _TRADING_DAY_CACHE["value"]
            if cached_source == "unavailable":
                try:
                    if time.monotonic() - float(_TRADING_DAY_CACHE.get("checked_at") or 0.0) < TRADING_DAY_RETRY_SECONDS:
                        return _TRADING_DAY_CACHE["value"]
                except (TypeError, ValueError):
                    pass
        official = _fetch_official_calendar_day(now)
        if official is not None:
            value, source = official
        else:
            dates = _fetch_index_kline_dates()
            if dates:
                if dates[-1] == key:
                    value, source = True, "index"
                elif before_open:
                    value, source = True, "pending"
                else:
                    value, source = False, "index"
            else:
                # Compatibility: callers still see a weekday as a candidate,
                # but ``_trading_day_pending`` prevents automatic screening or
                # T+1 scheduling while the day is unconfirmed.
                value, source = True, "unavailable"
        _TRADING_DAY_CACHE.update(date=key, value=value, source=source, checked_at=time.monotonic())
        return value


def _trading_day_pending(now: datetime | None = None) -> bool:
    """Whether today's positive result is not confirmed by an official/index source."""
    now = now or datetime.now()
    with _TRADING_DAY_LOCK:
        return (
            _TRADING_DAY_CACHE["date"] == now.strftime("%Y-%m-%d")
            and _TRADING_DAY_CACHE.get("source") in {"pending", "unavailable"}
        )


def is_trading_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if not is_trading_day(now) or _trading_day_pending(now):
        return False
    current = now.hour * 60 + now.minute
    for h1, m1, h2, m2 in TRADING_SESSIONS:
        if h1 * 60 + m1 <= current <= h2 * 60 + m2:
            return True
    return False


def _sina_reachable(*, timeout: float | None = None) -> bool:
    """Boundedly probe one structurally usable Sina quote without accepting it as a result.

    Eastmoney path health is endpoint-specific. This check only decides whether
    to let the normal shared engine try its independent fallback; the engine
    still fetches and validates the full market, marks Sina data degraded, and
    uses the usual round-commit gate. The daemon worker cannot mutate screening
    state after this probe's deadline expires.
    """
    import a_share_daily_screen as screen

    budget = SINA_REACHABILITY_BUDGET_SECONDS if timeout is None else max(0.0, timeout)
    if budget <= 0:
        return False
    deadline = time.monotonic() + budget
    outcome: dict[str, bool] = {"usable": False}

    def _probe() -> None:
        try:
            data = screen.fetch_json(
                screen.SINA_MARKET_URL,
                {"page": 1, "num": 1, "sort": "symbol", "asc": 1,
                 "node": "hs_a", "_s_r_a": "page"},
                timeout=min(SINA_REACHABILITY_REQUEST_TIMEOUT_SECONDS, budget),
                retries=0,
                deadline=deadline,
            )
            if not isinstance(data, list):
                return
            outcome["usable"] = any(
                isinstance(row, dict)
                and (normalized := screen._normalize_sina_row(row)) is not None
                and re.fullmatch(r"\d{6}", str(normalized.get("f12") or "")) is not None
                and (normalized.get("f2") or 0) > 0
                and (normalized.get("f18") or 0) > 0
                for row in data
            )
        except Exception:
            outcome["usable"] = False

    probe_thread = threading.Thread(target=_probe, name="sina-reachability-probe", daemon=True)
    probe_thread.start()
    probe_thread.join(timeout=budget)
    return not probe_thread.is_alive() and outcome["usable"]


def _inject_proxy_to_session() -> None:
    """把实测最快的代理路径注入 REQUESTS_SESSION；最优为直连（或全不通）时清掉代理。
    路径由 network_path 实测决定，不依赖 scutil / 任何代理软件。"""
    proxy_url = network_path.best_proxy_url()  # 实测最快；直连最优时为 None
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


def _announcement_check_skipped(meta: dict) -> bool:
    """展示快照的实际执行口径：本轮公告检查是否被跳过。

    以结果里记录的 ``announcement_check_skipped`` 为准（不是"当前设置"）；老快照
    没有该字段时回退看来源文案，避免漏报已有的跳过快照。
    """
    value = (meta or {}).get("announcement_check_skipped")
    if value is None:
        return "公告已跳过" in str((meta or {}).get("source") or "")
    return bool(value)


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
        self._background_lock = threading.Lock()
        self._background_thread: threading.Thread | None = None
        self.background_enabled = True
        self._prewarm_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.settings = {
            # 公告检查是框架一票否决门禁：不放进可设置项，任何入口都关不掉。
            "skip_capital_ranking": False,
            "network_mode": "auto",
            "auto_refresh": True,
            "auto_shutdown": True,
            "interval": 90,
            "top": 15,
        }
        # 持久配置（配置版本 + 负超单观察模式 + 运行参数）。文件缺失/损坏时
        # 回退严格默认值，并把错误暴露到状态接口，便于页面提示。
        self.config, self.config_error = dashboard_settings.load()
        self._apply_config_to_settings()
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

    def _start_market_background(self, result: dict, data_date: str | None) -> None:
        """Refresh evidence after publishing the core result, outside the run lock."""
        if not getattr(self, "background_enabled", False):
            return
        lock = getattr(self, "_background_lock", None)
        if lock is None:
            return
        if not lock.acquire(blocking=False):
            return
        current = getattr(self, "_background_thread", None)
        if current is not None and current.is_alive():
            lock.release()
            return
        result["market_background"] = {
            "status": "loading",
            "data_date": data_date,
            "note": "背景证据异步刷新，不改变核心筛选结果或交易权限",
        }

        def _worker() -> None:
            try:
                try:
                    background = build_market_background(
                        data_date,
                        budget_seconds=BACKGROUND_REFRESH_BUDGET_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    background = {
                        "data_date": data_date,
                        "status": "unavailable",
                        "error": {"code": "background_source_error", "message": f"{type(exc).__name__}: {exc}"},
                        "warnings": ["背景源失败，不影响核心筛选结果"],
                    }
                # A later screening round owns the UI snapshot; an old
                # background worker must not overwrite it.
                if self.latest_result is result:
                    result["market_background"] = background
                    try:
                        self._save_markdown(result)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[dashboard] background snapshot save failed: {exc}", file=sys.stderr)
            finally:
                lock.release()

        thread = threading.Thread(target=_worker, name="market-background", daemon=True)
        self._background_thread = thread
        thread.start()

    def run_screening(self, force: bool = False) -> bool:
        if not self._screening_lock.acquire(blocking=False):
            return False
        sina_probe_succeeded = False
        try:
            self.is_running = True
            # 每轮开始时固定一份配置快照：运行途中改配置只作用于下一轮。
            round_settings = dict(self.settings)
            round_config_revision = int((self.config or {}).get("revision") or 1)
            round_negative_super_view = dashboard_settings.view_of(self.config)
            # 交易板范围同样在轮初冻结：运行途中改配置只作用于下一轮。
            round_enabled_boards = dashboard_settings.boards_of(self.config)
            # network_path 实测的是东财，不代表独立行情源也不可达。东财全灭时，
            # 只有新浪返回可归一化报价才进入共用引擎；探测不替代完整性检查。
            eastmoney_reachable = network_path.has_working_path()
            if not eastmoney_reachable:
                sina_probe_succeeded = _sina_reachable()
            if not eastmoney_reachable and not sina_probe_succeeded:
                self.proxy_unavailable = True
                prev = self.latest_result
                prev_meta = (prev or {}).get("meta", {})
                if prev and "error" not in prev and not prev_meta.get("market_data_degraded"):
                    self.preserve_snapshot = True
                    self.preserved_from = prev_meta.get("timestamp")
                    self.last_run_time = datetime.now()
                    self.last_run_duration = 0.0
                    print("[dashboard] no working network path; preserving last snapshot", file=sys.stderr)
                    return True
                print("[dashboard] no working network path and no valid snapshot to preserve", file=sys.stderr)
                return False
            self.proxy_unavailable = False
            _inject_proxy_to_session()
            from realtime_engine import run_screening as _engine_run

            # 在守护线程中执行引擎调用，并加硬超时兜底：代理在筛选中途掉线时，
            # 引擎会空耗很久；超时后中止本轮、标记代理不可用并保留上次快照。
            #
            # 本轮所有运行状态（资金基准/交集/观察池突破/K线缓存）先暂存在
            # round_commit，只有本轮确认成功后由这里统一提交；超时/失败一律 abort，
            # 旧线程此后 stage 的写入全部丢弃，不会覆盖正式状态。
            ctx: dict = {}
            round_commit = state_commit.RoundCommit(f"dashboard-{time.strftime('%H%M%S')}")

            def _worker() -> None:
                try:
                    t0 = time.time()
                    ctx["result"] = _engine_run(
                        modes={"strict", "low", "watchlist"},
                        workers=6,
                        top=round_settings["top"],
                        # 服务端强制：正式筛选必须执行公告检查，不提供关闭入口。
                        skip_announcements=False,
                        skip_capital_ranking=round_settings["skip_capital_ranking"],
                        network_mode=round_settings["network_mode"],
                        settings_snapshot={
                            "revision": round_config_revision,
                            "negative_super_view": round_negative_super_view,
                            "enabled_boards": list(round_enabled_boards),
                        },
                        state_commit=round_commit,
                    )
                    ctx["elapsed"] = time.time() - t0
                except Exception as e:  # noqa: BLE001
                    ctx["error"] = e

            _th = threading.Thread(target=_worker, daemon=True)
            _th.start()
            _th.join(timeout=SCREENING_TIMEOUT)

            if _th.is_alive() or "error" in ctx:
                # 超时/崩溃：中止本轮状态提交。旧工作线程可能仍在跑，但已无法写正式状态。
                round_commit.abort()
                # 代理掉线 / 引擎崩溃：不要覆盖已有有效数据，保留快照并提示。
                self.proxy_unavailable = not sina_probe_succeeded
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

                # 降级/不完整快照不得提交运行状态：否则会用缺字段的一轮去推进
                # 交集/观察池突破状态机，把上一份有效状态清空或误判过期。
                if is_incomplete:
                    round_commit.abort()
                else:
                    try:
                        round_commit.commit()
                    except Exception as e:  # noqa: BLE001
                        # 状态落盘失败必须可诊断，不伪装成本轮成功。
                        print(f"[dashboard] state commit failed: {e}", file=sys.stderr)
                        round_commit.abort()
                        return False

                # The evidence-only background starts as loading and is
                # refreshed after the core result is published below.
                result["market_background"] = {
                    "status": "loading",
                    "data_date": (meta.get("timestamp", "")[:10] or None),
                    "note": "背景证据异步刷新，不改变核心筛选结果或交易权限",
                }

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
                self._start_market_background(result, meta.get("timestamp", "")[:10] or None)
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
                round_commit.abort()
                self.proxy_unavailable = not sina_probe_succeeded
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
            _rc = locals().get("round_commit")
            if _rc is not None:
                _rc.abort()
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

            # 预热与筛选用同一份冻结范围，否则新交易板的首轮会因 K 线未预热而变慢
            result = prewarm_kline_cache(
                workers=6, progress_callback=progress_cb,
                boards=dashboard_settings.boards_of(self.config),
            )
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

    def stop(self) -> None:
        """Stop the scheduler loop when its owning HTTP service exits."""
        self._stop_event.set()

    def _initial_run(self) -> None:
        """At startup: prewarm if cache is cold, then run screening."""
        time.sleep(1)
        if not is_trading_day():
            print("[dashboard] non-trading day (holiday?), skip initial screening",
                  file=sys.stderr)
            return
        if _trading_day_pending():
            print("[dashboard] trading day not confirmed before open, defer initial screening",
                  file=sys.stderr)
            return
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
                and (self.latest_result is not None or not is_trading_day())
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
            state_commit.atomic_write_text(
                LAST_VALID_RESULT_PATH, json.dumps(result, ensure_ascii=False)
            )
        except Exception as e:
            print(f"[dashboard] save last valid failed: {e}", file=sys.stderr)

    def _load_last_valid(self) -> dict | None:
        """Load the last persisted valid result, or None.

        落盘的旧快照（含两板仅观察时期的产物）没有「交易板」字段，这里按代码补上。
        交易板是代码的静态属性，补标注不会改写该快照原有的筛选口径——旧口径由
        ``snapshot_screen_method`` 另行提示，页面据此提示不可与当前结果直接比较。
        """
        try:
            if LAST_VALID_RESULT_PATH.exists():
                result = json.loads(LAST_VALID_RESULT_PATH.read_text(encoding="utf-8"))
                if isinstance(result, dict):
                    import a_share_daily_screen as _screen
                    _screen.stamp_board_fields(result)
                return result
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
            min5_table = self._render_min5_table(result)

            def _render() -> str:
                content = screen.render_markdown(result)
                return content + ("\n" + min5_table + "\n" if min5_table else "")

            MD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            md_path = MD_OUTPUT_DIR / f"A股筛选结果_{stamp}.md"
            md_path.write_text(_render(), encoding="utf-8")
            self.latest_md_path = str(md_path)
            # 报告落盘后做只读影子判定，回填负超单观察徽标，再重写一次保证
            # 报告与页面口径一致（判定器读的就是刚写出的这份报告）。
            if self.refresh_shadow_badges(result):
                md_path.write_text(_render(), encoding="utf-8")
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

    # 配置模块托管的运行参数：走白名单校验并持久化，不再由旧接口直接塞值。
    _CONFIG_OWNED_KEYS = ("top", "interval", "network_mode")

    def _apply_config_to_settings(self) -> None:
        params = dashboard_settings.run_params(self.config)
        self.settings["top"] = params["top"]
        self.settings["interval"] = params["interval"]
        self.settings["network_mode"] = params["network_mode"]

    def _snapshot_screen_method(self) -> str | None:
        """当前快照的筛选口径：

        - ``full_board_screening``：新结果，所选交易板统一参与正式筛选；
        - ``legacy_extended_observation``：旧快照（当时两板只进观察列表）；
        - ``None``：更早的产物，没有可用于判断该口径的字段。

        两个页面都要用它提示「不可与当前口径直接比较」，因此配置接口与状态接口
        读的是同一个判定，避免只在其中一处生效。
        """
        result = self.latest_result if isinstance(self.latest_result, dict) else {}
        if "board_scope_status" in result:
            return "full_board_screening"
        if "extended_board_observations" in result:
            return "legacy_extended_observation"
        return None

    def config_public(self) -> dict:
        """配置只读视图：当前生效版本 + 看板最新快照使用的版本。"""
        config = self.config or dashboard_settings.default_config()
        snapshot = self.latest_result if isinstance(self.latest_result, dict) else {}
        meta = snapshot.get("meta", {})
        snapshot_revision = meta.get("config_revision")
        revision = config.get("revision", 1)
        # 影响数量只按「有时间标记的完整快照」计算；没有可比数据时由页面显示“待下一轮筛选”。
        # 严格模式不构造观察行（省钱），但仍报数量，所以优先取 negative_super_count。
        neg_status = (self.latest_result or {}).get("negative_super_status") if isinstance(self.latest_result, dict) else None
        neg_count = (self.latest_result or {}).get("negative_super_count") if isinstance(self.latest_result, dict) else None
        neg_rows = (self.latest_result or {}).get("negative_super_observations") if isinstance(self.latest_result, dict) else None
        if neg_status != "ok":
            neg_count = None
        elif not isinstance(neg_count, int):
            neg_count = len(neg_rows) if isinstance(neg_rows, list) else None
        # 快照的筛选方式：新结果由 board_scope_status 标记（所选交易板统一参与正式筛选）；
        # 旧快照带 extended_board_observations（当时两板仅观察）。两者口径不同，页面必须提示。
        snapshot_screen_method = self._snapshot_screen_method()
        return {
            "schema_version": config.get("schema_version", dashboard_settings.SCHEMA_VERSION),
            "revision": revision,
            "updated_at": config.get("updated_at"),
            "dashboard": dict(config.get("dashboard") or dashboard_settings.DEFAULT_DASHBOARD),
            "error": self.config_error,
            "snapshot_revision": snapshot_revision,
            "snapshot_view": meta.get("negative_super_view"),
            # 当前快照**实际**用的交易板范围；缺字段说明是旧快照（当时还没有这个口径）
            "snapshot_enabled_boards": meta.get("enabled_boards"),
            "snapshot_enabled_boards_label": meta.get("enabled_boards_label"),
            "snapshot_screen_method": snapshot_screen_method,
            "snapshot_board_scope_status": snapshot.get("board_scope_status"),
            "snapshot_board_scope_note": snapshot.get("board_scope_note"),
            "affected_count": neg_count,
            "affected_status": neg_status,
            "pending": bool(snapshot_revision is not None and snapshot_revision != revision),
            "running": bool(self.is_running or self.is_prewarming),
        }

    def apply_config(self, payload: dict) -> dict:
        """应用一次配置提交（带版本冲突检测）；成功后刷新运行参数。"""
        result = dashboard_settings.apply(payload)
        if result.get("ok"):
            self.config = result["config"]
            self.config_error = result.get("warning")
            self._apply_config_to_settings()
        return result

    def update_settings(self, updates: dict) -> dict:
        """兼容旧 /api/settings：配置类字段走统一校验并持久化，其余为会话级。

        旧接口不能成为绕过配置校验的后门：top/interval/network_mode 一律经
        dashboard_settings 白名单校验后落盘，非法值直接拒绝。
        """
        config_updates = {k: v for k, v in updates.items() if k in self._CONFIG_OWNED_KEYS}
        for k, v in updates.items():
            if k in self.settings and k not in self._CONFIG_OWNED_KEYS:
                self.settings[k] = v
        if config_updates:
            merged = dict((self.config or {}).get("dashboard") or dashboard_settings.DEFAULT_DASHBOARD)
            merged.update(config_updates)
            result = dashboard_settings.apply(
                {"revision": (self.config or {}).get("revision"), "dashboard": merged}
            )
            if not result.get("ok"):
                raise ValueError("；".join(result.get("errors") or ["配置非法"]))
            self.config = result["config"]
            self.config_error = result.get("warning")
            self._apply_config_to_settings()
        return dict(self.settings)

    @staticmethod
    def refresh_shadow_badges(result: dict) -> bool:
        """报告落盘后用只读判定器回填负超单观察行的影子徽标。

        只读：不写影子库、不调用 --record。历史不足、报告尚未落盘或该股不在
        判定器读取的低吸表中时，判定器返回 undetermined，页面显示“未完成判定”。
        """
        if not isinstance(result, dict) or result.get("negative_super_observations") is None:
            return False
        meta = result.setdefault("meta", {})
        stamp = str(meta.get("timestamp") or "")
        date_str = stamp[:10].replace("-", "")
        if len(date_str) != 8 or not date_str.isdigit():
            return False
        try:
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            from tools.detect_divergence_leader import evaluate_day_badges
            badges = evaluate_day_badges(date_str)
        except Exception as e:  # noqa: BLE001
            meta["shadow_badge_error"] = f"{type(e).__name__}: {e}"
            return False
        for row in result.get("negative_super_observations") or []:
            code = str(row.get("code") or "")
            row["shadow_badge"] = badges.get(code) or {
                "status": "undetermined",
                "reason": "不在判定器的低吸表中或报告尚未落盘",
            }
        meta["shadow_badge_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meta["shadow_badge_source"] = f"detect_divergence_leader/{date_str}"
        return True

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
            # 供两页共用的运行状态条：数据时点与可读数据源（meta 里的是文案，
            # market_fetch_status.source 是内部标识如 eastmoney_push2，不用它）。
            "data_timestamp": meta.get("timestamp"),
            "data_source": meta.get("source") or mfs.get("source"),
            # 当前快照的交易板范围（供状态条显示“本轮实际筛了什么范围”）
            "enabled_boards": meta.get("enabled_boards"),
            "enabled_boards_label": meta.get("enabled_boards_label"),
            # 范围结论：数据完整时无候选 = 「未符合条件」；降级/不完整 = 结果不可用。
            # 两者不能混为一谈，故把状态、候选数与整句结论一并交给页面。
            "board_scope_status": (self.latest_result or {}).get("board_scope_status"),
            "board_scope_candidates": (self.latest_result or {}).get("board_scope_candidates"),
            "board_scope_note": (self.latest_result or {}).get("board_scope_note"),
            # 快照的筛选口径：看板靠它提示“这份快照是旧口径、不可与当前结果比较”。
            # 与配置接口用同一个判定，避免只在工作台显示而看板静默。
            "snapshot_screen_method": self._snapshot_screen_method(),
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
            # 配置口径：当前生效版本 + 看板最新快照使用的版本（不同则待下一轮生效）
            "config_revision": (self.config or {}).get("revision"),
            "config_updated_at": (self.config or {}).get("updated_at"),
            "config_error": self.config_error,
            "negative_super_view": dashboard_settings.view_of(self.config),
            "negative_super_view_snapshot": meta.get("negative_super_view"),
            "config_revision_snapshot": meta.get("config_revision"),
            "negative_super_status": (self.latest_result or {}).get("negative_super_status")
            if isinstance(self.latest_result, dict) else None,
            # 防呆：这份快照是否真的跳过了公告检查（一票否决门禁失效）
            "announcement_check_skipped": _announcement_check_skipped(meta),
            "config_pending": bool(
                meta.get("config_revision") is not None
                and meta.get("config_revision") != (self.config or {}).get("revision")
            ),
        }


def _next_trading_open() -> datetime | None:
    """返回官方日历确认的下一个交易时段开始时间。"""
    now = datetime.now()
    try:
        result = _official_calendar().next_session(now)
        if result.status == "ok" and isinstance(result.data, dict) and result.data.get("start"):
            value = datetime.fromisoformat(str(result.data["start"]))
            return value.replace(tzinfo=None)
    except Exception:
        pass
    # 不把工作日推断成已确认交易日；上层状态会保留“待确认”，让用户手动诊断。
    return None


scheduler = ScreeningScheduler()
_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()
_server_shutdown_requested = False


def _attach_server(server: ThreadingHTTPServer) -> None:
    """Publish the active HTTP server before its scheduler can request shutdown."""
    global _server, _server_shutdown_requested
    with _server_lock:
        _server = server
        _server_shutdown_requested = False


def _detach_server(server: ThreadingHTTPServer) -> None:
    """Clear only the server instance owned by the exiting entry point."""
    global _server, _server_shutdown_requested
    with _server_lock:
        if _server is server:
            _server = None
            _server_shutdown_requested = False


def _shutdown_server() -> None:
    """Request shutdown from a safe thread; the serving thread closes its socket."""
    global _server_shutdown_requested
    with _server_lock:
        server = _server
        if server is None or _server_shutdown_requested:
            return
        _server_shutdown_requested = True

    def _shutdown() -> None:
        try:
            server.shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboard] server shutdown failed: {exc}", file=sys.stderr)

    try:
        threading.Thread(target=_shutdown, name="dashboard-http-shutdown", daemon=True).start()
    except Exception:
        with _server_lock:
            if _server is server:
                _server_shutdown_requested = False
        raise

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


def _query_flag(query: str, name: str) -> bool:
    """查询串里的布尔开关（如 ``?force=1``）。"""
    values = parse_qs(query).get(name) or []
    return any(str(value).strip().lower() in ("1", "true", "yes", "on") for value in values)


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
        elif path == "/style.css":
            self._serve_static("style.css")
        elif path == "/app.js":
            self._serve_static("app.js")
        elif path == "/common.css":
            self._serve_shared_static("common.css")
        elif path == "/common.js":
            self._serve_shared_static("common.js")
        elif path == "/api/data":
            self._serve_json(scheduler.latest_result or {"error": "waiting for first screening..."})
        elif path == "/api/status":
            self._serve_json(scheduler.get_status())
        elif path == "/api/update":
            # 只读快照：命中缓存纯本地返回，未命中丢给后台线程，接口不阻塞等网络。
            self._serve_json(update_check.snapshot(force=_query_flag(parsed.query, "force")))
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
        elif path == "/api/config":
            self._serve_json(scheduler.config_public())
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
        elif path == "/api/config/apply":
            if self._reject_cross_origin():
                return
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return
            result = scheduler.apply_config(payload if isinstance(payload, dict) else {})
            if result.get("ok"):
                code = 200
            elif result.get("conflict"):
                code = 409
            else:
                code = 400
            self._serve_json({
                "status": "ok" if result.get("ok") else "error",
                "errors": result.get("errors") or [],
                "conflict": bool(result.get("conflict")),
                "config": scheduler.config_public(),
            }, status=code)
        elif path == "/api/settings":
            # 旧接口保留给看板顶部会话级开关；配置类字段走同一套校验，不能绕过。
            if self._reject_cross_origin():
                return
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                updates = json.loads(body)
                new_settings = scheduler.update_settings(updates)
                self._serve_json({"status": "ok", "settings": new_settings})
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
            except ValueError as e:
                self.send_error(400, str(e))
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
        self._serve_static_from(STATIC_DIR, filename)

    def _serve_shared_static(self, filename: str) -> None:
        """导航与运行状态条的单一实现，两个入口共用同一份文件。"""
        self._serve_static_from(SHARED_STATIC_DIR, filename)

    def _serve_static_from(self, directory: Path, filename: str) -> None:
        filepath = directory / filename
        if not filepath.exists():
            self.send_error(404, f"File not found: {filename}")
            return
        content = filepath.read_bytes()
        ext = filepath.suffix
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        # 本机工具、文件很小：禁用缓存，改完前端刷新即生效（否则浏览器会拿旧的 CSS/JS）
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data: object, status: int = 200) -> None:
        content = json.dumps(
            _sanitize_json(data),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        self.send_response(status)
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

    def _same_origin_request(self) -> bool:
        """浏览器请求只允许来自页面自身来源；无 Origin 的命令行客户端照常放行。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == self.headers.get(
            "Host", ""
        ).lower()

    def _reject_cross_origin(self) -> bool:
        if self._same_origin_request():
            return False
        self.send_error(403, "Cross-origin requests are disabled")
        return True

    def log_message(self, format: str, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="A股实时筛选看板")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    # Auto-kill any stale process occupying the port
    import subprocess
    had_stale_process = False
    try:
        stale = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
        if stale.stdout.strip():
            had_stale_process = True
            for pid in stale.stdout.strip().split("\n"):
                os.kill(int(pid), 9)
                print(f"[dashboard] killed stale process {pid} on port {PORT}", file=sys.stderr)
            time.sleep(1)
    except Exception:
        pass

    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    _attach_server(server)
    try:
        scheduler.start()

        import atexit
        atexit.register(scheduler._archive_markdown)

        url = f"http://localhost:{PORT}"
        print(f"[dashboard] server running at {url}")
        print(f"[dashboard] trading hours: {is_trading_hours()}")
        print(f"[dashboard] auto-refresh: every {scheduler.settings['interval']}s during trading")
        print("[dashboard] auto-shutdown at 15:15 on weekdays")
        print("[dashboard] press Ctrl+C to stop")

        # A previous dashboard process may have left a usable browser tab behind.
        # Do not create another tab on every restart; callers can also suppress
        # browser handling explicitly with --no-browser.
        if not args.no_browser and not had_stale_process:
            try:
                webbrowser.open_new_tab(url)
            except Exception:
                pass

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[dashboard] shutting down...")
            scheduler._archive_markdown()
            # serve_forever has already unwound; shutdown() belongs to a separate
            # thread and is unnecessary for this KeyboardInterrupt path.
    finally:
        scheduler.stop()
        try:
            server.server_close()
        finally:
            _detach_server(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
