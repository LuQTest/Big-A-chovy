#!/usr/bin/env python3
"""A 股 B/S 网络工作台服务器（跨平台，Windows 可用）。

在 realtime_dashboard（实时看板）基础上扩展为完整 B/S 架构，单端口提供：
  1. 实时看板      —— 原有页面与 API 完全保留（/ 与 /api/data 等）
  2. 筛选工作台    —— 一次性筛选任务（后台线程执行 + 前端轮询），等价原 GUI 能力
  3. 报告库        —— 浏览 筛选结果/ 下所有 Markdown 报告
  4. 工具箱        —— 行情 / 基本面 / 持仓 / 报告扫描 / T+1 验证 / 单股跟踪

设计要点：
  - 仅用 Python 标准库 HTTP 服务；筛选经 realtime_engine.run_screening 与
    看板共用同一条已验证管线（含 network_path 择优、降级保留快照）。
  - 与看板共享同一个 ScreeningScheduler 与筛选锁：工作台手动任务和看板自动
    刷新互斥，避免并发跑引擎造成数据竞争。
  - 跨平台：不依赖 macOS 的 scutil/pgrep/lsof（Windows 下端口清理改用 netstat）；
    Windows 需要 tzdata 包提供 IANA 时区（代码内自动提示）。

运行：
  python web_workbench.py [--port 8765] [--no-browser]
  浏览器打开 http://localhost:8765/workbench （实时看板仍在 /）
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote


def _smart_unquote(s: str) -> str:
    """Query 值智能解码：优先 UTF-8（浏览器/标准客户端），回退 GBK（Windows 中文
    命令行客户端如 git-bash→curl 会按系统代码页发送），再回退原始字节还原。
    不用 parse_qs：它 errors='replace' 会把无法解码的字节提前毁成 U+FFFD。"""
    results: list[str] = []
    if "%" in s:
        for enc in ("utf-8", "gbk"):
            try:
                results.append(unquote(s, encoding=enc, errors="strict"))
            except UnicodeDecodeError:
                pass
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        raw = None
    if raw is not None and "%" not in s:
        for enc in ("utf-8", "gbk"):
            try:
                results.append(raw.decode(enc))
            except UnicodeDecodeError:
                pass
    for r in results:
        if "\ufffd" not in r:
            return r
    return unquote(s, errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))  # tools 包（tools.rule_config 等）

import realtime_dashboard as dash  # noqa: E402  （导入时完成网络路径预热）

REPORTS_DIR = PROJECT_ROOT / "筛选结果"
WORKBENCH_STATIC = SCRIPT_DIR / "workbench_static"
SCREENING_TIMEOUT_WB = 900  # 一次性筛选硬超时（秒）。Windows 冷缓存+SSL握手慢，实测数百秒；
# 引擎 K 线缓存预热后，后续轮次会快很多


def _ensure_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def decode_console_bytes(data: bytes | str | None) -> str:
    """Decode Windows console output without letting locale decoding abort startup.

    Chinese Windows commonly emits GBK/CP936 for commands such as ``netstat``;
    UTF-8 is kept as a fallback for newer terminals and localized environments.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("gbk", "utf-8", "utf-8-sig"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _kill_stale_port_windows(port: int) -> None:
    """Windows 下清理占用端口的残留进程（netstat + taskkill，尽力而为）。"""
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=False, timeout=10,
        )
        out = decode_console_bytes(completed.stdout)
    except Exception:
        return
    pids: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        # TCP    0.0.0.0:8765    0.0.0.0:0    LISTENING    12345
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            local = parts[1]
            if local.rsplit(":", 1)[-1] == str(port):
                pids.add(parts[4])
    my_pid = str(os.getpid())
    for pid in pids - {my_pid}:
        if not pid.isdigit():
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", pid],
                capture_output=True,
                text=False,
                timeout=10,
            )
            print(f"[workbench] killed stale process {pid} on port {port}", file=sys.stderr)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 一次性筛选任务管理
