#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股筛选结果 Markdown 报告动态解析器
严格禁止硬编码列号，按表头动态匹配字段，防止列位变动导致的漏筛或错位。
"""

import os
import re
import glob
from typing import Dict, List, Any, Optional

def parse_markdown_table(lines: List[str], start_idx: int) -> tuple:
    """
    解析 Markdown 表格，从 start_idx 行开始（表头行）。
    返回: (行记录列表[字典], 表格结束行号)
    """
    if start_idx >= len(lines):
        return [], start_idx

    header_line = lines[start_idx].strip()
    if not header_line.startswith("|"):
        return [], start_idx

    # 提取表头
    raw_headers = [c.strip() for c in header_line.strip("|").split("|")]
    headers = [re.sub(r"\s+", "", h) for h in raw_headers]

    idx = start_idx + 1
    # 跳过分隔线 | --- | ---: | ...
    if idx < len(lines) and re.match(r"^\|\s*[-:]+\s*\|", lines[idx].strip()):
        idx += 1

    records = []
    while idx < len(lines):
        line = lines[idx].strip()
        if not line.startswith("|"):
            break
        # 提取单元格
        cells = [c.strip() for c in line.strip("|").split("|")]
        # 如果列数对得上或部分对上
        record = {}
        for h, c in zip(headers, cells):
            record[h] = c
        if record:
            records.append(record)
        idx += 1

    return records, idx

def parse_screening_report(filepath: str) -> Dict[str, Any]:
    """
    解析单个 A股筛选结果_YYYYMMDD_HHMM.md 文件
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    lines = content.split("\n")
    filename = os.path.basename(filepath)
    
    # 提取时间与日期
    m_time = re.search(r"(\d{8})_(\d{4})", filename)
    date_str = m_time.group(1) if m_time else ""
    time_str = m_time.group(2) if m_time else ""

    result: Dict[str, Any] = {
        "file": filename,
        "filepath": filepath,
        "date": date_str,
        "time": f"{time_str[:2]}:{time_str[2:]}" if len(time_str) == 4 else time_str,
        "meta": {},
        "market": {},
        "pools_count": {},
        "tables": {
            "capital_ranking": [],     # 主力资金优选
            "dual_intersection": [],   # 双池交集
            "state_machine": [],       # 交集状态机表格
            "short_pool": [],          # 超短池
            "trend_obs_pool": [],      # 趋势观察池
            "trend_conf_pool": [],     # 趋势确认池
            "low_absorb_short": [],    # 低吸超短线 A/B/C
            "low_absorb_trend": [],    # 低吸短线趋势 A/B/C
            "low_open_wash": [],       # 低开洗盘
            "tomorrow_watchlist": [],  # 明日观察池
            "capital_tracking": [],    # 重点候选资金追踪
        },
        "raw_sections": {}
    }

    # 提取头部宏观与市场宽度信息
    for line in lines[:10]:
        line_s = line.strip()
        if line_s.startswith("市场宽度：") or line_s.startswith("市场宽度:"):
            # 上涨 421 / 下跌 4760 / 平盘 23 / 跌停 145
            up = re.search(r"上涨\s*(\d+)", line_s)
            down = re.search(r"下跌\s*(\d+)", line_s)
            zt = re.search(r"涨停\s*(\d+)", line_s)
            dt = re.search(r"跌停\s*(\d+)", line_s)
            if up: result["market"]["up"] = int(up.group(1))
            if down: result["market"]["down"] = int(down.group(1))
            if zt: result["market"]["zt"] = int(zt.group(1))
            if dt: result["market"]["dt"] = int(dt.group(1))
        elif line_s.startswith("指数：") or line_s.startswith("指数:"):
            result["market"]["index_line"] = line_s
        elif line_s.startswith("双池运行："):
            short_p = re.search(r"超短池\s*(\d+)", line_s)
            trend_o = re.search(r"趋势观察池\s*(\d+)", line_s)
            trend_c = re.search(r"趋势确认池\s*(\d+)", line_s)
            inter = re.search(r"交集\s*(\d+)", line_s)
            if short_p: result["pools_count"]["short"] = int(short_p.group(1))
            if trend_o: result["pools_count"]["trend_obs"] = int(trend_o.group(1))
            if trend_c: result["pools_count"]["trend_conf"] = int(trend_c.group(1))
            if inter: result["pools_count"]["intersection"] = int(inter.group(1))

    # 解析各章节表格
    i = 0
    current_section = ""
    current_subsection = ""
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("## "):
            current_section = line[3:].strip()
            current_subsection = ""
            i += 1
            continue
        elif line.startswith("### "):
            current_subsection = line[4:].strip()
            i += 1
            continue
        elif line.startswith("|") and ("代码" in line or "类" in line):
            # 发现表格头
            records, next_i = parse_markdown_table(lines, i)
            # 如果在状态机子节下，为每条记录附加状态阶段标签
            if "状态机" in current_section or "门槛未过" in current_section:
                for r in records:
                    if "状态阶段" not in r and current_subsection:
                        r["状态阶段"] = current_subsection
            # 根据当前 section 分配
            sec_lower = current_section.lower()
            if "主力资金优选" in current_section:
                result["tables"]["capital_ranking"] = records
            elif "双池交集" in current_section:
                result["tables"]["dual_intersection"] = records
            elif "状态机" in current_section or "门槛未过" in current_section:
                result["tables"]["state_machine"].extend(records)
            elif "超短池" in current_section and "诊断" not in current_section:
                result["tables"]["short_pool"] = records
            elif "趋势观察池" in current_section:
                result["tables"]["trend_obs_pool"] = records
            elif "趋势确认池" in current_section:
                result["tables"]["trend_conf_pool"] = records
            elif "低吸超短线" in current_section:
                result["tables"]["low_absorb_short"] = records
            elif "低吸短线趋势" in current_section:
                result["tables"]["low_absorb_trend"] = records
            elif "低开洗盘" in current_section:
                result["tables"]["low_open_wash"] = records
            elif "明日观察池" in current_section:
                result["tables"]["tomorrow_watchlist"] = records
            elif "重点候选资金追踪" in current_section:
                result["tables"]["capital_tracking"] = records
            
            i = next_i
            continue
        i += 1

    return result

def get_report_files(base_dir: str, date_str: Optional[str] = None) -> List[str]:
    """获取指定日期或最新一天的所有报告文件列表（按时间排序）"""
    if date_str:
        target_dir = os.path.join(base_dir, date_str)
        if os.path.isdir(target_dir):
            files = sorted(glob.glob(os.path.join(target_dir, "A股筛选结果_*.md")))
            if files:
                return files
        direct_files = sorted(glob.glob(os.path.join(base_dir, f"A股筛选结果_{date_str}_*.md")))
        if direct_files:
            return direct_files
        return []
    
    # 查找最新的日期文件夹或直接文件
    direct_files = sorted(glob.glob(os.path.join(base_dir, "A股筛选结果_*.md")))
    if direct_files:
        # 提取最新日期
        dates = []
        for f in direct_files:
            m = re.search(r"(\d{8})_\d{4}", os.path.basename(f))
            if m:
                dates.append(m.group(1))
        if dates:
            latest_date = max(dates)
            return sorted(glob.glob(os.path.join(base_dir, f"A股筛选结果_{latest_date}_*.md")))

    all_subdirs = sorted([d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d)) and re.match(r"^\d{8}$", d)])
    if not all_subdirs:
        return []
    latest_day = all_subdirs[-1]
    return sorted(glob.glob(os.path.join(base_dir, latest_day, "A股筛选结果_*.md")))
