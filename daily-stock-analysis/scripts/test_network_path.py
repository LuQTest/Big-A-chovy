"""network_path 单元测试。

覆盖：候选枚举、端点验活、多端点降级、排序、慢路径剔除、切换粘性、
缓存 TTL/负缓存、invalidate、熔断冷却、session 兜底、配置读取。
全部用 mock 打桩，不依赖真实网络。
"""

import os
import time
import unittest
from unittest.mock import patch

import network_path as np


def _path(label, proxy, latency, score=None, degraded=None):
    return {
        "label": label,
        "proxy": proxy,
        "latency_ms": latency,
        "score": score if score is not None else latency,
        "degraded": degraded or [],
        "endpoints": {},
    }


class NetworkPathTests(unittest.TestCase):
    def setUp(self):
        # 每个用例前重置模块级缓存与状态，避免相互污染
        np.invalidate()
        np._current_label = None
        np._last_switch_reason = ""
        np._fail_streak.clear()
        np._cooldown_until.clear()

    # ---------- 候选枚举 ----------
    def test_candidate_paths_always_includes_direct_first(self):
        with patch.object(np, "LOCAL_PROXY_PORTS", (7890,)), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(np, "_scutil_proxy_url", return_value=""):
            paths = np.candidate_paths()
        self.assertEqual(paths[0], ("直连", None))
        self.assertIn(("代理127.0.0.1:7890", "http://127.0.0.1:7890"), paths)

    def test_candidate_paths_dedups_and_rejects_malformed(self):
        with patch.object(np, "LOCAL_PROXY_PORTS", (7890, 7890)), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}), \
             patch.object(np, "_scutil_proxy_url", return_value="not-a-url"):
            paths = np.candidate_paths()
        urls = [u for _, u in paths if u]
        self.assertEqual(len(urls), len(set(urls)), f"候选未去重: {urls}")
        # "not-a-url" 无端口，必须被丢弃
        self.assertTrue(all(":7890" in u for u in urls), urls)

    def test_local_ports_take_priority_over_env_proxy(self):
        """本机端口应排在环境变量代理之前（避免 IDE 沙盒代理抢先）。"""
        with patch.object(np, "LOCAL_PROXY_PORTS", (7890,)), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:50341"}), \
             patch.object(np, "_scutil_proxy_url", return_value=""):
            paths = np.candidate_paths()
        self.assertEqual(paths[1], ("代理127.0.0.1:7890", "http://127.0.0.1:7890"))
        self.assertEqual(paths[2], ("代理127.0.0.1:50341", "http://127.0.0.1:50341"))

    # ---------- 验活 ----------
    def test_is_em_json_accepts_eastmoney_shapes(self):
        self.assertTrue(np._is_em_json({"rc": 0, "data": {}}))
        self.assertTrue(np._is_em_json({"data": {"total": 1}}))
        self.assertFalse(np._is_em_json({"error": "x"}))   # 本地端口误答 200
        self.assertFalse(np._is_em_json([1, 2, 3]))
        self.assertFalse(np._is_em_json(None))

    def test_probe_one_requires_primary_endpoint(self):
        primary = np.PROBE_ENDPOINTS[0][0]
        with patch.object(np, "_probe_all",
                          return_value={name: (None if name == primary else 10.0)
                                        for name, _, _ in np.PROBE_ENDPOINTS}):
            self.assertIsNone(np._probe_one("直连", None))

    def test_probe_one_penalizes_degraded_endpoints(self):
        """辅助端点不通时路径仍可用，但要吃排序惩罚并被标记降级。"""
        names = [n for n, _, _ in np.PROBE_ENDPOINTS]
        raw = {names[0]: 100.0, names[1]: None, names[2]: 120.0}
        with patch.object(np, "_probe_all", return_value=raw):
            got = np._probe_one("直连", None)
        self.assertIsNotNone(got)
        self.assertEqual(got["degraded"], [names[1]])
        self.assertGreater(got["score"], max(100.0, 120.0))  # 惩罚已计入

    # ---------- 排序与剔除 ----------
    def test_probe_paths_sorts_by_score(self):
        with patch.object(np, "candidate_paths",
                          return_value=[("直连", None), ("代理A", "http://127.0.0.1:1")]), \
             patch.object(np, "_probe_one", side_effect=[
                 _path("直连", None, 300.0), _path("代理A", "http://127.0.0.1:1", 100.0)]):
            paths = np.probe_paths()
        self.assertEqual([p["label"] for p in paths], ["代理A", "直连"])

    def test_probe_paths_drops_slow_when_alternative_exists(self):
        with patch.object(np, "candidate_paths",
                          return_value=[("直连", None), ("代理A", "http://127.0.0.1:1")]), \
             patch.object(np, "_probe_one", side_effect=[
                 _path("直连", None, 100.0),
                 _path("代理A", "http://127.0.0.1:1", np.SLOW_PATH_MS + 500)]):
            paths = np.probe_paths()
        self.assertEqual([p["label"] for p in paths], ["直连"])

    def test_probe_paths_keeps_slowest_when_nothing_else(self):
        """全都慢时不能返回空列表，否则引擎直接放弃取数。"""
        with patch.object(np, "candidate_paths", return_value=[("直连", None)]), \
             patch.object(np, "_probe_one",
                          side_effect=[_path("直连", None, np.SLOW_PATH_MS + 900)]):
            paths = np.probe_paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]["label"], "直连")

    # ---------- 切换粘性 ----------
    def test_sticky_keeps_current_path_within_margin(self):
        np._current_label = "直连"
        paths = [_path("代理A", "http://127.0.0.1:1", 100.0), _path("直连", None, 120.0)]
        got = np._apply_sticky(paths)
        self.assertEqual(got[0]["label"], "直连", "20ms 差距不应触发切换")

    def test_sticky_switches_beyond_margin(self):
        np._current_label = "直连"
        paths = [_path("代理A", "http://127.0.0.1:1", 100.0),
                 _path("直连", None, 100.0 + np.STICKY_MARGIN_MS + 50)]
        got = np._apply_sticky(paths)
        self.assertEqual(got[0]["label"], "代理A")

    def test_sticky_ignores_unknown_current_label(self):
        np._current_label = "已消失的路径"
        paths = [_path("代理A", "http://127.0.0.1:1", 100.0), _path("直连", None, 120.0)]
        self.assertEqual(np._apply_sticky(paths)[0]["label"], "代理A")

    # ---------- 缓存 ----------
    def test_best_paths_cache_hit_within_ttl(self):
        with patch.object(np, "probe_paths", return_value=[_path("直连", None, 50.0)]) as pp:
            np.best_paths()
            np.best_paths()
            np.best_paths()
        self.assertEqual(pp.call_count, 1, "TTL 内应复用缓存")

    def test_best_paths_reprobes_after_ttl(self):
        with patch.object(np, "probe_paths", return_value=[_path("直连", None, 50.0)]) as pp:
            np.best_paths()
            np._cached_at = time.time() - np.CACHE_TTL - 1
            np.best_paths()
        self.assertEqual(pp.call_count, 2)

    def test_negative_cache_is_shorter(self):
        with patch.object(np, "probe_paths", return_value=[]) as pp:
            np.best_paths()
            np._cached_at = time.time() - np.NEGATIVE_TTL - 1
            np.best_paths()
        self.assertEqual(pp.call_count, 2, "全失败时负缓存应更短")

    def test_invalidate_forces_reprobe(self):
        with patch.object(np, "probe_paths", return_value=[_path("直连", None, 50.0)]) as pp:
            np.best_paths()
            np.invalidate()
            np.best_paths()
        self.assertEqual(pp.call_count, 2)

    # ---------- 熔断 ----------
    def test_failure_streak_triggers_cooldown(self):
        for _ in range(np.FAIL_STREAK_LIMIT):
            np.record_failure("代理A")
        self.assertTrue(np._in_cooldown("代理A"))

    def test_success_clears_streak_and_cooldown(self):
        for _ in range(np.FAIL_STREAK_LIMIT):
            np.record_failure("代理A")
        np.record_success("代理A")
        self.assertFalse(np._in_cooldown("代理A"))

    def test_all_in_cooldown_still_probes(self):
        """全部路径都在冷却时不能返回空，否则等于自杀。"""
        for _ in range(np.FAIL_STREAK_LIMIT):
            np.record_failure("直连")
        with patch.object(np, "candidate_paths", return_value=[("直连", None)]), \
             patch.object(np, "_probe_one", side_effect=[_path("直连", None, 80.0)]):
            paths = np.probe_paths()
        self.assertEqual(len(paths), 1)

    # ---------- 对外接口 ----------
    def test_best_proxy_url_returns_none_when_direct_wins(self):
        with patch.object(np, "best_paths",
                          return_value=[_path("直连", None, 50.0),
                                        _path("代理A", "http://127.0.0.1:1", 90.0)]):
            self.assertIsNone(np.best_proxy_url())

    def test_best_proxy_url_returns_fastest_proxy(self):
        with patch.object(np, "best_paths",
                          return_value=[_path("代理A", "http://127.0.0.1:1", 50.0),
                                        _path("直连", None, 90.0)]):
            self.assertEqual(np.best_proxy_url(), "http://127.0.0.1:1")

    def test_has_working_path_counts_direct(self):
        with patch.object(np, "best_paths", return_value=[_path("直连", None, 50.0)]):
            self.assertTrue(np.has_working_path())
        with patch.object(np, "best_paths", return_value=[]):
            self.assertFalse(np.has_working_path())

    def test_ordered_sessions_falls_back_to_direct(self):
        sentinel = object()
        with patch.object(np, "best_paths", return_value=[]):
            got = np.ordered_sessions(sentinel)
        self.assertEqual(got, [("直连", sentinel)], "无可用路径时必须给直连兜底")

    def test_ordered_sessions_records_current_label(self):
        sentinel = object()
        # 本环境 requests 可能为 None（会走 urllib 兜底分支），这里显式注入以测完整逻辑
        with patch.object(np, "requests", object()), \
             patch.object(np, "best_paths",
                          return_value=[_path("代理A", "http://127.0.0.1:1", 50.0)]), \
             patch.object(np, "_session_for", return_value=sentinel):
            np.ordered_sessions(sentinel)
        self.assertEqual(np._current_label, "代理A")

    # ---------- 配置 ----------
    def test_load_candidate_ports_falls_back_on_missing_file(self):
        with patch.object(np, "_PORTS_CONFIG_PATH", np.Path("/nonexistent/proxy_ports.json")):
            self.assertEqual(np.load_candidate_ports(), np.DEFAULT_LOCAL_PROXY_PORTS)

    def test_load_candidate_ports_reads_config(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"candidate_ports": [1111, 2222]}')
            tmp = f.name
        try:
            with patch.object(np, "_PORTS_CONFIG_PATH", np.Path(tmp)):
                self.assertEqual(np.load_candidate_ports(), (1111, 2222))
        finally:
            os.unlink(tmp)

    def test_load_candidate_ports_ignores_garbage(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"candidate_ports": "not-a-list"}')
            tmp = f.name
        try:
            with patch.object(np, "_PORTS_CONFIG_PATH", np.Path(tmp)):
                self.assertEqual(np.load_candidate_ports(), np.DEFAULT_LOCAL_PROXY_PORTS)
        finally:
            os.unlink(tmp)

    def test_shipped_config_is_valid(self):
        ports = np.load_candidate_ports()
        self.assertTrue(ports, "项目自带的 proxy_ports.json 必须能解析出端口")
        self.assertTrue(all(isinstance(p, int) for p in ports))


if __name__ == "__main__":
    unittest.main()
