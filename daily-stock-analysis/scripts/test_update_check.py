#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新检查的回归测试（不联网、不启动服务、不写真实运行状态）。

这里锁住四类容易回退的性质：

1. **渠道隔离**：stable 安装不会被告知 preview/docker 的版本，反之亦然；
   ``0.6.0-docker.2`` 与 ``0.6.0-preview.2`` 是同一源码提交的两种打包形态，
   跨渠道比大小会推出错误的升级建议。
2. **升级指令与实际安装方式一致**：仓库自带的 compose 用 ``build:``，此时
   ``docker compose pull`` 是无意义的，必须 ``git pull`` 后重建。
3. **静默失败**：离线、限流、返回体畸形都只体现在 ``status``/``detail`` 里，
   不抛出、不影响软件使用，并保留上一次已知的好结果。
4. **状态隔离**：缓存跟随 ``A_SHARE_STATE_DIR``，测试与验证跑绝不写真实目录。
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib import error

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import runtime_paths  # noqa: E402
import update_check  # noqa: E402
import version_info  # noqa: E402
from tools.validate_version import check_version_consistency  # noqa: E402

SHARED_JS = (SCRIPT_DIR / "shared_static" / "common.js").read_text(encoding="utf-8")
SHARED_CSS = (SCRIPT_DIR / "shared_static" / "common.css").read_text(encoding="utf-8")
DASHBOARD_PY = (SCRIPT_DIR / "realtime_dashboard.py").read_text(encoding="utf-8")
REALTIME_HTML = (SCRIPT_DIR / "realtime_static" / "index.html").read_text(encoding="utf-8")
WORKBENCH_HTML = (SCRIPT_DIR / "workbench_static" / "index.html").read_text(encoding="utf-8")

T0 = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)


def _release(tag, *, prerelease=False, body="", name=None, url=None, draft=False):
    """构造一条与 GitHub Releases 列表同形的记录。"""
    return {
        "tag_name": tag,
        "name": name if name is not None else tag,
        "body": body,
        "html_url": (
            url
            if url is not None
            else f"https://github.com/LuQTest/Big-A-chovy/releases/tag/{tag}"
        ),
        "published_at": "2026-10-08T05:45:58Z",
        "prerelease": prerelease,
        "draft": draft,
    }


SAMPLE_RELEASES = [
    _release("v0.6.1", name="v0.6.1 — 正式版", body="- 补丁"),
    _release("v0.6.2", name="v0.6.2 — 正式版", body="- 新正式版"),
    _release("v0.6.2-preview.1", prerelease=True, body="- 预览"),
    _release("v0.6.2-docker.1", prerelease=True, body="- 容器"),
]


def _payload_fetcher(payload, calls=None):
    def fetch(timeout=None):
        if calls is not None:
            calls.append(timeout)
        return payload, ""

    return fetch


def _failing_fetcher(detail="网络不可达：测试替身", calls=None):
    def fetch(timeout=None):
        if calls is not None:
            calls.append(timeout)
        return None, detail

    return fetch


def _snapshot_as(local_version="0.6.1", force=False):
    """让 snapshot 使用与缓存种子相同的本地版本，避免依赖仓库 VERSION。"""
    with mock.patch.dict(update_check.os.environ, {"A_SHARE_VERSION": local_version}):
        return update_check.snapshot(force=force)


