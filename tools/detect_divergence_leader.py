#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙头分歧识别器（dominance_type=divergence_leader）· 影子验证通道 · 待验证项⑨
=====================================================================
来源案例：
- 正例 0821 兴业银锡(000426)：全天"超单主导"✗、早盘超大单一度 -1.25亿，
  但主力净占比 7.9%→12% 单向走高、价格创新高，收盘 +6.02%（全场总龙头）。
- 反证锚 0807 中国巨石(600176)：主力占比始终<5%、超大单为负、价格滞涨 = 真·散户堆量。

治理约束（见 选股框架.md 待验证项⑨）：
1) 本检测器是"生产报告无此标签"前提下的唯一数值推导例外，判定函数固定于本文件，
   不得盘中手改参数或人工改判；
2) 触发结果只进模拟仓影子采样（shadow_samples.json 的 divergence 类），
   在攒满 20 个完整结算样本前，不得据此给真实仓建议；
3) 不改写现行"超大单为负一票否决"的正式判定（scan_reports.py 不动）；
4) 公告 avoid 仍是一票否决，本通道不可绕过。

判定条件（初值，影子期校准；全部基于报告已有字段）：
  结构三条件（须同时成立）：
    S1 资金趋势：主力净占比 > 5%，且为当日截至今快照的最高值（创新高），
       且要求至少 LOOKBACK+1 个快照历史、较 LOOKBACK 个快照前上升 >= RISE 个百分点；
    S2 强势结构：高位回落 < 1.0pct 且 均价线上方；
    S3 主线地位：板块共振=是 且 板块内候选 >= 2。
  触发场景（S1~S3 全真时二选一）：
    A 否决旁路：超大单 < 0（分歧抛压被承接）；
    B 比值不足：生产主导标签为明确 "✗"（✓/✓(绝对)/✓(合力) 均视为已具正式主导，
      不进入旁路）且 超单/主力净额 < 20%。
  同股同日只记首个触发快照；当日任一快照出现 avoid 即整股隔离。
