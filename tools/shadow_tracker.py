#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
20样本量化影子验证系统 (Shadow Tracking System) - 权威升级版
来源：《选股框架.md》与《CLAUDE.md》2026-08-21 优化待验证项

负责采集与追踪 4 大待验证创新机制的实战样本：
1. coalition: 机构游资合力主升（超大单≥2000万、大单>0、主力≥5000万、
   20%<=超单/主力<50%、5分增量≥1000万、连续两期主力与超大单均未衰减、
   主买比≥1.5）；采样只接受生产报告明确标签 `✓(合力)`，不在采集端数值推导
2. breakout: 明日观察池突破升级状态机 (CONFIRMED / B_BREAKOUT / A_STRICT)
3. sector_boost: 主线板块协同加分器 (20亿锚点 + 3只共振 + 协同加15分)
4. divergence: 龙头分歧识别(divergence_leader·待验证项⑨)，由
   detect_divergence_leader.py --record 全日序列判定后写入；本模块只负责
   保留与 T+1 结算，不做采集端数值推导

严格仅用于模拟仓影子验证，记录 T+1 09:45 收益、最大浮盈、最大回撤与假突破率。
"""

import os
import sys
import json
import re
import argparse
import importlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "daily-stock-analysis" / "scripts"
for import_path in (PROJECT_ROOT, TOOLS_DIR, SCRIPTS_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from tools.report_parser import parse_screening_report, get_report_files
from tools.rule_config import RULE_CONFIG, is_complete_shadow_result, shadow_targets

SHADOW_DATA_DIR = Path(__file__).resolve().parent / "shadow_data"
SHADOW_DB_FILE = SHADOW_DATA_DIR / "shadow_samples.json"
T1_PENDING = "待补算"
T1_SOURCE_DAILY_KLINE = "daily_kline"
T1_SOURCE_REPORT_SNAPSHOTS_ONLY = "report_snapshots_only"
T1_SOURCE_UNAVAILABLE = "unavailable"


def init_db() -> Dict[str, Any]:
    """初始化或加载影子样本数据库。"""
    SHADOW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SHADOW_DB_FILE.exists():
        try:
            data = json.loads(SHADOW_DB_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("targets", {})
                data.setdefault("samples", {})
                # 迁移：为新配置登记的类别补齐空结构，但不覆盖已有样本
                # 或旧目标值；目标值漂移由 validate_consistency.py 报告。
                for category, meta in shadow_targets().items():
                    data["targets"].setdefault(category, dict(meta))
                    data["samples"].setdefault(category, [])
                return data
        except Exception:
            pass
    return {
        "version": "2.0",
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targets": shadow_targets(),
        "samples": {category: [] for category in shadow_targets()},
    }


def save_db(db: Dict[str, Any]) -> None:
    """持久化保存影子样本数据库。"""
    SHADOW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    db["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SHADOW_DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def is_number(val: Any) -> bool:
    if val is None or val in ("", "-", "--", "None"):
        return False
    try:
        float(val)
        return True
    except (TypeError, ValueError):
        return False


def parse_val(val_str: Any) -> float:
    if val_str is None or val_str in ("-", "None", ""):
        return 0.0
    s = str(val_str).replace("+", "").replace("%", "").replace("pct", "").replace(",", "").strip()
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


def collect_samples_from_report(filepath: str, db: Dict[str, Any]) -> int:
    """从单份筛选报告中提取合力主升、观察池突破与板块协同影子样本。"""
    rep = parse_screening_report(filepath)
    date_str = rep["date"]
    time_str = rep["time"]

    for category in ("coalition", "breakout", "sector_boost"):
        db.setdefault("samples", {}).setdefault(category, [])
    existing_keys = {
        cat: {f"{s['code']}_{s['date']}" for s in db["samples"][cat]}
        for cat in ("coalition", "breakout", "sector_boost")
    }

    added_count = 0
    tables = rep.get("tables") or {}

    # 1. 采集 合力主升 (coalition) 样本（严格互斥：20% <= 超单占比 < 50%）
    low_short = tables.get("low_absorb_short") or []
    for r in low_short:
        code = r.get("代码", "")
        name = r.get("名称", "")
        super_lead = r.get("超单主导", "")
        super_str = r.get("超大单", "0")
        super_wan = parse_val(super_str)
        main_pct = parse_val(r.get("主力净占比", "0"))
        amt_wan = parse_val(r.get("成交额", "0"))
        main_net_wan = parse_val(r.get("主力净额", "0"))
        if main_net_wan == 0.0 and main_pct > 0 and amt_wan > 0:
            main_net_wan = amt_wan * (main_pct / 100.0)
        inc5_wan = parse_val(r.get("5分钟增量", "0"))
        price = parse_val(r.get("现价", "0"))

        super_ratio = (super_wan / main_net_wan * 100) if main_net_wan > 0 else 0.0
        # 影子样本只接受生产报告明确写出的严格合力标签。数值字段仅
        # 用于记录样本，不得在采集端重新推导标签并绕过主买比、历史快照
        # 与大单条件。
        is_pure_coalition = super_lead == "✓(合力)"

        sample_key = f"{code}_{date_str}"
        if is_pure_coalition and sample_key not in existing_keys["coalition"]:
            sample = {
                "id": f"COAL_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "plate": r.get("板块", "-"),
                "super_wan": round(super_wan, 1),
                "main_net_wan": round(main_net_wan, 1),
                "super_ratio": round(super_ratio, 1),
                "inc5_wan": round(inc5_wan, 1),
                "t1_result": None,
            }
            db["samples"]["coalition"].append(sample)
            existing_keys["coalition"].add(sample_key)
            added_count += 1

    # 2. 采集 观察池突破状态机 (breakout) 样本
    watchlist = tables.get("tomorrow_watchlist") or []
    for r in watchlist:
        code = r.get("代码", "")
        name = r.get("名称", "")
        state = r.get("突破状态", "")
        price = parse_val(r.get("当前价", r.get("现价", "0")))
        trigger = parse_val(r.get("触发价", "0"))
        dom = r.get("超单主导", "")

        is_breakout = (
            state in ("CONFIRMED", "B_BREAKOUT", "A_STRICT") or
            ("已站稳" in r.get("状态说明", "") and price >= trigger and trigger > 0)
        )
        sample_key = f"{code}_{date_str}"
        if is_breakout and sample_key not in existing_keys["breakout"]:
            sample = {
                "id": f"BRK_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "benchmark_trigger": trigger,
                "plate": r.get("板块", "-"),
                "state": state,
                "confirm_count": r.get("确认次数", "2次"),
                "dominance": dom,
                "t1_result": None,
            }
            db["samples"]["breakout"].append(sample)
            existing_keys["breakout"].add(sample_key)
            added_count += 1

    # 3. 采集 主线板块协同 (sector_boost) 样本
    cap_rank = tables.get("capital_ranking") or []
    for r in cap_rank:
        code = r.get("代码", "")
        name = r.get("名称", "")
        reason = (
            r.get("评分依据") or r.get("理由") or r.get("资金理由") or
            r.get("capital_reason") or r.get("capital_reason", "") or ""
        )
        price = parse_val(r.get("现价", "0"))
        boost_val = parse_val(r.get("sector_boost", 0))

        is_boosted = "主线板块协同" in reason or "20亿锚点" in reason or "锚点带动" in reason or boost_val > 0
        sample_key = f"{code}_{date_str}"
        if is_boosted and sample_key not in existing_keys["sector_boost"]:
            sample = {
                "id": f"BOOST_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "plate": r.get("板块", "-"),
                "score": parse_val(r.get("评分", "0")),
                "boost_points": 15,
                "t1_result": None,
            }
            db["samples"]["sector_boost"].append(sample)
            existing_keys["sector_boost"].add(sample_key)
            added_count += 1

    return added_count


def find_next_trading_day_reports(reports_dir: str, date_str: str) -> List[str]:
    """寻找指定日期的下一个交易日报告文件（支持递归子目录与各类文件名）。"""
    dates_set = set()
    if os.path.exists(reports_dir):
        for root, dirs, files in os.walk(reports_dir):
            for d in dirs:
                if re.match(r"^\d{8}$", d):
                    dates_set.add(d)
            for f in files:
                if f.endswith(".md"):
                    m = re.search(r"(\d{8})", f)
                    if m:
                        dates_set.add(m.group(1))

    sorted_dates = sorted(list(dates_set))
    if date_str in sorted_dates:
        idx = sorted_dates.index(date_str)
        if idx + 1 < len(sorted_dates):
            next_date = sorted_dates[idx + 1]
            return get_report_files(reports_dir, next_date)
    return []


def pending_t1_label(reports_dir: str, date_str: str) -> str:
    """生成待结算提示；有下一交易日报告时带真实日期，否则不猜日期。"""
    try:
        next_reports = find_next_trading_day_reports(reports_dir, date_str)
        if next_reports:
            next_date = parse_screening_report(next_reports[0]).get("date")
            if next_date:
                return f"待下一个交易日({next_date})"
    except Exception:
        pass
    return "待下一个交易日"


def fetch_t1_day_kline_extremes(code: str, t1_date: str) -> Optional[Tuple[float, float]]:
    """尝试从日K数据获取该股票在次日交易日的真实全日最高价与最低价。"""
    try:
        a_share_daily_screen = importlib.import_module("a_share_daily_screen")
        k_rows, _source = a_share_daily_screen.fetch_kline(code)
        target_fmt = f"{t1_date[:4]}-{t1_date[4:6]}-{t1_date[6:]}" if len(t1_date) == 8 else t1_date
        for kr in (k_rows or []):
            row_date = str(kr.get("date") or kr.get("day") or "")[:10]
            if row_date.replace("/", "-") == target_fmt.replace("/", "-"):
                h = float(kr.get("high", 0))
                l = float(kr.get("low", 0))
                if h > 0 and l > 0:
                    return h, l
    except Exception:
        pass
    return None


def calculate_t1_for_sample(sample: Dict[str, Any], t1_reports: List[str]) -> Optional[Dict[str, Any]]:
    """根据 T+1 次日报告与日K全日极值真实核算 09:45 收益、最大浮盈、最大回撤与假突破。"""
    if not t1_reports:
        return None

    code = sample["code"]
    trigger_price = float(sample["trigger_price"])
    if trigger_price <= 0:
        return None

    # 扫描次日所有快照，按与 09:45 (585 分钟) 的最小分钟差锁定 p_0945
    best_0945_diff = float("inf")
    p_0945 = None
    all_prices = []
    t1_date = None

    for r_file in t1_reports:
        try:
            rep = parse_screening_report(r_file)
            t1_date = rep["date"]
            t_str = rep.get("time", "")

            # 计算与配置的目标时刻（默认 09:45）的时间差
            time_diff = float("inf")
            m = re.match(r"^(\d{1,2}):(\d{2})", t_str)
            if m:
                h, mi = int(m.group(1)), int(m.group(2))
                target_minute = int(RULE_CONFIG["shadow"]["t1_target_minute"])
                time_diff = abs((h * 60 + mi) - target_minute)

            for table_name, rows in rep.get("tables", {}).items():
                for row in rows:
                    if row.get("代码") == code:
                        price = parse_val(row.get("现价") or row.get("当前价") or row.get("价格"))
                        if price > 0:
                            all_prices.append(price)
                            # 提取表格中可能记录的最高价与最低价
                            if is_number(row.get("最高")):
                                all_prices.append(parse_val(row.get("最高")))
                            if is_number(row.get("最低")):
                                all_prices.append(parse_val(row.get("最低")))

                            # 精确锁定距 09:45 最近的时间点
                            if time_diff < best_0945_diff:
                                best_0945_diff = time_diff
                                p_0945 = price
        except Exception:
            continue

    if not all_prices:
        return None

    if p_0945 is None:
        p_0945 = all_prices[0]

    # 结合日K获取全日真实极值（防止中途退出候选表导致漏统计日内极值）。
    # 日K缺失时只能保留09:45快照收益，严禁用筛选快照冒充全日极值闭环。
    day_extremes = fetch_t1_day_kline_extremes(code, t1_date) if t1_date else None
    if day_extremes is not None:
        extremes_complete = True
        p_high = max(max(all_prices), day_extremes[0])
        p_low = min(min(all_prices), day_extremes[1])
    else:
        extremes_complete = False

    t1_ret = (p_0945 - trigger_price) / trigger_price * 100
    if extremes_complete:
        max_gain = round((p_high - trigger_price) / trigger_price * 100, 2)
        max_dd = round((p_low - trigger_price) / trigger_price * 100, 2)
        stop_pct = float(RULE_CONFIG["shadow"]["false_breakout_stop_pct"])
        is_false_breakout: Any = p_low < trigger_price * (1.0 - stop_pct / 100.0)
        extremes_source = T1_SOURCE_DAILY_KLINE
    else:
        max_gain = T1_PENDING
        max_dd = T1_PENDING
        is_false_breakout = T1_PENDING
        extremes_source = T1_SOURCE_REPORT_SNAPSHOTS_ONLY

    return {
        # checked 表示完整T+1结算，不是“找到了某个报告快照”。
        "checked": extremes_complete,
        "t1_date": t1_date or "次日",
        "t1_0945_price": round(p_0945, 2),
        "t1_0945_return_pct": round(t1_ret, 2),
        "t1_max_gain_pct": max_gain,
        "t1_max_drawdown_pct": max_dd,
        "is_false_breakout": is_false_breakout,
        "extremes_complete": extremes_complete,
        "source": extremes_source,
    }


def update_all_t1_metrics(db: Dict[str, Any], reports_dir: Optional[str] = None) -> None:
    """自动核算所有样本的 T+1 次日真实表现（含 divergence 等全部机制类别）。"""
    reports_dir = reports_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))
    for category in sorted(db["samples"].keys()):
        for sample in db["samples"][category]:
            date_str = sample["date"]
            t1_reports = find_next_trading_day_reports(reports_dir, date_str)
            if t1_reports:
                res = calculate_t1_for_sample(sample, t1_reports)
                if res:
                    sample["t1_result"] = res
            if sample.get("t1_result") is None:
                sample["t1_result"] = {
                    "checked": False,
                    "t1_date": pending_t1_label(reports_dir, date_str),
                    "t1_0945_price": None,
                    "t1_0945_return_pct": None,
                    "t1_max_gain_pct": T1_PENDING,
                    "t1_max_drawdown_pct": T1_PENDING,
                    "is_false_breakout": T1_PENDING,
                    "extremes_complete": False,
                    "source": T1_SOURCE_UNAVAILABLE,
                }
            elif (
                isinstance(sample.get("t1_result"), dict)
                and not sample["t1_result"].get("checked")
                and sample["t1_result"].get("source") == T1_SOURCE_UNAVAILABLE
            ):
                # 迁移旧版本写入的固定日期提示，避免历史样本继续显示错误日期。
                sample["t1_result"]["t1_date"] = pending_t1_label(reports_dir, date_str)


def generate_report(db: Dict[str, Any]) -> str:
    """生成 20 样本影子验证进度与指标汇总报告。"""
    lines = []
    lines.append("# 📊 20 样本量化影子验证系统进度报表 (权威定版)")
    lines.append(f"**更新时点**：`{db.get('last_updated', '-')}` ｜ **风控状态**：`待验证 · 仅模拟仓权限`\n")

    lines.append("## 一、四大待验证项目进度汇总")
    lines.append("| 待验证项目 | 目标样本 | 已收集样本 | 收集完成度 | 胜率 (09:45>0) | 平均 09:45 收益 | 平均最大浮盈 | 平均最大回撤 | 假突破率 | 当前状态 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    for cat, meta in db["targets"].items():
        samples = db["samples"].get(cat, [])
        count = len(samples)
        target = meta["target_samples"]
        pct = count / target * 100

        # 计算指标
        evaluated_samples = [s for s in samples if is_complete_shadow_result(s.get("t1_result"))]
        pending_extremes = any(
            s.get("t1_result")
            and s["t1_result"].get("extremes_complete") is False
            and s["t1_result"].get("t1_0945_price") is not None
            for s in samples
        )
        if evaluated_samples:
            wins = sum(1 for s in evaluated_samples if (s["t1_result"].get("t1_0945_return_pct") or 0) > 0)
            win_rate = f"{wins / len(evaluated_samples) * 100:.1f}%"
            avg_ret = f"{sum(s['t1_result']['t1_0945_return_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            avg_gain = f"{sum(s['t1_result']['t1_max_gain_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            avg_dd = f"{sum(s['t1_result']['t1_max_drawdown_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            false_bo = f"{sum(1 for s in evaluated_samples if s['t1_result'].get('is_false_breakout')) / len(evaluated_samples) * 100:.1f}%"
        else:
            win_rate = "0.0% (待补算日K极值)" if pending_extremes else "0.0% (待下一个交易日结算)"
            avg_ret = "0.00%"
            avg_gain = "0.00%"
            avg_dd = "0.00%"
            false_bo = "0.0%"

        status = (
            "🟢 验证达标"
            if count >= target and len(evaluated_samples) >= target
            else "🟡 影子数据采集中"
        )
        lines.append(f"| **{meta['name']}** | {target} | **{count}** | {pct:.1f}% | {win_rate} | {avg_ret} | {avg_gain} | {avg_dd} | {false_bo} | {status} |")

    lines.append("\n## 二、当前已入库影子样本明细")
    for cat, meta in db["targets"].items():
        samples = db["samples"].get(cat, [])
        lines.append(f"\n### 📌 {meta['name']} 样本池 ({len(samples)}/{meta['target_samples']})")
        if samples:
            lines.append("| 样本ID | 代码 | 名称 | 入库日期 | 触发时点 | 触发价 | 板块 | 关键量化特征 | T+1 09:45 收益 | 结算状态 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for s in samples:
                feat = (
                    f"超单:{s.get('super_wan',0):.0f}万/主力:{s.get('main_net_wan',0):.0f}万(占比{s.get('super_ratio',0):.1f}%)"
                    if cat == "coalition"
                    else (f"状态:{s.get('state')} 触发基准:{s.get('benchmark_trigger')}" if cat == "breakout"
                          else (f"场景:{s.get('scenario','-')} 主力:{s.get('mainp_pct',0)}% 超单:{s.get('xl_wan',0):+.0f}万 回落:{s.get('pullback_pct',0)}%"
                                if cat == "divergence"
                                else f"协同评分:{s.get('score')} (+15分)"))
                )
                t1_res = s.get("t1_result", {})
                t1_txt = t1_res.get("t1_0945_return_pct")
                t1_disp = f"{t1_txt:+.2f}%" if t1_txt is not None else "-"
                if t1_res.get("checked") and t1_res.get("extremes_complete") is True:
                    status_disp = f"✅ 已核算({t1_res.get('t1_date')})"
                elif t1_res.get("extremes_complete") is False and t1_res.get("t1_0945_price") is not None:
                    status_disp = f"⏳ 待补算日K极值({t1_res.get('source', T1_SOURCE_REPORT_SNAPSHOTS_ONLY)})"
                else:
                    status_disp = "⏳ 待下一个交易日结算"
                lines.append(f"| `{s['id']}` | {s['code']} | {s['name']} | {s['date']} | {s['trigger_time']} | {s['trigger_price']:.2f} | {s['plate']} | {feat} | {t1_disp} | {status_disp} |")
        else:
            lines.append("*（暂无样本）*")

    return "\n".join(lines)


def scan_and_update(date_str: Optional[str] = None) -> None:
    # 强制重构并重新从报告解析以保证核心三类样本严格互斥。
    # divergence 等旁路类样本由各自检测器（如 detect_divergence_leader.py --record）
    # 全日序列判定维护，本扫描原样保留，避免重建核心三类时被覆盖清除。
    previous = init_db()
    db = {
        "version": "2.0",
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targets": shadow_targets(),
        "samples": {category: [] for category in ("coalition", "breakout", "sector_boost")},
    }
    for cat, arr in previous.get("samples", {}).items():
        if cat not in db["samples"] and arr:
            db["targets"][cat] = previous.get("targets", {}).get(
                cat, {"name": cat, "target_samples": 20})
            db["samples"][cat] = arr
    reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))
    files = get_report_files(reports_dir, date_str)
    
    total_added = 0
    for f in files:
        added = collect_samples_from_report(f, db)
        total_added += added

    update_all_t1_metrics(db)
    save_db(db)
    print(f"=== 影子系统扫描完成：新增/更新 {total_added} 个样本，当前总样本库状态已更新 ===")
    print(generate_report(db))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="20样本量化影子验证系统")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD")
    parser.add_argument("--report", action="store_true", help="输出当前影子验证报表")
    args = parser.parse_args()

    if args.report:
        db = init_db()
        print(generate_report(db))
    else:
        scan_and_update(args.date)
