#!/usr/bin/env python3
"""Compare Eastmoney standard and webguest routes through the app's network selector."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "daily-stock-analysis" / "scripts"))
import a_share_daily_screen as em  # noqa: E402

BASE = "https://push2.eastmoney.com"
CLIST_PARAMS = {
    "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
    "fs": "m:1+t:2", "fields": "f12,f14,f2,f3,f62,f66,f184",
}
ULIST_PARAMS = {
    "fltt": 2, "invt": 2, "secids": "1.600519,0.000001",
    "fields": "f12,f14,f2",
}
FFLOW_PARAMS = {
    "secid": "0.002106", "klt": 1, "lmt": 0,
    "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56",
}
STOCK_PARAMS = {
    "secid": "1.600519",
    "fields": "f57,f58,f43,f59,f162,f163,f164,f167,f173,f183,f184,f185,f186,f187",
}
KLINE_PARAMS = {
    "secid": "1.600000", "klt": 101, "fqt": 1, "lmt": 2,
    "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56",
}
TRENDS_PARAMS = {
    "secid": "1.600519", "fields1": "f1,f2,f3,f8",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "ndays": 1, "iscr": 0,
}
# 2026-09-25 实测：push2his 主机没有 /webguest 路由，其标准路由 kline/trends2/fflow 也已下线。
# 默认不探测这些端点：每次都会等到超时，既拖慢诊断又会刷出 4 条 FAIL 假警报，
# 掩盖真正的新问题。需要复核是否恢复时加 --include-retired。
RETIRED_ROUTES = [
    ("push2his kline standard", "https://push2his.eastmoney.com/api/qt/stock/kline/get", KLINE_PARAMS, "kline"),
    ("push2his kline webguest", "https://push2his.eastmoney.com/webguest/api/qt/stock/kline/get", KLINE_PARAMS, "kline"),
    ("push2his trends2 standard", "https://push2his.eastmoney.com/api/qt/stock/trends2/get", TRENDS_PARAMS, "trends"),
    ("push2his trends2 webguest", "https://push2his.eastmoney.com/webguest/api/qt/stock/trends2/get", TRENDS_PARAMS, "trends"),
]


def _report(label: str, url: str, params: dict, kind: str) -> bool:
    try:
        payload = em.fetch_json(url, params, timeout=6)
        body = payload.get("data") if isinstance(payload, dict) else None
        body = body if isinstance(body, dict) else {}
        if kind in {"clist", "ulist"}:
            rows = body.get("diff") or []
            summary = f"rows={len(rows)} codes={[r.get('f12') for r in rows[:3]]}"
            if kind == "clist" and rows:
                required = ("f62", "f66", "f184")
                missing = [key for key in required if key not in rows[0]]
                summary += f" flow_fields_missing={missing}"
            ok = isinstance(rows, list) and bool(rows)
        elif kind == "stock":
            summary = f"code={body.get('f57')} name={body.get('f58')} price_raw={body.get('f43')}"
            ok = bool(body.get("f57") and body.get("f58") and body.get("f43"))
        elif kind == "trends":
            bars = body.get("trends") or []
            latest = str(bars[-1]).split(",", 1)[0] if bars else "-"
            summary = f"code={body.get('code') or body.get('f57')} trends={len(bars)} latest={latest}"
            ok = isinstance(bars, list) and bool(bars)
        else:
            bars = body.get("klines") or []
            latest = str(bars[-1]).split(",", 1)[0] if bars else "-"
            summary = f"code={body.get('code') or body.get('f57')} klines={len(bars)} latest_bar={latest}"
            ok = isinstance(bars, list) and bool(bars)
        print(f"{'OK  ' if ok else 'EMPTY'} {label}: rc={payload.get('rc')} {summary}")
        return ok
    except Exception as exc:
        print(f"FAIL {label}: {type(exc).__name__}: {' '.join(str(exc).split())[:180]}")
        return False


def main() -> int:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    print(f"=== Eastmoney route check {now:%Y-%m-%d %H:%M:%S} Asia/Shanghai ===")
    paths = em.network_path.best_paths()
    print("network paths:", [(p["label"], p["latency_ms"], p["degraded"]) for p in paths])

    routes = [
        ("clist standard", f"{BASE}/api/qt/clist/get", CLIST_PARAMS, "clist"),
        ("clist webguest", f"{BASE}/webguest/api/qt/clist/get", CLIST_PARAMS, "clist"),
        ("ulist standard", f"{BASE}/api/qt/ulist.np/get", ULIST_PARAMS, "ulist"),
        ("ulist webguest", f"{BASE}/webguest/api/qt/ulist.np/get", ULIST_PARAMS, "ulist"),
        ("fflow standard", f"{BASE}/api/qt/stock/fflow/kline/get", FFLOW_PARAMS, "fflow"),
        ("fflow webguest", f"{BASE}/webguest/api/qt/stock/fflow/kline/get", FFLOW_PARAMS, "fflow"),
        ("stock.get standard", f"{BASE}/api/qt/stock/get", STOCK_PARAMS, "stock"),
        ("stock.get webguest", f"{BASE}/webguest/api/qt/stock/get", STOCK_PARAMS, "stock"),
        ("push2 kline webguest", f"{BASE}/webguest/api/qt/stock/kline/get", KLINE_PARAMS, "kline"),
        ("push2 trends2 webguest", f"{BASE}/webguest/api/qt/stock/trends2/get", TRENDS_PARAMS, "trends"),
    ]
    include_retired = "--include-retired" in sys.argv
    if include_retired:
        routes = routes + RETIRED_ROUTES
    results = {label: _report(label, url, params, kind) for label, url, params, kind in routes}
    if not include_retired:
        print("RETIRED (已知下线，本次未探测；加 --include-retired 可复核): "
              + ", ".join(label for label, _, _, _ in RETIRED_ROUTES))

    if not (now.weekday() < 5 and ((9 * 60 + 30) <= now.hour * 60 + now.minute <= (11 * 60 + 30)
                                   or 13 * 60 <= now.hour * 60 + now.minute <= 15 * 60)):
        print("NOTE: outside trading hours; successful responses confirm connectivity and fields, not live-price freshness.")

    required = ("clist webguest", "ulist webguest", "fflow webguest", "stock.get webguest", "push2 trends2 webguest")
    guest_ok = all(results.get(label) for label in required)
    print("RESULT:", "required webguest routes usable" if guest_ok else "a required webguest route failed")
    return 0 if guest_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
