#!/usr/bin/env python3
"""非权威诊断导出工具；超单主导类型只读取生产报告标签，不做数值推导。"""
import os, sys, glob, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.report_parser import parse_screening_report
from tools.query_quote import fetch_realtime_quotes

files = sorted(glob.glob("筛选结果/A股筛选结果_20260820_*.md"))
files_1024 = [f for f in files if re.search(r"_(\d{4})\.md$", f) and re.search(r"_(\d{4})\.md$", f).group(1) >= "1024"]

# 获取所有候选标的最新快照数据
all_stocks = {}
for f in files_1024:
    rep = parse_screening_report(f)
    t = rep["time"]
    for tbl_name in ["low_absorb_short", "capital_ranking", "state_machine"]:
        for r in rep["tables"].get(tbl_name, []):
            code = r.get("代码")
            if not code: continue
            if code not in all_stocks:
                all_stocks[code] = {"name": r.get("名称"), "last_seen": t, "records": []}
            all_stocks[code]["records"].append((t, tbl_name, r))

# 核心标的池
target_codes = [
    # 模拟持仓
    "601899", "603993", "000756",
    # 重点候选 / 观察池
    "000963", "000973", "600531", "002603", "000887", "600490", "605179", "605167", "600415", "601666",
    # 其他在表候选
    "000989", "603998", "002923", "600488", "600161", "603387", "003010", "600353", "002637"
]

quotes = fetch_realtime_quotes(target_codes)

def to_wan(s):
    if not s or s in ["-", "None", "基准不足"]: return 0.0
    s = s.replace("+", "").replace(",", "").strip()
    if "亿" in s: return float(s.replace("亿", "")) * 10000.0
    if "万" in s: return float(s.replace("万", ""))
    try: return float(s)
    except: return 0.0

parsed_candidates = []
for code in target_codes:
    q = quotes.get(code, {})
    name = q.get("name", all_stocks.get(code, {}).get("name", code))
    recs = all_stocks.get(code, {}).get("records", [])
    
    # 找最近的有效数据
    latest_r = {}
    last_t = "历史"
    for t_val, tbl, r_val in reversed(recs):
        if r_val.get("主力净占比") or r_val.get("主力净额") or r_val.get("超大单"):
            latest_r = r_val
            last_t = t_val
            break
    if not latest_r and recs:
        latest_r = recs[-1][2]
        last_t = recs[-1][0]
        
    main_pct_str = latest_r.get("主力净占比", "-")
    main_net_str = latest_r.get("主力净额", "-")
    inc5_str = latest_r.get("5分钟增量", "-")
    super_str = latest_r.get("超大单", "-")
    super_lead_str = latest_r.get("超单主导", "-")
    pb_str = latest_r.get("高位回落", "-")
    vwap_str = latest_r.get("均价线", "-")
    risk_str = latest_r.get("公告风控", "clean")
    plate_str = latest_r.get("板块", "-")
    
    main_net_wan = to_wan(main_net_str)
    super_wan = to_wan(super_str)
    inc5_wan = to_wan(inc5_str)
    
    # 如果报告里主力净额为空，但主力净占比和成交额在，可以推算
    if main_net_wan == 0.0 and main_pct_str not in ["-", "None"] and q.get("amount_wan", 0) > 0:
        try:
            pct_val = float(main_pct_str.replace("%", ""))
            main_net_wan = q.get("amount_wan", 0) * (pct_val / 100.0)
        except: pass

    # 超大单主导判定：生产报告标签是唯一权威来源，禁止数值兜底。
    is_super_pos = super_wan > 0
    super_ratio = (super_wan / main_net_wan * 100) if main_net_wan > 0 else (100.0 if super_wan > 0 else 0.0)

    dominance_type = "none"
    if is_super_pos and super_lead_str in ["✓", "✓(绝对)"]:
        dominance_type = "absolute"
    elif is_super_pos and super_lead_str == "✓(合力)":
        dominance_type = "coalition"
        
    is_super_dominant = dominance_type in ["absolute", "coalition"]
    
    parsed_candidates.append({
        "code": code,
        "name": name,
        "last_time": last_t,
        "quote": q,
        "main_pct": main_pct_str,
        "main_net_str": main_net_str,
        "main_net_wan": main_net_wan,
        "inc5_str": inc5_str,
        "inc5_wan": inc5_wan,
        "super_str": super_str,
        "super_wan": super_wan,
        "super_lead_str": super_lead_str,
        "super_ratio": super_ratio,
        "dominance_type": dominance_type,
        "is_super_pos": is_super_pos,
        "is_super_dominant": is_super_dominant,
        "pullback": pb_str,
        "vwap": vwap_str,
        "risk": risk_str,
        "plate": plate_str
    })

print(json.dumps(parsed_candidates, ensure_ascii=False, indent=2))
