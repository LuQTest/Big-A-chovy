#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读审计选股框架、执行代码、影子库和权限门槛的一致性。

该工具不筛选股票、不改变样本、不更新持仓，也不自动修正文档。它的职责
是尽早发现“规则已经改了，但某个脚本、报告或状态库仍停留在旧口径”的
问题。发现 FAIL 时返回非零退出码，WARN 不阻断日常运行。

用法：
    python3 tools/validate_consistency.py
    python3 tools/validate_consistency.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.rule_config import (  # noqa: E402
    RULE_CONFIG,
    hhmm_to_minutes,
    is_complete_shadow_result,
    normalize_hhmm,
    shadow_targets,
)


def _issue(level: str, message: str) -> Dict[str, str]:
    return {"level": level, "message": message}


def _add(result: Dict[str, List[Dict[str, str]]], level: str, message: str) -> None:
    result[level].append(_issue(level.upper(), message))


def _empty_result() -> Dict[str, List[Dict[str, str]]]:
    return {"pass": [], "warn": [], "fail": []}


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _numeric(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def check_config(config: Dict[str, Any] = RULE_CONFIG) -> Dict[str, List[Dict[str, str]]]:
    """检查共享参数本身的结构和互斥关系。"""
    result = _empty_result()
    try:
        absolute = config["dominance"]["absolute"]
        coalition = config["dominance"]["coalition"]
        execution = config["execution"]
        risk = config["risk"]
        shadow = config["shadow"]
        permissions = config["permissions"]
    except (KeyError, TypeError):
        _add(result, "fail", "共享参数缺少 dominance/execution/risk/shadow/permissions 核心区段")
        return result

    try:
        lower = float(coalition["min_super_ratio"])
        upper = float(coalition["max_super_ratio_exclusive"])
        if not lower < upper <= float(absolute["min_super_ratio"]):
            _add(result, "fail", "coalition 比例区间未保持 20% ≤ ratio < 50% 的严格互斥关系")
        else:
            _add(result, "pass", "双轨主导比例边界有效且 coalition 与 absolute 互斥")

        if float(coalition["min_buy_ratio"]) < 1.5:
            _add(result, "fail", "合力主升主买比门槛低于 1.5")
        if int(coalition["min_history_snapshots"]) < 2:
            _add(result, "fail", "合力主升历史快照门槛低于 2 期")
        if float(coalition["max_decay_pct"]) != 0.0:
            _add(result, "fail", "合力主升要求未衰减，不能配置资金衰减容忍度")
    except (KeyError, TypeError, ValueError):
        _add(result, "fail", "合力主升参数类型或字段不完整")

    try:
        target = int(shadow["target_samples"])
        categories = shadow["categories"]
        if target <= 0 or not categories:
            _add(result, "fail", "影子样本目标或机制类别为空")
        else:
            _add(result, "pass", f"影子验证登记 {len(categories)} 类，目标为每类 {target} 个样本")
    except (KeyError, TypeError, ValueError):
        _add(result, "fail", "影子验证参数类型或字段不完整")

    try:
        windows = execution["time_windows"]
        if not isinstance(windows, dict) or not windows:
            raise ValueError("time_windows is empty")
        for key, window in windows.items():
            if not isinstance(window, dict):
                raise ValueError(f"{key} is not an object")
            start = hhmm_to_minutes(window["start"])
            end = hhmm_to_minutes(window["end"])
            if start >= end or not str(window.get("label", "")).strip():
                raise ValueError(f"invalid window: {key}")
        fallback_key = str(execution["fallback_window"])
        if fallback_key not in windows:
            raise ValueError("fallback window is not registered")
        pre_market = execution["pre_market"]
        hhmm_to_minutes(pre_market["start"])
        if not pre_market.get("key") or not pre_market.get("label"):
            raise ValueError("pre_market metadata is incomplete")
        t1_window = execution["t1_exit_window"]
        t1_start = hhmm_to_minutes(t1_window["start"])
        t1_end = hhmm_to_minutes(t1_window["end"])
        t1_target = hhmm_to_minutes(t1_window["target"])
        if not t1_start <= t1_target <= t1_end:
            raise ValueError("T+1 target is outside its exit window")
        _add(result, "pass", "执行时段与 T+1 窗口已集中登记且边界有效")
    except (KeyError, TypeError, ValueError):
        _add(result, "fail", "执行时段或 T+1 窗口参数类型、字段或边界不完整")

    try:
        statuses = risk["statuses"]
        expected_status_keys = {"clean", "watch_risk", "avoid", "unknown"}
        if set(statuses) != expected_status_keys or any(not str(value) for value in statuses.values()):
            raise ValueError("risk status registry is incomplete")
        announcement = risk["announcement"]
        for key in ("hard_keywords", "watch_keywords", "ignore_keywords"):
            if not isinstance(announcement[key], list):
                raise ValueError(f"announcement {key} is not a list")
        exclusion = risk["low_absorb_exclusion"]
        for key in (
            "five_day_return_min_ratio",
            "main_pct_max_inclusive",
            "history_lookback_snapshots",
            "negative_main_snapshots_min",
        ):
            if not _numeric(exclusion[key]):
                raise ValueError(f"low_absorb_exclusion missing {key}")
        if not isinstance(risk["hard_blacklist"], dict):
            raise ValueError("hard_blacklist is not an object")
        _add(result, "pass", "公告风控状态、关键词、黑名单与低吸排除参数已集中登记")
    except (KeyError, TypeError, ValueError):
        _add(result, "fail", "风险策略参数类型或字段不完整")

    if permissions.get("real_account_requires_complete_samples") is not True:
        _add(result, "fail", "真实仓权限门槛未设置为完整结算样本强制门禁")
    else:
        _add(result, "pass", "真实仓权限要求完整结算样本，实验机制不能直接放行")

    return result


def check_documents(project_root: Path = PROJECT_ROOT) -> Dict[str, List[Dict[str, str]]]:
    """检查规则权威文档是否仍包含关键口径和权限声明。"""
    result = _empty_result()
    framework_path = project_root / "选股框架.md"
    claude_path = project_root / "CLAUDE.md"
    framework = _read_text(framework_path)
    claude = _read_text(claude_path)

    if framework is None:
        _add(result, "fail", f"找不到规则权威文件：{framework_path}")
        return result
    if claude is None:
        _add(result, "fail", f"找不到执行摘要文件：{claude_path}")
        return result

    framework_required = (
        "超大单为负仍是一票否决",
        "影子采样只接受生产报告明确标签 `✓(合力)`",
        "20个完整结算样本",
        "checked=true",
        "extremes_complete=true",
    )
    missing = [fragment for fragment in framework_required if fragment not in framework]
    if missing:
        _add(result, "fail", f"选股框架缺少关键口径：{'、'.join(missing)}")
    else:
        _add(result, "pass", "选股框架保留双轨主导、严格标签和完整结算权限口径")

    # 用配置值核对最容易发生漂移的合力参数；允许文档中存在空格。
    coalition = RULE_CONFIG["dominance"]["coalition"]
    expected_patterns = (
        rf"{int(coalition['min_super_net'] / 10000)}\s*万",
        rf"{int(coalition['min_main_net'] / 10000)}\s*万",
        rf"{int(coalition['min_flow_5m'] / 10000)}\s*万",
        rf"{int(coalition['min_super_ratio'] * 100)}%\s*≤",
        rf"<\s*{int(coalition['max_super_ratio_exclusive'] * 100)}%",
        rf"主买比\s*≥\s*{coalition['min_buy_ratio']:g}",
    )
    missing_values = [pattern for pattern in expected_patterns if not re.search(pattern, framework)]
    if missing_values:
        _add(result, "fail", "选股框架中的合力参数与共享配置不一致或无法解析")
    else:
        _add(result, "pass", "选股框架合力参数与共享配置逐项匹配")

    claude_required = ("选股框架.md", "skills/盘中/SKILL.md", "七项支撑")
    missing = [fragment for fragment in claude_required if fragment not in claude]
    if missing:
        _add(result, "fail", f"CLAUDE.md 缺少规则/执行入口引用：{'、'.join(missing)}")
    else:
        _add(result, "pass", "CLAUDE.md 引用唯一规则与项目skill，保留七项支撑工作流")

    # 针对已发生的旧指令回流；这不是通用自然语言语义证明。
    skill = _read_text(project_root / "skills/盘中/SKILL.md") or ""
    display = _read_text(project_root / "docs/ggp_display_prompt.md") or ""
    for phrase in ("直接出手（核心仓位）", "感觉单需双倍纪律", "clean 优先` →", "14:20 后（午后买点截止）不再扫描", "- 决策：买/空仓"):
        if phrase in framework + claude + skill:
            _add(result, "fail", f"现行文档重新出现已废弃指令：{phrase}")
    if "七项支撑" not in display or "一句规则依据" in display:
        _add(result, "fail", "ggp显示层未保留真实仓完整七项支撑")
    if not skill or "../../选股框架.md" not in skill:
        _add(result, "fail", "项目盘中skill缺失或未引用框架")
    if not result["fail"]:
        _add(result, "pass", "已知冲突短语未回流，显示层与项目入口保留规则引用")

    return result


def _progress_line(text: str, marker: str) -> Optional[str]:
    for line in text.splitlines():
        if marker in line:
            return line
    return None


def check_framework_progress(
    project_root: Path = PROJECT_ROOT,
    db: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """分别核对采集数和完整结算进度，未结算不得计入 x/20。"""
    result = _empty_result()
    framework = _read_text(project_root / "选股框架.md")
    if framework is None:
        _add(result, "fail", "无法读取选股框架，不能对账影子进度")
        return result
    if db is None:
        _add(result, "warn", "影子样本库不可用，跳过框架进度对账")
        return result

    markers = {
        "coalition": "⑥合力主升主导验证",
        "breakout": "⑦观察池突破状态机",
        "sector_boost": "⑧主线板块协同加分器",
        "divergence": "⑨龙头分歧识别",
    }
    samples = db.get("samples") or {}
    targets = db.get("targets") or {}
    for category, marker in markers.items():
        line = _progress_line(framework, marker)
        if line is None:
            _add(result, "fail", f"选股框架缺少待验证项：{category}")
            continue
        configured_target = int(RULE_CONFIG["shadow"]["target_samples"])
        match = re.search(r"完整结算\s*(\d+)\s*/\s*(\d+)", line)
        collected = re.search(r"已采集\s*(\d+)", line)
        if not match or not collected:
            _add(result, "fail", f"选股框架必须分开标注 {category} 的已采集数与完整结算进度")
            continue
        reported_count, reported_target = int(match[1]), int(match[2])
        rows = samples.get(category) or []
        actual_count = sum(is_complete_shadow_result(row.get("t1_result")) for row in rows if isinstance(row, dict))
        if int(collected[1]) != len(rows):
            _add(result, "fail", f"{category} 采集数不一致：框架{collected[1]}，影子库{len(rows)}")
        if reported_count != actual_count or reported_target != configured_target:
            _add(
                result,
                "fail",
                f"{category} 进度不一致：框架 {reported_count}/{reported_target}，"
                f"影子库 {actual_count}/{configured_target}",
            )
        else:
            db_target = (targets.get(category) or {}).get("target_samples")
            if db_target != configured_target:
                _add(result, "fail", f"{category} 影子库目标值为 {db_target}，配置要求 {configured_target}")
            else:
                _add(result, "pass", f"{category} 完整结算进度一致：{actual_count}/{configured_target}，已采集{len(rows)}")
    return result


def check_shadow_database(
    project_root: Path = PROJECT_ROOT,
    db_path: Optional[Path] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """检查影子数据库结构、完整结算口径和未完成样本隔离。"""
    result = _empty_result()
    path = db_path or project_root / "tools" / "shadow_data" / "shadow_samples.json"
    if not path.exists():
        _add(result, "warn", f"影子样本库不存在，尚未开始采样：{path}")
        return result
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _add(result, "fail", f"影子样本库不是有效 JSON：{exc}")
        return result
    if not isinstance(db, dict):
        _add(result, "fail", "影子样本库顶层结构不是对象")
        return result

    expected = set(shadow_targets())
    actual_targets = set((db.get("targets") or {}).keys())
    actual_samples = set((db.get("samples") or {}).keys())
    if actual_targets != expected or actual_samples != expected:
        _add(
            result,
            "fail",
            f"影子机制类别不一致：配置={sorted(expected)}，"
            f"targets={sorted(actual_targets)}，samples={sorted(actual_samples)}",
        )
        return result

    for category, meta in shadow_targets().items():
        target_meta = db["targets"].get(category) or {}
        if target_meta.get("target_samples") != meta["target_samples"]:
            _add(result, "fail", f"{category} 数据库目标值与共享配置不一致")
            continue
        rows = db["samples"].get(category)
        if not isinstance(rows, list):
            _add(result, "fail", f"{category} 样本不是数组")
            continue

        complete = 0
        incomplete = 0
        for sample in rows:
            if not isinstance(sample, dict):
                _add(result, "fail", f"{category} 存在非对象样本")
                continue
            t1_result = sample.get("t1_result")
            if is_complete_shadow_result(t1_result):
                complete += 1
            else:
                incomplete += 1
                if isinstance(t1_result, dict) and t1_result.get("checked") is True:
                    _add(result, "fail", f"{category}/{sample.get('id', '?')} checked=true 但未满足完整结算口径")
        _add(
            result,
            "pass",
            f"{category} 结构有效：总样本 {len(rows)}，完整结算 {complete}，未完成 {incomplete}（未计入统计）",
        )
    return result


def check_code_wiring(project_root: Path = PROJECT_ROOT) -> Dict[str, List[Dict[str, str]]]:
    """确认关键运行模块已接入共享配置和严格影子门槛。"""
    result = _empty_result()
    paths = (
        project_root / "daily-stock-analysis" / "scripts" / "a_share_daily_screen.py",
        project_root / "tools" / "scan_reports.py",
        project_root / "tools" / "verify_t1.py",
        project_root / "tools" / "shadow_tracker.py",
        project_root / "tools" / "detect_divergence_leader.py",
    )
    for path in paths:
        text = _read_text(path)
        if text is None:
            _add(result, "fail", f"关键运行文件不存在：{path}")
        elif "rule_config" not in text or "RULE_CONFIG" not in text:
            _add(result, "fail", f"关键运行文件未接入共享配置：{path.relative_to(project_root)}")
    if not result["fail"]:
        _add(result, "pass", "生产筛选、诊断、影子结算和分歧检测均已接入共享配置")

    screen_text = _read_text(project_root / "daily-stock-analysis" / "scripts" / "a_share_daily_screen.py") or ""
    if "RISK_CONFIG" not in screen_text:
        _add(result, "fail", "生产筛选引擎未接入共享风险策略配置")
    else:
        _add(result, "pass", "生产筛选引擎已接入共享风险策略配置")

    for relative in (Path("tools/scan_reports.py"), Path("tools/verify_t1.py")):
        text = _read_text(project_root / relative) or ""
        if re.search(r"^\s*TIME_WINDOWS\s*=", text, re.MULTILINE):
            _add(result, "fail", f"{relative} 仍私自声明 TIME_WINDOWS")
        elif relative.name == "verify_t1.py" and re.search(r"[\"'](?:0930|0945)[\"']", text):
            _add(result, "fail", f"{relative} 仍硬编码 T+1 时刻")
        else:
            _add(result, "pass", f"{relative} 已从共享配置读取执行窗口")

    shadow_text = _read_text(project_root / "tools" / "shadow_tracker.py") or ""
    if 'super_lead == "✓(合力)"' not in shadow_text:
        _add(result, "fail", "影子采集未锁定生产报告严格标签 ✓(合力)")
    elif "is_complete_shadow_result" not in shadow_text:
        _add(result, "fail", "影子报表未使用统一完整结算判断")
    else:
        _add(result, "pass", "影子采样严格读取 ✓(合力)，报表使用完整结算门槛")

    # 该检查针对曾经出现过的固定次日日期问题，只扫运行工具，不扫历史测试夹具。
    for relative in (Path("tools/shadow_tracker.py"), Path("tools/detect_divergence_leader.py")):
        text = _read_text(project_root / relative) or ""
        if re.search(r"8\s*/\s*24|20260824|待下一个交易日\s*\(\s*8/24\s*\)", text):
            _add(result, "fail", f"运行工具残留固定次日日期：{relative}")
    if not result["fail"]:
        _add(result, "pass", "影子运行工具未发现固定 8/24 次日结算日期")
    return result


def check_authority_boundaries(project_root: Path = PROJECT_ROOT) -> Dict[str, List[Dict[str, str]]]:
    """检查工具层没有重新建立第二套交易裁决规则。"""
    result = _empty_result()
    skill = _read_text(project_root / "daily-stock-analysis" / "SKILL.md") or ""
    trading_rules = _read_text(project_root / "daily-stock-analysis" / "references" / "trading-rules.md") or ""
    if "不承担最终买卖" not in skill or "选股框架.md" not in skill:
        _add(result, "fail", "daily-stock-analysis/SKILL.md 未明确筛选工具与交易裁决边界")
    else:
        _add(result, "pass", "筛选工具已声明由盘中 skill、框架和决策记录负责最终裁决")
    if "不再维护第二套交易规则" not in trading_rules and "不承担最终买卖" not in trading_rules:
        _add(result, "fail", "trading-rules.md 可能重新形成第二套交易规则")
    else:
        _add(result, "pass", "辅助 trading-rules 文档未建立第二套交易裁决规则")
    return result


def check_active_skill(project_root: Path = PROJECT_ROOT) -> Dict[str, List[Dict[str, str]]]:
    """项目版为唯一入口；旧安装版不得重新启用。"""
    result = _empty_result()
    workspace_skill = project_root / "skills" / "盘中" / "SKILL.md"
    active_skill = Path.home() / ".codex" / "skills" / "盘中" / "SKILL.md"
    workspace_text = _read_text(workspace_skill)
    if workspace_text is None:
        _add(result, "fail", "项目盘中skill不存在")
    elif active_skill.exists():
        _add(result, "fail", "已停用的安装版盘中skill重新出现，应仅使用项目入口")
    else:
        _add(result, "pass", "仅项目盘中skill有效，旧安装版入口已停用")
    return result


def _merge(into: Dict[str, List[Dict[str, str]]], other: Dict[str, List[Dict[str, str]]]) -> None:
    for level in into:
        into[level].extend(other.get(level, []))


def validate_workspace(project_root: Path = PROJECT_ROOT) -> Dict[str, List[Dict[str, str]]]:
    """执行完整审计并返回 pass/warn/fail 三类结果。"""
    result = _empty_result()
    for check in (
        check_config(),
        check_documents(project_root),
        check_shadow_database(project_root),
        check_framework_progress(project_root, _load_shadow_db(project_root)),
        check_code_wiring(project_root),
        check_authority_boundaries(project_root),
        check_active_skill(project_root),
    ):
        _merge(result, check)
    return result


def _load_shadow_db(project_root: Path) -> Optional[Dict[str, Any]]:
    path = project_root / "tools" / "shadow_data" / "shadow_samples.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="检查选股框架、代码、影子库和权限门槛的一致性")
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = validate_workspace()
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for issue in result["pass"]:
            print(f"PASS: {issue['message']}")
        for issue in result["warn"]:
            print(f"WARN: {issue['message']}")
        for issue in result["fail"]:
            print(f"FAIL: {issue['message']}")
        print(
            f"\n一致性检查：{len(result['fail'])} 个 FAIL，"
            f"{len(result['warn'])} 个 WARN，{len(result['pass'])} 个 PASS"
        )
    return 1 if result["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
