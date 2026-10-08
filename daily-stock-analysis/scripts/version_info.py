#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本与发布渠道的唯一来源（仅标准库，不依赖其它模块，避免循环导入）。

**渠道是安装版本 tag 的属性，不是运行环境的属性。** 镜像
``ghcr.io/luqtest/big-a-chovy:v0.6.1`` 属于 stable 渠道（它的升级路径就是
「0.6.2 发布了 → 拉新镜像」），``v0.6.0-docker.2`` 才属于 docker 渠道。把
「是否跑在容器里」当成渠道会让所有用稳定镜像的用户永远收不到更新提示。

tag 约定（与 CHANGELOG、``docs/v0.6.0-发布执行方案.md`` 一致）：

    ``vX.Y.Z``             → stable
    ``vX.Y.Z-preview.N``   → preview
    ``vX.Y.Z-docker.N``    → docker

仓库根 ``VERSION`` 只保存**共同的基础版本**（如 ``0.6.1``），渠道与渠道序号由
发布 tag 提供。本地运行物的口径：

    1. 设了 ``A_SHARE_VERSION``（Docker 构建按 tag 烤入）→ 以它为准；
    2. 否则读 ``VERSION`` 作基础版本，渠道按 ``A_SHARE_CHANNEL``（默认 stable）。

不认识的 tag 形态（例如 ``v0.6.0-rc.1``）解析为 ``None``：宁可报「无法比较」，
也不要把未知渠道猜成 stable 后推出错误的升级提示。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"

VERSION_ENV = "A_SHARE_VERSION"
CHANNEL_ENV = "A_SHARE_CHANNEL"

STABLE = "stable"
PREVIEW = "preview"
DOCKER = "docker"
CHANNELS = (STABLE, PREVIEW, DOCKER)

CHANNEL_LABELS = {
    STABLE: "正式版",
    PREVIEW: "预览版",
    DOCKER: "容器版",
}

# 允许 tag 带 v 前缀（git tag 用 ``v0.6.1``，烤进镜像的环境变量用 ``0.6.1``）。
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(preview|docker)\.(\d+))?$")


@dataclass(frozen=True)
class VersionTag:
    """一个可比较的版本标识：基础版本 + 渠道 + 渠道序号。"""

    major: int
    minor: int
    patch: int
    channel: str = STABLE
    index: int | None = None

    @property
    def base(self) -> str:
        """共同的基础版本（写进 ``VERSION``、由 CI 与 CHANGELOG 对账的部分）。"""
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def full(self) -> str:
        """展示与镜像 tag 用的完整版本（不含 ``v`` 前缀）。"""
        if self.channel == STABLE or self.index is None:
            return self.base
        return f"{self.base}-{self.channel}.{self.index}"

    @property
    def label(self) -> str:
        return f"{self.full}（{CHANNEL_LABELS.get(self.channel, self.channel)}）"

    @property
    def image_ref(self) -> str:
        """对应的 GHCR 镜像引用。发布 manifest 里的写法就是 ``:v0.6.1``。"""
        return f"ghcr.io/luqtest/big-a-chovy:v{self.full}"

    def sort_key(self) -> tuple[int, int, int, int]:
        return (self.major, self.minor, self.patch, self.index or 0)


def parse_version(raw: str | None) -> VersionTag | None:
    """解析 tag 或版本串；形态不认识时返回 ``None``（不猜渠道）。"""
    if raw is None:
        return None
    match = _TAG_RE.match(str(raw).strip())
    if not match:
        return None
    channel = match.group(4) or STABLE
    index = int(match.group(5)) if match.group(5) is not None else None
    return VersionTag(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        channel=channel,
        index=index,
    )


def read_base_version(version_file: Path | None = None) -> str | None:
    """读 ``VERSION`` 的第一行有效内容（忽略空行与 ``#`` 注释）。"""
    path = version_file or VERSION_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def local_version(
    version_file: Path | None = None,
    env: dict | None = None,
) -> VersionTag | None:
    """当前运行物的版本。解析不出来返回 ``None``，由调用方决定怎么提示。"""
    environ = os.environ if env is None else env
    baked = parse_version(environ.get(VERSION_ENV))
    if baked is not None:
        return baked

    base = read_base_version(version_file)
    if base is None:
        return None
    tag = parse_version(base)
    if tag is None:
        return None

    channel = (environ.get(CHANNEL_ENV) or STABLE).strip().lower()
    if channel == STABLE or channel not in CHANNELS:
        # 渠道写错时退回 stable：stable 是「只与正式版比较」的最保守口径，
        # 不会把预览版或容器版的更新推给一个渠道未知的安装。
        return tag
    return VersionTag(tag.major, tag.minor, tag.patch, channel, tag.index)


def is_newer(candidate: VersionTag, local: VersionTag) -> bool:
    """同一渠道内的严格新于判断；渠道不同一律返回 ``False``。

    跨渠道比大小正是要避免的：``0.6.0-docker.2`` 与 ``0.6.0-preview.2`` 来自
    同一源码提交（见 CHANGELOG），把渠道序号当成功能新旧会推出错误的升级建议。
    """
    if candidate.channel != local.channel:
        return False
    return candidate.sort_key() > local.sort_key()