# ---------------------------------------------------------------------------
class ScreenJob:
    """一次性筛选后台任务。与看板共享筛选锁，同一时刻只允许一个任务。"""

    def __init__(self) -> None:
        self.state = "idle"  # idle | running | done | error
        self.params: dict = {}
        self.result: dict | None = None
        self.md_text: str | None = None
        self.md_path: str | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self.elapsed: float | None = None
        self._timeout_hit = False
        self.zombie = False  # 超时后引擎线程仍在后台跑（Python 无法杀线程）

    def start(self, params: dict) -> dict:
        if self.state == "running" or self.zombie:
            return {"status": "already_running" if self.state == "running" else "busy",
                    "reason": "上一次筛选仍在后台执行中，等它结束后再试" if self.zombie else None}
        # K线预热中不开新任务：预热会占满网络，并发筛选只会互相拖慢（实测两者一起跑双超时）
        if dash.scheduler.is_prewarming:
            return {"status": "busy", "reason": "K线预热中，请等预热完成后再试（首次启动约需几分钟）"}
        # 与看板自动刷新互斥：拿不到锁说明看板正在筛选
        if not dash.scheduler._screening_lock.acquire(blocking=False):
            return {"status": "busy", "reason": "看板正在自动刷新筛选，请稍后再试"}
        self._screening_lock_held = True
        self.params = params
        self.state = "running"
        self.error = None
        self.result = None
        self.md_text = None
        self.md_path = None
        self._timeout_hit = False
        self.started_at = time.time()
        self.elapsed = None
        threading.Thread(target=self._run, daemon=True).start()
        return {"status": "started"}

    def _run(self) -> None:
        try:
            from realtime_engine import run_screening as engine_run

            modes = set(self.params.get("modes") or ["strict", "low", "watchlist"])
            ctx: dict = {}

            def _worker() -> None:
                try:
                    t0 = time.time()
                    ctx["result"] = engine_run(
                        modes=modes,
                        workers=6,
                        top=int(self.params.get("top") or 15),
                        skip_announcements=bool(self.params.get("skip_announcements")),
                        skip_capital_ranking=bool(self.params.get("skip_capital_ranking")),
                        network_mode=self.params.get("network_mode") or "auto",
                    )
                    ctx["elapsed"] = time.time() - t0
                except Exception as e:  # noqa: BLE001
                    ctx["error"] = f"{type(e).__name__}: {e}"

            th = threading.Thread(target=_worker, daemon=True)
            th.start()
            th.join(timeout=SCREENING_TIMEOUT_WB)
            if th.is_alive():
                # Python 无法杀线程：引擎继续在后台跑完。锁不移交，由收割线程等
                # 引擎真正结束后再释放，避免看板新任务与僵尸引擎竞争模块级状态。
                self._timeout_hit = True
                self.state = "error"
                self.error = (f"筛选超时（>{SCREENING_TIMEOUT_WB}s）。后台任务仍在执行中，"
                              f"完成后自动释放；Windows 冷缓存全模式实测约 5~6 分钟")
                self.zombie = True

                def _reap() -> None:
                    th.join()
                    self.zombie = False
                    self.elapsed = time.time() - (self.started_at or time.time())
                    print(f"[workbench] zombie engine finished at {self.elapsed:.0f}s; lock released",
                          file=sys.stderr)
                    try:
                        dash.scheduler._screening_lock.release()
                    except Exception:
                        pass

                threading.Thread(target=_reap, daemon=True).start()
                return  # 锁由 _reap 释放，不走 finally
            if "error" in ctx:
                self.state = "error"
                self.error = str(ctx["error"])
                return
            self.result = ctx["result"]
            self.elapsed = ctx.get("elapsed", 0.0)
            if "error" in (self.result or {}):
                self.state = "error"
                self.error = str(self.result.get("error"))
                return
            self._render_and_save_md()
            # 同步给看板：/api/data 与实时看板页面展示最新手动筛选结果
            dash.scheduler.latest_result = self.result
            self.state = "done"
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"
        finally:
            if not self.zombie:
                self.elapsed = time.time() - (self.started_at or time.time())
                try:
                    dash.scheduler._screening_lock.release()
                except Exception:
                    pass

    def _render_and_save_md(self) -> None:
        try:
            import a_share_daily_screen as screen
            md = screen.render_markdown(self.result or {})
            min5 = dash.ScreeningScheduler._render_min5_table(self.result or {})
            if min5:
                md += "\n" + min5 + "\n"
            self.md_text = md
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            path = REPORTS_DIR / f"A股筛选结果_{stamp}.md"
            path.write_text(md, encoding="utf-8")
            # API 只返回项目内相对路径，避免把本机用户名/绝对路径暴露给浏览器。
            self.md_path = str(path.relative_to(PROJECT_ROOT))
            dash.scheduler.latest_md_path = str(path)
            print(f"[workbench] markdown saved: {path}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[workbench] markdown save error: {e}", file=sys.stderr)

    def status(self) -> dict:
        meta = (self.result or {}).get("meta", {}) if isinstance(self.result, dict) else {}
        return {
            "state": self.state,
            "params": self.params,
            "elapsed": round(self.elapsed, 1) if self.elapsed else (
                round(time.time() - self.started_at, 1) if self.started_at and self.state == "running" else None),
            "error": self.error,
            "timeout": self._timeout_hit,
            "engine_still_running": self.zombie,
            "md_path": self.md_path,
            "timestamp": meta.get("timestamp"),
            "degraded": bool(meta.get("market_data_degraded")),
            "has_result": self.result is not None,
        }


