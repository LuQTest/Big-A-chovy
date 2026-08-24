#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情、五档盘口与分时K线快速查询工具
数据源：腾讯金融 API (qt.gtimg.cn / web.ifzq.gtimg.cn)
"""

import sys
import json
import ssl
import urllib.request
import argparse
from typing import List, Dict, Any

ssl_ctx = ssl._create_unverified_context()

def normalize_code(code: str) -> str:
    """自动添加市场前缀 sh / sz"""
    code_clean = code.strip().lower()
    if code_clean.startswith("sh") or code_clean.startswith("sz"):
        return code_clean
    if code_clean.startswith("6") or code_clean.startswith("9"):
        return f"sh{code_clean}"
    return f"sz{code_clean}"

def fetch_realtime_quotes(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量查询实时行情与五档买卖盘口"""
    symbols = [normalize_code(c) for c in codes]
    url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
    
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            content = resp.read().decode("gbk", errors="ignore")
    except Exception as e:
        print(f"请求失败: {e}", file=sys.stderr)
        return {}

    results = {}
    lines = content.strip().split(";")
    for line in lines:
        line = line.strip()
        if not line or "~" not in line:
            continue
        parts = line.split("~")
        if len(parts) < 40:
            continue
        
        name = parts[1]
        code = parts[2]
        price = float(parts[3]) if parts[3] else 0.0
        pre_close = float(parts[4]) if parts[4] else 0.0
        open_price = float(parts[5]) if parts[5] else 0.0
        pct = float(parts[32]) if parts[32] else 0.0
        high = float(parts[33]) if parts[33] else 0.0
        low = float(parts[34]) if parts[34] else 0.0
        amount_wan = float(parts[37]) if parts[37] else 0.0
        time_str = parts[30]
        
        # 买卖五档
        buy_orders = []
        for b_idx in range(5):
            p = float(parts[9 + b_idx * 2]) if parts[9 + b_idx * 2] else 0.0
            v = int(parts[10 + b_idx * 2]) if parts[10 + b_idx * 2] else 0
            buy_orders.append((p, v))

        sell_orders = []
        for s_idx in range(5):
            p = float(parts[19 + s_idx * 2]) if parts[19 + s_idx * 2] else 0.0
            v = int(parts[20 + s_idx * 2]) if parts[20 + s_idx * 2] else 0
            sell_orders.append((p, v))

        results[code] = {
            "name": name,
            "code": code,
            "price": price,
            "pre_close": pre_close,
            "open": open_price,
            "pct": pct,
            "high": high,
            "low": low,
            "amount_wan": amount_wan,
            "time": time_str,
            "buy_orders": buy_orders,
            "sell_orders": sell_orders,
            "zt": float(parts[48]) if len(parts) > 48 and parts[48] else 0.0,
            "dt": float(parts[47]) if len(parts) > 47 and parts[47] else 0.0,
        }

    return results

def fetch_minute_data(code: str) -> List[str]:
    """查询当日1分钟分时明细"""
    sym = normalize_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sub_key = list(data.get("data", {}).keys())[0]
        m_lines = data["data"][sub_key]["data"]["data"]
        return m_lines
    except Exception as e:
        print(f"获取分时失败: {e}", file=sys.stderr)
        return []

def fetch_daily_kline(code: str, count: int = 10) -> List[Dict[str, Any]]:
    """查询近期前复权日K线"""
    sym = normalize_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{count + 1},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sym_data = data.get("data", {}).get(sym, {})
        days = sym_data.get("qfqday") or sym_data.get("day") or []
        # format: [date, open, close, high, low, volume, ...]
        out = []
        for i in range(len(days)):
            d = days[i]
            close = float(d[2])
            prev_close = float(days[i-1][2]) if i > 0 else float(d[1])
            pct = ((close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
            out.append({
                "date": d[0],
                "open": float(d[1]),
                "close": close,
                "high": float(d[3]),
                "low": float(d[4]),
                "pct": pct,
                "vol": float(d[5])
            })
        return out[-count:]
    except Exception as e:
        print(f"获取日K失败: {e}", file=sys.stderr)
        return []

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="实时行情/分时查询工具")
    parser.add_argument("codes", nargs="+", help="股票代码列表，如 600188 000768")
    parser.add_argument("--minute", action="store_true", help="显示尾盘分时明细")
    parser.add_argument("--kline", action="store_true", help="显示近期日K")
    args = parser.parse_args()

    quotes = fetch_realtime_quotes(args.codes)
    for code, q in quotes.items():
        print(f"==================================================")
        print(f"【{q['name']}】({q['code']}) 现价: {q['price']:.2f} ({q['pct']:+.2f}%)")
        print(f"昨收: {q['pre_close']:.2f} | 今开: {q['open']:.2f} | 最高: {q['high']:.2f} | 最低: {q['low']:.2f}")
        print(f"成交额: {q['amount_wan']:.0f} 万元 | 时间: {q['time']}")
        print(f"\n[买卖五档盘口]")
        for i in range(4, -1, -1):
            sp, sv = q['sell_orders'][i]
            print(f"  卖{i+1}: {sp:.2f}  ({sv}手)")
        print("  -------------------")
        for i in range(5):
            bp, bv = q['buy_orders'][i]
            print(f"  买{i+1}: {bp:.2f}  ({bv}手)")

        if args.minute:
            m_data = fetch_minute_data(code)
            print(f"\n[尾盘分时末尾 8 分钟]")
            for line in m_data[-8:]:
                print(f"  {line}")

        if args.kline:
            k_data = fetch_daily_kline(code, count=6)
            print(f"\n[近期日K]")
            for k in k_data:
                print(f"  {k['date']}: 收盘 {k['close']:.2f} ({k['pct']:+.2f}%) 开 {k['open']:.2f} 高 {k['high']:.2f} 低 {k['low']:.2f}")
