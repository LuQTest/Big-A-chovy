#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个股全天快照资金与状态机轨迹跟踪工具
用于复盘或盘中排查特定个股在所有时间快照中的表现（主力占比、超大单、5分增量、回落、共振、所在池与状态）。
"""

import os
import sys
import argparse
from typing import Dict, List, Any, Optional
from report_parser import parse_screening_report, get_report_files

BASE_REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))

def track_stock_timeline(code_or_name: str, date_str: Optional[str] = None):
    files = get_report_files(BASE_REPORTS_DIR, date_str)
    if not files:
        print(f"未找到相关报告文件: date={date_str}")
        return

    print(f"=== 正在检索标的 [{code_or_name}] 在 {len(files)} 份报告中的轨迹 ({files[0].split('/')[-2]}) ===")
    
    hits = []
    for f in files:
        rep = parse_screening_report(f)
        time_tag = rep["time"]
        
        # 在各个表格中搜索
        found_rows = []
        for table_name, rows in rep["tables"].items():
            for r in rows:
                c = r.get("代码", "")
                n = r.get("名称", "")
                if code_or_name == c or code_or_name in n:
                    found_rows.append((table_name, r))
        
        if found_rows:
            hits.append((time_tag, rep["file"], found_rows))

    if not hits:
        print(f"未在任何报告表格中找到标的: {code_or_name}")
        return

    print(f"共在 {len(hits)} 份报告中出现：\n")
    print(f"{'时间':<6} | {'现价':<6} | {'涨幅':<7} | {'主力%':<6} | {'5分增量':<9} | {'超大单':<8} | {'超单主导':<5} | {'回落':<7} | {'均价线':<6} | {'所在表/池'}")
    print("-" * 95)

    for time_tag, filename, rows in hits:
        for tbl_name, r in rows:
            price = r.get("现价", r.get("当前价", r.get("触发价", "-")))
            pct = r.get("涨幅", "-")
            main_pct = r.get("主力净占比", "-")
            inc5 = r.get("5分钟增量", r.get("5分资金", "-"))
            super_order = r.get("超大单", "-")
            super_lead = r.get("超单主导", "-")
            pullback = r.get("高位回落", "-")
            vwap = r.get("均价线", r.get("当时VWAP", "-"))
            
            # 格式化所在表/池与状态机阶段
            pool_label = tbl_name
            if tbl_name == "state_machine":
                stage = r.get("状态阶段", r.get("相位", "状态机"))
                retest = r.get("回踩观察区", "")
                retest_txt = f"[{retest}]" if retest and retest != "-" else ""
                pool_label = f"🎯状态机:{stage}{retest_txt}"
            
            print(f"{time_tag:<6} | {price:<6} | {pct:<7} | {main_pct:<6} | {inc5:<9} | {super_order:<8} | {super_lead:<5} | {pullback:<7} | {vwap:<6} | {pool_label}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="个股筛选轨迹跟踪")
    parser.add_argument("stock", type=str, help="股票代码或名称，如 601666 或 平煤股份")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD，默认最新日期")
    args = parser.parse_args()

    track_stock_timeline(args.stock, args.date)
