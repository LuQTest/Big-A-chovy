"""回归：分钟线新鲜度按完整来源行情时间判定。"""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import realtime_engine as engine


TZ = ZoneInfo("Asia/Shanghai")


class MinuteAsOfTests(unittest.TestCase):
    def tearDown(self):
        with engine._min5_lock:
            engine._min5_cache.clear()

    def test_trend_parser_preserves_trading_date(self):
        payload = {"data": {"trends": [
            "2026-10-09 10:00,10,10,10,10,100,100000,10",
            "2026-10-09 10:01,10,10,10,10,100,100000,10",
        ]}}
        with patch.object(engine.screen, "fetch_json", return_value=payload):
            bars = engine._fetch_minute_trends("000001")

        self.assertEqual(bars[0][0], "10:00")
        self.assertTrue(bars[0][7].startswith("2026-10-09T10:00"))

    def test_newly_downloaded_old_date_is_stale(self):
        now_dt = datetime(2026, 10, 11, 10, 2, tzinfo=TZ)
        now = now_dt.timestamp()
        snap = {
            "bar_end": "10:01",
            "bar_end_asof": "2026-07-27T10:01:00+08:00",
            "closed_5m": {"cur": {"close": 10.0, "vwap": 9.9, "vol": 100}},
        }
        with engine._min5_lock:
            engine._min5_cache["000001"] = {"time": now, "data": snap}
        with patch.object(engine.time, "time", return_value=now):
            minute_map = engine.build_minute_map(
                {"dual_pool_raw": [{"code": "000001"}]}, {}
            )

        self.assertEqual(minute_map["000001"]["status"], "stale")
        self.assertEqual(minute_map["000001"]["last_bar_at"], snap["bar_end_asof"])
        self.assertGreater(minute_map["000001"]["age_seconds"], 180)

    def test_same_day_recent_source_bar_is_fresh(self):
        now_dt = datetime(2026, 10, 9, 10, 2, tzinfo=TZ)
        now = now_dt.timestamp()
        snap = {
            "bar_end": "10:01",
            "bar_end_asof": "2026-10-09T10:01:00+08:00",
            "closed_5m": {"cur": {"close": 10.0, "vwap": 9.9, "vol": 100}},
        }
        with engine._min5_lock:
            engine._min5_cache["000001"] = {"time": now, "data": snap}
        with patch.object(engine.time, "time", return_value=now):
            minute_map = engine.build_minute_map(
                {"dual_pool_raw": [{"code": "000001"}]}, {}
            )

        self.assertEqual(minute_map["000001"]["status"], "fresh")
        self.assertEqual(minute_map["000001"]["age_seconds"], 60.0)

    def test_future_source_bar_is_stale_and_cannot_pass_entry_freshness(self):
        now_dt = datetime(2026, 10, 9, 10, 2, tzinfo=TZ)
        now = now_dt.timestamp()
        snap = {
            "bar_end": "10:03",
            "bar_end_asof": "2026-10-09T10:03:00+08:00",
            "closed_5m": {"cur": {"close": 10.0, "vwap": 9.9, "vol": 100}},
        }
        with engine._min5_lock:
            engine._min5_cache["000001"] = {"time": now, "data": snap}
        with patch.object(engine.time, "time", return_value=now):
            minute_map = engine.build_minute_map(
                {"dual_pool_raw": [{"code": "000001"}]}, {}
            )

        self.assertEqual(minute_map["000001"]["status"], "stale")
        self.assertEqual(minute_map["000001"]["age_seconds"], -60.0)
        fresh, *_ = engine.screen._minute_freshness(
            minute_map["000001"], engine.screen.DEFAULT_INTERSECTION_CONFIG
        )
        self.assertFalse(fresh)


if __name__ == "__main__":
    unittest.main()
