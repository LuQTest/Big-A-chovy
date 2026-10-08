#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新检查：查 GitHub Releases、按渠道筛选、只与同渠道比较（仅标准库）。

设计约束（都是为了让「有新版本」这件事不能反过来伤害盘中工作流）：

1. **只比同一渠道。** 渠道取自 tag 后缀（见 ``version_info``）。stable 安装只
   与正式版比，preview 只与预览版比，docker 只与容器版比，绝不跨渠道比大小——
   ``0.6.0-docker.2`` 与 ``0.6.0-preview.2`` 是同一源码提交的不同打包形态。
2. **查 Releases 列表而不是 ``/releases/latest``。** 后者按 GitHub 定义只返回
   非预发布、非草稿的 Release，预览渠道一旦按发布方案标成 prerelease 就会被它
   整体漏掉。
3. **失败完全静默。** 离线、限流、证书过期、返回体畸形都只体现在返回值里：
   不抛出、不打印、不写日志、不弹窗，软件照常使用。
4. **绝不阻塞任何请求。** 首次检查在后台线程里跑；接口只读缓存快照，没有结果
   时返回 ``checking``。缓存 24 小时内不重复联网，检查失败时保留上一次的好结果
   并顺延下次尝试，避免上游不可用期间反复打接口。
5. **不下载、不替换、不重启。** 只提示版本号、更新内容和升级命令。

隐私：这是本项目**唯一**的对外请求，只发出版本检查本身，不带任何本机数据
（无报告、无持仓、无决策记录、无机器标识）。用 ``A_SHARE_UPDATE_CHECK=off`` 关闭。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request as urlrequest

import runtime_paths
import version_info
from version_info import VersionTag

REPO = "LuQTest/Big-A-chovy"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
CHANGELOG_URL = f"https://github.com/{REPO}/blob/main/CHANGELOG.md"
USER_AGENT = "Big-A-chovy-update-check"

# 只接受 https://github.com/ 开头的 Release 链接；上游响应被中间人改写时，
# 一个 javascript: 或 data: 的 href 就是一次点击即执行的 XSS。
ALLOWED_RELEASE_URL_PREFIX = "https://github.com/"

CACHE_NAME = "update_check.json"
CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT = 4.0
MAX_RESPONSE_BYTES = 1 << 20
MAX_NOTES_CHARS = 4000

CHECK_ENV = "A_SHARE_UPDATE_CHECK"
INSTALL_METHOD_ENV = "A_SHARE_INSTALL_METHOD"
DOCKER_MODE_ENV = "A_SHARE_DOCKER_MODE"

DISABLED_VALUES = {"0", "off", "false", "no", "disable", "disabled"}