JOB = ScreenJob()


# ---------------------------------------------------------------------------
# 工具箱包装（复用 tools/ 中已验证的实现）
# ---------------------------------------------------------------------------
def _capture_stdout(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def _tool_quote(codes: list[str], minute: bool, kline: bool) -> dict:
    from tools.query_quote import (
        fetch_realtime_quotes, fetch_minute_data, fetch_daily_kline, normalize_code,
    )
    norm = [normalize_code(c) for c in codes]
    norm = [c for c in norm if c]
    out: dict = {"codes": norm, "quotes": {}, "minute": {}, "kline": {}}
    if not norm:
        out["error"] = "未提供有效代码"
        return out
    out["quotes"] = fetch_realtime_quotes(norm)
    if minute:
        for c in norm:
            out["minute"][c] = fetch_minute_data(c)
    if kline:
        for c in norm:
            out["kline"][c] = fetch_daily_kline(c, count=10)
    return out


def _tool_scan(date: str | None, latest: int, file: str | None) -> dict:
    from tools.scan_reports import scan_single_file
    from tools.report_parser import get_report_files

    if file:
        p = _resolve_report_path(file)
        if p is None:
            return {"error": "file 参数只允许访问 筛选结果/ 下的 Markdown 报告"}
        files = [p]
    else:
        files = get_report_files(str(REPORTS_DIR), date) or []
        if latest > 0:
            files = files[-latest:]
    results = []
    for f in files:
        try:
            res = scan_single_file(str(f))
            results.append({
                "file": res.get("file"),
                "time": res.get("time"),
                "passed_5": res.get("passed_5", []),
                "near_4": res.get("near_4", []),
            })
        except Exception as e:  # noqa: BLE001
            results.append({
                "file": _display_project_path(f),
                "error": f"{type(e).__name__}: {e}",
            })
    return {"count": len(results), "results": results}


def _tool_position(date: str | None) -> dict:
    from tools.get_position import get_latest_decision_file, load_position_snapshot
    fpath = get_latest_decision_file(date)
    if not fpath:
        return {"error": f"未找到决策记录文件: date={date}", "found": False}
    snap = load_position_snapshot(fpath)
    return {"found": True, "file": _display_project_path(fpath), "snapshot": snap}


def _tool_verify_t1(date: str) -> dict:
    from tools.verify_t1 import verify_watchlist_t1
    if not date:
        return {"error": "缺少 date 参数（YYYYMMDD）"}
    return {"text": _capture_stdout(verify_watchlist_t1, date)}


def _tool_track(code: str, date: str | None) -> dict:
    from tools.track_stock import track_stock_timeline
    if not code:
        return {"error": "缺少 code 参数"}
    return {"text": _capture_stdout(track_stock_timeline, code, date)}


def _tool_financials(code: str) -> dict:
    from tools.query_financials import query_financial_profile
    if not code:
        return {"error": "缺少 code 参数"}
    return query_financial_profile(code)


# ---------------------------------------------------------------------------
# HTTP Handler：在原有 DashboardHandler 上追加工作台路由
# ---------------------------------------------------------------------------
def _sanitize_json(obj):
    return dash._sanitize_json(obj)


def _display_project_path(path: str | Path) -> str:
    """Return a stable project-relative path without leaking local prefixes."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return Path(path).name


def _resolve_report_path(rel: str) -> Path | None:
    """Resolve a report path without allowing absolute paths or symlink escape."""
    if not rel or not rel.strip():
        return None
    raw = rel.strip()
    p = (PROJECT_ROOT / raw).resolve()
    try:
        p.relative_to(REPORTS_DIR.resolve())
    except ValueError:
        return None
    if p.suffix.lower() != ".md":
        return None
    return p


class WorkbenchHandler(dash.DashboardHandler):

    def _same_origin_request(self) -> bool:
        """Allow browser API calls only from the page's own origin.

        The workbench serves private local reports and position snapshots.  A
        wildcard CORS header would let an unrelated webpage read those values
        from localhost, so cross-origin browser requests are rejected.
        Command-line clients without an Origin header remain supported.
        """
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

    def _send_cors_headers(self) -> None:
        """Do not emit the inherited wildcard CORS policy."""
        self.send_header("X-Content-Type-Options", "nosniff")

    # -- 静态资源 ----------------------------------------------------------
    def _serve_workbench_static(self, filename: str) -> None:
        filepath = WORKBENCH_STATIC / filename
        if not filepath.exists():
            self.send_error(404, f"File not found: {filename}")
            return
        content = filepath.read_bytes()
        ctype = dash.MIME_TYPES.get(filepath.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    # -- API ----------------------------------------------------------------
    def _api_wb_get(self, path: str, query: str) -> bool:
        """处理 /api/wb/* GET 路由。返回 True 表示已处理。"""
        params: dict[str, list[str]] = {}
        for part in query.split("&"):
            if not part:
                continue
            k, _, v = part.partition("=")
            params.setdefault(k, []).append(_smart_unquote(v))
        def g1(k: str) -> str:
            return (params.get(k) or [""])[0]

        if path == "/api/wb/job":
            self._serve_json(JOB.status())
        elif path == "/api/wb/result":
            if JOB.result is None:
                self._serve_json({"error": "尚无结果，请先运行筛选"})
            else:
                self._serve_json(JOB.result)
        elif path == "/api/wb/report":
            if JOB.md_text is None:
                self._serve_json({"error": "尚无 Markdown 报告"})
            else:
                self._serve_text(JOB.md_text, "text/markdown; charset=utf-8")
        elif path == "/api/wb/reports":
            self._serve_json(self._list_reports())
        elif path == "/api/wb/md":
            self._serve_report_file(g1("path"))
        elif path == "/api/wb/quote":
            codes = [c for c in re.split(r"[,\s，、]+", g1("codes")) if c.strip()]
            self._serve_json(_tool_quote(
                codes, minute=g1("minute") in ("1", "true"), kline=g1("kline") in ("1", "true")))
        elif path == "/api/wb/scan":
            latest = int(g1("latest") or 0)
            self._serve_json(_tool_scan(g1("date") or None, latest, g1("file") or None))
        elif path == "/api/wb/position":
            self._serve_json(_tool_position(g1("date") or None))
        elif path == "/api/wb/verify_t1":
            self._serve_json(_tool_verify_t1(g1("date")))
        elif path == "/api/wb/track":
            self._serve_json(_tool_track(g1("code"), g1("date") or None))
        elif path == "/api/wb/financials":
            self._serve_json(_tool_financials(g1("code")))
        else:
            return False
        return True

    @staticmethod
    def _list_reports() -> dict:
        files = []
        if REPORTS_DIR.exists():
            for p in REPORTS_DIR.rglob("*.md"):
                try:
                    resolved = p.resolve()
                    resolved.relative_to(REPORTS_DIR.resolve())
                    if not resolved.is_file():
                        continue
                    st = p.stat()
                    files.append({
                        "path": str(p.relative_to(PROJECT_ROOT)),
                        "name": p.name,
                        "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except OSError:
                    continue
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return {"count": len(files), "files": files[:500]}

    def _serve_report_file(self, rel: str) -> None:
        if not rel:
            self._serve_json({"error": "缺少 path 参数"})
            return
        p = _resolve_report_path(rel)
        if p is None:
            self._serve_json({"error": "路径越界：只允许访问 筛选结果/ 下的 Markdown 报告"})
            return
        if not p.is_file():
            self._serve_json({"error": "文件不存在或不是普通文件"})
            return
        try:
            self._serve_text(p.read_text(encoding="utf-8"), "text/markdown; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._serve_json({"error": f"读取失败: {e}"})

    def do_GET(self) -> None:  # noqa: D102
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/") and self._reject_cross_origin():
            return
        if path == "/workbench" or path == "/workbench.html":
            self._serve_workbench_static("index.html")
        elif path == "/workbench.js":
            self._serve_workbench_static("app.js")
        elif path == "/workbench.css":
            self._serve_workbench_static("style.css")
        elif path.startswith("/api/wb/"):
            if not self._api_wb_get(path, parsed.query):
                self.send_error(404, "Not found")
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: D102
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and self._reject_cross_origin():
            return
        if parsed.path == "/api/wb/screen/run":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return
            self._serve_json(JOB.start(body if isinstance(body, dict) else {}))
        else:
            super().do_POST()

    def do_OPTIONS(self) -> None:  # noqa: D102
        if self._reject_cross_origin():
            return
        super().do_OPTIONS()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="A股 B/S 网络工作台")
    parser.add_argument("--port", type=int, default=dash.PORT)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址，默认仅本机访问；需要局域网访问时显式指定 0.0.0.0",
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-dashboard-refresh", action="store_true",
                        help="不启动看板盘中自动刷新/预热（纯手动筛选模式，适合低配机或非交易时段）")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "[workbench] WARNING: non-loopback host exposes reports and position "
            "snapshots to the network; use only on a trusted LAN",
            file=sys.stderr,
        )

    # Windows 下 lsof 不存在，用 netstat 清理残留端口
    if sys.platform == "win32":
        _kill_stale_port_windows(args.port)

    try:
        server = ThreadingHTTPServer((args.host, args.port), WorkbenchHandler)
    except OSError as e:
        print(f"[workbench] 端口 {args.port} 被占用（{e}）。请先停掉旧进程："
              f" web_workbench.py --port {args.port + 1}", file=sys.stderr)
        return 1
    # 接通调度器的收盘 auto-shutdown：它停的是 dash._server 指向的实例
    dash._server = server

    if args.no_dashboard_refresh:
        print("[workbench] dashboard auto-refresh disabled (--no-dashboard-refresh)", file=sys.stderr)
    else:
        dash.scheduler.start()  # 看板自动刷新（交易时段内每 interval 秒一轮）
    url = f"http://localhost:{args.port}"
    print(f"[workbench] server running at {url}")
    print(f"[workbench] 工作台: {url}/workbench   实时看板: {url}/")
    print(f"[workbench] trading hours: {dash.is_trading_hours()}")
    print("[workbench] press Ctrl+C to stop")

    if not args.no_browser:
        try:
            webbrowser.open(f"{url}/workbench")
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[workbench] shutting down...")
        dash.scheduler._archive_markdown()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
