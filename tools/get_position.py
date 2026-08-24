#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓与观察池快照查询工具
读取最新（或指定日期）《决策记录/YYYYMMDD.md》中的结构化 YAML 快照，
支持跨会话秒级还原持仓状态与次日计划。

用法示例：
  python3 tools/get_position.py               # 查看最新持仓与观察池
  python3 tools/get_position.py --date 20260814
  python3 tools/get_position.py --json        # 机器可读 JSON 输出
"""

import os
import sys
import re
import json
import glob
import argparse
from typing import Dict, Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

BASE_DECISION_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "决策记录"))


def get_latest_decision_file(date_str: Optional[str] = None) -> Optional[str]:
    """获取最新或指定日期的决策记录文件"""
    if date_str:
        f = os.path.join(BASE_DECISION_DIR, f"{date_str}.md")
        return f if os.path.exists(f) else None

    files = sorted(glob.glob(os.path.join(BASE_DECISION_DIR, "*.md")))
    return files[-1] if files else None


def parse_yaml_block_fallback(text: str) -> Dict[str, Any]:
    """若无 pyyaml，使用正则轻量解析 YAML 块"""
    result: Dict[str, Any] = {"positions": {"simulated": [], "real": []}, "watchlist": [], "t1_plan": []}
    
    # 提取 positions.simulated
    sim_match = re.search(r"simulated:\s*(\[.*?\]|\n(?:(?:\s+-\s*\{.*?\})*\n?))", text)
    # 简单提取 dict 列表
    for block_name, key in [("simulated", "simulated"), ("real", "real"), ("watchlist", "watchlist"), ("t1_plan", "t1_plan")]:
        items = []
        for line in text.splitlines():
            if "{" in line and "}" in line:
                # 提取大括号内容
                m = re.search(r"\{([^}]+)\}", line)
                if m:
                    raw_dict = {}
                    parts = m.group(1).split(",")
                    for p in parts:
                        if ":" in p:
                            k, v = p.split(":", 1)
                            k = k.strip().strip('"').strip("'")
                            v = v.strip().strip('"').strip("'")
                            try:
                                if "." in v:
                                    v = float(v)
                                else:
                                    v = int(v)
                            except ValueError:
                                pass
                            raw_dict[k] = v
                    if raw_dict:
                        items.append(raw_dict)
    return result


def load_position_snapshot(filepath: str) -> Dict[str, Any]:
    """从决策记录中加载 YAML 快照"""
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # 匹配 ## 收盘持仓快照 下的 yaml 块
    m = re.search(r"##\s*收盘持仓快照\s*```ya?ml\s*\n(.*?)\n```", content, re.DOTALL)
    if not m:
        return {"file": os.path.basename(filepath), "has_yaml": False, "positions": {"simulated": [], "real": []}, "watchlist": [], "t1_plan": []}

    yaml_text = m.group(1).strip()
    
    data = {}
    if yaml is not None:
        try:
            data = yaml.safe_load(yaml_text) or {}
        except Exception:
            data = parse_yaml_block_fallback(yaml_text)
    else:
        data = parse_yaml_block_fallback(yaml_text)

    return {
        "file": os.path.basename(filepath),
        "date": os.path.basename(filepath).replace(".md", ""),
        "has_yaml": True,
        "positions": data.get("positions", {"simulated": [], "real": []}),
        "watchlist": data.get("watchlist", []),
        "t1_plan": data.get("t1_plan", [])
    }


def print_position_summary(snap: Dict[str, Any]):
    """打印易读的持仓与观察池摘要"""
    date_str = snap.get("date", "-")
    print(f"=== 决策记录持仓快照 [{date_str}] ({snap['file']}) ===")
    
    if not snap.get("has_yaml"):
        print("⚠️ 该决策记录尚未包含标准 YAML 快照，请参照 CLAUDE.md 追加。\n")
        return

    pos = snap.get("positions", {})
    sim = pos.get("simulated", []) if isinstance(pos, dict) else []
    real = pos.get("real", []) if isinstance(pos, dict) else []
    wl = snap.get("watchlist", [])
    t1 = snap.get("t1_plan", [])

    # 1. 模拟仓
    print("\n【模拟仓持仓】")
    if sim:
        for p in sim:
            print(f"  📦 {p.get('code')} {p.get('name')} | {p.get('qty')}股 @{p.get('cost')} | 止损:{p.get('stop')} | 板块:{p.get('sector','-')} (买入日:{p.get('buy_date','-')})")
    else:
        print("  (空仓)")

    # 2. 真实仓
    print("\n【真实仓持仓】")
    if real:
        for p in real:
            print(f"  💎 {p.get('code')} {p.get('name')} | {p.get('qty')}股 @{p.get('cost')} | 止损:{p.get('stop')} | 板块:{p.get('sector','-')}")
    else:
        print("  (空仓)")

    # 3. 明日观察池
    print("\n【明日观察池】")
    if wl:
        for w in wl:
            note = f" ({w.get('note')})" if w.get('note') else ""
            print(f"  📌 {w.get('code')} {w.get('name')} | 触发:{w.get('trigger')} | 低吸区:{w.get('low_buy')} | 失效:{w.get('invalid')} | 禁区:{w.get('no_chase')}{note}")
    else:
        print("  (无观察标的)")

    # 4. T+1 操作计划
    print("\n【T+1 预案】")
    if t1:
        for t in t1:
            print(f"  ⚡ {t.get('code')} [{t.get('priority','-')}]: {t.get('action')} -> {t.get('condition')}")
    else:
        print("  (无待执行 T+1 计划)")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="持仓与观察池快照查询")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD，默认最新日期")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args()

    fpath = get_latest_decision_file(args.date)
    if not fpath:
        print(f"未找到决策记录文件: date={args.date}")
        sys.exit(1)

    snapshot = load_position_snapshot(fpath)

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print_position_summary(snapshot)
