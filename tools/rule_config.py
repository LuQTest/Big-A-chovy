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
    "version": "20260908-v1",
    "authority": {
        "framework": "选股框架.md",
        "decision_records_dir": "决策记录",
        "screening_skill": "盘中",
    },
    "execution": {
        # 这些时段是诊断报告的阶段映射，具体买卖裁决仍以选股框架和
        # 盘中 skill 为准；集中登记是为了避免各工具各自漂移。
        "time_windows": {
            "observe_only": {"start": "09:30", "end": "09:40", "label": "观察期·禁买"},
            "pending_confirm": {"start": "09:40", "end": "09:50", "label": "等待确认"},
            "primary_window": {"start": "09:50", "end": "10:45", "label": "第一买点窗口"},
            "morning_confirm": {"start": "10:45", "end": "11:30", "label": "早盘确认期"},
            "lunch_break": {"start": "11:30", "end": "13:00", "label": "午休"},
            "afternoon_reflux": {"start": "13:00", "end": "13:45", "label": "午后回流·仓位减半"},
            "afternoon_late": {"start": "13:45", "end": "14:20", "label": "午后尾段"},
            "tail_risk": {"start": "14:20", "end": "14:40", "label": "尾盘风控·禁新仓"},
            "market_close": {"start": "14:40", "end": "15:15", "label": "收盘·仅持仓管理"},
        },
        "pre_market": {"key": "pre_market", "start": "09:30", "label": "盘前"},
        "fallback_window": "market_close",
        "t1_exit_window": {"start": "09:30", "end": "09:45", "target": "09:45"},
    },
    "risk": {
        "statuses": {
            "clean": "clean",
            "watch_risk": "watch_risk",
            "avoid": "avoid",
            "unknown": "unknown",
        },
        "hard_blacklist": {
            "600664": "20260728-框架教训：哈药股份暴涨后高位派发+追高→死亡螺旋，绝不补仓/不做低吸",
        },
        "low_absorb_exclusion": {
            "five_day_return_min_ratio": 0.12,
            "main_pct_max_inclusive": 0.0,
            "history_lookback_snapshots": 6,
            "negative_main_snapshots_min": 3,
        },
        "announcement": {
            "hard_keywords": [
                "减持", "被动减持", "清仓式减持", "监管函", "问询函", "关注函", "警示函",
                "立案", "调查", "行政处罚", "纪律处分", "公开谴责", "退市风险", "其他风险警示",
                "业绩预亏", "业绩亏损", "业绩预损", "业绩下修", "业绩修正", "大幅下降", "计提减值", "商誉减值",
                "限售股上市流通", "解除限售", "解禁", "股份冻结", "司法冻结", "诉讼", "仲裁",
                "债务逾期", "担保逾期", "资金占用", "无法表示意见", "保留意见", "停牌核查",
            ],
            "watch_keywords": [
                "质押", "担保", "关联交易", "业绩快报", "业绩预告", "更正公告", "补充公告",
                "高管辞职", "董事辞职", "会计政策变更", "审计机构", "股东大会延期",
            ],
            "ignore_keywords": [
                "权益分派", "分红", "法律意见书", "独立意见", "任职资格核准", "股东大会决议",
            ],
        },
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
            "max_decay_pct": 0.0,
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
            "morning_observe_end": "09:40",
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


def normalize_hhmm(value: Any) -> str:
    """将 09:45、0945、945 统一为四位 HHMM，并校验时刻格式。"""
    text = str(value).strip().replace(":", "")
    if len(text) == 3:
        text = f"0{text}"
    if len(text) != 4 or not text.isdigit():
        raise ValueError(f"invalid HHMM value: {value!r}")
    hour, minute = int(text[:2]), int(text[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid HHMM value: {value!r}")
    return text


def hhmm_to_minutes(value: Any) -> int:
    """将 HHMM/HH:MM 转为当天分钟数，供时间窗口逻辑复用。"""
    text = normalize_hhmm(value)
    return int(text[:2]) * 60 + int(text[2:])


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
