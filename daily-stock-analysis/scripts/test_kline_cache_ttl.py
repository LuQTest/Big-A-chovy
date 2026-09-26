"""K 线缓存 TTL 行为测试。

2026-09-26 修正的回归点：TTL 计时器原先用"整份缓存的保存时间"，而保存函数每轮筛选结束
都会被调用，于是计时器被不断重置、缓存整天不过期——注释承诺的 30 分钟刷新从未生效。
本文件锁定"按条目计时"的正确行为。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import realtime_engine as engine

RESULT = ([{"date": "2026-09-24", "open": 1.0, "close": 2.0}], "tencent_qfq")


class KlineCacheTtlTests(unittest.TestCase):
    def setUp(self):
        engine._kline_cache = {}
        engine._kline_cache_date = engine._today()
        engine._kline_fetch_count = 0
        engine._kline_cache_hit_count = 0
        self._wait_patch = patch.object(engine._rate_limiter, "wait", lambda: None)
        self._wait_patch.start()
        self.addCleanup(self._wait_patch.stop)
        # 缓存是模块级状态：测试结束必须清空，否则会污染其他测试（如 K 线取数用例命中缓存）
        self.addCleanup(self._reset_cache)

    @staticmethod
    def _reset_cache():
        engine._kline_cache = {}
        engine._kline_cache_date = ""

    def _entry(self, age_seconds: float):
        return {"fetched_at": time.time() - age_seconds, "data": RESULT}

    # ---------- 命中与过期 ----------
    def test_fresh_entry_is_served_without_fetch(self):
        engine._kline_cache["600519"] = self._entry(60)
        with patch.object(engine, "_original_fetch_kline") as fetch:
            got = engine._cached_fetch_kline("600519")
        fetch.assert_not_called()
        self.assertEqual(got, RESULT)

    def test_entry_beyond_ttl_is_refetched(self):
        engine._kline_cache["600519"] = self._entry(engine.KLINE_CACHE_TTL + 10)
        with patch.object(engine, "_original_fetch_kline", return_value=RESULT) as fetch:
            got = engine._cached_fetch_kline("600519")
        fetch.assert_called_once()
        self.assertEqual(got, RESULT)

    def test_refetch_updates_entry_timestamp(self):
        engine._kline_cache["600519"] = self._entry(engine.KLINE_CACHE_TTL + 10)
        with patch.object(engine, "_original_fetch_kline", return_value=RESULT):
            engine._cached_fetch_kline("600519")
        self.assertTrue(engine._is_entry_fresh(engine._kline_cache["600519"]))

    # ---------- 回归点：保存不得刷新 TTL ----------
    def test_saving_does_not_refresh_entry_ttl(self):
        engine._kline_cache["600519"] = self._entry(engine.KLINE_CACHE_TTL + 10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".kline_cache.json"
            with patch.object(engine, "KLINE_CACHE_FILE", path):
                engine._save_kline_cache()
        self.assertFalse(engine._is_entry_fresh(engine._kline_cache["600519"]))

    def test_fresh_entries_count_reflects_ttl(self):
        engine._kline_cache["600519"] = self._entry(60)
        engine._kline_cache["000859"] = self._entry(engine.KLINE_CACHE_TTL + 10)
        self.assertEqual(engine._fresh_entry_count(), 1)

    # ---------- 文件读写 ----------
    def test_legacy_cache_file_loads_but_is_stale(self):
        """旧格式（整条即结果）按文件时间补 fetched_at，保存时间过久即视为过期。"""
        legacy = {
            "date": engine._today(),
            "timestamp": time.time() - engine.KLINE_CACHE_TTL - 10,
            "data": {"600519": RESULT},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".kline_cache.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(engine, "KLINE_CACHE_FILE", path):
                engine._load_kline_cache()
                self.assertIn("600519", engine._kline_cache)
                self.assertFalse(engine._is_entry_fresh(engine._kline_cache["600519"]))
                with patch.object(engine, "_original_fetch_kline", return_value=RESULT) as fetch:
                    engine._cached_fetch_kline("600519")
                fetch.assert_called_once()

    def test_new_format_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".kline_cache.json"
            with patch.object(engine, "KLINE_CACHE_FILE", path):
                engine._kline_cache["600519"] = self._entry(30)
                engine._save_kline_cache()
                engine._kline_cache = {}
                engine._kline_cache_date = ""
                engine._load_kline_cache()
        self.assertTrue(engine._is_entry_fresh(engine._kline_cache.get("600519")))

    def test_cross_day_cache_file_is_discarded(self):
        stale = {"date": "2026-01-01", "timestamp": time.time(), "data": {"600519": RESULT}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".kline_cache.json"
            path.write_text(json.dumps(stale), encoding="utf-8")
            with patch.object(engine, "KLINE_CACHE_FILE", path):
                engine._load_kline_cache()
        self.assertEqual(engine._kline_cache, {})


if __name__ == "__main__":
    unittest.main()