class _IsolatedStateTestCase(unittest.TestCase):
    """把运行状态目录定向到临时目录：本项目绝不允许测试写真实状态。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        patcher = mock.patch.object(runtime_paths, "STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        update_check.reset_runtime_state()
        self.addCleanup(update_check.reset_runtime_state)


class VersionParsingTests(unittest.TestCase):
    def test_stable_tag_with_and_without_v_prefix(self):
        for raw in ("v0.6.1", "0.6.1"):
            with self.subTest(raw=raw):
                tag = version_info.parse_version(raw)
                self.assertEqual(tag.base, "0.6.1")
                self.assertEqual(tag.full, "0.6.1")
                self.assertEqual(tag.channel, version_info.STABLE)
                self.assertIsNone(tag.index)

    def test_preview_and_docker_channels_keep_their_index(self):
        preview = version_info.parse_version("v0.6.0-preview.2")
        self.assertEqual(preview.channel, version_info.PREVIEW)
        self.assertEqual(preview.full, "0.6.0-preview.2")
        docker = version_info.parse_version("v0.6.0-docker.1")
        self.assertEqual(docker.channel, version_info.DOCKER)
        self.assertEqual(docker.full, "0.6.0-docker.1")

    def test_unknown_channel_is_not_guessed_as_stable(self):
        """认不出的渠道宁可「无法比较」，也不能猜成正式版。"""
        self.assertIsNone(version_info.parse_version("v0.7.0-rc.1"))
        self.assertIsNone(version_info.parse_version("main"))
        self.assertIsNone(version_info.parse_version(""))
        self.assertIsNone(version_info.parse_version(None))

    def test_image_ref_matches_release_manifest_style(self):
        self.assertEqual(
            version_info.parse_version("v0.6.1").image_ref,
            "ghcr.io/luqtest/big-a-chovy:v0.6.1",
        )

    def test_local_version_prefers_baked_env_over_file(self):
        baked = version_info.local_version(env={"A_SHARE_VERSION": "0.6.0-docker.2"})
        self.assertEqual(baked.channel, version_info.DOCKER)
        self.assertEqual(baked.full, "0.6.0-docker.2")

    def test_local_version_channel_env_selects_channel(self):
        tag = version_info.local_version(env={"A_SHARE_CHANNEL": "preview"})
        self.assertEqual(tag.channel, version_info.PREVIEW)
        self.assertEqual(tag.base, version_info.read_base_version())

    def test_local_version_bad_channel_env_falls_back_to_stable(self):
        tag = version_info.local_version(env={"A_SHARE_CHANNEL": "nightly"})
        self.assertEqual(tag.channel, version_info.STABLE)

    def test_local_version_from_missing_file_is_none(self):
        missing = Path(tempfile.gettempdir()) / "definitely-not-a-version-file"
        self.assertIsNone(version_info.local_version(version_file=missing, env={}))


class ChannelIsolationTests(unittest.TestCase):
    def test_is_newer_requires_same_channel(self):
        stable_new = version_info.parse_version("v0.6.2")
        stable_old = version_info.parse_version("v0.6.1")
        self.assertTrue(version_info.is_newer(stable_new, stable_old))
        self.assertFalse(version_info.is_newer(stable_old, stable_new))

    def test_cross_channel_is_never_newer(self):
        """这是本次实现的核心约定：跨渠道一律不比大小。"""
        docker = version_info.parse_version("v0.9.0-docker.1")
        preview = version_info.parse_version("v0.9.0-preview.1")
        stable = version_info.parse_version("v0.6.1")
        for candidate, local in (
            (docker, stable),
            (preview, stable),
            (docker, preview),
            (preview, docker),
            (stable, docker),
        ):
            with self.subTest(candidate=candidate.full, local=local.full):
                self.assertFalse(version_info.is_newer(candidate, local))

    def test_candidates_are_filtered_by_channel(self):
        stable = update_check.release_candidates(SAMPLE_RELEASES, version_info.STABLE)
        self.assertEqual([c.tag.full for c in stable], ["0.6.1", "0.6.2"])
        preview = update_check.release_candidates(SAMPLE_RELEASES, version_info.PREVIEW)
        self.assertEqual([c.tag.full for c in preview], ["0.6.2-preview.1"])
        docker = update_check.release_candidates(SAMPLE_RELEASES, version_info.DOCKER)
        self.assertEqual([c.tag.full for c in docker], ["0.6.2-docker.1"])

    def test_stable_channel_skips_releases_flagged_prerelease(self):
        payload = [_release("v0.6.2", prerelease=True)]
        self.assertEqual(update_check.release_candidates(payload, version_info.STABLE), [])
        # preview/docker 渠道本身就是预发布物，该布尔位不参与判定。
        payload = [_release("v0.6.2-preview.1", prerelease=True)]
        self.assertEqual(
            len(update_check.release_candidates(payload, version_info.PREVIEW)), 1
        )

    def test_drafts_are_ignored(self):
        payload = [_release("v0.6.2", draft=True)]
        self.assertEqual(update_check.release_candidates(payload, version_info.STABLE), [])

    def test_select_latest_picks_max_within_channel(self):
        local = version_info.parse_version("0.6.1")
        candidates = update_check.release_candidates(SAMPLE_RELEASES, version_info.STABLE)
        self.assertEqual(update_check.select_latest(candidates, local).tag.full, "0.6.2")

    def test_select_latest_returns_none_for_empty_channel(self):
        local = version_info.parse_version("0.6.0-docker.9")
        self.assertIsNone(update_check.select_latest([], local))

    def test_malformed_entries_do_not_break_filtering(self):
        payload = ["not-a-dict", {"tag_name": None}, {"tag_name": "v0.6.2"}, _release("v0.6.3")]
        candidates = update_check.release_candidates(payload, version_info.STABLE)
        self.assertEqual([c.tag.full for c in candidates], ["0.6.2", "0.6.3"])


class SanitizationTests(unittest.TestCase):
    def test_release_url_only_allows_https_github(self):
        self.assertEqual(
            update_check.sanitize_release_url("https://github.com/a/b"),
            "https://github.com/a/b",
        )
        for bad in (
            "javascript:alert(1)",
            "http://github.com/a/b",
            "data:text/html,<script>",
            "https://evil.example.com/a",
            "https://github.com/a/b onmouseover=x",
            "",
            None,
            123,
        ):
            with self.subTest(bad=bad):
                self.assertEqual(update_check.sanitize_release_url(bad), "")

    def test_release_url_with_control_characters_is_rejected(self):
        self.assertEqual(
            update_check.sanitize_release_url("https://github.com/a/b\n<script>"), ""
        )

    def test_notes_are_truncated_and_stripped_of_control_chars(self):
        dirty = "a" + chr(7) + "b\n" + "x" * 9000
        cleaned = update_check._clean_text(dirty, 100)
        self.assertNotIn(chr(7), cleaned)
        self.assertLessEqual(len(cleaned), 100)
        self.assertTrue(cleaned.endswith("…"))

    def test_release_notes_are_escaped_before_rendering(self):
        """Release 正文是第三方文本，必须转义后按纯文本渲染。

        只断言前端确实用了 esc()，并断言没有把 notes 直接拼进 innerHTML——
        这里不做 Markdown→HTML，也就不存在需要过滤的标记注入面。
        """
        self.assertIn("esc(notes)", SHARED_JS)
        self.assertNotIn("+ notes +", SHARED_JS)
        self.assertIn("releaseNotes: releaseNotes", SHARED_JS)

    def test_frontend_rechecks_the_release_url(self):
        self.assertIn("/^https:\\/\\/github\\.com\\//", SHARED_JS)
        self.assertIn("safeReleaseUrl", SHARED_JS)


class SilentFailureTests(_IsolatedStateTestCase):
    def _check(self, fetcher, **kwargs):
        return update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=fetcher,
            env={},
            **kwargs,
        )

    def test_fetch_failure_is_reported_not_raised(self):
        status = self._check(_failing_fetcher("网络不可达：测试替身"))
        self.assertEqual(status.status, "unavailable")
        self.assertFalse(status.update_available)
        self.assertIn("网络不可达", status.detail)

    def test_missing_version_file_is_reported(self):
        with mock.patch.object(version_info, "local_version", return_value=None):
            status = update_check.check_for_update(now=T0, fetcher=_failing_fetcher(), env={})
        self.assertEqual(status.status, "unavailable")
        self.assertIn("VERSION", status.detail)

    def test_http_error_statuses_become_human_readable_details(self):
        for code in (403, 429, 500, 503):
            with self.subTest(code=code):
                def opener(request, timeout=None, code=code):
                    failure = error.HTTPError(request.full_url, code, "boom", {}, None)
                    failure.close()  # 显式关闭，避免 GC 时留下 ResourceWarning 噪音
                    raise failure

                payload, detail = update_check.fetch_releases(urlopen=opener)
                self.assertIsNone(payload)
                self.assertIn(str(code), detail)
                if code in (403, 429):
                    self.assertIn("限流", detail)

    def test_url_error_is_swallowed(self):
        def opener(request, timeout=None):
            raise error.URLError("no route to host")

        payload, detail = update_check.fetch_releases(urlopen=opener)
        self.assertIsNone(payload)
        self.assertIn("网络不可达", detail)

    def test_malformed_json_and_wrong_shape_are_swallowed(self):
        class _Response:
            def __init__(self, raw):
                self._raw = raw

            def read(self, size=None):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        for raw, expect in ((b"{not json", "无法解析"), (b'{"a":1}', "结构异常")):
            with self.subTest(raw=raw):
                payload, detail = update_check.fetch_releases(
                    urlopen=lambda request, timeout=None, raw=raw: _Response(raw)
                )
                self.assertIsNone(payload)
                self.assertIn(expect, detail)

    def test_successful_check_reports_release_details(self):
        status = self._check(_payload_fetcher(SAMPLE_RELEASES))
        self.assertEqual(status.status, "ok")
        self.assertTrue(status.update_available)
        self.assertEqual(status.latest.full, "0.6.2")
        self.assertEqual(status.release_url, "https://github.com/LuQTest/Big-A-chovy/releases/tag/v0.6.2")
        self.assertEqual(status.releases_seen, 2)

    def test_local_newer_or_equal_reports_no_update(self):
        status = update_check.check_for_update(
            local=version_info.parse_version("0.6.9"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        self.assertEqual(status.status, "ok")
        self.assertFalse(status.update_available)
        self.assertEqual(status.latest.full, "0.6.2")

    def test_preview_install_only_compares_against_preview(self):
        status = update_check.check_for_update(
            local=version_info.parse_version("0.6.0-preview.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        self.assertEqual(status.latest.full, "0.6.2-preview.1")
        self.assertEqual(status.channel, version_info.PREVIEW)

    def test_channel_without_any_release_is_reported_honestly(self):
        """docker 渠道目前没有 Release 记录：要如实说，而不是谎报「已是最新」。"""
        stable_only = [
            _release("v0.6.1"),
            _release("v0.6.2"),
            _release("v0.6.2-preview.1", prerelease=True),
        ]
        status = update_check.check_for_update(
            local=version_info.parse_version("0.6.0-docker.1"),
            now=T0,
            fetcher=_payload_fetcher(stable_only),
            env={},
        )
        self.assertEqual(status.status, "ok")
        self.assertFalse(status.update_available)
        self.assertIsNone(status.latest)
        self.assertEqual(status.releases_seen, 0)
        self.assertIn("docker 渠道暂无 Release", status.detail)

    def test_timeout_is_passed_to_the_fetcher(self):
        calls = []
        self._check(_payload_fetcher(SAMPLE_RELEASES, calls), timeout=1.5)
        self.assertEqual(calls, [1.5])


class CacheTests(_IsolatedStateTestCase):
    def _check(self, fetcher, now=T0, force=False):
        return update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=now,
            fetcher=fetcher,
            force=force,
            env={},
        )

    def test_fresh_cache_skips_the_network_entirely(self):
        first = self._check(_payload_fetcher(SAMPLE_RELEASES))
        self.assertFalse(first.from_cache)
        calls = []
        second = self._check(_payload_fetcher(SAMPLE_RELEASES, calls), now=T0 + timedelta(hours=1))
        self.assertEqual(calls, [])
        self.assertTrue(second.from_cache)
        self.assertTrue(second.update_available)

    def test_stale_cache_triggers_a_new_check(self):
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        calls = []
        self._check(
            _payload_fetcher(SAMPLE_RELEASES, calls), now=T0 + timedelta(seconds=update_check.CACHE_TTL_SECONDS + 1)
        )
        self.assertEqual(len(calls), 1)

    def test_force_bypasses_a_fresh_cache(self):
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        calls = []
        self._check(_payload_fetcher(SAMPLE_RELEASES, calls), now=T0 + timedelta(minutes=1), force=True)
        self.assertEqual(len(calls), 1)

    def test_cache_from_another_channel_is_ignored(self):
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        calls = []
        update_check.check_for_update(
            local=version_info.parse_version("0.6.0-preview.1"),
            now=T0 + timedelta(minutes=1),
            fetcher=_payload_fetcher(SAMPLE_RELEASES, calls),
            env={},
        )
        self.assertEqual(len(calls), 1)

    def test_cache_from_another_local_version_is_ignored(self):
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        calls = []
        update_check.check_for_update(
            local=version_info.parse_version("0.5.0"),
            now=T0 + timedelta(minutes=1),
            fetcher=_payload_fetcher(SAMPLE_RELEASES, calls),
            env={},
        )
        self.assertEqual(len(calls), 1)

    def test_failure_keeps_the_last_known_release_information(self):
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        status = self._check(
            _failing_fetcher("网络不可达：测试替身"),
            now=T0 + timedelta(seconds=update_check.CACHE_TTL_SECONDS + 1),
        )
        self.assertEqual(status.status, "unavailable")
        self.assertTrue(status.update_available)
        self.assertEqual(status.latest.full, "0.6.2")
        self.assertTrue(status.from_cache)
        self.assertIn("网络不可达", status.detail)

    def test_hint_is_present_when_the_result_comes_from_cache(self):
        """缓存命中是常态而不是例外：从缓存出结果时升级命令不能缺。"""
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        status = self._check(_payload_fetcher(SAMPLE_RELEASES), now=T0 + timedelta(hours=2))
        self.assertTrue(status.from_cache)
        self.assertTrue(status.update_available)
        self.assertEqual(status.install_hint, "git pull")

    def test_snapshot_read_from_disk_cache_keeps_the_install_hint(self):
        """进程刚起来时首帧走磁盘缓存，这一帧同样要有升级命令。"""
        self._check(_payload_fetcher(SAMPLE_RELEASES))
        update_check.reset_runtime_state()
        with mock.patch.object(
            update_check, "_utc_now", return_value=T0 + timedelta(hours=2)
        ):
            payload = _snapshot_as()
        self.assertTrue(payload["update_available"])
        self.assertTrue(payload["install_hint"])

    def test_corrupt_cache_is_ignored_without_raising(self):
        update_check.cache_path().write_text("{not json", encoding="utf-8")
        calls = []
        status = self._check(_payload_fetcher(SAMPLE_RELEASES, calls))
        self.assertEqual(len(calls), 1)
        self.assertEqual(status.status, "ok")

    def test_unwritable_cache_directory_does_not_break_the_check(self):
        with mock.patch.object(
            runtime_paths, "STATE_DIR", self.state_dir / "file-not-a-dir"
        ):
            (self.state_dir / "file-not-a-dir").write_text("x", encoding="utf-8")
            status = self._check(_payload_fetcher(SAMPLE_RELEASES))
        self.assertEqual(status.status, "ok")
        self.assertTrue(status.update_available)


class StateIsolationTests(_IsolatedStateTestCase):
    def test_cache_lives_in_the_redirected_state_dir(self):
        update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        self.assertTrue((self.state_dir / update_check.CACHE_NAME).is_file())

    def test_cache_is_never_written_next_to_the_scripts(self):
        """回归红线：真实脚本目录里不该出现更新检查缓存。"""
        update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        self.assertFalse((SCRIPT_DIR / update_check.CACHE_NAME).exists())


class InstallHintTests(_IsolatedStateTestCase):
    """升级指令必须与实际安装方式一致（本项目当前用 build 版 compose）。"""

    def test_source_install_pulls_the_repository(self):
        self.assertEqual(update_check.build_install_hint("source", "", None), "git pull")

    def test_image_compose_is_the_only_mode_that_uses_compose_pull(self):
        hint = update_check.build_install_hint(
            "docker", "image", version_info.parse_version("0.6.2")
        )
        self.assertEqual(hint, "docker compose pull && docker compose up -d")

    def test_build_compose_must_not_use_compose_pull(self):
        hint = update_check.build_install_hint(
            "docker", "build", version_info.parse_version("0.6.2")
        )
        self.assertNotIn("docker compose pull", hint)
        self.assertIn("git pull", hint)
        self.assertIn("up -d --build", hint)

    def test_plain_docker_run_pulls_the_image_tag(self):
        hint = update_check.build_install_hint(
            "docker", "run", version_info.parse_version("0.6.2")
        )
        self.assertEqual(hint, "docker pull ghcr.io/luqtest/big-a-chovy:v0.6.2")

    def test_shipped_compose_is_detected_as_build_mode(self):
        """仓库自带的 docker-compose.yml 用 build:，因此不能提示 compose pull。"""
        self.assertEqual(update_check.detect_docker_mode(env={}), "build")

    def test_image_compose_is_detected_as_image_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                "services:\n  dashboard:\n    image: ghcr.io/luqtest/big-a-chovy:v0.6.2\n",
                encoding="utf-8",
            )
            self.assertEqual(
                update_check.detect_docker_mode(env={}, project_root=root), "image"
            )

    def test_missing_compose_falls_back_to_plain_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                update_check.detect_docker_mode(env={}, project_root=Path(tmp)), "run"
            )

    def test_explicit_overrides_win(self):
        self.assertEqual(
            update_check.detect_docker_mode(env={"A_SHARE_DOCKER_MODE": "image"}), "image"
        )
        self.assertEqual(
            update_check.detect_install_method(env={"A_SHARE_INSTALL_METHOD": "docker"}),
            "docker",
        )

    def test_container_detection_selects_docker_method(self):
        with mock.patch.object(update_check, "in_container", return_value=True):
            self.assertEqual(update_check.detect_install_method(env={}), "docker")
        with mock.patch.object(update_check, "in_container", return_value=False):
            self.assertEqual(update_check.detect_install_method(env={}), "source")

    def test_check_result_carries_the_matching_hint(self):
        status = update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={"A_SHARE_INSTALL_METHOD": "docker", "A_SHARE_DOCKER_MODE": "build"},
        )
        self.assertEqual(status.install_method, "docker")
        self.assertNotIn("docker compose pull", status.install_hint)
        self.assertIn("up -d --build", status.install_hint)


class SnapshotApiTests(_IsolatedStateTestCase):
    def test_check_can_be_disabled(self):
        with mock.patch.dict(update_check.os.environ, {"A_SHARE_UPDATE_CHECK": "off"}):
            payload = update_check.snapshot()
        self.assertEqual(payload["status"], "disabled")
        self.assertFalse(payload["update_available"])

    def test_first_snapshot_reports_local_version_while_checking(self):
        with mock.patch.object(update_check, "start_background_check", return_value=True):
            payload = update_check.snapshot()
        self.assertEqual(payload["status"], "checking")
        self.assertTrue(payload["checking"])
        self.assertTrue(payload["local_version"])

    def test_snapshot_serves_the_cache_without_touching_the_network(self):
        update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        with mock.patch.object(update_check, "fetch_releases") as fetch:
            payload = _snapshot_as()
        fetch.assert_not_called()
        self.assertTrue(payload["update_available"])
        self.assertEqual(payload["latest_version"], "0.6.2")

    def test_payload_contract_has_the_fields_the_frontend_reads(self):
        update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        payload = _snapshot_as()
        for key in (
            "status",
            "update_available",
            "local_version",
            "channel_label",
            "latest_version",
            "release_notes",
            "release_url",
            "install_hint",
            "image_ref",
            "detail",
            "changelog_url",
        ):
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_fresh_snapshot_neither_fetches_nor_spawns_a_thread(self):
        """缓存新鲜时轮询必须彻底空转：不起线程、不联网，checking 也不该闪烁。"""
        update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        with mock.patch.object(
            update_check, "_utc_now", return_value=T0 + timedelta(hours=1)
        ), mock.patch.object(update_check, "start_background_check") as starter, mock.patch.object(
            update_check, "fetch_releases"
        ) as fetch:
            payload = _snapshot_as()
        starter.assert_not_called()
        fetch.assert_not_called()
        self.assertFalse(payload["checking"])
        self.assertTrue(payload["update_available"])

    def test_stale_snapshot_spawns_a_background_check(self):
        update_check.check_for_update(
            local=version_info.parse_version("0.6.1"),
            now=T0,
            fetcher=_payload_fetcher(SAMPLE_RELEASES),
            env={},
        )
        with mock.patch.object(
            update_check,
            "_utc_now",
            return_value=T0 + timedelta(seconds=update_check.CACHE_TTL_SECONDS + 5),
        ), mock.patch.object(update_check, "start_background_check") as starter:
            _snapshot_as()
        starter.assert_called_once_with(force=False)

    def test_background_check_collapses_concurrent_calls(self):
        update_check.start_background_check(force=True)
        with update_check._lock:
            update_check._inflight = True
        try:
            self.assertFalse(update_check.start_background_check())
        finally:
            with update_check._lock:
                update_check._inflight = False


class VersionConsistencyTests(unittest.TestCase):
    """VERSION / CHANGELOG / 发布 tag 三者对账，基础版本与渠道分别校验。"""

    def _fixture(self, version_text: str, changelog_text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "VERSION").write_text(version_text, encoding="utf-8")
        (root / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
        return root

    def _messages(self, result, level):
        return " | ".join(issue["message"] for issue in result[level])

    def test_working_copy_is_consistent(self):
        """工作副本自身必须一致：这条守住 VERSION 与 CHANGELOG 不再漂移。"""
        result = check_version_consistency(PROJECT_ROOT)
        self.assertEqual(result["fail"], [], self._messages(result, "fail"))
        self.assertIn(version_info.read_base_version(PROJECT_ROOT / "VERSION"), self._messages(result, "pass"))

    def test_result_shape_is_mergeable_into_workspace_audit(self):
        result = check_version_consistency(PROJECT_ROOT)
        self.assertEqual(set(result), {"pass", "warn", "fail"})
        for level in result:
            for issue in result[level]:
                self.assertIn("level", issue)
                self.assertIn("message", issue)

    def test_release_tag_with_matching_base_passes(self):
        base = version_info.read_base_version(PROJECT_ROOT / "VERSION")
        result = check_version_consistency(PROJECT_ROOT, tag=f"v{base}")
        self.assertEqual(result["fail"], [], self._messages(result, "fail"))
        self.assertIn("stable", self._messages(result, "pass"))

    def test_base_version_and_channel_are_validated_separately(self):
        """基础版本不符要单独报，不能和渠道判定混成一句。"""
        result = check_version_consistency(PROJECT_ROOT, tag="v0.0.1-preview.3")
        self.assertIn("preview", self._messages(result, "pass"))
        self.assertIn("基础版本", self._messages(result, "fail"))
        self.assertIn("0.0.1", self._messages(result, "fail"))

    def test_unknown_channel_tag_fails(self):
        result = check_version_consistency(PROJECT_ROOT, tag="v0.6.1-rc.1")
        self.assertNotEqual(result["fail"], [])
        self.assertIn("rc.1", self._messages(result, "fail"))

    def test_docker_channel_tag_is_recognised(self):
        base = version_info.read_base_version(PROJECT_ROOT / "VERSION")
        result = check_version_consistency(PROJECT_ROOT, tag=f"v{base}-docker.2")
        self.assertEqual(result["fail"], [], self._messages(result, "fail"))
        self.assertIn("docker", self._messages(result, "pass"))

    def test_changelog_drift_is_reported(self):
        root = self._fixture("0.6.1\n", "# 更新日志\n\n## v0.6.0 — 某次发布\n")
        result = check_version_consistency(root)
        self.assertIn("不一致", self._messages(result, "fail"))

    def test_version_file_must_be_a_bare_base_version(self):
        root = self._fixture("0.6.1-preview.1\n", "# 更新日志\n\n## v0.6.1-preview.1 — x\n")
        result = check_version_consistency(root)
        self.assertIn("纯基础版本", self._messages(result, "fail"))

    def test_missing_version_file_is_reported(self):
        root = self._fixture("0.6.1\n", "# 更新日志\n\n## v0.6.1 — x\n")
        (root / "VERSION").unlink()
        result = check_version_consistency(root)
        self.assertIn("缺少", self._messages(result, "fail"))

    def test_missing_changelog_entry_is_reported(self):
        root = self._fixture("0.6.1\n", "# 更新日志\n\n尚无版本条目\n")
        result = check_version_consistency(root)
        self.assertIn("CHANGELOG.md 未找到", self._messages(result, "fail"))


class UpdateEndpointFlagTests(unittest.TestCase):
    """手动「检查更新」靠 ?force=1 跳过缓存；解析写错会静默退化成普通轮询。"""

    def test_force_flag_accepts_common_truthy_spellings(self):
        import realtime_dashboard as dash

        for query in ("force=1", "force=true", "force=YES", "force=on", "x=1&force=1"):
            with self.subTest(query=query):
                self.assertTrue(dash._query_flag(query, "force"))

    def test_force_flag_defaults_to_false(self):
        import realtime_dashboard as dash

        for query in ("", "force=0", "force=no", "force=", "other=1"):
            with self.subTest(query=query):
                self.assertFalse(dash._query_flag(query, "force"))


class FrontendWiringTests(unittest.TestCase):
    def test_dashboard_serves_the_update_endpoint(self):
        self.assertIn('path == "/api/update"', DASHBOARD_PY)
        self.assertIn("update_check.snapshot", DASHBOARD_PY)

    def test_workbench_inherits_the_dashboard_routes(self):
        workbench = (SCRIPT_DIR / "web_workbench.py").read_text(encoding="utf-8")
        self.assertIn("class WorkbenchHandler(dash.DashboardHandler)", workbench)
        self.assertNotIn('"/api/update"', workbench)

    def test_both_pages_have_the_update_slot(self):
        for name, html in (("realtime", REALTIME_HTML), ("workbench", WORKBENCH_HTML)):
            with self.subTest(page=name):
                self.assertIn('id="update-notice"', html)

    def test_shared_layer_owns_the_rendering_and_self_mounts(self):
        self.assertIn("renderUpdate: renderUpdate", SHARED_JS)
        self.assertIn("mountUpdateNotice: mountUpdateNotice", SHARED_JS)
        self.assertIn("mountUpdateNotice();", SHARED_JS)
        # 两个入口不该各写一份版本条逻辑。
        for name in ("realtime_static/app.js", "workbench_static/app.js"):
            with self.subTest(page=name):
                page_js = (SCRIPT_DIR / name).read_text(encoding="utf-8")
                self.assertNotIn("update-notice", page_js)

    def test_update_styles_define_their_own_hidden_state(self):
        self.assertIn(".update-notice.hidden", SHARED_CSS)
        self.assertIn(".un-panel.hidden", SHARED_CSS)

    def test_empty_action_notice_is_hidden_on_both_pages(self):
        """看板页没有全局 .hidden 规则，共用层必须自己带上这条。"""
        self.assertIn(".ss-notice.hidden", SHARED_CSS)

    def test_hint_is_rendered_as_text_not_markup(self):
        self.assertIn("esc(st.install_hint)", SHARED_JS)
        self.assertIn("esc(st.image_ref)", SHARED_JS)


if __name__ == "__main__":
    unittest.main()
