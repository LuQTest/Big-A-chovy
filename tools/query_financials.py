#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票财务盈利/亏损与年内表现(YTD)查询工具
支持单股/批量体检、雪球代码格式（SH600598 / SZ003010 / 600598），
输出：
1. 今年公司经营业绩（盈利/亏损、归母净利润、扣非净利润、每股收益EPS、动态PE、市净率PB、营收同比）
2. 今年股价走势（年初至今涨跌幅 YTD %）
3. 建仓基本面安全过滤建议（排除亏损股、动态PE负值股）
"""

import sys
import json
import re
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "daily-stock-analysis" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import tencent_kline  # noqa: E402  腾讯日 K 主机列表单一来源

def normalize_code_clean(code: str) -> str:
    """提取6位纯数字代码"""
    code_str = str(code).strip().upper()
    m = re.search(r"\d{6}", code_str)
    if m:
        return m.group(0)
    return code_str

def get_secid(code: str) -> str:
    c = normalize_code_clean(code)
    return ("1." if c.startswith("6") or c.startswith("9") else "0.") + c

def get_tsym(code: str) -> str:
    c = normalize_code_clean(code)
    return ("sh" if c.startswith("6") or c.startswith("9") else "sz") + c

def query_financial_profile(code: str) -> Dict[str, Any]:
    c = normalize_code_clean(code)
    secid = get_secid(c)
    tsym = get_tsym(c)
    
    # 1. 东财 webguest 财务与基本面接口
    url = f"https://push2.eastmoney.com/webguest/api/qt/stock/get?secid={secid}&fields=f57,f58,f43,f59,f162,f163,f164,f167,f173,f183,f184,f185,f186,f187"
    cmd = f"curl -s --connect-timeout 4 \"{url}\""
    
    d = {}
    try:
        out = subprocess.check_output(cmd, shell=True).decode("utf-8", errors="ignore")
        d = json.loads(out).get("data", {}) or {}
    except Exception:
        d = {}

    name = d.get("f58", "")
    price = (float(d.get("f43")) / 100) if d.get("f43") and d.get("f43") != "-" else 0.0
    
    # PE / PB / EPS / Revenue / NetProfit
    pe = (float(d.get("f162")) / 100) if d.get("f162") and d.get("f162") != "-" else None
    pe_ttm = (float(d.get("f164")) / 100) if d.get("f164") and d.get("f164") != "-" else None
    pb = (float(d.get("f167")) / 100) if d.get("f167") and d.get("f167") != "-" else None
    eps = (float(d.get("f187")) / 100) if d.get("f187") and d.get("f187") != "-" else None
    rev_yi = (float(d.get("f183")) / 100000000) if d.get("f183") and d.get("f183") != "-" else None
    rev_tb = (float(d.get("f184"))) if d.get("f184") and d.get("f184") != "-" else None
    net_profit_tb = (float(d.get("f186"))) if d.get("f186") and d.get("f186") != "-" else None

    # 2. 腾讯行情快速补充（防止东财偶发缺失）
    t_url = f"https://qt.gtimg.cn/q={tsym}"
    t_cmd = f"curl -s --connect-timeout 4 \"{t_url}\" | iconv -f GBK -t UTF-8"
    try:
        t_out = subprocess.check_output(t_cmd, shell=True).decode("utf-8", errors="ignore")
        if "~" in t_out:
            t_parts = t_out.split("~")
            if not name and len(t_parts) > 2:
                name = t_parts[1]
            if price == 0.0 and len(t_parts) > 3 and t_parts[3]:
                price = float(t_parts[3])
            if pe is None and len(t_parts) > 39 and t_parts[39]:
                pe = float(t_parts[39])
            if pb is None and len(t_parts) > 46 and t_parts[46]:
                pb = float(t_parts[46])
    except Exception:
        pass

    # 3. 计算今年股价涨跌幅 (YTD)
    # 2026-09-26 口径修正：以「去年最后一个交易日收盘价」为基准（常见 YTD 口径）；
    # 旧实现用年内首个交易日开盘价作分母，会把首日跳空算进年内涨跌。
    # 次新股没有去年数据时退回年内首个交易日开盘价，并在结果里标注基准。
    ytd_pct = None
    ytd_error = None
    ytd_basis = ""
    year = datetime.now().year
    try:
        # require_qfq=True：YTD 跨除权日，必须用前复权价；腾讯若只给未复权 day 就报缺失，
        # 宁可显示"未取到"也不给出被除权污染的涨跌幅。
        payload, _ = tencent_kline.fetch_kline_json(
            tsym, 300, start=f"{year - 1}-12-01", end=f"{year}-12-31",
            require_qfq=True, timeout=6)
        days = tencent_kline.kline_rows(payload, tsym, require_qfq=True)
        prev_year_rows = [d for d in days if str(d[0]) < f"{year}-01-01"]
        this_year_rows = [d for d in days if str(d[0]) >= f"{year}-01-01"]
        base = None
        if prev_year_rows:
            base = float(prev_year_rows[-1][2])
            ytd_basis = f"{prev_year_rows[-1][0]} 收盘"
        elif this_year_rows:
            base = float(this_year_rows[0][1])
            ytd_basis = f"{this_year_rows[0][0]} 开盘（次新股，无去年数据）"
        if base and base > 0:
            last_close = price if price > 0 else float((this_year_rows or days)[-1][2])
            ytd_pct = round((last_close - base) / base * 100.0, 2)
        else:
            ytd_error = "腾讯日K未返回可用于 YTD 的数据"
    except Exception as exc:
        # 不做静默失败：YTD 是框架要求的基本面四项之一，取不到必须显式标注待核验
        ytd_error = f"腾讯日K不可用（{type(exc).__name__}）"

    # 盈利与亏损状态判定
    if eps is not None:
        if eps > 0:
            fin_status = "盈利 🟢"
            fin_color = "green"
        elif eps < 0:
            fin_status = "亏损 🔴"
            fin_color = "red"
        else:
            fin_status = "微利/平衡 🟡"
            fin_color = "yellow"
    elif pe is not None:
        if pe > 0:
            fin_status = "盈利 🟢"
            fin_color = "green"
        else:
            fin_status = "亏损 🔴"
            fin_color = "red"
    else:
        fin_status = "待披露/未知 ⚪"
        fin_color = "gray"

    # 建仓安全评级建议
    if fin_color == "red" or (pe is not None and pe < 0):
        safety_advice = "❌ 亏损股(不宜重仓)"
    elif fin_color == "green" and (pe is not None and pe > 0 and pe < 60):
        safety_advice = "✅ 稳健盈利(安全)"
    elif fin_color == "green" and (pe is not None and pe >= 60):
        safety_advice = "⚠️ 盈利但高估值"
    else:
        safety_advice = "⚪ 正常观察"

    return {
        "code": c,
        "name": name,
        "price": price,
        "pe": pe,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "eps": eps,
        "revenue_yi": rev_yi,
        "revenue_growth": rev_tb,
        "net_profit_growth": net_profit_tb,
        "fin_status": fin_status,
        "fin_color": fin_color,
        "safety_advice": safety_advice,
        "ytd_pct": ytd_pct,
        "ytd_error": ytd_error,
        "ytd_basis": ytd_basis,
    }

def print_summary_table(results: List[Dict[str, Any]]):
    print("\n" + "=" * 92)
    print(f"{'代码':<8} {'名称':<8} {'现价':>7} {'今年经营业绩':<12} {'每股收益EPS':>11} {'动态PE':>8} {'今年股价涨跌(YTD)':>17} {'基本面建议':<14}")
    print("-" * 92)
    for r in results:
        code = r["code"]
        name = r["name"]
        price = f"{r['price']:.2f}" if r["price"] else "-"
        fin = r["fin_status"]
        eps = f"{r['eps']:+.3f}元" if r["eps"] is not None else "-"
        pe = f"{r['pe']:.1f}" if r["pe"] is not None else "-"
        ytd = f"{r['ytd_pct']:+.2f}%" if r["ytd_pct"] is not None else "-"
        ytd_colored = f"{ytd} 🟢" if (r["ytd_pct"] and r["ytd_pct"] > 0) else (f"{ytd} 🔴" if (r["ytd_pct"] and r["ytd_pct"] < 0) else f"{ytd}")
        advice = r["safety_advice"]
        print(f"{code:<8} {name:<8} {price:>7} {fin:<12} {eps:>11} {pe:>8} {ytd_colored:>19} {advice:<14}")
    print("=" * 92)
    bases = {r.get("ytd_basis") for r in results if r.get("ytd_basis")}
    if bases:
        print(f"YTD 口径：以去年最后一个交易日收盘价为基准；本批基准 = {', '.join(sorted(bases))}")
    # 数据缺失必须显式说明，不能只显示 "-" 让人误以为没有该项
    missing = [r for r in results if r.get("ytd_error")]
    for r in missing:
        print(f"⚠ {r['code']} {r['name']}：YTD 未取到 —— {r['ytd_error']}。"
              "按框架，基本面数据缺失应列为待核验项、暂不开真实仓。")
    if missing:
        print("=" * 92)
    print()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="股票今年盈利/亏损及年内收益查询工具")
    parser.add_argument("codes", nargs="+", help="股票代码列表，支持 600598, SH600598, sz003010 等格式")
    args = parser.parse_args()

    res_list = []
    for code_input in args.codes:
        for single_code in code_input.split(","):
            c = single_code.strip()
            if c:
                info = query_financial_profile(c)
                res_list.append(info)
    
    print_summary_table(res_list)