"""

import argparse
import glob
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tools.report_parser import parse_screening_report  # noqa: E402
from tools.shadow_tracker import init_db, save_db, SHADOW_DB_FILE  # noqa: E402

# ---- 参数区（初值·影子期校准；改动需留痕）----
D1_MIN_MAINP = 5.0    # S1: 主力净占比下限（巨石隔离锚：巨石始终<5%）
D1_LOOKBACK = 5       # S1: 回看快照数（要求 i >= LOOKBACK 才允许触发）
D1_RISE = 1.5         # S1: 最小升幅（pct 点）
D2_PULLBACK = 1.0     # S2: 高位回落上限 pct
D3_MIN_SECTOR = 2     # S3: 板块内候选下限
BYPASS_RATIO = 0.20   # 场景B: 超单/主力 上限

REPORTS_ROOT = PROJECT_ROOT / "筛选结果"


def num_wan(s):
    """'+4894万'/'-1.22亿' -> 万为单位 float；无法解析返回 None"""
    if s is None:
        return None
    s = str(s).strip().replace('+', '')
    m = re.match(r'^(-?[\d.]+)(万|亿)?$', s)
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2) == '亿':
        v *= 10000.0
    elif m.group(2) is None:
        return None
    return v


def fnum(s, default=None):
    try:
        return float(str(s).replace('%', '').replace('pct', '').strip())
    except (TypeError, ValueError):
        return default


def day_files(date_str):
    d = REPORTS_ROOT / date_str
    return sorted(glob.glob(str(d / f"A股筛选结果_{date_str}_*.md")))


def evaluate_history(code, name, plate, snaps, date_str):
    """纯函数：对单只股票的当日快照序列做分歧判定，返回触发dict或None。

    snaps 须按时间升序；每个元素至少含：
    time, price, pull, vwap_up, mainp, xl, dom, amt, reso, n_sec, ann, report_file
    """
    if any("avoid" in (s.get("ann") or "") for s in snaps):
        return None
    seen_mainp = []
    for i, s in enumerate(snaps):
        mp = s.get("mainp")
        seen_mainp.append(mp)
        if mp is None or s.get("pull") is None or s.get("price") is None:
            continue
        prev = seen_mainp[:-1]
        is_new_high = all((p is None) or (mp > p) for p in prev)
        risen = False
        if i >= D1_LOOKBACK:
            base = seen_mainp[i - D1_LOOKBACK]
            risen = (base is not None) and (mp - base >= D1_RISE)
        s1 = (mp > D1_MIN_MAINP) and is_new_high and risen
        s2 = (s["pull"] < D2_PULLBACK) and bool(s.get("vwap_up"))
        s3 = bool(s.get("reso")) and (s.get("n_sec") or 0) >= D3_MIN_SECTOR
        if not (s1 and s2 and s3):
            continue

        xl = s.get("xl")
        scenario = None
        if xl is not None and xl < 0:
            scenario = "A否决旁路(超大单为负)"
        elif xl is not None and str(s.get("dom", "")).strip() == "✗":
            mv = None
            if mp and s.get("amt"):
                mv = s["amt"] * (mp / 100.0)
            ratio = (xl / mv) if (mv and mv > 0) else None
            if ratio is None or ratio < BYPASS_RATIO:
                scenario = "B比值不足(超单/主力<20%)"
        if scenario is None:
            continue

        return dict(code=code, name=name, plate=plate, date=date_str,
                    trigger_time=s["time"], trigger_price=float(s["price"]),
                    report_file=s.get("report_file", ""),
                    mainp=mp, xl=xl, pull=s["pull"], scenario=scenario,
                    dom_label=s.get("dom", ""), ann=s.get("ann", ""))
    return None


def group_histories(files):
    """解析报告文件序列 -> {code: 快照列表}，附带元数据。"""
    history, names, plates = {}, {}, {}
    for fp in files:
        try:
            rep = parse_screening_report(fp)
        except Exception as e:
            print(f"[divergence] 解析失败 {os.path.basename(fp)}: {e}")
            continue
        rows = rep.get("tables", {}).get("low_absorb_short") or []
        t = rep.get("time", "")
        fname = rep.get("file", os.path.basename(fp))
        for r in rows:
            code = r.get("代码", "")
            if not code:
                continue
            names.setdefault(code, r.get("名称", ""))
            plates.setdefault(code, r.get("板块", ""))
            history.setdefault(code, []).append(dict(
                time=t,
                price=fnum(r.get("现价")),
                pull=fnum(r.get("高位回落")),
                vwap_up=r.get("均价线") == "均价线上方",
                mainp=fnum(r.get("主力净占比")),
                xl=num_wan(r.get("超大单")),
                dom=r.get("超单主导", ""),
                amt=num_wan(r.get("成交额")),
                reso=r.get("共振") == "是",
                n_sec=fnum(r.get("板块内候选"), 0),
                ann=r.get("公告风险", ""),
                report_file=fname,
            ))
    return history, names, plates


def detect_day(date_str, verbose=True):
    files = day_files(date_str)
    if not files:
        print(f"[divergence] {date_str} 无报告文件")
        return []

    history, names, plates = group_histories(files)
    triggers = []
    for code, snaps in history.items():
        snaps.sort(key=lambda s: s["time"])
        tg = evaluate_history(code, names[code], plates[code], snaps, date_str)
        if tg:
            tg["last_price"] = snaps[-1].get("price")
            tg["last_time"] = snaps[-1]["time"]
            triggers.append(tg)

    triggers.sort(key=lambda x: x["trigger_time"])
    if verbose:
        print(f"\n=== 龙头分歧识别 · {date_str} · 共{len(files)}份快照 ===")
        if not triggers:
            print("（无触发）")
        for tg in triggers:
            print(f"{tg['trigger_time']} {tg['code']} {tg['name']:<6} [{tg['plate']}] "
                  f"触发行情@{tg['trigger_price']} 主力{tg['mainp']:.1f}% 超大单{tg['xl']:+.0f}万 "
                  f"回落{tg['pull']:.2f}pct | {tg['scenario']} | 主导标签={tg['dom_label']}")
            print(f"    末快照 {tg['last_time']}@{tg['last_price']} | 公告={tg['ann']}")
    return triggers


def record(triggers):
    """写入影子库 divergence 类；字段与 shadow_tracker 结算/报表管线完全对齐。"""
    db = init_db()
    db["targets"].setdefault(
        "divergence", {"name": "龙头分歧识别(divergence_leader)", "target_samples": 20})
    db["samples"].setdefault("divergence", [])
    exist_ids = {s.get("id") for s in db["samples"]["divergence"]}
    legacy_keys = {f"{s.get('code')}_{s.get('date')}" for s in db["samples"]["divergence"]}
    added = 0
    for tg in triggers:
        new_id = f"DIV_{tg['date']}_{tg['code']}"
        legacy_key = f"{tg['code']}_{tg['date']}"
        if new_id in exist_ids or legacy_key in legacy_keys:
            continue
        db["samples"]["divergence"].append({
            "id": new_id,
            "mechanism": "divergence",
            "code": tg["code"], "name": tg["name"], "date": tg["date"],
            "trigger_time": tg["trigger_time"],
            "report_file": tg.get("report_file", ""),
            "trigger_price": round(float(tg["trigger_price"]), 2),
            "plate": tg["plate"],
            "mainp_pct": round(float(tg["mainp"]), 2),
            "xl_wan": round(float(tg["xl"]), 1),
            "pullback_pct": round(float(tg["pull"]), 2),
            "dominance_label": tg.get("dom_label", ""),
            "scenario": tg["scenario"],
            "t1_result": None,
        })
        added += 1
    save_db(db)
    total = len(db["samples"]["divergence"])
    print(f"\n[record] 写入 divergence 样本 {added} 条 → {SHADOW_DB_FILE.name}"
          f"（当前 {total}/20）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="龙头分歧识别器（影子验证·待验证项⑨）")
    ap.add_argument("--date", help="YYYYMMDD，缺省取最新交易日目录")
    ap.add_argument("--record", action="store_true", help="将触发写入影子样本库")
    args = ap.parse_args()

    date_str = args.date
    if not date_str:
        days = sorted(d for d in os.listdir(REPORTS_ROOT) if re.match(r'^\d{8}$', d))
        date_str = days[-1] if days else None
    if not date_str:
        sys.exit("未找到报告目录")

    tgs = detect_day(date_str)
    if args.record and tgs:
        record(tgs)
