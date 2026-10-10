"""低开观察必须使用20个完整日频资金样本，不得累加盘中快照。"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import a_share_daily_screen as screen
from test_capital_flow_ranking import stock


def daily_dates(count=20):
    day = datetime(2026, 10, 9)
    dates = []
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    return list(reversed(dates))


def candidate():
    item = stock("000001", main_pct=8.0, super_pct=4.0)
    item.open = 9.7
    item.prev_close = 10.0
    item.price = 10.1
    item.main_net = 2_000_000.0
    item.price_above_vwap = True
    return item


class LowOpenDailyFlowTests(unittest.TestCase):
    def setUp(self):
        with screen._LOW_OPEN_DAILY_FLOW_CACHE_LOCK:
            screen._LOW_OPEN_DAILY_FLOW_CACHE.clear()

    def _fetch_mock(self, rows):
        return patch.object(
            screen, "fetch_json", return_value={"data": {"klines": rows}}
        )

    def test_twenty_completed_daily_bars_qualify_and_sum_once_each(self):
        rows = [f"{day},1000000,0" for day in daily_dates()]
        with self._fetch_mock(rows), patch.object(screen, "_pace_flow_minute_request"):
            result = screen.fetch_daily_main_flow_history("000001", "2026-10-12")
            candidates = screen.low_open_wash_rows([candidate()], "2026-10-12")

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["sessions"], 20)
        self.assertEqual(result["persistent_net"], 20_000_000.0)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["wash_qualified"])
        self.assertEqual(candidates[0]["persistent_net"], 20_000_000.0)

    def test_missing_twenty_day_history_is_unknown_and_cannot_pass(self):
        rows = [f"{day},2000000,0" for day in daily_dates(5)]
        item = candidate()
        with self._fetch_mock(rows), patch.object(screen, "_pace_flow_minute_request"):
            history = screen.fetch_daily_main_flow_history("000001", "2026-10-12")
            candidates = screen.low_open_wash_rows([item], "2026-10-12")

        self.assertEqual(history["status"], "insufficient")
        self.assertIsNone(history["persistent_net"])
        self.assertFalse(screen._qualifies_low_open_wash(item, history))
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["wash_qualified"])
        self.assertIsNone(candidates[0]["persistent_net"])
        self.assertIn("未通过", candidates[0]["persistent_flow_status"])

    def test_duplicate_daily_dates_are_not_counted_twice(self):
        days = daily_dates()
        rows = [f"{day},1000000,0" for day in days for _ in range(2)]
        with self._fetch_mock(rows), patch.object(screen, "_pace_flow_minute_request"):
            history = screen.fetch_daily_main_flow_history("000001", "2026-10-12")

        self.assertEqual(history["sessions"], 20)
        self.assertEqual(history["persistent_net"], 20_000_000.0)

    def test_malformed_endpoint_shape_fails_closed_without_breaking_screen(self):
        with patch.object(screen, "fetch_json", return_value={"data": []}), patch.object(
            screen, "_pace_flow_minute_request"
        ):
            history = screen.fetch_daily_main_flow_history("000001", "2026-10-12")

        self.assertEqual(history["status"], "insufficient")
        self.assertIsNone(history["persistent_net"])

    def test_open_rebound_below_previous_close_is_not_red(self):
        item = candidate()
        item.price = 9.8  # Above open, but still below previous close.
        with patch.object(screen, "fetch_json") as fetch:
            candidates = screen.low_open_wash_rows([item], "2026-10-12")

        self.assertFalse(screen._low_open_wash_current_conditions(item))
        self.assertEqual(candidates, [])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
