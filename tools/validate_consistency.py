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
from tools.validate_version import check_version_consistency  # noqa: E402


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
        screening = config["screening"]
        realtime = config["realtime"]
        shadow = config["shadow"]
        permissions = config["permissions"]
    except (KeyError, TypeError):
        _add(result, "fail", "共享参数缺少 dominance/execution/risk/screening/realtime/shadow/permissions 核心区段")
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

    try:
        if not isinstance(screening, dict) or not isinstance(realtime, dict):
            raise ValueError("screening/realtime must be objects")
        required_screening = (
            "low_absorb",
            "breakout",
            "sector_boost",
            "flow",
            "resonance",
            "market_snapshot",
            "strict_ultra",
            "strict_trend",
            "trend_observation",
            "low_ultra",
            "low_trend",
            "low_open_wash",
            "watchlist",
            "capital_rank",
        )
        missing_screening = [key for key in required_screening if not isinstance(screening.get(key), dict)]
        if missing_screening:
            raise ValueError(f"missing screening sections: {','.join(missing_screening)}")
        experimental = screening["low_absorb"]["experimental_retest_gate"]
        if experimental.get("enabled") is not False or experimental.get("permission") != "simulated_only":
            raise ValueError("experimental retest gate must remain disabled and simulated-only")
        for key in ("market_thermometer", "cross_validation", "entry_exit"):
            if not isinstance(realtime.get(key), dict):
                raise ValueError(f"missing realtime section: {key}")
        for section_name in ("strict_ultra", "strict_trend", "trend_observation", "low_ultra", "low_trend", "watchlist", "capital_rank"):
            section = screening[section_name]
            if not section or any(not _numeric(value) for value in section.values() if not isinstance(value, (dict, list))):
                raise ValueError(f"{section_name} contains invalid numeric parameters")
        _add(result, "pass", "筛选池、低吸评分、板块共振、资金排序与实时风控阈值已集中登记")
    except (KeyError, TypeError, ValueError):
        _add(result, "fail", "筛选或实时风控阈值区段类型、字段或实验权限不完整")

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


# 实验登记表的项目名（框架第六节）。用于核对“框架是否声明了目标与方法”。
PROGRESS_MARKERS = {
    "coalition": "⑥合力主升主导验证",
    "breakout": "⑦观察池突破状态机",
    "sector_boost": "⑧主线板块协同加分器",
    "divergence": "⑨龙头分歧识别",
}

# 框架必须保留的两条声明：进度只在本地维护；达标前仅模拟。
FRAMEWORK_PROGRESS_LOCAL_ONLY = "只在本地影子库维护"
FRAMEWORK_SIMULATED_UNTIL_TARGET = "各自达到20个完整结算样本并完成转正评估前仅模拟运行"

# 公开框架里**不得**出现的本地进度写法（出现即说明进度被写回了公开文档）。
_FRAMEWORK_PROGRESS_LEAK = re.compile(r"已采集\s*\d+|完整结算\s*\d+\s*/\s*\d+")


