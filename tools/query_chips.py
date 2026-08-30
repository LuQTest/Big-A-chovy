#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/query_chips.py - 主力筹码与分时价格-成交量分布 (Volume-by-Price / Chip Distribution) 实时查询工具

用法：
    python3 tools/query_chips.py 600522
    python3 tools/query_chips.py 600522 600722 603897
    python3 tools/query_chips.py 600522 --buckets 15
"""

import sys
import json
import ssl
import urllib.request
import argparse

ssl_ctx = ssl._create_unverified_context()

def normalize_code(code: str) -> str:
    code_clean = code.strip().lower()
    if code_clean.startswith("sh") or code_clean.startswith("sz") or code_clean.startswith("bj"):
        return code_clean
    if code_clean.startswith("6") or code_clean.startswith("9"):
        return f"sh{code_clean}"
    elif code_clean.startswith("0") or code_clean.startswith("3"):
        return f"sz{code_clean}"
    return f"sh{code_clean}"

def fetch_minute_data(sym: str):
    """通过腾讯金融 API 获取全天分钟明细"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sub_key = list(data.get("data", {}).keys())[0]
        m_lines = data["data"][sub_key]["data"]["data"]
        # Also get name and pre_close from qt API
        return m_lines
    except Exception as e:
        return None

def fetch_quote_info(sym: str):
    """获取股票基本信息与昨收"""
    url = f"https://qt.gtimg.cn/q={sym}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as resp:
            content = resp.read().decode("gbk", errors="ignore")
        parts = content.split("~")
        if len(parts) > 35:
            return {
                "name": parts[1],
                "code": parts[2],
                "price": float(parts[3]),
                "pre_close": float(parts[4]),
                "high": float(parts[33]),
                "low": float(parts[34]),
                "turnover_pct": float(parts[38]) if parts[38] else 0.0,
                "amount_wan": float(parts[37]) if parts[37] else 0.0,
            }
    except Exception:
        pass
    return None

