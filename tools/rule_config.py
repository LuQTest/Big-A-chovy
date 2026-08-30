#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享的机器执行参数注册表。

《选股框架.md》是规则的语义权威；本文件不是第二份规则文档，而是把
跨模块、容易漂移的执行参数集中登记，供生产筛选、诊断工具和影子结算
读取。规则含义、案例、状态和权限仍以框架及决策记录为准。

修改这里的参数时，应同步修改《选股框架.md》的参数总表，并运行：
    python3 tools/validate_consistency.py
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, Dict


RULE_CONFIG: Dict[str, Any] = {
    "version": "20260827-v1",
    "authority": {
        "framework": "选股框架.md",
        "decision_records_dir": "决策记录",
        "screening_skill": "盘中",
    },
    "dominance": {
        "absolute": {
            "min_super_net": 0.0,
            "min_super_ratio": 0.50,
            "label": "✓(绝对)",
        },
        "coalition": {
            "min_super_net": 20_000_000.0,
            "min_big_net": 0.0,
            "min_super_ratio": 0.20,
            "max_super_ratio_exclusive": 0.50,
            "min_main_net": 50_000_000.0,
            "min_flow_5m": 10_000_000.0,
            "min_history_snapshots": 2,
            "max_decay_pct": 10.0,
            "min_buy_ratio": 1.5,
            "label": "✓(合力)",
        },
        "none_label": "✗",
        "negative_super_veto": True,
    },
    "screening": {
        "low_absorb": {
            "main_pct_min_exclusive": 5.0,
            "flow_5m_b_min": 1_000_000.0,
            "flow_5m_a_min": 5_000_000.0,
            "resonance_candidates_min": 2,
            "pullback_tiers": [
                {"max_pullback_exclusive": 1.0, "main_pct_min_exclusive": 5.0},
                {"max_pullback_exclusive": 2.0, "main_pct_min_exclusive": 10.0},
                {"max_pullback_exclusive": 3.0, "main_pct_min_exclusive": 15.0},
            ],
        },
        "breakout": {
            "morning_observe_start": "09:30",
            "morning_observe_end": "10:10",
            "flow_5m_min": 5_000_000.0,
            "confirmations_min": 2,
            "no_chase_multiplier": 1.025,
            "a_main_pct_min": 5.0,
            "a_high_pullback_max_exclusive": 1.5,
        },
        "sector_boost": {
            "anchor_amount_min": 2_000_000_000.0,
            "anchor_high_pullback_max_exclusive": 2.0,
            "resonance_total_min": 3,
            "non_anchor_resonance_min": 2,
            "stock_main_pct_min_exclusive": 5.0,
            "boost_points": 15.0,
        },
        "flow": {
            "volume_surge_ratio_min": 2.0,
            "buy_ratio_denominator_floor": 1.0,
            "buy_ratio_surge_cap": 0.5,
            "buy_ratio_surge_scale": 20.0,
        },
    },
    "intersection": {
        "confirmation_snapshots": 2,
        "morning_cutoff": "11:00",
        "afternoon_start": "13:05",
        "afternoon_buy_deadline": "14:20",
        "overheat_change_pct": 4.5,
        "overheat_turnover_pct": 8.0,
        "signal_age_window_minutes": 30,
        "intersection_basis": "strict_trend",
        "pre_gate_main_net": True,
        "pre_gate_flow_5m": True,
        "pre_gate_above_vwap": True,
        "pre_gate_resonance": False,
        "late_change_pct": 4.6,
        "late_vwap_dist_pct": 1.2,
        "late_turnover_pct": 7.0,
        "late_high_pull_pct": 1.5,
        "late_pulse_change_pct": 3.5,
        "late_pulse_high_pull_pct": 0.5,
        "late_pulse_vol_ratio": 5.0,
        "intersection_latch_minutes": 15,
        "pullback_min_pct": 0.5,
        "pullback_max_pct": 1.5,
        "pullback_vol_ratio": 0.7,
        "pullback_recover_pct": 0.4,
        "pullback_vwap_hold": True,
        "pullback_flow_5m_positive": True,
        "pre_confirm_snapshots": 2,
        "retest_confirm_snapshots": 2,
        "minute_fresh_seconds": 180,
        "market_breadth_normal": 55.0,
        "market_breadth_light": 48.0,
        "market_breadth_downgrade": 42.0,
        "index_extreme_change_pct": -5.0,
        "index_extreme_codes": ["399006"],
    },
    "divergence": {
        "min_main_pct_exclusive": 5.0,
        "lookback_snapshots": 5,
        "min_rise_pct": 1.5,
        "max_pullback_exclusive": 1.0,
        "min_sector_candidates": 2,
        "bypass_ratio_exclusive": 0.20,
    },
    "shadow": {
        "target_samples": 20,
        "t1_target_minute": 585,
        "false_breakout_stop_pct": 1.5,
        "required_complete_source": "daily_kline",
        "categories": {
            "coalition": "合力主升主导",
            "breakout": "观察池突破状态机",
            "sector_boost": "主线板块协同加分器",
            "divergence": "龙头分歧识别(divergence_leader)",
        },
    },
    "permissions": {
        "real_account_requires_complete_samples": True,
        "experimental_categories": [
            "coalition",
            "breakout",
            "sector_boost",
            "divergence",
        ],
    },
}


def get_rule_config() -> Dict[str, Any]:
    """返回独立副本，避免调用方意外修改共享配置。"""
    return deepcopy(RULE_CONFIG)


def shadow_targets() -> Dict[str, Dict[str, Any]]:
    """生成影子数据库使用的标准 targets 结构。"""
    shadow = RULE_CONFIG["shadow"]
    target = int(shadow["target_samples"])
    return {
        category: {"name": name, "target_samples": target}
        for category, name in shadow["categories"].items()
    }


def is_complete_shadow_result(result: Any) -> bool:
    """统一判断一个 T+1 结果是否达到“完整结算样本”口径。

    只有 checked、extremes_complete、日K来源和全部指标同时满足，才可
    计入胜率、极值统计或 20 样本达标判断。
    """
    if not isinstance(result, dict):
        return False
    if result.get("checked") is not True:
        return False
    if result.get("extremes_complete") is not True:
        return False
    if result.get("source") != RULE_CONFIG["shadow"]["required_complete_source"]:
        return False

    numeric_fields = (
        "t1_0945_price",
        "t1_0945_return_pct",
        "t1_max_gain_pct",
        "t1_max_drawdown_pct",
    )
    for field in numeric_fields:
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not isfinite(float(value)):
            return False
    return isinstance(result.get("is_false_breakout"), bool)