def check_framework_progress(
    project_root: Path = PROJECT_ROOT,
    db: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """核对框架的验证目标与权限声明，并把真实进度从本地影子库读出来。

    进度数字**不进公开框架**：框架第六节明确“样本采集与结算进度、单笔案例及金额
    只在本地影子库维护；公开框架只列验证目标和方法”。所以这里不再去解析框架里的
    x/20（那既要求把私有进度公开，也会让框架与本地库各存一份互相漂移），而是分两步：

    1. 框架侧：每类实验必须声明验证目标（``target_samples`` 个完整结算样本）与验证
       方法，并保留「进度只在本地影子库维护」与「达标前仅模拟运行」两条声明；框架里
       一旦出现 ``已采集 N`` / ``完整结算 N/M`` 这类进度写法即判 FAIL。
    2. 影子库侧：按统一完整结算口径（:func:`is_complete_shadow_result`）算出实际进度
       并通报，同时核对影子库的类别与目标值和共享配置一致。未结算样本不计入进度。
    """
    result = _empty_result()
    framework = _read_text(project_root / "选股框架.md")
    if framework is None:
        _add(result, "fail", "无法读取选股框架，不能核对验证目标与权限声明")
        return result

    configured_target = int(RULE_CONFIG["shadow"]["target_samples"])

    # ── 1. 框架侧：目标 + 方法 + 权限声明，且不得夹带本地进度 ──
    for category, marker in PROGRESS_MARKERS.items():
        line = _progress_line(framework, marker)
        if line is None:
            _add(result, "fail", f"选股框架缺少待验证项：{category}")
            continue
        if f"{configured_target}个完整结算样本" not in line:
            _add(
                result,
                "fail",
                f"{category} 未声明验证目标：应为「{configured_target}个完整结算样本」",
            )
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[2]:
            _add(result, "fail", f"{category} 只登记了目标，缺少验证方法")
        else:
            _add(result, "pass", f"{category} 已在框架登记验证目标与方法")

    if FRAMEWORK_PROGRESS_LOCAL_ONLY not in framework:
        _add(result, "fail", "选股框架未声明“进度只在本地影子库维护”，公开文档有变成进度台账的风险")
    elif FRAMEWORK_SIMULATED_UNTIL_TARGET not in framework:
        _add(result, "fail", "选股框架未声明实验达标前的权限规则（达标前仅模拟运行）")
    else:
        _add(result, "pass", "选股框架声明了本地维护进度与达标前仅模拟的权限规则")

    leaked = sorted(set(_FRAMEWORK_PROGRESS_LEAK.findall(framework)))
    if leaked:
        _add(
            result,
            "fail",
            "公开框架出现本地进度数字（应只存在于本地影子库）：" + "、".join(leaked),
        )

    # ── 2. 影子库侧：实际进度按统一完整结算口径读取，未结算不计入 ──
    if db is None:
        _add(result, "warn", "本地影子库不可用，只核对了框架声明，无法读取实际进度")
        return result

    samples = db.get("samples")
    targets = db.get("targets")
    if not isinstance(samples, dict) or not isinstance(targets, dict):
        _add(result, "fail", "本地影子库 targets/samples 结构异常，不能核对进度")
        return result
    for category in PROGRESS_MARKERS:
        rows = samples.get(category)
        if not isinstance(rows, list):
            _add(result, "fail", f"本地影子库缺少 {category} 的样本数组")
            continue
        db_target = (targets.get(category) or {}).get("target_samples")
        if db_target != configured_target:
            _add(
                result,
                "fail",
                f"{category} 影子库目标值为 {db_target}，共享配置要求 {configured_target}",
            )
            continue
        complete = sum(
            1
            for row in rows
            if isinstance(row, dict) and is_complete_shadow_result(row.get("t1_result"))
        )
        _add(
            result,
            "pass",
            f"{category} 实际进度（本地影子库）：完整结算 {complete}/{configured_target}；"
            f"已采集 {len(rows)}（未结算不计入）",
        )
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

    configured_targets = shadow_targets()
    expected = set(configured_targets)
    targets = db.get("targets")
    samples = db.get("samples")
    if not isinstance(targets, dict) or not isinstance(samples, dict):
        _add(result, "fail", "影子样本库 targets/samples 必须是对象")
        return result
    actual_targets = set(targets)
    actual_samples = set(samples)
    missing_targets = expected - actual_targets
    missing_samples = expected - actual_samples
    if missing_targets or missing_samples or actual_targets != actual_samples:
        _add(
            result,
            "fail",
            f"影子机制类别缺失/错位：配置至少包含={sorted(expected)}，"
            f"targets={sorted(actual_targets)}，samples={sorted(actual_samples)}",
        )
        return result

    for category in sorted(actual_targets):
        target_meta = targets.get(category)
        if not isinstance(target_meta, dict):
            _add(result, "fail", f"{category} 数据库目标配置不是对象")
            continue
        configured = configured_targets.get(category)
        target = target_meta.get("target_samples")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            _add(result, "fail", f"{category} 数据库目标值不是正整数")
            continue
        if configured is not None and target != configured["target_samples"]:
            _add(result, "fail", f"{category} 数据库目标值与共享配置不一致")
            continue
        rows = samples.get(category)
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
        project_root / "daily-stock-analysis" / "scripts" / "realtime_engine.py",
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
    entry = _read_text(project_root / "AGENTS.md") or ""
    required = (
        "选股框架.md",
        "交易规则唯一权威",
        "skills/盘中/SKILL.md",
        "项目唯一入口",
        "daily-stock-analysis/",
        "不做最终交易裁决",
    )
    missing = [item for item in required if item not in entry]
    if missing:
        _add(result, "fail", f"AGENTS.md 缺少权威边界声明：{'、'.join(missing)}")
    else:
        _add(result, "pass", "统一入口已明确框架、项目盘中和筛选工具的权威边界")
    if (project_root / "daily-stock-analysis" / "SKILL.md").exists():
        _add(result, "fail", "筛选引擎重新出现独立 SKILL.md，应仅保留项目盘中入口")
    else:
        _add(result, "pass", "筛选引擎未另设独立 AI Skill 入口")
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
        check_version_consistency(project_root),
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
