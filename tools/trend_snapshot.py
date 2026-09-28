"""Trend snapshot for one A-share ticker: MA structure, MA20 slope, distance to 60d high, volume.

Usage:
    python trend_snapshot.py 601127

Read-only: fetches daily K-line from the same public endpoint family the project uses.
"""
from __future__ import annotations

import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def fetch_kline(code: str, count: int = 120):
    # eastmoney daily kline (secid: 1.=SH, 0.=SZ)
    market = "1" if code.startswith(("6", "9")) else "0"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={market}.{code}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1&end=20500101&lmt={count}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    klines = (payload.get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        date, open_, close, high, low, vol, amount = line.split(",")[:7]
        rows.append(
            {
                "date": date,
                "open": float(open_),
                "close": float(close),
                "high": float(high),
                "low": float(low),
                "volume": float(vol),
                "amount": float(amount),
            }
        )
    return (payload.get("data") or {}).get("name"), rows


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python trend_snapshot.py <code>")
        return 1
    code = sys.argv[1]
    name, rows = fetch_kline(code)
    if not rows:
        print("no kline data")
        return 1

    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    vols = [r["volume"] for r in rows]
    last = closes[-1]

    ma5, ma10, ma20, ma60 = (sma(closes, n) for n in (5, 10, 20, 60))
    prev_ma20 = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else None
    ma20_slope = (ma20 - prev_ma20) if (ma20 and prev_ma20) else None

    high60 = max(highs[-60:]) if len(highs) >= 60 else max(highs)
    low60 = min(r["low"] for r in rows[-60:]) if len(rows) >= 60 else min(r["low"] for r in rows)
    vol5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else None

    print(f"【{name}】{code}  最新收盘 {last}  ({rows[-1]['date']})")
    print(f"  样本: {len(rows)} 根日K  {rows[0]['date']} ~ {rows[-1]['date']}")
    for label, value in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20), ("MA60", ma60)):
        if value:
            diff = (last / value - 1) * 100
            print(f"  {label:5s} = {value:7.2f}   收盘相对 {diff:+6.2f}%")
    print(f"  MA20 斜率(较前一日): {'上行 +' if (ma20_slope or 0) > 0 else '下行 '}{ma20_slope:+.4f}" if ma20_slope is not None else "  MA20 斜率: 数据不足")
    print(f"  60日高 = {high60:.2f}   距60日高点 {(last/high60-1)*100:+.2f}%")
    print(f"  60日低 = {low60:.2f}   距60日低点 {(last/low60-1)*100:+.2f}%")
    if vol5:
        print(f"  当日量/前5日均量 = {vols[-1]/vol5:.2f}")

    print("\n  最近 15 个交易日:")
    for r in rows[-15:]:
        chg = ""
        print(f"    {r['date']}  收 {r['close']:7.2f}  高 {r['high']:7.2f}  低 {r['low']:7.2f}")

    # simple structure read
    print("\n  结构判读:")
    if ma5 and ma10 and ma20:
        if last > ma5 > ma10 > ma20:
            print("    多头排列(收盘 > MA5 > MA10 > MA20)")
        elif last < ma5 < ma10 < ma20:
            print("    空头排列(收盘 < MA5 < MA10 < MA20)")
        else:
            print("    均线交织/非标准排列")
        print(f"    收盘站上 MA20: {'是' if last > ma20 else '否'}")
        print(f"    MA20 方向: {'向上' if (ma20_slope or 0) > 0 else '向下'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