def query_stock_chips(code: str, num_buckets: int = 12):
    sym = normalize_code(code)
    quote_info = fetch_quote_info(sym)
    m_lines = fetch_minute_data(sym)

    if not m_lines:
        print(f"❌ 股票 {code} 获取分时成交数据失败")
        return

    name = quote_info["name"] if quote_info else code
    pre_close = quote_info["pre_close"] if quote_info else 0.0

    price_vol = {}
    total_vol = 0
    total_amt = 0
    high_p = -1e9
    low_p = 1e9
    latest_p = 0.0
    
    prev_vol = 0
    prev_amt = 0.0

    for l in m_lines:
        # Format: '0930 35.70 28590 102066300.00' (time, price, cum_vol_lots, cum_amt_yuan)
        parts = l.split()
        if len(parts) >= 4:
            p = float(parts[1])
            cum_v = int(parts[2])
            cum_a = float(parts[3])
            
            inc_v = cum_v - prev_vol
            inc_a = cum_a - prev_amt
            prev_vol = cum_v
            prev_amt = cum_a

            price_vol[p] = price_vol.get(p, 0) + inc_v
            total_vol += inc_v
            total_amt += inc_a
            
            if p > high_p: high_p = p
            if p < low_p: low_p = p
            latest_p = p

    if total_vol == 0:
        print(f"⚠️ {name}({code}) 今日暂无有效成交量")
        return

    vwap = (total_amt / (total_vol * 100)) if total_vol else 0.0
    chg_pct = ((latest_p - pre_close) / pre_close * 100) if pre_close > 0 else 0.0

    print("\n" + "=" * 80)
    print(f"📊 【{name} ({code})】主力筹码与分时价格-成交量分布透视 (Volume-by-Price)")
    print(f"  现价: {latest_p:.2f}元 ({chg_pct:+.2f}%) | VWAP均价线: {vwap:.2f}元 | 日内高低: {high_p:.2f} ~ {low_p:.2f}元")
    print(f"  总成交量: {total_vol:,} 手 | 总成交金额: {total_amt/1e8:.2f} 亿元")
    print("=" * 80)

    # 1. 价格区间分桶 (Bucketing)
    p_range = high_p - low_p
    if p_range <= 0.02:
        bucket_size = 0.01
    else:
        bucket_size = max(0.02, round(p_range / num_buckets, 2))

    buckets = {}
    for p, v in price_vol.items():
        b_idx = round(p / bucket_size) * bucket_size
        b_idx = round(b_idx, 2)
        buckets[b_idx] = buckets.get(b_idx, 0) + v

    sorted_b = sorted(buckets.keys(), reverse=True)
    max_b_vol = max(buckets.values()) if buckets else 1
    
    print("\n【📈 今日价格 - 筹码堆积分布带】")
    for b in sorted_b:
        v = buckets[b]
        pct = (v / total_vol) * 100
        amt_b = (v * b * 100) / 1e8
        bar_len = int((v / max_b_vol) * 26)
        bar = "█" * bar_len
        
        mark = ""
        if abs(latest_p - b) <= bucket_size / 2:
            mark += " 👈[现价]"
        if abs(vwap - b) <= bucket_size / 2:
            mark += " ⭐[VWAP均线]"
            
        print(f"  {b-bucket_size/2:5.2f} ~ {b+bucket_size/2:5.2f}元 | 成交 {v:7,d}手 ({pct:5.2f}%) | 沉淀 {amt_b:5.2f}亿 | {bar}{mark}")

    # 2. 单点绝对最大成交量 Top 5 筹码峰
    print("\n【🎯 今日单点绝对成交量 Top 5 核心筹码峰】")
    sorted_single = sorted(price_vol.items(), key=lambda x: x[1], reverse=True)
    for rank, (p, v) in enumerate(sorted_single[:5], 1):
        pct = (v / total_vol) * 100
        amt_single = (v * p * 100) / 1e8
        nature = ""
        if p < vwap - 0.20:
            nature = "【低吸承接/铁底支撑峰】"
        elif abs(p - vwap) <= 0.15:
            nature = "【多空中枢/均价平衡峰】"
        else:
            nature = "【高位分歧/冲高套牢峰】"
        print(f"  {rank}. 🎯 {p:5.2f}元 : 成交 {v:7,d}手 ({pct:5.2f}%) | 沉淀 {amt_single:5.2f}亿元 | {nature}")

    # 3. 三层筹码结构剖析
    low_zone_amt = sum(p * v * 100 for p, v in price_vol.items() if p < vwap - 0.1) / 1e8
    mid_zone_amt = sum(p * v * 100 for p, v in price_vol.items() if abs(p - vwap) <= 0.1) / 1e8
    high_zone_amt = sum(p * v * 100 for p, v in price_vol.items() if p > vwap + 0.1) / 1e8
    
    print("\n【💡 主力三层筹码沉淀总结】")
    print(f"  • 底层支撑吸筹区 (<{vwap-0.1:.2f}元): 沉淀资金 {low_zone_amt:.2f} 亿元 ({(low_zone_amt/(total_amt/1e8))*100:.1f}%)")
    print(f"  • 中层多空中枢区 ({vwap-0.1:.2f}~{vwap+0.1:.2f}元): 沉淀资金 {mid_zone_amt:.2f} 亿元 ({(mid_zone_amt/(total_amt/1e8))*100:.1f}%)")
    print(f"  • 顶层阻力套牢区 (>{vwap+0.1:.2f}元): 沉淀资金 {high_zone_amt:.2f} 亿元 ({(high_zone_amt/(total_amt/1e8))*100:.1f}%)")
    print("=" * 80 + "\n")

def main():
    parser = argparse.ArgumentParser(description="A股主力筹码与分时成交量分布查询工具")
    parser.add_argument("codes", nargs="+", help="股票代码，如 600522 600722")
    parser.add_argument("--buckets", type=int, default=12, help="价格区间分桶数量，默认12")
    args = parser.parse_args()

    for code in args.codes:
        query_stock_chips(code, num_buckets=args.buckets)

if __name__ == "__main__":
    main()
