#!/usr/bin/env python3
"""Render a valid ggp.v1 JSON response as a short Chinese human summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.validate_ggp_output import validate


def _code(value: Any) -> str:
    if value is None:
        return "未知"
    text = str(value)
    return text.zfill(6) if text.isdigit() else text


def _candidate_line(item: dict[str, Any], include_plan: bool = True) -> str:
    code = _code(item.get("code"))
    name = item.get("name") or "未知"
    parts = [f"{code} {name}"]
    plan = item.get("buy_plan") or {}
    if include_plan:
        low = plan.get("buy_low")
        high = plan.get("buy_high")
        stop = plan.get("stop")
        if low is not None and high is not None:
            parts.append(f"买点{low}-{high}")
        elif low is not None:
            parts.append(f"买点{low}")
        if stop is not None:
            parts.append(f"止损{stop}")
    blockers = item.get("blockers") or []
    if blockers:
        parts.append(f"卡点：{'; '.join(str(x) for x in blockers)}")
    return "｜".join(parts)


def _position_line(item: dict[str, Any]) -> str:
    code = _code(item.get("code"))
    name = item.get("name") or "未知"
    qty = item.get("qty")
    cost = item.get("cost")
    stop = item.get("stop")
    return f"{code} {name} {qty if qty is not None else '未知'}股｜成本{cost if cost is not None else '未知'}｜止损{stop if stop is not None else '未知'}"


def _real_candidate_line(item: dict[str, Any]) -> str:
    support = item.get("support") or {}
    labels = (
        ("sector", "主线与共振"), ("capital", "主力与超大单"),
        ("order_book", "分笔与五档"), ("vwap", "买点与VWAP"),
        ("fundamentals", "基本面与YTD"), ("simulation", "模拟验证"),
        ("risk_reward", "盈亏比"),
    )
    lines = [_candidate_line(item)]
    lines.extend(f"  {label}：{support.get(key) or '未核验'}" for key, label in labels)
    plan = item.get("buy_plan") or {}
    lines.append(f"  止盈：{plan.get('take_profit')}｜T+1：{plan.get('t1_action') or '未核验'}")
    return "\n".join(lines)


def _lines_for(items: Any, formatter) -> str:
    if not isinstance(items, list) or not items:
        return "无"
    return "\n".join(f"- {formatter(item)}" for item in items if isinstance(item, dict)) or "无"


def render(obj: dict[str, Any]) -> str:
    decision = obj["decision"]
    holdings = obj["holdings"]
    poll = obj["poll"]
    real_open = decision["real_open"]
    simulated_buy = decision["simulated_buy"]
    blocked = decision["watch_or_real_blocked"]

    if decision["overall"] == "real_open":
        conclusion = "真实仓开仓"
    elif decision["overall"] == "simulated_only":
        conclusion = "真实仓暂不开+模拟仓试错"
    else:
        conclusion = "完全空仓"

    as_of = obj.get("as_of") or "未知时间"
    as_of = as_of.replace("T", " ")
    status_map = {"complete": "完成", "partial": "部分完成", "error": "失败"}
    problems = list(obj.get("errors") or []) + list(obj.get("warnings") or [])
    cash = (holdings.get("cash") or {}).get("real_available")

    t1_lines = []
    for item in obj.get("t1_plan") or []:
        if not isinstance(item, dict):
            continue
        t1_lines.append(
            f"- {_code(item.get('code'))} {item.get('name') or '未知'}｜{item.get('execute_at') or '执行时间未知'}｜{item.get('action') or '未知'}｜{item.get('condition') or '未知'}"
        )

    return "\n".join([
        f"【ggp结果】{as_of}",
        f"状态：{status_map.get(obj.get('status'), obj.get('status') or '未知')}",
        f"结论：{conclusion}",
        f"理由：{decision.get('reason') or '无'}",
        "",
        "【新增决策】",
        f"真实仓可开仓：{_lines_for(real_open, _real_candidate_line)}",
        f"模拟仓可买：{_lines_for(simulated_buy, _candidate_line)}",
        f"仅观察/真实仓暂不开：{_lines_for(blocked, _candidate_line)}",
        f"完全空仓：{'是' if decision.get('fully_cash') else '否'}",
        "",
        "【当前持仓】",
        f"模拟仓：{_lines_for(holdings.get('simulated'), _position_line)}",
        f"真实仓：{_lines_for(holdings.get('real'), _position_line)}",
        f"解套仓：{_lines_for(holdings.get('real_mother'), _position_line)}",
        f"可用现金：{cash if cash is not None else '未知'}",
        "",
        "【明日计划】",
        "\n".join(t1_lines) if t1_lines else "无",
        "",
        "【轮询范围】",
        f"已检查：{poll.get('report_count', 0)} 份报告",
        f"最新报告：{poll.get('latest_report') or '无'}",
        f"数据问题：{'; '.join(problems) if problems else '无'}",
    ])


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a valid ggp.v1 response")
    parser.add_argument("path", help="JSON file to render, or - for stdin")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        raw = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        obj = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID JSON: {exc}", file=sys.stderr)
        return 1
    errors = validate(obj)
    if errors:
        print("INVALID ggp.v1; refuse to render:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(render(obj))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
