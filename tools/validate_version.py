#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本一致性校验：VERSION、CHANGELOG 顶部条目与发布 tag 必须互相对账。

引入版本号之后，漂移是必然的：``VERSION`` 改了 CHANGELOG 没改、tag 打成了别的
基础版本、或者渠道后缀写成了 ``-rc.1`` 这种没人认识的形态。这里把三条线索锁在
一起，**基础版本与渠道分别校验**：

    VERSION            共同的基础版本（如 ``0.6.1``）
    CHANGELOG 顶部条目   ``## v0.6.0-docker.2`` → 基础版本必须等于 VERSION
    发布 tag            基础版本必须等于 VERSION；渠道必须是 preview/docker 之一
                        （无后缀即 stable）

用法：
    python3 tools/validate_version.py                     # 只校验工作副本
    python3 tools/validate_version.py --tag v0.6.1        # 发布流水线里带上 tag
    python3 tools/validate_version.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "daily-stock-analysis" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import version_info  # noqa: E402

CHANGELOG_HEADING_RE = re.compile(r"^##\s+(v?\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?)\b", re.MULTILINE)


def _issue(level: str, message: str) -> Dict[str, str]:
    return {"level": level, "message": message}


def _empty() -> Dict[str, List[Dict[str, str]]]:
    return {"pass": [], "warn": [], "fail": []}


def top_changelog_version(project_root: Path = PROJECT_ROOT) -> Optional[str]:
    """CHANGELOG 最上面那条版本条目的 tag。"""
    path = project_root / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = CHANGELOG_HEADING_RE.search(text)
    return match.group(1) if match else None


def check_version_consistency(
    project_root: Path = PROJECT_ROOT,
    tag: Optional[str] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """校验 VERSION / CHANGELOG / 发布 tag 三者一致。只读，不改任何文件。"""
    result = _empty()

    base = version_info.read_base_version(project_root / "VERSION")
    if base is None:
        result["fail"].append(_issue("fail", "缺少仓库根 VERSION 文件或其内容为空"))
        return result
    parsed_base = version_info.parse_version(base)
    if parsed_base is None or parsed_base.channel != version_info.STABLE or parsed_base.index is not None:
        result["fail"].append(
            _issue("fail", f"VERSION 必须是纯基础版本（如 0.6.1），当前为 {base!r}")
        )
        return result
    result["pass"].append(_issue("pass", f"VERSION 基础版本：{parsed_base.base}"))

    changelog_tag = top_changelog_version(project_root)
    if changelog_tag is None:
        result["fail"].append(_issue("fail", "CHANGELOG.md 未找到形如 ## v0.6.1 的版本条目"))
    else:
        parsed_changelog = version_info.parse_version(changelog_tag)
        if parsed_changelog is None:
            result["fail"].append(
                _issue("fail", f"CHANGELOG 顶部版本条目形态无法识别：{changelog_tag}")
            )
        elif parsed_changelog.base != parsed_base.base:
            result["fail"].append(
                _issue(
                    "fail",
                    f"CHANGELOG 顶部版本 {changelog_tag} 的基础版本与 VERSION "
                    f"{parsed_base.base} 不一致",
                )
            )
        else:
            result["pass"].append(
                _issue(
                    "pass",
                    f"CHANGELOG 顶部版本 {changelog_tag} 与 VERSION 基础版本一致"
                    f"（渠道 {version_info.CHANNEL_LABELS.get(parsed_changelog.channel)}）",
                )
            )

    local = version_info.local_version(version_file=project_root / "VERSION", env={})
    if local is None:
        result["fail"].append(_issue("fail", "version_info 无法从 VERSION 解析出本机版本"))
    else:
        result["pass"].append(_issue("pass", f"本机默认渠道解析为 {local.channel}"))

    if tag is None:
        return result

    parsed_tag = version_info.parse_version(tag)
    if parsed_tag is None:
        result["fail"].append(
            _issue(
                "fail",
                f"发布 tag {tag} 形态无法识别：渠道只允许 preview / docker，"
                "无后缀即正式版",
            )
        )
        return result

    # 基础版本与渠道分开校验：两者是不同的失败原因，合并成一句会让排查变难。
    if parsed_tag.base != parsed_base.base:
        result["fail"].append(
            _issue(
                "fail",
                f"发布 tag {tag} 的基础版本 {parsed_tag.base} 与 VERSION "
                f"{parsed_base.base} 不一致",
            )
        )
    else:
        result["pass"].append(_issue("pass", f"发布 tag {tag} 的基础版本与 VERSION 一致"))

    result["pass"].append(
        _issue(
            "pass",
            f"发布 tag {tag} 渠道：{parsed_tag.channel}"
            f"（{version_info.CHANNEL_LABELS.get(parsed_tag.channel)}）",
        )
    )
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校验 VERSION、CHANGELOG 与发布 tag 的一致性")
    parser.add_argument("--tag", help="发布 tag（如 v0.6.1 或 v0.6.0-preview.1）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    args = parser.parse_args(argv)

    result = check_version_consistency(tag=args.tag)
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
            f"\n版本一致性：{len(result['fail'])} 个 FAIL，"
            f"{len(result['warn'])} 个 WARN，{len(result['pass'])} 个 PASS"
        )
    return 1 if result["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