_API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# compose 里的键名探测：只用来选升级提示文案，不做 YAML 解析。
# 仓库与 Release 资产里的 docker-compose.yml 都用 build:，此时
# `docker compose pull` 是无意义的；只有镜像版 compose 才该提示 pull。
_COMPOSE_IMAGE_RE = re.compile(r"^\s+image:\s*(\S+)", re.MULTILINE)
_COMPOSE_BUILD_RE = re.compile(r"^\s+build:\s*(?:\S+)?\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ReleaseCandidate:
    tag: VersionTag
    name: str = ""
    notes: str = ""
    url: str = ""
    published_at: str = ""
    prerelease: bool = False


@dataclass
class UpdateStatus:
    """一次更新检查的完整结果；可直接序列化给前端。"""

    status: str = "unavailable"  # ok | checking | unavailable | disabled
    local: VersionTag | None = None
    latest: VersionTag | None = None
    update_available: bool = False
    channel: str = version_info.STABLE
    release_name: str = ""
    release_notes: str = ""
    release_url: str = ""
    published_at: str = ""
    install_method: str = "source"
    docker_mode: str = ""
    install_hint: str = ""
    checked_at: str = ""
    from_cache: bool = False
    detail: str = ""
    releases_seen: int = 0

    def as_dict(self) -> dict:
        """前端契约。字段名固定，前端渲染依赖它们。"""
        return {
            "status": self.status,
            "update_available": self.update_available,
            "channel": self.channel,
            "channel_label": version_info.CHANNEL_LABELS.get(self.channel, self.channel),
            "local_version": self.local.full if self.local else "",
            "local_label": self.local.label if self.local else "",
            "latest_version": self.latest.full if self.latest else "",
            "latest_label": self.latest.label if self.latest else "",
            "release_name": _clean_text(self.release_name, 200),
            "release_notes": _clean_text(self.release_notes, MAX_NOTES_CHARS),
            "release_url": self.release_url,
            "published_at": _clean_text(self.published_at, 40),
            "install_method": self.install_method,
            "docker_mode": self.docker_mode,
            "install_hint": _clean_text(self.install_hint, 300),
            "image_ref": self.latest.image_ref if self.latest else "",
            "checked_at": _clean_text(self.checked_at, 40),
            "from_cache": self.from_cache,
            "detail": _clean_text(self.detail, 300),
            "releases_seen": self.releases_seen,
            "changelog_url": CHANGELOG_URL,
        }


# --------------------------------------------------------------------------- #
# 文本与 URL 清洗
# --------------------------------------------------------------------------- #

def _clean_text(value: object, limit: int) -> str:
    """去掉控制字符并截断。正文来自上游，任何字段都不能无界地进前端。"""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
    text = text.strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def sanitize_release_url(url: object) -> str:
    """只放行 GitHub 的 https 链接，其余一律丢弃（返回空串）。"""
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate.startswith(ALLOWED_RELEASE_URL_PREFIX):
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        return ""
    return candidate


# --------------------------------------------------------------------------- #
# 上游响应 → 候选版本
# --------------------------------------------------------------------------- #

def release_candidates(payload: object, channel: str) -> list[ReleaseCandidate]:
    """把 Releases 列表筛成「本渠道」的候选版本。

    渠道判定只看 tag 后缀：这是发布方案里唯一定义的渠道标记，比 ``prerelease``
    布尔位更精确（它区分不了 preview 与 docker）。
    """
    if not isinstance(payload, list):
        return []
    out: list[ReleaseCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft"):
            continue
        tag = version_info.parse_version(item.get("tag_name"))
        if tag is None or tag.channel != channel:
            continue
        # 渠道只看 tag 后缀（发布方案里唯一定义的渠道标记）；正式版再额外要求上游
        # 没标 prerelease——宁可漏一次提示，也不要把预发布物当成正式版推给只跟
        # 正式版的安装。preview/docker 渠道本身就是预发布物，这个布尔位不参与判定。
        if channel == version_info.STABLE and item.get("prerelease"):
            continue
        out.append(
            ReleaseCandidate(
                tag=tag,
                name=_clean_text(item.get("name") or tag.full, 200),
                notes=_clean_text(item.get("body"), MAX_NOTES_CHARS),
                url=sanitize_release_url(item.get("html_url")),
                published_at=_clean_text(item.get("published_at"), 40),
                prerelease=bool(item.get("prerelease")),
            )
        )
    return out


def select_latest(
    candidates: list[ReleaseCandidate],
    local: VersionTag,
) -> ReleaseCandidate | None:
    """同渠道候选里取最大的版本；空集返回 ``None``（不是错误）。"""
    same = [c for c in candidates if c.tag.channel == local.channel]
    if not same:
        return None
    return max(same, key=lambda c: c.tag.sort_key())


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #

def fetch_releases(
    timeout: float = DEFAULT_TIMEOUT,
    urlopen=None,
) -> tuple[list | None, str]:
    """取 Releases 列表，返回 ``(payload, detail)``；任何失败都只体现在返回值里。"""
    opener = urlopen or urlrequest.urlopen
    req = urlrequest.Request(RELEASES_API, headers=dict(_API_HEADERS))
    try:
        with opener(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except error.HTTPError as exc:
        if exc.code in (403, 429):
            return None, f"GitHub 接口限流（HTTP {exc.code}），稍后自动重试"
        return None, f"GitHub 接口返回 HTTP {exc.code}"
    except error.URLError as exc:
        return None, f"网络不可达：{exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"网络请求失败：{exc}"
    except Exception as exc:  # noqa: BLE001  兜底：检查更新绝不能让调用方炸
        return None, f"检查更新失败：{exc}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "GitHub 接口返回了无法解析的内容"
    if not isinstance(payload, list):
        return None, "GitHub 接口返回结构异常"
    return payload, ""


# --------------------------------------------------------------------------- #
# 运行方式与升级指令
# --------------------------------------------------------------------------- #

def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def detect_install_method(env: dict | None = None) -> str:
    """``source`` 或 ``docker``。只决定「怎么升级」，与渠道无关。"""
    environ = os.environ if env is None else env
    explicit = (environ.get(INSTALL_METHOD_ENV) or "").strip().lower()
    if explicit in ("source", "docker"):
        return explicit
    return "docker" if in_container() else "source"


def detect_docker_mode(env: dict | None = None, project_root: Path | None = None) -> str:
    """容器安装的形态：``image`` / ``build`` / ``run``。

    仓库自带的 ``docker-compose.yml`` 用的是 ``build:``，这时 ``docker compose
    pull`` 没有意义，必须 ``git pull`` 后重建；只有引用了 ghcr.io 镜像的 compose
    才适用 ``docker compose pull``。判不出来就退回 ``run``（直接 ``docker pull``），
    不猜。
    """
    environ = os.environ if env is None else env
    explicit = (environ.get(DOCKER_MODE_ENV) or "").strip().lower()
    if explicit in ("image", "build", "run"):
        return explicit

    compose = (project_root or version_info.PROJECT_ROOT) / "docker-compose.yml"
    try:
        text = compose.read_text(encoding="utf-8")
    except OSError:
        return "run"
    if any("ghcr.io/" in hit for hit in _COMPOSE_IMAGE_RE.findall(text)):
        return "image"
    if _COMPOSE_BUILD_RE.search(text):
        return "build"
    return "run"


def build_install_hint(
    install_method: str,
    docker_mode: str,
    target: VersionTag | None,
) -> str:
    """按实际安装形态给升级命令。绝不给出与本机安装方式不符的指令。"""
    if install_method == "source":
        return "git pull"
    if docker_mode == "image":
        return "docker compose pull && docker compose up -d"
    if docker_mode == "build":
        return "git pull && docker compose up -d --build"
    if target is not None:
        return f"docker pull {target.image_ref}"
    return "docker pull ghcr.io/luqtest/big-a-chovy:<新版本标签>"


def _apply_install_fields(status: UpdateStatus, env: dict | None = None) -> UpdateStatus:
    """补上「怎么升级」三件套。

    缓存里只存版本与正文，升级指令每次按**当前**安装方式现算：从缓存出结果时
    也必须走这里，否则升级命令会整个缺失（缓存命中是常态，不是例外）。
    """
    status.install_method = detect_install_method(env)
    status.docker_mode = (
        detect_docker_mode(env) if status.install_method == "docker" else ""
    )
    status.install_hint = build_install_hint(
        status.install_method, status.docker_mode, status.latest
    )
    return status


# --------------------------------------------------------------------------- #
# 缓存（跟随 A_SHARE_STATE_DIR，验证跑不会写真实目录）
# --------------------------------------------------------------------------- #

def cache_path() -> Path:
    return runtime_paths.state_file(CACHE_NAME)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _read_cache() -> dict | None:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(payload: dict) -> None:
    """缓存写失败不影响检查结果：拿不到缓存只是下次多查一次。"""
    try:
        path = cache_path()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except (OSError, RuntimeError, ValueError):
        return


def _cache_fresh(cache: dict | None, local: VersionTag, now: datetime) -> bool:
    if not cache:
        return False
    if cache.get("channel") != local.channel or cache.get("local_base") != local.base:
        return False
    stamp = cache.get("checked_at")
    if not isinstance(stamp, str):
        return False
    try:
        checked = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return now - checked < timedelta(seconds=CACHE_TTL_SECONDS)


def _status_from_cache(cache: dict | None, local: VersionTag) -> UpdateStatus | None:
    if not cache:
        return None
    latest = version_info.parse_version(cache.get("latest_version"))
    if latest is not None and latest.channel != local.channel:
        # 缓存里的版本属于别的渠道（例如用户换了渠道）：宁可无视缓存。
        latest = None
    return _apply_install_fields(
        UpdateStatus(
            status=str(cache.get("last_status") or "unavailable"),
            local=local,
            latest=latest,
            update_available=bool(
                latest is not None and version_info.is_newer(latest, local)
            ),
            channel=local.channel,
            release_name=str(cache.get("release_name") or ""),
            release_notes=str(cache.get("release_notes") or ""),
            release_url=sanitize_release_url(cache.get("release_url")),
            published_at=str(cache.get("published_at") or ""),
            checked_at=str(cache.get("checked_at") or ""),
            from_cache=True,
            detail=str(cache.get("detail") or ""),
            releases_seen=int(cache.get("releases_seen") or 0),
        )
    )


def read_cached_status(local: VersionTag | None = None) -> UpdateStatus | None:
    """同步读本地缓存（不联网），供接口首帧直接出内容。"""
    resolved = local or version_info.local_version()
    if resolved is None:
        return None
    return _status_from_cache(_read_cache(), resolved)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def check_disabled(env: dict | None = None) -> bool:
    environ = os.environ if env is None else env
    return (environ.get(CHECK_ENV) or "").strip().lower() in DISABLED_VALUES


def check_for_update(
    local: VersionTag | None = None,
    now: datetime | None = None,
    fetcher=None,
    timeout: float = DEFAULT_TIMEOUT,
    force: bool = False,
    env: dict | None = None,
    project_root: Path | None = None,
) -> UpdateStatus:
    """执行一次检查。返回 UpdateStatus，任何情况下都不抛出。"""
    moment = now or _utc_now()
    install_method = detect_install_method(env)
    docker_mode = detect_docker_mode(env) if install_method == "docker" else ""

    if check_disabled(env):
        return UpdateStatus(
            status="disabled",
            local=local,
            install_method=install_method,
            docker_mode=docker_mode,
            detail="更新检查已关闭（A_SHARE_UPDATE_CHECK=off）",
        )

    resolved = local or version_info.local_version()
    if resolved is None:
        return UpdateStatus(
            status="unavailable",
            install_method=install_method,
            docker_mode=docker_mode,
            checked_at=_iso(moment),
            detail="未找到可解析的 VERSION，无法比较版本",
        )

    cache = _read_cache()
    if not force and _cache_fresh(cache, resolved, moment):
        cached = _status_from_cache(cache, resolved)
        if cached is not None:
            return _apply_install_fields(cached, env)

    payload, detail = (fetcher or fetch_releases)(timeout=timeout)
    if payload is None:
        # 上游不可用：保留上一次已知的好结果，只把失败原因附上，并顺延下次尝试。
        previous = _status_from_cache(cache, resolved)
        carried = UpdateStatus(
            status="unavailable",
            local=resolved,
            latest=previous.latest if previous else None,
            update_available=bool(previous and previous.update_available),
            channel=resolved.channel,
            release_name=previous.release_name if previous else "",
            release_notes=previous.release_notes if previous else "",
            release_url=previous.release_url if previous else "",
            published_at=previous.published_at if previous else "",
            install_method=install_method,
            docker_mode=docker_mode,
            checked_at=_iso(moment),
            from_cache=bool(previous),
            detail=detail,
            releases_seen=previous.releases_seen if previous else 0,
        )
        _apply_install_fields(carried, env)
        _write_cache(
            {
                "checked_at": carried.checked_at,
                "local_base": resolved.base,
                "channel": resolved.channel,
                "last_status": "unavailable",
                "latest_version": carried.latest.full if carried.latest else None,
                "release_name": carried.release_name,
                "release_notes": carried.release_notes,
                "release_url": carried.release_url,
                "published_at": carried.published_at,
                "detail": detail,
                "releases_seen": carried.releases_seen,
            }
        )
        return carried

    candidates = release_candidates(payload, resolved.channel)
    chosen = select_latest(candidates, resolved)
    newer = chosen is not None and version_info.is_newer(chosen.tag, resolved)

    status = UpdateStatus(
        status="ok",
        local=resolved,
        latest=chosen.tag if chosen else None,
        update_available=newer,
        channel=resolved.channel,
        release_name=chosen.name if chosen else "",
        release_notes=chosen.notes if chosen else "",
        release_url=chosen.url if chosen else "",
        published_at=chosen.published_at if chosen else "",
        install_method=install_method,
        docker_mode=docker_mode,
        checked_at=_iso(moment),
        detail="" if candidates else f"{resolved.channel} 渠道暂无 Release 记录",
        releases_seen=len(candidates),
    )
    _apply_install_fields(status, env)
    _write_cache(
        {
            "checked_at": status.checked_at,
            "local_base": resolved.base,
            "channel": resolved.channel,
            "last_status": "ok",
            "latest_version": status.latest.full if status.latest else None,
            "release_name": status.release_name,
            "release_notes": status.release_notes,
            "release_url": status.release_url,
            "published_at": status.published_at,
            "detail": status.detail,
            "releases_seen": status.releases_seen,
        }
    )
    return status


# --------------------------------------------------------------------------- #
# 后台线程 + 接口快照
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_last: UpdateStatus | None = None
_inflight = False


def _run_check(force: bool) -> None:
    global _last, _inflight
    try:
        status = check_for_update(force=force)
    except Exception as exc:  # noqa: BLE001  后台线程不允许把异常打出去
        status = UpdateStatus(
            status="unavailable",
            local=version_info.local_version(),
            checked_at=_iso(_utc_now()),
            detail=f"检查更新失败：{exc}",
        )
    with _lock:
        _last = status
        _inflight = False


def start_background_check(force: bool = False) -> bool:
    """起一个后台检查线程；已有线程在跑时返回 ``False``。"""
    global _inflight
    with _lock:
        if _inflight:
            return False
        _inflight = True
    thread = threading.Thread(
        target=_run_check, args=(force,), name="update-check", daemon=True
    )
    thread.start()
    return True


def checking() -> bool:
    with _lock:
        return _inflight


def _needs_refresh(current: UpdateStatus | None, force: bool) -> bool:
    """要不要真的去查一次：缓存新鲜时不起线程，避免每次轮询都空转并让
    ``checking`` 标志无谓闪烁。"""
    if force:
        return True
    if current is None:
        return True
    resolved = version_info.local_version()
    if resolved is None:
        return True
    return not _cache_fresh(_read_cache(), resolved, _utc_now())


def snapshot(force: bool = False) -> dict:
    """接口用的即时快照：只读缓存与内存状态，永不阻塞在网络上。"""
    global _last
    if check_disabled():
        return UpdateStatus(status="disabled").as_dict()

    with _lock:
        current = _last
    if current is None:
        # 进程刚起来时先把磁盘缓存拿出来，别让用户对着一条空白条等后台线程。
        current = read_cached_status()
        if current is not None:
            with _lock:
                _last = current

    if _needs_refresh(current, force):
        start_background_check(force=force)

    if current is None:
        # 首帧就把本机版本给出去：用户先看到「本机 v0.6.1 · 检查中…」，
        # 而不是一条空白占位。
        payload = _apply_install_fields(
            UpdateStatus(
                status="checking",
                local=version_info.local_version(),
                detail="正在检查更新…",
            )
        ).as_dict()
        payload["checking"] = True
        return payload

    payload = current.as_dict()
    payload["checking"] = checking()
    return payload


def reset_runtime_state() -> None:
    """清空内存快照与飞行标记（测试用；不触碰磁盘缓存）。"""
    global _last, _inflight
    with _lock:
        _last = None
        _inflight = False
