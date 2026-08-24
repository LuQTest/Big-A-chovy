#!/usr/bin/env python3
"""Small Tkinter wrapper for the A-share screening helper."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, END, IntVar, StringVar, Tk, Toplevel, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from a_share_daily_screen import build_url_opener, NETWORK_MODE


SCRIPT_DIR = Path(__file__).resolve().parent
SCREEN_SCRIPT = SCRIPT_DIR / "a_share_daily_screen.py"
HOLDINGS_FILE = SCRIPT_DIR / "holdings.json"
SETTINGS_FILE = SCRIPT_DIR / "gui_settings.json"
OUTPUT_DIR = Path("/Users/luqiang/Documents/Others/股票/筛选结果")


def default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"A股筛选结果_{stamp}.md"


def _fetch_sector_boards() -> dict[str, dict]:
    """Fetch all industry board indices from East Money. Returns {name: board_data}."""
    import urllib.request, ssl
    result: dict[str, dict] = {}
    for page in range(1, 6):  # 5 pages × 100 per page covers all ~500 boards
        params = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f3",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f4,f8,f104,f105,f124",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        ok = False
        for host in ["push2.eastmoney.com", "82.push2.eastmoney.com"]:
            url = f"https://{host}/api/qt/clist/get?{query}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                ctx = ssl._create_unverified_context()
                with build_url_opener(NETWORK_MODE).open(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                boards = ((data.get("data") or {}).get("diff") or [])
                for b in boards:
                    name = b.get("f14", "")
                    if name:
                        result[name] = {
                            "name": name,
                            "change": b.get("f3", 0),
                            "price": b.get("f2"),
                            "up_count": b.get("f104", 0),
                            "down_count": b.get("f105", 0),
                        }
                ok = True
                break
            except Exception:
                continue
        if not ok or len(((data.get("data") or {}).get("diff") or [])) < 100:
            break
    return result


def _fetch_stock_industries(codes: list[str]) -> dict[str, str]:
    """Fetch industry names for stock codes. Returns {code: industry}."""
    import urllib.request, ssl
    secids = []
    for code in codes:
        if code.startswith("6"):
            secids.append(f"1.{code}")
        elif code.startswith(("0", "3")):
            secids.append(f"0.{code}")
        else:
            secids.append(f"1.{code}")
    params = {
        "fltt": "2", "invt": "2",
        "fields": "f12,f100",
        "secids": ",".join(secids),
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    urls = [
        f"https://push2.eastmoney.com/api/qt/ulist.np/get?{query}",
        f"https://82.push2.eastmoney.com/api/qt/ulist.np/get?{query}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            ctx = ssl._create_unverified_context()
            with build_url_opener(NETWORK_MODE).open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = {}
            for item in ((data.get("data") or {}).get("diff") or []):
                code = str(item.get("f12", ""))
                industry = item.get("f100", "")
                if code and industry and industry != "-":
                    result[code] = industry
            return result
        except Exception:
            continue
    return {}


class App:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("A股短线筛选")
        self._load_window_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # macOS workarounds for background-window click issues
        self.root.bind("<FocusIn>", self._on_focus_in)
        self.root.bind("<Map>", self._on_focus_in)
        self._disable_app_nap()
        self._keepalive()

        # --- state variables ---
        self._syncing = False
        self._process = None
        self._running = False
        self._result_queue: queue.Queue[tuple] = queue.Queue()
        self._last_content = ""
        self.mode_all = BooleanVar(value=False)
        self.mode_strict = BooleanVar(value=True)
        self.mode_low = BooleanVar(value=False)
        self.mode_watchlist = BooleanVar(value=False)
        self.capital_ranking = BooleanVar(value=True)
        self.check_ann = BooleanVar(value=True)
        self.network_mode = StringVar(value="自动")
        self.top = IntVar(value=10)
        self.output_path = StringVar(value=str(default_output_path()))
        self.holdings: list[dict] = self._load_holdings()
        self._user_chosen_path = False

        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        # --- screening content checkboxes ---
        screen_frame = ttk.LabelFrame(frame, text="快速筛选", padding=8)
        screen_frame.pack(fill="x", pady=(0, 6))

        ttk.Checkbutton(screen_frame, text="超短池 + 趋势观察池（推荐）", variable=self.mode_strict,
                        command=self._on_mode_toggle).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(screen_frame, text="主力资金二次排序", variable=self.capital_ranking).pack(
            side="left", padx=(4, 16)
        )
        ttk.Label(screen_frame, text="趋势确认池保留原有严格条件；双池交集=超短池∩趋势观察池").pack(side="left")

        advanced_frame = ttk.LabelFrame(frame, text="高级筛选（按需开启，会增加查询时间）", padding=8)
        advanced_frame.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(advanced_frame, text="低吸A/B/C", variable=self.mode_low,
                        command=self._on_mode_toggle).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(advanced_frame, text="明日观察池", variable=self.mode_watchlist,
                        command=self._on_mode_toggle).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(advanced_frame, text="公告风险检查（较慢）", variable=self.check_ann).pack(side="left", padx=(4, 16))

        # --- options row ---
        opt_row = ttk.Frame(frame)
        opt_row.pack(fill="x", pady=(6, 6))

        ttk.Label(opt_row, text="每组数量").pack(side="left", padx=(4, 2))
        ttk.Spinbox(opt_row, from_=3, to=30, textvariable=self.top, width=6).pack(side="left", padx=(2, 18))
        ttk.Label(opt_row, text="网络连接").pack(side="left", padx=(0, 4))
        ttk.Combobox(
            opt_row,
            textvariable=self.network_mode,
            values=("自动", "强制直连", "系统代理"),
            state="readonly",
            width=10,
        ).pack(side="left")
        ttk.Label(opt_row, text="自动会依次尝试代理和直连").pack(side="left", padx=(6, 0))

        # --- holdings section ---
        hold_frame = ttk.LabelFrame(frame, text="持仓管理", padding=4)
        hold_frame.pack(fill="x", pady=(4, 4))

        hold_inner = ttk.Frame(hold_frame)
        hold_inner.pack(fill="x")

        self.hold_tree = ttk.Treeview(
            hold_inner, columns=("code", "name", "cost", "qty", "price", "pnl_pct", "pnl_amt"),
            show="headings", height=3, selectmode="browse",
        )
        self.hold_tree.heading("code", text="代码")
        self.hold_tree.heading("name", text="名称")
        self.hold_tree.heading("cost", text="成本价")
        self.hold_tree.heading("qty", text="数量")
        self.hold_tree.heading("price", text="现价")
        self.hold_tree.heading("pnl_pct", text="盈亏%")
        self.hold_tree.heading("pnl_amt", text="盈亏额")
        self.hold_tree.column("code", width=70, anchor="center")
        self.hold_tree.column("name", width=80, anchor="center")
        self.hold_tree.column("cost", width=70, anchor="e")
        self.hold_tree.column("qty", width=60, anchor="e")
        self.hold_tree.column("price", width=70, anchor="e")
        self.hold_tree.column("pnl_pct", width=70, anchor="e")
        self.hold_tree.column("pnl_amt", width=80, anchor="e")
        self.hold_tree.tag_configure("profit", foreground="#27ae60")
        self.hold_tree.tag_configure("loss", foreground="#e74c3c")
        self.hold_tree.pack(side="left", fill="x", expand=True)

        hold_btns = ttk.Frame(hold_inner)
        hold_btns.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(hold_btns, text="新增", command=self.hold_add, width=6).pack(pady=2)
        ttk.Button(hold_btns, text="编辑", command=self.hold_edit, width=6).pack(pady=2)
        ttk.Button(hold_btns, text="删除", command=self.hold_delete, width=6).pack(pady=2)
        ttk.Button(hold_btns, text="查询盈亏", command=self.hold_query_pnl, width=6).pack(pady=2)

        self._refresh_hold_tree()

        # --- output path ---
        path_row = ttk.Frame(frame)
        path_row.pack(fill="x", pady=(6, 8))
        ttk.Label(path_row, text="保存到").pack(side="left")
        ttk.Entry(path_row, textvariable=self.output_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(path_row, text="选择", command=self.choose_file).pack(side="left")

        # --- action row ---
        action_row = ttk.Frame(frame)
        action_row.pack(fill="x", pady=(0, 10))
        self.run_button = ttk.Button(action_row, text="开始双池筛选（观察交集）", command=self.run)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(action_row, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="清空", command=lambda: self.text.delete("1.0", END)).pack(side="left", padx=8)
        ttk.Button(action_row, text="打开所在文件夹", command=self.open_output_folder).pack(side="left")

        self.status = StringVar(value="准备就绪")
        ttk.Label(action_row, textvariable=self.status).pack(side="left", padx=10)

        # --- progress bar (hidden until screening starts) ---
        self._progress_frame = ttk.Frame(frame)
        self._progress_frame.pack(fill="x", pady=(0, 4))
        self._progress_var = IntVar(value=0)
        self._progress_bar = ttk.Progressbar(
            self._progress_frame, variable=self._progress_var,
            maximum=100, mode="determinate", length=400,
        )
        self._progress_bar.pack(side="left", fill="x", expand=True)
        self._phase_label = StringVar(value="")
        ttk.Label(self._progress_frame, textvariable=self._phase_label, width=30).pack(side="left", padx=(8, 0))
        self._progress_frame.pack_forget()  # hidden by default

        self.text = ScrolledText(
            frame, wrap="word", height=30,
            font=("Menlo", 14),
            bg="#1e1e2e", fg="#cdd6f4",
            insertbackground="#cdd6f4",
            selectbackground="#45475a",
            relief="flat", borderwidth=0,
            spacing1=2, spacing3=2,
        )
        self.text.pack(fill="both", expand=True)
        # --- text tags for colored output ---
        self.text.tag_configure("header", foreground="#89b4fa", font=("Menlo", 15, "bold"))
        self.text.tag_configure("meta", foreground="#a6adc8")
        self.text.tag_configure("cls_A", foreground="#a6e3a1", font=("Menlo", 14, "bold"))
        self.text.tag_configure("cls_B", foreground="#fab387", font=("Menlo", 14, "bold"))
        self.text.tag_configure("cls_C", foreground="#6c7086")
        self.text.tag_configure("risk", foreground="#f38ba8")
        self.text.tag_configure("num", foreground="#89dceb")
        self.text.tag_configure("table_border", foreground="#585b70")
        self.text.tag_configure("flow_good", foreground="#a6e3a1", font=("Menlo", 14, "bold"))
        self.text.tag_configure("flow_maybe", foreground="#f9e2af")
        self.text.tag_configure("flow_diverge", foreground="#fab387")
        self.text.tag_configure("flow_risk", foreground="#f38ba8", font=("Menlo", 14, "bold"))
        self.root.after(100, self._poll_worker_results)

    # ── window geometry persistence ─────────────────────────────

    def _on_focus_in(self, event: object) -> None:
        """Force UI refresh when window regains focus or becomes visible."""
        self.root.update_idletasks()
        self.root.lift()
        try:
            self.root.tk.call("::tk::unsupported::MacWindowStyle", "style",
                              self.root._w, "document", "closeBox collapseBox")
        except Exception:
            pass

    def _disable_app_nap(self) -> None:
        """Prevent macOS from throttling the event loop when window is in background."""
        try:
            import ctypes
            import ctypes.util
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            process_cls = objc.objc_getClass(b"NSProcessInfo")
            sel_info = objc.sel_registerName(b"processInfo")
            sel_disable = objc.sel_registerName(b"beginActivityWithOptions:reason:")
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            info = objc.objc_msgSend(process_cls, sel_info)
            reason = ctypes.c_void_p
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_uint64, ctypes.c_void_p]
            # NSActivityUserInitiatedAllowingIdleSystemSleep = 0x00FFFFFF
            objc.objc_msgSend(info, sel_disable, 0x00FFFFFF, reason)
        except Exception:
            pass  # non-macOS or ctypes unavailable; harmless

    def _keepalive(self) -> None:
        """Periodic tick to keep the Tkinter event loop responsive."""
        self.root.update_idletasks()
        self.root.after(2000, self._keepalive)

    def _load_window_geometry(self) -> None:
        default = "1200x900"
        try:
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            geo = settings.get("geometry", default)
            self.root.geometry(geo)
        except Exception:
            self.root.geometry(default)

    def _on_close(self) -> None:
        try:
            geo = self.root.geometry()
            settings = {}
            try:
                settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            settings["geometry"] = geo
            SETTINGS_FILE.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        self.root.destroy()

    # ── holding management ──────────────────────────────────────

    @staticmethod
    def _load_holdings() -> list[dict]:
        try:
            data = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_holdings(self) -> None:
        HOLDINGS_FILE.write_text(
            json.dumps(self.holdings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _refresh_hold_tree(self) -> None:
        self.hold_tree.delete(*self.hold_tree.get_children())
        for h in self.holdings:
            price = h.get("price")
            cost = h.get("cost", 0)
            qty = h.get("qty", 0)
            pnl_pct = ((price - cost) / cost * 100) if (price and cost) else None
            pnl_amt = ((price - cost) * qty) if (price and cost and qty) else None
            tag = ""
            if pnl_pct is not None:
                tag = "profit" if pnl_pct >= 0 else "loss"
            self.hold_tree.insert("", "end", values=(
                h.get("code", ""),
                h.get("name", ""),
                f"{cost:.2f}" if cost else "",
                qty if qty else "",
                f"{price:.2f}" if price else "-",
                f"{pnl_pct:+.2f}%" if pnl_pct is not None else "-",
                f"{pnl_amt:+,.0f}" if pnl_amt is not None else "-",
            ), tags=(tag,))

    def hold_query_pnl(self) -> None:
        """Fetch current prices for all holdings and calculate P&L."""
        if not self.holdings:
            messagebox.showinfo("提示", "暂无持仓记录")
            return

        self.status.set("查询持仓行情...")
        self.root.update_idletasks()

        # Build secids: 1.XXXXXX for SH(60), 0.XXXXXX for SZ(00/30)
        secids = []
        for h in self.holdings:
            code = h.get("code", "")
            if code.startswith("6"):
                secids.append(f"1.{code}")
            elif code.startswith(("0", "3")):
                secids.append(f"0.{code}")
            else:
                secids.append(f"1.{code}")  # default SH

        try:
            import urllib.request
            import ssl
            params = {
                "fltt": "2", "invt": "2",
                "fields": "f12,f14,f2,f3",
                "secids": ",".join(secids),
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            }
            query = "&".join(f"{k}={v}" for k, v in params.items())
            urls = [
                f"https://82.push2.eastmoney.com/api/qt/ulist.np/get?{query}",
                f"https://push2.eastmoney.com/api/qt/ulist.np/get?{query}",
            ]
            data = None
            for url in urls:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    ctx = ssl._create_unverified_context()
                    with build_url_opener(NETWORK_MODE).open(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    break
                except Exception:
                    continue

            if data is None:
                self.status.set("行情接口失败")
                return

            # Map code → current price
            price_map: dict[str, float] = {}
            diff = (data.get("data") or {}).get("diff") or []
            for item in diff:
                code = str(item.get("f12", ""))
                price = item.get("f2")
                if code and isinstance(price, (int, float)) and price > 0:
                    price_map[code] = price

            # Update holdings with current prices
            updated = 0
            for h in self.holdings:
                code = h.get("code", "")
                if code in price_map:
                    h["price"] = price_map[code]
                    updated += 1

            self._save_holdings()
            self._refresh_hold_tree()
            self.status.set(f"持仓行情已更新 ({updated}/{len(self.holdings)})")

        except Exception as exc:
            self.status.set(f"查询失败: {exc}")

    def _silent_refresh_prices(self) -> None:
        """Silently refresh holdings prices without UI updates. Called during screening."""
        if not self.holdings:
            return
        try:
            import urllib.request, ssl
            secids = []
            for h in self.holdings:
                code = h.get("code", "")
                if code.startswith("6"):
                    secids.append(f"1.{code}")
                elif code.startswith(("0", "3")):
                    secids.append(f"0.{code}")
                else:
                    secids.append(f"1.{code}")
            params = {
                "fltt": "2", "invt": "2",
                "fields": "f12,f14,f2,f3",
                "secids": ",".join(secids),
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            }
            query = "&".join(f"{k}={v}" for k, v in params.items())
            for host in ["push2.eastmoney.com", "82.push2.eastmoney.com"]:
                url = f"https://{host}/api/qt/ulist.np/get?{query}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    ctx = ssl._create_unverified_context()
                    with build_url_opener(NETWORK_MODE).open(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    price_map: dict[str, float] = {}
                    for item in ((data.get("data") or {}).get("diff") or []):
                        code = str(item.get("f12", ""))
                        price = item.get("f2")
                        if code and isinstance(price, (int, float)) and price > 0:
                            price_map[code] = price
                    for h in self.holdings:
                        code = h.get("code", "")
                        if code in price_map:
                            h["price"] = price_map[code]
                    self._save_holdings()
                    self._refresh_hold_tree()
                    return
                except Exception:
                    continue
        except Exception:
            pass

    def _hold_dialog(self, title: str, init: dict | None = None) -> dict | None:
        dlg = Toplevel(self.root)
        dlg.title(title)
        dlg.geometry("320x260")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        result: dict | None = None

        code_var = StringVar(value=init.get("code", "") if init else "")
        name_var = StringVar(value=init.get("name", "") if init else "")
        cost_var = StringVar(value=str(init.get("cost", "")) if init else "")
        qty_var = StringVar(value=str(init.get("qty", "")) if init else "")
        price_init = init.get("price") if init else None
        price_var = StringVar(value=f"{price_init:.2f}" if price_init else "")

        fields = [
            ("代码", code_var),
            ("名称", name_var),
            ("成本价", cost_var),
            ("数量", qty_var),
            ("现价", price_var),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(dlg, text=label).grid(row=i, column=0, sticky="e", padx=(16, 6), pady=6)
            ttk.Entry(dlg, textvariable=var, width=20).grid(row=i, column=1, padx=(0, 16), pady=6)

        def on_ok() -> None:
            nonlocal result
            code = code_var.get().strip()
            name = name_var.get().strip()
            if not code and not name:
                messagebox.showwarning("提示", "请至少填写代码或名称", parent=dlg)
                return
            try:
                cost = float(cost_var.get()) if cost_var.get().strip() else 0.0
            except ValueError:
                messagebox.showwarning("提示", "成本价请输入数字", parent=dlg)
                return
            try:
                qty = int(qty_var.get()) if qty_var.get().strip() else 0
            except ValueError:
                messagebox.showwarning("提示", "数量请输入整数", parent=dlg)
                return
            try:
                price = float(price_var.get()) if price_var.get().strip() else None
            except ValueError:
                messagebox.showwarning("提示", "现价请输入数字", parent=dlg)
                return
            result = {"code": code, "name": name, "cost": cost, "qty": qty}
            if price is not None:
                result["price"] = price
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.grid(row=len(fields), column=0, columnspan=2, pady=12)
        ttk.Button(btn_row, text="确定", command=on_ok, width=8).pack(side="left", padx=8)
        ttk.Button(btn_row, text="取消", command=dlg.destroy, width=8).pack(side="left", padx=8)

        self.root.wait_window(dlg)
        return result

    def hold_add(self) -> None:
        data = self._hold_dialog("新增持仓")
        if data is None:
            return
        self.holdings.append(data)
        self._save_holdings()
        self._refresh_hold_tree()

    def hold_edit(self) -> None:
        sel = self.hold_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条持仓记录")
            return
        idx = self.hold_tree.index(sel[0])
        data = self._hold_dialog("编辑持仓", self.holdings[idx])
        if data is None:
            return
        self.holdings[idx] = data
        self._save_holdings()
        self._refresh_hold_tree()

    def hold_delete(self) -> None:
        sel = self.hold_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条持仓记录")
            return
        idx = self.hold_tree.index(sel[0])
        name = self.holdings[idx].get("name") or self.holdings[idx].get("code")
        if not messagebox.askyesno("确认删除", f"确定删除 {name} 的持仓记录？"):
            return
        self.holdings.pop(idx)
        self._save_holdings()
        self._refresh_hold_tree()

    # ── mode toggle helpers ─────────────────────────────────────

    def _reset_after_empty(self) -> None:
        self._running = False
        self.status.set("请至少勾选一项筛选内容")
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.root.update_idletasks()

    def _on_all_toggle(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        val = self.mode_all.get()
        self.mode_strict.set(val)
        self.mode_low.set(val)
        self.mode_watchlist.set(val)
        self._syncing = False

    def _on_mode_toggle(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        all_on = self.mode_strict.get() and self.mode_low.get() and self.mode_watchlist.get()
        self.mode_all.set(all_on)
        self._syncing = False

    def choose_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存筛选结果",
            initialdir=str(OUTPUT_DIR),
            initialfile=Path(self.output_path.get()).name,
            defaultextension=".md",
            filetypes=(("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")),
        )
        if path:
            self.output_path.set(path)
            self._user_chosen_path = True

    # ── progress & timer ──────────────────────────────────────
    # Phase mapping: CLI stderr "[计时] XXX" → (progress%, label)
    _PHASE_MAP = [
        (r"行情分页", 60, "行情数据获取"),
        (r"K线增强", 80, "K线数据分析"),
        (r"资金流向", 92, "资金流向计算"),
        (r"公告检查", 97, "公告风险检查"),
        (r"总计", 100, "完成"),
    ]

    def _start_timer(self) -> None:
        self._run_start = time.time()
        self._timer_id = None
        self._progress_frame.pack(fill="x", pady=(0, 4), before=self.text)
        self._progress_var.set(0)
        self._phase_label.set("准备中...")
        self._update_timer()

    def _update_timer(self) -> None:
        if not self._running:
            return
        elapsed = int(time.time() - self._run_start)
        phase = self._phase_label.get()
        if phase and phase != "完成":
            self.status.set(f"{phase}... {elapsed}s")
        self._timer_id = self.root.after(1000, self._update_timer)

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None

    def _update_progress(self, stderr_line: str) -> None:
        """Parse CLI stderr timing lines to advance the progress bar."""
        for pattern, pct, label in self._PHASE_MAP:
            if pattern in stderr_line:
                self._progress_var.set(pct)
                self._phase_label.set(label)
                break

    def run(self) -> None:
        if self._running:
            return

        selected_modes = []
        if self.mode_strict.get():
            selected_modes.append("strict")
        if self.mode_low.get():
            selected_modes.append("low")
        if self.mode_watchlist.get():
            selected_modes.append("watchlist")
        if not selected_modes:
            self._reset_after_empty()
            return

        if not self._user_chosen_path:
            self.output_path.set(str(default_output_path()))
        output = Path(self.output_path.get()).expanduser()
        top = self.top.get()
        check_ann = self.check_ann.get()
        capital_ranking = self.capital_ranking.get()
        network_mode = {"自动": "auto", "强制直连": "direct", "系统代理": "proxy"}.get(
            self.network_mode.get(), "auto"
        )

        self._running = True
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("查询中...")
        self._start_timer()
        self.text.delete("1.0", END)
        thread = threading.Thread(
            target=self.run_worker,
            args=(
                output,
                selected_modes,
                top,
                check_ann,
                capital_ranking,
                network_mode,
            ),
            daemon=True,
        )
        thread.start()

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self.status.set("正在停止...")
            self.stop_button.configure(state="disabled")

    def _poll_worker_results(self) -> None:
        """Deliver worker results on Tk's main thread."""
        try:
            while True:
                item = self._result_queue.get_nowait()
                if item[0] == "progress":
                    self._update_progress(item[1])
                    continue
                _, ok, content, output = item
                self.finish(ok, content, output)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_worker_results)

    def open_output_folder(self) -> None:
        output = Path(self.output_path.get()).expanduser()
        target = output if output.exists() else output.parent
        try:
            if sys.platform == "darwin":
                # Keep this Tk window active. Without -g, Finder steals focus and
                # the next button click is consumed only to reactivate the app.
                cmd = ["open", "-g", "-R", str(output)] if output.exists() else ["open", "-g", str(output.parent)]
            elif sys.platform.startswith("win"):
                cmd = ["explorer", str(target)]
            else:
                cmd = ["xdg-open", str(target)]
            subprocess.Popen(cmd)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def run_worker(
        self,
        output: Path,
        selected_modes: list[str],
        top: int,
        check_ann: bool,
        capital_ranking: bool,
        network_mode: str,
    ) -> None:
        cmd = [
            sys.executable,
            str(SCREEN_SCRIPT),
            "--mode", *selected_modes,
            "--format", "md",
            "--top", str(top),
            "--save", str(output),
            "--network-mode", network_mode,
        ]
        if not check_ann:
            cmd.append("--skip-announcements")
        if not capital_ranking:
            cmd.append("--skip-capital-ranking")
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def drain(stream, target: list[str], show_progress: bool = False) -> None:
                for line in stream:
                    text = line.rstrip()
                    if text:
                        target.append(text)
                        if show_progress:
                            self._result_queue.put(("progress", text, output))

            stdout_thread = threading.Thread(
                target=drain,
                args=(self._process.stdout, stdout_lines),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=drain,
                args=(self._process.stderr, stderr_lines, True),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                self._process.wait(timeout=240)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
                raise
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            if self._process.returncode == -15 or self._process.returncode < 0:
                self._result_queue.put(("result", False, "已手动停止", output))
                return
            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
            content = stdout if stdout else stderr
            if self._process.returncode != 0:
                content = content or "筛选失败，未返回错误信息。"
                self._result_queue.put(("result", False, content, output))
                return
            self._result_queue.put(("result", True, content, output))
        except subprocess.TimeoutExpired:
            if self._process and self._process.poll() is None:
                self._process.kill()
            self._result_queue.put(("result", False, "查询超时（240s），已自动停止", output))
        except Exception as exc:
            self._result_queue.put(("result", False, str(exc), output))
        finally:
            self._process = None

    def _apply_colors(self) -> None:
        """Apply syntax coloring to the screening output in the text widget."""
        self.text.tag_remove("header", "1.0", END)
        self.text.tag_remove("meta", "1.0", END)
        self.text.tag_remove("cls_A", "1.0", END)
        self.text.tag_remove("cls_B", "1.0", END)
        self.text.tag_remove("cls_C", "1.0", END)
        self.text.tag_remove("risk", "1.0", END)
        self.text.tag_remove("num", "1.0", END)
        self.text.tag_remove("flow_good", "1.0", END)
        self.text.tag_remove("flow_maybe", "1.0", END)
        self.text.tag_remove("flow_diverge", "1.0", END)
        self.text.tag_remove("flow_risk", "1.0", END)

        for i, line in enumerate(self.text.get("1.0", END).split("\n"), start=1):
            row = str(i)
            if line.startswith("## "):
                self.text.tag_add("header", f"{row}.0", f"{row}.end")
            elif not line.startswith("|") and not line.startswith("数据") and line.strip():
                self.text.tag_add("meta", f"{row}.0", f"{row}.end")
            if line.startswith("|"):
                # A/B/C class column (first cell after |)
                for tag, marker in [("cls_A", "| A "), ("cls_B", "| B "), ("cls_C", "| C ")]:
                    idx = line.find(marker)
                    if idx >= 0:
                        self.text.tag_add(tag, f"{row}.{idx}", f"{row}.{idx + 4}")
                # risk keywords
                for kw in ("追高风险", "冲高回落风险", "尾盘追高风险", "巨量滞涨", "均价线下方",
                           "放量滞涨风险", "板块共振不足", "持仓区/止盈区", "趋势观察池",
                           "avoid", "watch_risk", "公告硬风险", "公告观察风险"):
                    start = 0
                    while True:
                        idx = line.find(kw, start)
                        if idx < 0:
                            break
                        self.text.tag_add("risk", f"{row}.{idx}", f"{row}.{idx + len(kw)}")
                        start = idx + len(kw)
                # percentage values like 3.81%, -0.53%
                import re as _re
                for m in _re.finditer(r"-?\d+\.\d+%?", line):
                    self.text.tag_add("num", f"{row}.{m.start()}", f"{row}.{m.end()}")
                # flow status keywords
                for kw, tag in [("有效流入", "flow_good"), ("疑似流入", "flow_maybe"),
                                ("价量背离", "flow_diverge"), ("疑似派发", "flow_risk")]:
                    idx = line.find(kw)
                    if idx >= 0:
                        self.text.tag_add(tag, f"{row}.{idx}", f"{row}.{idx + len(kw)}")


    def _holdings_report(self) -> str:
        """Generate a Markdown section for current holdings with P&L."""
        if not self.holdings:
            return ""
        lines = ["\n## 当前持仓\n"]
        lines.append("| 代码 | 名称 | 成本价 | 数量 | 现价 | 盈亏% | 盈亏额 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
        total_cost = 0.0
        total_value = 0.0
        for h in self.holdings:
            code = h.get("code", "")
            name = h.get("name", "")
            cost = h.get("cost", 0)
            qty = h.get("qty", 0)
            price = h.get("price")
            if price and cost and qty:
                pnl_pct = (price - cost) / cost * 100
                pnl_amt = (price - cost) * qty
                total_cost += cost * qty
                total_value += price * qty
                pnl_str = f"{pnl_pct:+.2f}%"
                amt_str = f"{pnl_amt:+,.0f}"
                price_str = f"{price:.2f}"
            else:
                pnl_str = "-"
                amt_str = "-"
                price_str = f"{price:.2f}" if price else "-"
            lines.append(f"| {code} | {name} | {cost:.2f} | {qty} | {price_str} | {pnl_str} | {amt_str} |")
        if total_cost > 0:
            total_pnl = (total_value - total_cost) / total_cost * 100
            total_amt = total_value - total_cost
            lines.append(f"| **合计** | | | | | **{total_pnl:+.2f}%** | **{total_amt:+,.0f}** |")
        return "\n".join(lines) + "\n"

    def finish(self, ok: bool, content: str, output: Path) -> None:
        self._running = False
        self._stop_timer()
        if ok:
            self._progress_var.set(100)
            self._phase_label.set("完成")
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

        # Append holdings report + sector indices if query succeeded
        if ok:
            if self.holdings:
                # Refresh holdings prices before generating report
                self._silent_refresh_prices()
                report = self._holdings_report()
                if report:
                    content = content + report

                # Fetch holding industries and merge into sector table
                try:
                    codes = [h.get("code", "") for h in self.holdings]
                    ind_map = _fetch_stock_industries(codes)
                    holding_industries = set(ind_map.values())
                    if holding_industries:
                        boards = _fetch_sector_boards()
                        # Parse existing sectors from CLI's table
                        existing = set()
                        if "## 相关板块指数" in content:
                            for line in content.split("\n"):
                                if line.startswith("|") and "##" not in line and "---" not in line:
                                    cols = [c.strip() for c in line.split("|")]
                                    if len(cols) > 2:
                                        existing.add(cols[1])
                        # Add holding sectors not already present
                        new_rows = []
                        for ind in sorted(holding_industries):
                            board = boards.get(ind)
                            if not board:
                                for bn, bd in boards.items():
                                    if ind in bn or bn in ind:
                                        board = bd
                                        break
                            if board and board["name"] not in existing:
                                price_s = f"{board['price']:.2f}" if board.get("price") else "-"
                                ud = f"{board.get('up_count', 0)}↑{board.get('down_count', 0)}↓"
                                chg = board.get("change", 0)
                                new_rows.append((chg, f"| {board['name']} | {chg:.2f}% | {price_s} | {ud} | 持仓 |"))
                        if new_rows:
                            new_rows.sort(key=lambda x: x[0], reverse=True)
                            lines_to_add = [r[1] for r in new_rows]
                            if "## 相关板块指数" in content:
                                # Find the last row of the sector table and insert after it
                                lines = content.split("\n")
                                insert_idx = -1
                                in_sector_table = False
                                for i, line in enumerate(lines):
                                    if "## 相关板块指数" in line:
                                        in_sector_table = True
                                        continue
                                    if in_sector_table:
                                        if line.startswith("|"):
                                            insert_idx = i
                                        elif line.strip() == "" or line.startswith("##"):
                                            break
                                if insert_idx >= 0:
                                    lines.insert(insert_idx + 1, "\n".join(lines_to_add))
                                    content = "\n".join(lines)
                            else:
                                sec_lines = ["\n## 相关板块指数\n",
                                             "| 板块 | 涨跌% | 现价 | 涨/跌 | 来源 |",
                                             "| --- | ---: | --- | --- | --- |"]
                                sec_lines.extend(lines_to_add)
                                marker = "\n备注：脚本只负责查询和分层"
                                content = content.replace(marker, "\n".join(sec_lines) + marker)
                except Exception:
                    pass

            # Re-save file with holdings + sector data appended
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(content, encoding="utf-8")
            except Exception:
                pass

        self.text.insert("1.0", content)
        self._apply_colors()
        self._last_content = content if ok else ""
        self.root.update_idletasks()
        elapsed = int(time.time() - self._run_start) if hasattr(self, "_run_start") else 0

        # Mark problem reports: rename file if data is degraded
        _PROBLEM_MARKERS = ("新浪财经实时备用快照", "数据质量降级", "行情全量快照不完整")
        is_problem = ok and any(m in content for m in _PROBLEM_MARKERS)
        if is_problem and output.exists():
            new_name = output.stem + "(问题报告)" + output.suffix
            new_path = output.with_name(new_name)
            try:
                output.rename(new_path)
                output = new_path
                self.output_path.set(str(new_path))
            except OSError:
                pass

        if ok:
            tag = " (问题报告)" if is_problem else ""
            self.status.set(f"已完成{tag} ({elapsed}s)")
        elif content == "已手动停止":
            self.status.set(f"已停止 ({elapsed}s)")
        else:
            self.status.set(f"失败 ({elapsed}s)")
            messagebox.showerror("失败", content[:1000])

    def mainloop(self) -> None:
        self.root.mainloop()


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
