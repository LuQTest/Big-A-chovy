#!/usr/bin/env python3
"""历史候选诊断脚本（非权威交易裁决，最终以盘中 skill 和选股框架为准）。"""
import os, glob, re, json
from tools.report_parser import parse_screening_report
from tools.query_quote import fetch_realtime_quotes

files = sorted(glob.glob("筛选结果/A股筛选结果_20260820_*.md"))
files_1024 = [f for f in files if re.search(r"_(\d{4})\.md$", f) and re.search(r"_(\d{4})\.md$", f).group(1) >= "1024"]

# 获取所有在 10:24-11:02 出现过的标的最新快照数据
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

codes = list(all_stocks.keys())
# 加上持仓标的与在盯标的
extra_codes = ["601899", "603993", "000756", "000963", "000973", "600531", "002603", "601666", "600490", "000887", "600415", "605179", "605167"]
for c in extra_codes:
    if c not in codes: codes.append(c)

quotes = fetch_realtime_quotes(codes)

print("=== 候选标的深度盘问与资金强度排序 ===")

# 解析每只股票在报告中的最新资金指标与实时盘口
summary_list = []

for code in codes:
    quote = quotes.get(code, {})
    name = quote.get("name", all_stocks.get(code, {}).get("name", code))
    
    # 提取最近的报告指标
    recs = all_stocks.get(code, {}).get("records", [])
    latest_r = recs[-1][2] if recs else {}
    last_t = recs[-1][0] if recs else "历史"
    
    main_pct_str = latest_r.get("主力净占比", "-")
    main_net_str = latest_r.get("主力净额", "-")
    inc5_str = latest_r.get("5分钟增量", "-")
    super_str = latest_r.get("超大单", "-")
    super_lead_str = latest_r.get("超单主导", "-")
    pb_str = latest_r.get("高位回落", "-")
    vwap_str = latest_r.get("均价线", "-")
    risk_str = latest_r.get("公告风控", "clean")
    plate_str = latest_r.get("板块", "-")
    
    # 解析主力净额数值用于排序
    def to_wan(s):
        if not s or s in ["-", "None", "基准不足"]: return 0.0
        s = s.replace("+", "").replace(",", "").strip()
        if "亿" in s: return float(s.replace("亿", "")) * 10000.0
        if "万" in s: return float(s.replace("万", ""))
        try: return float(s)
        except: return 0.0
    
    main_net_wan = to_wan(main_net_str)
    super_wan = to_wan(super_str)
    
    summary_list.append({
        "code": code,
        "name": name,
        "last_time": last_t,
        "quote": quote,
        "main_pct": main_pct_str,
        "main_net": main_net_str,
        "main_net_wan": main_net_wan,
        "inc5": inc5_str,
        "super": super_str,
        "super_wan": super_wan,
        "super_lead": super_lead_str,
        "pullback": pb_str,
        "vwap": vwap_str,
        "risk": risk_str,
        "plate": plate_str,
        "raw_record": latest_r
    })

# 按主力净额（万元）从大到小排序
summary_list.sort(key=lambda x: x["main_net_wan"], reverse=True)

for item in summary_list:
    c = item["code"]
    n = item["name"]
    q = item["quote"]
    p = q.get("price", 0.0)
    pct = q.get("pct", 0.0)
    high = q.get("high", 0.0)
    low = q.get("low", 0.0)
    amt = q.get("amount_wan", 0.0)
    
    b_orders = q.get("buy_orders", [])
    s_orders = q.get("sell_orders", [])
    
    b_total = sum([v for _, v in b_orders])
    s_total = sum([v for _, v in s_orders])
    
    # 验证超大单是否为正且主导（双轨判定：absolute 绝对主导 vs coalition 合力主导）
    is_super_pos = item["super_wan"] > 0
    super_ratio = (item["super_wan"] / item["main_net_wan"] * 100) if item["main_net_wan"] > 0 else 0
    
    dom_type = item.get("dominance_type", "none")
    if dom_type == "none":
        if is_super_pos and super_ratio >= 50.0:
            dom_type = "absolute"
        elif item["super_wan"] >= 2000.0 and item["main_net_wan"] >= 5000.0 and super_ratio >= 20.0:
            dom_type = "coalition"
        elif item["super_lead"] in ["✓", "✓(绝对)"]:
            dom_type = "absolute"
        elif item["super_lead"] in ["✓(合力)"]:
            dom_type = "coalition"
            
    is_super_dominant = dom_type in ("absolute", "coalition")
    
    if dom_type == "absolute":
        super_status = "✅ 超大单绝对主导(>50%)"
    elif dom_type == "coalition":
        super_status = "🔥 游资机构合力主升(超单≥2000万+主力≥5000万)"
    elif item["super_wan"] < 0:
        super_status = "❌ 超大单为负/散户堆量"
    else:
        super_status = "⚠️ 超大单未主导"
    
    print(f"\n=======================================================")
    print(f"【{n}】({c}) | 现价: {p:.2f} ({pct:+.2f}%) | 最高: {high:.2f} 最低: {low:.2f} | 成交额: {amt:.1f}万")
    print(f"  报告时点: {item['last_time']} | 板块: {item['plate']} | 风控: {item['risk']}")
    print(f"  资金强度: 主力净额 {item['main_net']} (占比 {item['main_pct']}) | 5分增量 {item['inc5']}")
    print(f"  超大单: {item['super']} (主导标记: {item['super_lead']}, 占比: {super_ratio:.1f}%) → {super_status}")
    print(f"  回落: {item['pullback']} | 均价线: {item['vwap']}")
    print(f"  [五档买盘合计: {b_total} 手 | 五档卖盘合计: {s_total} 手 | 委比偏向: {'买盘占优' if b_total > s_total else '卖盘占优'}]")
    print(f"  买盘五档:")
    for idx, (bp, bv) in enumerate(b_orders, 1):
        print(f"    买{idx}: {bp:.2f} ({bv}手)")
    print(f"  卖盘五档:")
    for idx, (sp, sv) in enumerate(s_orders, 1):
        print(f"    卖{idx}: {sp:.2f} ({sv}手)")
