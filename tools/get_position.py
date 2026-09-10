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


def _normalize_code(value: Any) -> Optional[str]:
    """Keep stock codes as six-character strings, including leading zeroes."""
    if value is None:
        return None
    text = str(value).strip()
    return text.zfill(6) if text.isdigit() else text


def _parse_scalar(key: str, raw_value: str) -> Any:
    """Parse the small scalar subset used by the inline YAML snapshot format."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        # A quoted code is deliberately kept as text; int("000591") is data loss.
        return value[1:-1]
    if value.lower() in {"null", "~"}:
        return None
    if key == "code" and value.isdigit():
        return value.zfill(6)
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _parse_inline_mapping(line: str) -> Dict[str, Any]:
    """Parse one inline ``{key: value, ...}`` mapping used in the snapshots."""
    m = re.search(r"\{([^}]+)\}", line)
    if not m:
        return {}
    raw_dict: Dict[str, Any] = {}
    for part in m.group(1).split(","):
        if ":" not in part:
            continue
        key, raw_value = part.split(":", 1)
        key = key.strip().strip('"').strip("'")
        raw_dict[key] = _parse_scalar(key, raw_value)
    return raw_dict


def _normalize_item(item: Any) -> Any:
    """Normalize one snapshot item without throwing away extra explanatory fields."""
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    if "code" in normalized:
        normalized["code"] = _normalize_code(normalized["code"])
    return normalized


def _normalize_snapshot_data(data: Any) -> Dict[str, Any]:
    """Return the canonical position snapshot shape used by all callers."""
    if not isinstance(data, dict):
        data = {}
    raw_positions = data.get("positions")
    if not isinstance(raw_positions, dict):
        raw_positions = {}

    positions = {
        key: [_normalize_item(item) for item in (raw_positions.get(key) or [])]
        if isinstance(raw_positions.get(key) or [], list)
        else []
        for key in ("simulated", "real", "real_mother")
    }

    raw_watchlist = data.get("watchlist") or []
    raw_t1_plan = data.get("t1_plan") or []
    raw_cash = data.get("cash")
    cash = dict(raw_cash) if isinstance(raw_cash, dict) else {}
    if "real_available" in cash and isinstance(cash["real_available"], str):
        try:
            cash["real_available"] = float(cash["real_available"])
        except ValueError:
            pass
    cash.setdefault("real_available", None)

    return {
        "positions": positions,
        "cash": cash,
        "watchlist": [_normalize_item(item) for item in raw_watchlist] if isinstance(raw_watchlist, list) else [],
        "t1_plan": [_normalize_item(item) for item in raw_t1_plan] if isinstance(raw_t1_plan, list) else [],
    }


def get_latest_decision_file(date_str: Optional[str] = None) -> Optional[str]:
    """获取最新或指定日期的决策记录文件"""
    if date_str:
        f = os.path.join(BASE_DECISION_DIR, f"{date_str}.md")
        return f if os.path.exists(f) else None

    files = sorted([
        f for f in glob.glob(os.path.join(BASE_DECISION_DIR, "*.md"))
        if re.match(r"^\d{8}\.md$", os.path.basename(f))
    ])
    return files[-1] if files else None


def parse_yaml_block_fallback(text: str) -> Dict[str, Any]:
    """若无 pyyaml，使用正则轻量解析 YAML 块"""
    result: Dict[str, Any] = {
        "positions": {"simulated": [], "real": [], "real_mother": []},
        "cash": {"real_available": None},
        "watchlist": [],
        "t1_plan": [],
    }
    
    current_section = None
    current_sub = None
    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if trimmed.startswith("positions:"):
            current_section = "positions"
            continue
        elif trimmed.startswith("watchlist:"):
            current_section = "watchlist"
            continue
        elif trimmed.startswith("t1_plan:"):
            current_section = "t1_plan"
            continue
        elif trimmed.startswith("cash:"):
            current_section = "cash"
            continue

        if current_section == "positions":
            if trimmed.startswith("simulated:"):
                current_sub = "simulated"
                continue
            elif trimmed.startswith("real:"):
                current_sub = "real"
                continue
            elif trimmed.startswith("real_mother:"):
                current_sub = "real_mother"
                continue

        if current_section == "cash" and "{" not in line and ":" in trimmed:
            key, raw_value = trimmed.split(":", 1)
            result["cash"][key.strip()] = _parse_scalar(key.strip(), raw_value)
            continue

        if "{" in line and "}" in line:
            raw_dict = _parse_inline_mapping(line)
            if raw_dict:
                if current_section == "positions" and current_sub:
                    result["positions"][current_sub].append(raw_dict)
                elif current_section in ["watchlist", "t1_plan"]:
                    result[current_section].append(raw_dict)
    return _normalize_snapshot_data(result)


def load_position_snapshot(filepath: str) -> Dict[str, Any]:
    """从决策记录中加载 YAML 快照"""
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # 匹配 ## 收盘持仓快照 下的 yaml 块
    m = re.search(r"##\s*收盘持仓快照\s*```ya?ml\s*\n(.*?)\n```", content, re.DOTALL)
    if not m:
        return {
            "file": os.path.basename(filepath),
            "has_yaml": False,
            "positions": {"simulated": [], "real": [], "real_mother": []},
            "cash": {"real_available": None},
            "watchlist": [],
            "t1_plan": [],
        }

    yaml_text = m.group(1).strip()
    
    data = {}
    if yaml is not None:
        try:
            data = yaml.safe_load(yaml_text) or {}
        except Exception:
            data = parse_yaml_block_fallback(yaml_text)
    else:
        data = parse_yaml_block_fallback(yaml_text)

    normalized = _normalize_snapshot_data(data)

    return {
        "file": os.path.basename(filepath),
        "date": os.path.basename(filepath).replace(".md", ""),
        "has_yaml": True,
        "positions": normalized["positions"],
        "cash": normalized["cash"],
        "watchlist": normalized["watchlist"],
        "t1_plan": normalized["t1_plan"],
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
    real_mother = pos.get("real_mother", []) if isinstance(pos, dict) else []
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

    # 3. 真实仓② / real_mother
    print("\n【真实仓②持仓】")
    if real_mother:
        for p in real_mother:
            print(f"  🛡️ {p.get('code')} {p.get('name')} | {p.get('qty')}股 @{p.get('cost')} | 止损:{p.get('stop')} | 板块:{p.get('sector','-')}")
    else:
        print("  (空仓)")

    cash = snap.get("cash", {})
    if isinstance(cash, dict) and cash.get("real_available") is not None:
        print(f"\n【真实仓可用现金】\n  {cash['real_available']}")

    # 4. 明日观察池
    print("\n【明日观察池】")
    if wl:
        for w in wl:
            note = f" ({w.get('note')})" if w.get('note') else ""
            print(f"  📌 {w.get('code')} {w.get('name')} | 触发:{w.get('trigger')} | 低吸区:{w.get('low_buy')} | 失效:{w.get('invalid')} | 禁区:{w.get('no_chase')}{note}")
    else:
        print("  (无观察标的)")

    # 5. T+1 操作计划
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
