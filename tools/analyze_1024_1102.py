#!/usr/bin/env python3
import os, glob, re, json
from tools.report_parser import parse_screening_report
from tools.query_quote import fetch_realtime_quotes

files = sorted(glob.glob("筛选结果/A股筛选结果_20260820_*.md"))
files_1024 = [f for f in files if re.search(r"_(\d{4})\.md$", f) and re.search(r"_(\d{4})\.md$", f).group(1) >= "1024"]

print(f"=== 10:24 至当前共有 {len(files_1024)} 份筛选报告 ===")
for f in files_1024:
    rep = parse_screening_report(f)
    t = rep["time"]
    m = rep["market"]
    print(f"[{t}] 上涨: {m.get('up')} | 下跌: {m.get('down')} | 涨停: {m.get('zt')} | 跌停: {m.get('dt')}")

# 统计这期间出现的所有候选标的资金数据及演化
stocks_history = {}

for f in files_1024:
    rep = parse_screening_report(f)
    t = rep["time"]
    # 提取主力资金优选
    for r in rep["tables"].get("capital_ranking", []):
        code = r.get("代码")
        if not code: continue
        if code not in stocks_history: stocks_history[code] = {"name": r.get("名称"), "snapshots": []}
        stocks_history[code]["snapshots"].append({
            "time": t, "table": "capital_ranking", "price": r.get("现价"), "pct": r.get("涨幅"),
            "main_pct": r.get("主力净占比"), "main_net": r.get("主力净额"), "inc5": r.get("5分钟增量"),
            "super": r.get("超大单"), "super_lead": r.get("超单主导"), "rating": r.get("资金评级"),
            "risk": r.get("公告风控"), "plate": r.get("板块"), "vwap": r.get("均价线"), "pullback": r.get("高位回落")
        })
    # 提取低吸超短线
    for r in rep["tables"].get("low_absorb_short", []):
        code = r.get("代码")
        if not code: continue
        if code not in stocks_history: stocks_history[code] = {"name": r.get("名称"), "snapshots": []}
        stocks_history[code]["snapshots"].append({
            "time": t, "table": "low_absorb_short", "price": r.get("现价"), "pct": r.get("涨幅"),
            "main_pct": r.get("主力净占比"), "main_net": r.get("主力净额"), "inc5": r.get("5分钟增量"),
            "super": r.get("超大单"), "super_lead": r.get("超单主导"), "rating": r.get("资金评级"),
            "risk": r.get("公告风控"), "plate": r.get("板块"), "vwap": r.get("均价线"), "pullback": r.get("高位回落")
        })
    # 提取双池交集/状态机
    for r in rep["tables"].get("state_machine", []):
        code = r.get("代码")
        if not code: continue
        if code not in stocks_history: stocks_history[code] = {"name": r.get("名称"), "snapshots": []}
        stocks_history[code]["snapshots"].append({
            "time": t, "table": "state_machine", "state": r.get("状态阶段"), "price": r.get("现价"),
            "trigger": r.get("触发价"), "vwap": r.get("均价线"), "retest": r.get("回踩买点区间"), "result": r.get("判定结果")
        })

print("\n=== 候选标的在 10:24-11:02 期间的报告追踪 ===")
for code, data in sorted(stocks_history.items()):
    name = data["name"]
    snaps = data["snapshots"]
    print(f"\n【{name}】({code}) - 出现 {len(snaps)} 次快照:")
    for s in snaps[-5:]: # 显示最近5次快照
        if s.get("table") == "state_machine":
            print(f"  [{s['time']}] 状态机: {s.get('state')} | 现价:{s.get('price')} | 触发:{s.get('trigger')} | 回踩:{s.get('retest')} | 判定:{s.get('result')}")
        else:
            print(f"  [{s['time']}] 表:{s['table']} | 现价:{s.get('price')}({s.get('pct')}) | 主力%:{s.get('main_pct')} | 净额:{s.get('main_net')} | 5分:{s.get('inc5')} | 超大:{s.get('super')}(主导:{s.get('super_lead')}) | 回落:{s.get('pullback')}")
