#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块退潮盘中预警工具
持仓期间持续监控同板块入池数与资金方向，触发退潮预警。

用法示例：
  # 监控南山铝业（有色金属板块），从 8/12 10:05 买入后开始
  python3 tools/watch_sector.py 600219 有色金属 --date 20260812 --from 1005 --buy-count 5

  # 监控中航西飞（航天军工板块），从当日开盘起
  python3 tools/watch_sector.py 000768 航天军工

来源：优化分析 #1 —— 南山铝业 −222元、中航西飞 −198元 均因板块退潮未及时预警。
"""

import os
import sys
import re
import argparse
from typing import Dict, List, Any, Optional
from report_parser import parse_screening_report, get_report_files

BASE_REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))


def parse_amount_value(val_str: str) -> float:
    """将 '+1234万', '-5.6亿', '+0' 等转换为以万元为单位的浮点数"""
    if not val_str or val_str in ["-", "+0", "0", "数据不足", "基准不足"]:
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


def parse_pct_value(val_str: str) -> Optional[float]:
    """将 '8.8%', '-12.6%' 转换为浮点数，无法解析返回 None"""
    if not val_str or val_str in ["-", "数据不足", "基准不足"]:
        return None
    s = val_str.replace("%", "").replace("pct", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def extract_time_from_filename(filepath: str) -> str:
    """从文件名中提取 HHMM 时间"""
    basename = os.path.basename(filepath)
    m = re.search(r"_(\d{4})\.md$", basename)
    return m.group(1) if m else ""


def sector_match(plate_field: str, target_sector: str) -> bool:
    """板块匹配：支持子串匹配（'有色' 匹配 '有色金属'，'铝' 匹配 '铝'）"""
    if not plate_field or not target_sector:
        return False
    return target_sector in plate_field or plate_field in target_sector


def auto_detect_sector(code: str, files: List[str]) -> Optional[str]:
    """从报告中自动检测持仓股的板块名称"""
    SCAN_TABLES = [
        "low_absorb_short", "low_absorb_trend", "short_pool",
        "trend_obs_pool", "trend_conf_pool", "capital_ranking", "capital_tracking",
    ]
    for f in files[:20]:  # 只扫前 20 份报告
        rep = parse_screening_report(f)
        for table_name in SCAN_TABLES:
            for r in rep["tables"].get(table_name, []):
                if r.get("代码", "") == code:
                    plate = r.get("板块", "")
                    if plate:
                        return plate
    return None


def watch_sector(code: str, sector: str, date_str: Optional[str] = None,
                 from_time: Optional[str] = None, buy_sector_count: Optional[int] = None):
    """
    监控持仓股的板块联动退潮情况

    Args:
        code: 持仓股代码（如 600219）
        sector: 板块名称（如 工业金属），传 "auto" 自动从报告检测
        date_str: 日期 YYYYMMDD，默认最新日期
        from_time: 起始时间 HHMM（从此时开始监控），默认全天
        buy_sector_count: 买入时板块入池数（用于计算退潮幅度），默认从首次快照推断
    """
    files = get_report_files(BASE_REPORTS_DIR, date_str)
    if not files:
        print(f"未找到相关报告文件: date={date_str}")
        return

    # 自动检测板块
    if sector == "auto":
        detected = auto_detect_sector(code, files)
        if detected:
            sector = detected
            print(f"[自动检测] {code} 板块 → {sector}")
        else:
            print(f"[自动检测失败] 未在报告中找到 {code} 的板块信息，请手动指定")
            return

    # 按时间过滤
    if from_time:
        files = [f for f in files if extract_time_from_filename(f) >= from_time]
    if not files:
        print(f"从 {from_time} 起未找到报告")
        return

    date_label = date_str or os.path.basename(os.path.dirname(files[0]))
    print(f"=== 板块退潮监控 ===")
    print(f"标的: {code} | 板块: {sector} | 日期: {date_label} | 起始: {from_time or '全天'}")
    print(f"扫描报告: {len(files)} 份\n")

    # 需要遍历的表名（所有可能出现候选的表）
    SCAN_TABLES = [
        "low_absorb_short",   # 低吸超短线 A/B/C（主表）
        "low_absorb_trend",   # 低吸短线趋势 A/B/C
        "short_pool",         # 超短池
        "trend_obs_pool",     # 趋势观察池
        "trend_conf_pool",    # 趋势确认池
        "capital_ranking",    # 主力资金优选
    ]

    timeline: List[Dict[str, Any]] = []

    for f in files:
        rep = parse_screening_report(f)
        time_tag = rep["time"]

        # 在各表中搜索同板块入池股票
        sector_peers_codes = set()
        sector_peers_info = []
        held_stock_row = None

        for table_name in SCAN_TABLES:
            rows = rep["tables"].get(table_name, [])
            for r in rows:
                plate = r.get("板块", "")
                row_code = r.get("代码", "")

                if sector_match(plate, sector):
                    sector_peers_codes.add(row_code)
                    sector_peers_info.append({
                        "code": row_code,
                        "name": r.get("名称", ""),
                        "table": table_name,
                        "inc5": r.get("5分钟增量", "-"),
                        "main_pct": r.get("主力净占比", "-"),
                    })

                if row_code == code:
                    held_stock_row = r

        peer_count = len(sector_peers_codes)

        # 持仓股资金数据
        inc5_str = "-"
        main_pct_str = "-"
        if held_stock_row:
            inc5_str = held_stock_row.get("5分钟增量", "-")
            main_pct_str = held_stock_row.get("主力净占比", "-")

        # 5分钟增量方向判定
        inc5_val = parse_amount_value(inc5_str)
        inc5_negative = inc5_val < 0

        # 预警等级判定
        base_count = buy_sector_count if buy_sector_count else (
            timeline[0]["sector_count"] if timeline else peer_count
        )

        warning = ""
        if peer_count == 0:
            warning = "🔴 板块清零"
        elif base_count > 0 and peer_count < base_count * 0.5:
            warning = "🟡 退潮(<50%)"

        if inc5_negative:
            if warning:
                warning += " + 5分转负"
            else:
                warning = "⚠️ 5分转负"

        # 主力占比衰减检测（与前一快照比较）
        if timeline:
            prev_main_v = parse_pct_value(timeline[-1]["main_pct_str"])
            curr_main_v = parse_pct_value(main_pct_str)
            if prev_main_v is not None and curr_main_v is not None:
                if curr_main_v < prev_main_v - 1.0:
                    if warning:
                        warning += " + 主力衰减"
                    else:
                        warning = "⚠️ 主力衰减"

        # 同板块其他股票（排除自身）
        peers_display = [p["name"] for p in sector_peers_info if p["code"] != code]
        # 去重
        peers_display = list(dict.fromkeys(peers_display))

        snapshot = {
            "time": time_tag,
            "sector_count": peer_count,
            "held_in_pool": held_stock_row is not None,
            "inc5_str": inc5_str,
            "main_pct_str": main_pct_str,
            "inc5_negative": inc5_negative,
            "warning": warning,
            "peers": peers_display,
        }
        timeline.append(snapshot)

    if not timeline:
        print("（未产生任何监控快照）")
        return

    # 输出时间线
    print(f"{'时间':<6} | {'板块入池':>6} | {'持仓在池':<6} | {'主力%':<8} | {'5分增量':<12} | {'同板块标的':<24} | {'预警'}")
    print("-" * 100)

    for s in timeline:
        in_pool = "✅" if s["held_in_pool"] else "❌"
        peers_str = ", ".join(s["peers"][:4]) if s["peers"] else "(无)"
        if len(s["peers"]) > 4:
            peers_str += f"…(+{len(s['peers'])-4})"
        print(f"{s['time']:<6} | {s['sector_count']:>6} | {in_pool:<6} | {s['main_pct_str']:<8} | {s['inc5_str']:<12} | {peers_str:<24} | {s['warning']}")

    # === 汇总判定 ===
    print("\n" + "=" * 60)
    print("【板块退潮判定汇总】\n")

    peak_count = max(s["sector_count"] for s in timeline)
    final_count = timeline[-1]["sector_count"]
    first_warning = next((s for s in timeline if s["warning"]), None)
    base_count = buy_sector_count or timeline[0]["sector_count"]

    print(f"  买入时/起始入池数: {base_count} 只")
    print(f"  盘中峰值入池数:   {peak_count} 只")
    print(f"  末快照入池数:     {final_count} 只")

    # 连续 5分钟转负计数
    consecutive_neg = 0
    max_consecutive_neg = 0
    first_neg_time = None
    for s in timeline:
        if s["inc5_negative"]:
            consecutive_neg += 1
            if consecutive_neg == 1:
                first_neg_time = s["time"]
            max_consecutive_neg = max(max_consecutive_neg, consecutive_neg)
        else:
            consecutive_neg = 0
            first_neg_time = None

    if max_consecutive_neg >= 3:
        print(f"  ⚠️ 5分钟增量连续转负: {max_consecutive_neg} 次")

    # 退潮判定
    if final_count == 0:
        print(f"\n  🔴 判定: 板块清零退潮")
        print(f"  → 次日 9:45 优先出局，不问其他")
    elif base_count > 0 and final_count < base_count * 0.5:
        ratio = final_count / base_count * 100
        print(f"\n  🟡 判定: 板块显著退潮（{final_count}/{base_count}={ratio:.0f}%）")
        print(f"  → 次日优先出局；若次日开盘板块未回暖，不等 9:45 直接走")
    elif base_count > 0 and final_count < base_count:
        ratio = final_count / base_count * 100
        print(f"\n  ⚠️ 判定: 板块弱化（{final_count}/{base_count}={ratio:.0f}%）")
        print(f"  → 次日观察，不加仓；破止损线直接走")
    else:
        print(f"\n  ✅ 判定: 板块联动维持")

    if first_warning:
        print(f"\n  首次预警时间: {first_warning['time']} — {first_warning['warning']}")

    # 回头看：若买入后出现退潮信号，标注最早可感知时间
    retreat_snapshots = [s for s in timeline if "退潮" in s["warning"] or "清零" in s["warning"]]
    if retreat_snapshots:
        print(f"  最早可感知退潮: {retreat_snapshots[0]['time']}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="板块退潮盘中预警（持仓后监控同板块联动变化）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 tools/watch_sector.py 600219 有色金属 --date 20260812 --from 1005 --buy-count 5
  python3 tools/watch_sector.py 000768 航天军工 --date 20260814 --from 1023
        """
    )
    parser.add_argument("code", type=str, help="持仓股代码，如 600219")
    parser.add_argument("sector", type=str, help="板块名称，如 有色金属（支持子串匹配）")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD，默认最新日期")
    parser.add_argument("--from", dest="from_time", type=str, default=None,
                        help="起始时间 HHMM，如 1005")
    parser.add_argument("--buy-count", type=int, default=None,
                        help="买入时同板块入池数（用于计算退潮幅度）")
    args = parser.parse_args()

    watch_sector(args.code, args.sector, args.date, args.from_time, args.buy_count)
