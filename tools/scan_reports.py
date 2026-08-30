#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
筛选报告扫描与规则诊断工具（非权威交易裁决）
支持单文件解析、全天多时段扫描、5/5 与 4/5 候选过滤、在盯清单跟踪与信号衰减检测。
输出仅用于报告审计和候选发现；最终交易裁决由 `盘中` skill 按根目录《选股框架.md》执行。
"""

import os
import sys
import re
import argparse
from typing import Dict, List, Any, Optional

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)
sys.path.insert(0, PROJECT_ROOT)
from report_parser import parse_screening_report, get_report_files
from tools.rule_config import RULE_CONFIG

BASE_REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))

# 时间窗口交易权限映射（来源：trading-rules.md）
TIME_WINDOWS = {
    "observe_only":     ("0930", "1010", "观察期·禁买"),
    "pending_confirm":  ("1010", "1020", "等待确认"),
    "primary_window":   ("1020", "1045", "第一买点窗口"),
    "morning_confirm":  ("1045", "1130", "早盘确认期"),
    "lunch_break":      ("1130", "1300", "午休"),
    "afternoon_reflux": ("1300", "1345", "午后回流·仓位减半"),
    "afternoon_late":   ("1345", "1420", "午后尾段"),
    "tail_risk":        ("1420", "1440", "尾盘风控·禁新仓"),
    "market_close":     ("1440", "1515", "收盘·仅持仓管理"),
}

def get_time_permission(time_str: str) -> tuple:
    """
    根据报告时间返回交易权限标签。
    返回: (permission_key, display_label)
    """
    t = time_str.replace(":", "")
    for key, (start, end, label) in TIME_WINDOWS.items():
        if start <= t < end:
            return (key, label)
    if t < "0930":
        return ("pre_market", "盘前")
    return ("market_close", "收盘·仅持仓管理")

def parse_amount(val_str: str) -> float:
    """将 '+1234万', '-5.6亿', '+0' 等字符串转换为以万元为单位的浮点数"""
    if not val_str or val_str in ["-", "+0", "0", "数据不足"]:
        return 0.0
    s = val_str.replace("+", "").replace(",", "").strip()
    if "亿" in s:
        try:
            return float(s.replace("亿", "")) * 10000.0
        except ValueError:
            return 0.0
    if "万" in s:
        try:
            return float(s.replace("万", ""))
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def parse_pct(val_str: str) -> float:
    """将 '7.0%', '0.61pct', '-1.5%' 转换为浮点数百分比"""
    if not val_str or val_str in ["-", "数据不足"]:
        return 0.0
    s = val_str.replace("%", "").replace("pct", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0

def evaluate_low_absorb_candidate(row: Dict[str, str], is_morning_a: bool = False,
                                   locked_pullback: Optional[float] = None) -> Dict[str, Any]:
    """
    根据《选股框架.md》评估低吸候选标的是否满足 5/5 规则：
    1. 公告风控: clean (avoid 一票否决, watch_risk 仅减分)
    2. 主力净占比: >5% (分档: 回落<1%→>5%, 回落<2%→>10%, 回落<3%→>15%)
    3. 5分钟增量: >100万 (早盘A类 >500万)
    4. 高位回落: <1% (或符合主力分档)
    5. 超大单主导: 只读取生产报告标签 `✓`/`✓(绝对)`/`✓(合力)`；
       本诊断工具不得用数值重新推导主导类型
    6. 板块共振/联动: 共振=='是' 或 板块内候选>=2 (A类必备)

    locked_pullback: 若提供，使用此锁定的回落值进行分档判定（首次 5/5 触发时锁定），
                     解决回踩 VWAP 买点与回落分档互斥的悖论（8/14 超声电子教训）。
    """
    code = row.get("代码", "")
    name = row.get("名称", "")
    price = row.get("现价", "")
    pct = row.get("涨幅", "")
    plate = row.get("板块", "")
    plate_count_str = row.get("板块内候选", "1")
    plate_count = int(plate_count_str) if plate_count_str.isdigit() else 1
    resonance = row.get("共振", "否") == "是"
    pullback = parse_pct(row.get("高位回落", "0"))
    vwap = row.get("均价线", "")
    main_pct = parse_pct(row.get("主力净占比", "0"))
    main_net_wan = parse_amount(row.get("主力净额", "0"))
    if main_net_wan == 0.0 and main_pct != 0.0:
        amt_str = row.get("成交额", "0")
        amt_wan = parse_amount(amt_str)
        if amt_wan > 0:
            main_net_wan = amt_wan * (main_pct / 100.0)
    inc5_wan = parse_amount(row.get("5分钟增量", "0"))
    super_order_str = row.get("超大单", "0")
    super_lead_str = row.get("超单主导", "")
    announcement = row.get("公告风险", "clean")
    low_cfg = RULE_CONFIG["screening"]["low_absorb"]

    checks = {}
    fails = []
    passes = 0

    # 1. 公告检查 (avoid 一票否决)
    if "avoid" in announcement:
        checks["announcement"] = False
        fails.append(f"公告硬否决({announcement})")
    else:
        checks["announcement"] = True
        passes += 1

    # 2. 回落与主力匹配检查（回落分档）
    # 标准: 回落<1% -> 主力>5%; 回落<2% -> 主力>10%; 回落<3% -> 主力>15%
    # 回落基准锁定：首次 5/5 触发时锁定回落值，后续回踩买入时不重算分档
    # （解决 8/14 超声电子悖论：回踩 VWAP 是好事，但回落扩大卡分档 = 永远买不到）
    check_pullback = locked_pullback if locked_pullback is not None else pullback
    using_locked = locked_pullback is not None

    rebound_pass = False
    for tier in low_cfg["pullback_tiers"]:
        if (
            check_pullback < float(tier["max_pullback_exclusive"])
            and main_pct > float(tier["main_pct_min_exclusive"])
        ):
            rebound_pass = True
            break

    if rebound_pass:
        checks["main_fund_pullback"] = True
        passes += 1
    else:
        checks["main_fund_pullback"] = False
        lock_note = f" [锁定基准{locked_pullback:.2f}%]" if using_locked else ""
        fails.append(f"主力/回落未匹配(主力{main_pct:.1f}%, 回落{pullback:.2f}%{lock_note})")

    # 3. 5分钟增量检查
    min_inc5_cny = (
        float(low_cfg["flow_5m_a_min"])
        if is_morning_a
        else float(low_cfg["flow_5m_b_min"])
    )
    min_inc5 = min_inc5_cny / 10_000.0
    if inc5_wan >= min_inc5:
        checks["inc5"] = True
        passes += 1
    else:
        checks["inc5"] = False
        fails.append(f"5分增量不足({inc5_wan:+.0f}万 < {min_inc5:.0f}万)")

    # 4. 超大单主导检查：生产报告标签是唯一权威来源。
    # 本诊断工具不得按金额/比例重新推导 coalition，避免绕过主买比与历史快照门槛。
    super_wan = parse_amount(super_order_str)

    dominance_label = "✗"
    if RULE_CONFIG["dominance"]["negative_super_veto"] and super_wan < 0:
        checks["super_lead"] = False
        fails.append("超大单为负，一票否决")
    elif super_lead_str in {
        "✓",
        RULE_CONFIG["dominance"]["absolute"]["label"],
        RULE_CONFIG["dominance"]["coalition"]["label"],
    }:
        checks["super_lead"] = True
        dominance_label = (
            RULE_CONFIG["dominance"]["coalition"]["label"]
            if super_lead_str == RULE_CONFIG["dominance"]["coalition"]["label"]
            else RULE_CONFIG["dominance"]["absolute"]["label"]
        )
        passes += 1
    elif super_lead_str == "✓(合力)":
        checks["super_lead"] = True
        dominance_label = "✓(合力)"
        passes += 1
    else:
        checks["super_lead"] = False
        fails.append("生产报告未给出严格超单主导标签")

    # 5. 板块共振或联动
    if resonance or plate_count >= int(low_cfg["resonance_candidates_min"]):
        checks["plate_resonance"] = True
        passes += 1
    else:
        checks["plate_resonance"] = False
        fails.append(f"无板块共振/联动不足({plate_count}只)")

    # 均价线状态
    above_vwap = "上方" in vwap

    score = passes  # 满分 5
    is_5_of_5 = (score == 5) and ("avoid" not in announcement) and (checks.get("super_lead", False))

    return {
        "code": code,
        "name": name,
        "price": price,
        "pct": pct,
        "plate": plate,
        "main_pct": main_pct,
        "inc5_wan": inc5_wan,
        "pullback": pullback,
        "locked_pullback": locked_pullback,  # 回落锁定基准（None=未锁定）
        "super_lead": dominance_label,
        "resonance": resonance,
        "plate_count": plate_count,
        "announcement": announcement,
        "above_vwap": above_vwap,
        "score": score,
        "is_5_of_5": is_5_of_5,
        "fails": fails,
        "raw": row
    }

def scan_single_file(filepath: str, pullback_locks: Optional[Dict[str, Dict]] = None) -> Dict[str, Any]:
    """
    解析并评估单份报告。

    pullback_locks: 跨报告的回落锁定状态。key=股票代码, value={"pullback": float, "time": str}。
                    首次 5/5 通过时锁定该股票的回落值，后续报告使用锁定值进行分档判定。
    """
    rep = parse_screening_report(filepath)
    low_short = rep["tables"]["low_absorb_short"]
    
    # 判断是否早盘
    time_str = rep["time"].replace(":", "")
    is_morning = "0925" <= time_str <= "1030"

    evaluated = []
    for row in low_short:
        code = row.get("代码", "")
        is_a = (row.get("类") == "A") and is_morning

        # 查找回落锁定基准
        locked_pb = None
        if pullback_locks and code in pullback_locks:
            locked_pb = pullback_locks[code]["pullback"]

        ev = evaluate_low_absorb_candidate(row, is_morning_a=is_a, locked_pullback=locked_pb)
        evaluated.append(ev)

    # 状态机查找表
    sm_lookup = {}
    for sm_r in rep["tables"].get("state_machine", []):
        c = sm_r.get("代码", "")
        if c:
            stage = sm_r.get("状态阶段", sm_r.get("相位", "状态机"))
            sm_lookup[c] = {
                "stage": stage,
                "phase": sm_r.get("相位", ""),
                "trigger_price": sm_r.get("触发价", ""),
                "vwap": sm_r.get("当时VWAP", ""),
                "retest_zone": sm_r.get("回踩观察区", ""),
                "invalid_reason": sm_r.get("失效原因", "-")
            }

    for ev in evaluated:
        if ev["code"] in sm_lookup:
            ev["state_machine"] = sm_lookup[ev["code"]]
        else:
            ev["state_machine"] = None

    passed_5 = [e for e in evaluated if e["is_5_of_5"]]
    near_4 = [e for e in evaluated if e["score"] == 4 and "avoid" not in e["announcement"]]

    # 时间窗口交易权限
    perm_key, perm_label = get_time_permission(rep["time"])

    return {
        "file": rep["file"],
        "date": rep["date"],
        "time": rep["time"],
        "time_permission": perm_key,
        "time_permission_label": perm_label,
        "market": rep["market"],
        "pools_count": rep["pools_count"],
        "tomorrow_watchlist": rep["tables"]["tomorrow_watchlist"],
        "dual_intersection": rep["tables"]["dual_intersection"],
        "state_machine": rep["tables"]["state_machine"],
        "total_low_short": len(low_short),
        "passed_5": passed_5,
        "near_4": near_4,
        "all_evaluated": evaluated
    }

def scan_day(date_str: Optional[str] = None, top_n_latest: int = 0):
    """扫描指定日期的所有报告或最新报告（含跨报告回落锁定）"""
    files = get_report_files(BASE_REPORTS_DIR, date_str)
    if not files:
        print(f"未找到相关报告文件: date={date_str}")
        return

    if top_n_latest > 0:
        files = files[-top_n_latest:]

    print(f"=== 正在扫描 {len(files)} 份筛选报告 ({files[0].split('/')[-2]}) ===")
    
    all_passed_history = {}  # code -> [times]
    last_res = None

    # 回落锁定状态：首次 5/5 通过时锁定回落值，后续报告复用
    # 解决「回踩 VWAP = 好买点」与「回落扩大卡分档」的悖论（8/14 超声电子教训）
    pullback_locks: Dict[str, Dict] = {}  # code -> {"pullback": float, "time": str}
    all_state_machine_events = {}  # code -> list of (time, stage, summary)

    for f in files:
        res = scan_single_file(f, pullback_locks=pullback_locks)
        last_res = res
        time_tag = res["time"]

        # 记录全天状态机事件
        for sm_r in res.get("state_machine", []):
            sm_code = sm_r.get("代码", "")
            sm_name = sm_r.get("名称", "")
            if sm_code:
                key = f"{sm_code} {sm_name}"
                if key not in all_state_machine_events:
                    all_state_machine_events[key] = []
                stage = sm_r.get("状态阶段", sm_r.get("相位", "状态机"))
                retest = sm_r.get("回踩观察区", "")
                retest_txt = f" 回踩区:{retest}" if retest and retest != "-" else ""
                all_state_machine_events[key].append((time_tag, stage, f"{stage}{retest_txt}"))

        # 对新通过 5/5 的标的锁定回落基准
        for p in res["passed_5"]:
            if p["code"] not in pullback_locks:
                pullback_locks[p["code"]] = {
                    "pullback": p["pullback"],
                    "time": time_tag
                }

            code_key = f"{p['code']} {p['name']}"
            if code_key not in all_passed_history:
                all_passed_history[code_key] = []
            all_passed_history[code_key].append((time_tag, p))

    print(f"\n【最新快照 {last_res['time']}】")
    m = last_res["market"]
    if m:
        print(f"市场宽度: 上涨 {m.get('up','-')} / 下跌 {m.get('down','-')} | 涨停 {m.get('zt','-')} / 跌停 {m.get('dt','-')}")
    p_cnt = last_res["pools_count"]
    if p_cnt:
        print(f"双池状态: 超短池 {p_cnt.get('short',0)} | 趋势观察 {p_cnt.get('trend_obs',0)} | 趋势确认 {p_cnt.get('trend_conf',0)} | 交集 {p_cnt.get('intersection',0)}")

    # 输出双池交集状态机事件（default-v2 状态机打通）
    if all_state_machine_events or last_res.get("state_machine"):
        print(f"\n【双池交集状态机（default-v2）】")
        if last_res.get("state_machine"):
            for sm_r in last_res["state_machine"]:
                print(f"  🎯 [当前] {sm_r.get('代码')} {sm_r.get('名称')} | 阶段:{sm_r.get('状态阶段', sm_r.get('相位','-'))} | 触发价:{sm_r.get('触发价','-')} | VWAP:{sm_r.get('当时VWAP','-')} | 回踩区:{sm_r.get('回踩观察区','-')}")
        for code_name, events in all_state_machine_events.items():
            first_ev = events[0]
            last_ev = events[-1]
            print(f"  ⚡ {code_name}: 初次 {first_ev[0]}[{first_ev[1]}] → 最新 {last_ev[0]}[{last_ev[1]}] (共 {len(events)} 快照)")

    # 输出回落锁定状态
    if pullback_locks:
        print(f"\n【回落基准锁定】（首次 5/5 触发时锁定，后续回踩不重算分档）")
        for lk_code, lk_info in pullback_locks.items():
            print(f"  🔒 {lk_code}: 锁定回落 {lk_info['pullback']:.2f}% @ {lk_info['time']}")

    # 时间窗口标签
    perm_label = last_res["time_permission_label"]
    perm_key = last_res["time_permission"]
    tradeable = perm_key in ("primary_window", "morning_confirm", "afternoon_reflux", "afternoon_late")
    perm_icon = "🟢" if tradeable else "🔴"
    print(f"\n【时间窗口】{perm_icon} {last_res['time']} → {perm_label}")

    print("\n【当前 5/5 全通过标的】")
    if last_res["passed_5"]:
        for p in last_res["passed_5"]:
            lock_tag = " [锁定]" if p.get("locked_pullback") is not None else ""
            time_tag = f" [{perm_label}]" if not tradeable else ""
            sm_tag = f" 🎯[状态机:{p['state_machine']['stage']}]" if p.get("state_machine") else ""
            print(f"  ⭐ {p['code']} {p['name']} 现价:{p['price']}({p['pct']}) 主力:{p['main_pct']:.1f}% 5分:{p['inc5_wan']:+.0f}万 回落:{p['pullback']:.2f}%{lock_tag} 超单主导:{p['super_lead']} 板块:{p['plate']} 公告:{p['announcement']}{time_tag}{sm_tag}")
    else:
        print("  (无 5/5 通过标的)")

    print("\n【当前 4/5 差一步候选及卡点】")
    if last_res["near_4"]:
        for p in last_res["near_4"]:
            sm_tag = f" 🎯[状态机:{p['state_machine']['stage']}]" if p.get("state_machine") else ""
            print(f"  👀 {p['code']} {p['name']} 现价:{p['price']}({p['pct']}) 主力:{p['main_pct']:.1f}% 5分:{p['inc5_wan']:+.0f}万 | 卡点: {', '.join(p['fails'])}{sm_tag}")
    else:
        print("  (无 4/5 候选)")

    print("\n【全天曾通过 5/5 汇总及出现时段】")
    if all_passed_history:
        for code_name, records in all_passed_history.items():
            times = [r[0] for r in records]
            # 标注首次出现的时间窗口
            first_perm = get_time_permission(times[0])
            last_perm = get_time_permission(times[-1])
            print(f"  🎯 {code_name}: 出现 {len(times)} 次 (初次 {times[0]}[{first_perm[1]}] → 末次 {times[-1]}[{last_perm[1]}])")
    else:
        print("  (全天无 5/5 通过标的)")

    if last_res["tomorrow_watchlist"]:
        print("\n【明日观察池】")
        for r in last_res["tomorrow_watchlist"]:
            print(f"  📌 {r.get('代码')} {r.get('名称')} 触发:{r.get('触发价')} 低吸区:{r.get('低吸区')} 禁区:{r.get('追高禁区')} 板块:{r.get('板块')} 理由:{r.get('理由','')[:30]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A股短线筛选报告扫描与规则判定")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD，默认最新日期")
    parser.add_argument("--file", type=str, default=None, help="单份报告文件路径")
    parser.add_argument("--latest", type=int, default=0, help="仅扫描最新 N 份报告")
    args = parser.parse_args()

    if args.file:
        res = scan_single_file(args.file)
        print(f"=== 报告 {res['file']} 扫描结果 ===")
        print(f"时间: {res['time']}")
        print(f"5/5 通过: {len(res['passed_5'])} 只")
        for p in res["passed_5"]:
            print(f"  ⭐ {p['code']} {p['name']} 现价:{p['price']} 主力:{p['main_pct']:.1f}% 5分:{p['inc5_wan']:+.0f}万 超单:{p['super_lead']}")
        print(f"4/5 差一步: {len(res['near_4'])} 只")
        for p in res["near_4"]:
            print(f"  👀 {p['code']} {p['name']} 卡点: {', '.join(p['fails'])}")
    else:
        scan_day(args.date, args.latest)
