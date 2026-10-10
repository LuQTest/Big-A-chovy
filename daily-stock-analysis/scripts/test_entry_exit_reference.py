"""回归：实时参考价使用框架止盈止损比例和统一RR口径。"""
import unittest
from types import SimpleNamespace

import realtime_engine as engine


class EntryExitReferenceTests(unittest.TestCase):
    def test_enriched_and_fallback_use_same_framework_levels(self):
        enriched_row = {}
        engine._add_entry_exit(
            enriched_row,
            SimpleNamespace(price=10.0, ma5=9.8, low=9.5, prior_low=9.0),
        )
        fallback_row = {"price": 10.0, "ma5": 9.8, "low": 9.5}
        engine._add_entry_exit_from_row(fallback_row)

        for row in (enriched_row, fallback_row):
            with self.subTest(row=row):
                self.assertEqual(row["stop_loss"], 9.7)
                self.assertEqual(row["stop_loss_pct"], 3.0)
                self.assertEqual(row["take_profit_1"], 10.2)
                self.assertIsNone(row["take_profit_2"])
                self.assertEqual(row["rr_ratio"], 0.67)
                self.assertEqual(row["entry_reference_price"], 10.0)
                self.assertIn("按买点价重算", row["exit_reference_basis"])


if __name__ == "__main__":
    unittest.main()
