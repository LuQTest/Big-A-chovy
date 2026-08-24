#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T+1 纪律与样本验证工具
对照前日候选标的/观察池与次日 09:45 卖出窗口走势，统计盈亏比与规则兑现率。
"""

import os
import sys
import argparse
from typing import List, Dict, Any
from report_parser import parse_screening_report, get_report_files
from query_quote import fetch_minute_data, fetch_realtime_quotes, normalize_code

BASE_REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))

def get_morning_945_price(code: str) -> Dict[str, Any]:
    """获取次日早盘 09:45 附近的成交价格与最高点"""
    m_lines = fetch_minute_data(code)
    if not m_lines:
        return {}
    
    # 格式: HHMM price vol amount
    prices_morning = []
    price_945 = None
    for line in m_lines:
        parts = line.strip().split()
        if len(parts) >= 2:
            t, p = parts[0], float(parts[1])
            if "0930" <= t <= "0945":
                prices_morning.append(p)
                if t == "0945":
                    price_945 = p
    
    if not price_945 and prices_morning:
        price_945 = prices_morning[-1]
        
    return {
        "price_945": price_945,
        "morning_high": max(prices_morning) if prices_morning else None,
        "morning_low": min(prices_morning) if prices_morning else None,
        "open_price": prices_morning[0] if prices_morning else None
    }

def verify_watchlist_t1(date_str: str):
    """验证某日收盘观察池在次日早盘的表现"""
    files = get_report_files(BASE_REPORTS_DIR, date_str)
    if not files:
        print(f"未找到报告: {date_str}")
        return
    
    last_file = files[-1]
    rep = parse_screening_report(last_file)
    watchlist = rep["tables"]["tomorrow_watchlist"]
    
    if not watchlist:
        print(f"[{date_str}] 收盘报告中无明日观察池标的。")
        return
        
    print(f"=== 验证 [{date_str}] 收盘明日观察池标的 T+1 表现 ===")
    print(f"报告: {rep['file']}\n")
    
    for row in watchlist:
        code = row.get("代码", "")
        name = row.get("名称", "")
        price_str = row.get("当前价", "0")
        lowzone = row.get("低吸区", "")
        trig = row.get("触发价", "")
        
        try:
            buy_price = float(price_str)
        except ValueError:
            buy_price = 0.0
            
        t1_info = get_morning_945_price(code)
        p945 = t1_info.get("price_945")
        m_high = t1_info.get("morning_high")
        
        if p945 and buy_price > 0:
            diff_pct = (p945 - buy_price) / buy_price * 100.0
            max_pct = ((m_high - buy_price) / buy_price * 100.0) if m_high else diff_pct
            print(f"标的: {code} {name}")
            print(f"  前日收盘: {buy_price:.2f} | 低吸区: {lowzone} | 触发价: {trig}")
            print(f"  次日09:45分时价: {p945:.2f} ({diff_pct:+.2f}%) | 早盘最高: {m_high:.2f} ({max_pct:+.2f}%)")
            status = "✅ 达标+2%止盈" if max_pct >= 2.0 else ("⚠️ 浮亏/止损" if diff_pct < -3.0 else "➖ 震荡平出")
            print(f"  判定: {status}\n")
        else:
            print(f"标的: {code} {name} (前日收: {price_str}, 暂未拉取到次日09:45分时)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="T+1 表现与早盘窗口验证")
    parser.add_argument("date", type=str, help="基准日期 YYYYMMDD")
    args = parser.parse_args()

    verify_watchlist_t1(args.date)
