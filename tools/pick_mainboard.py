"""Classify every candidate in a Big-A screening report by board (主板/创业板/科创板/北交所).

Usage:
    python pick_mainboard.py [report.md]

Reads all Markdown tables in the report, extracts stock codes, de-duplicates,
and prints the main-board subset (eligible) plus the excluded boards.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

CODE_RE = re.compile(r"\b(\d{6})\b")

# board rules for A-shares
def board_of(code: str) -> str:
    if code.startswith("688") or code.startswith("689"):
        return "科创板"
    if code.startswith("300") or code.startswith("301"):
        return "创业板"
    if code.startswith(("600", "601", "603", "605")):
        return "主板-沪"
    if code.startswith(("000", "001", "002", "003")):
        return "主板-深"
    if code.startswith(("430", "83", "87", "88", "920")):
        return "北交所"
    return "其他/未知"


def parse(path: Path):
    """Return {code: set(section names)} for every table row containing a code."""
    found: dict[str, set[str]] = {}
    section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or set("".join(cells)) <= set("-: "):
            continue
        # a stock row: one cell is exactly a 6-digit code, and the next is a name
        for idx, cell in enumerate(cells):
            if re.fullmatch(r"\d{6}", cell):
                found.setdefault(cell, set()).add(section)
                break
    return found


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if path is None:
        # newest report
        cands = sorted(Path("../筛选结果" if Path("../筛选结果").exists() else "筛选结果").glob("A股筛选结果_*.md"))
        if not cands:
            print("no report found")
            return 1
        path = cands[-1]
    print(f"报告: {path.name}")
    found = parse(path)
    groups: dict[str, list[str]] = {}
    for code in sorted(found):
        groups.setdefault(board_of(code), []).append(code)

    order = ["主板-沪", "主板-深", "创业板", "科创板", "北交所", "其他/未知"]
    for board in order:
        codes = groups.get(board, [])
        if not codes:
            continue
        print(f"\n### {board} · {len(codes)} 只")
        for code in codes:
            print(f"  {code}  [{', '.join(sorted(found[code]))}]")

    main_codes = groups.get("主板-沪", []) + groups.get("主板-深", [])
    print(f"\n{'='*60}")
    print(f"主板可买: {len(main_codes)} 只")
    print("无资格(创业板+科创板):",
          len(groups.get("创业板", [])) + len(groups.get("科创板", [])), "只")
    print(f"全部候选: {len(found)} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
