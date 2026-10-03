"""财务字段映射回归测试，全部使用人工响应且不联网。"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import query_financials


class FinancialFieldTests(unittest.TestCase):
    def profile(self, fields):
        with patch.object(query_financials.subprocess, "check_output", side_effect=[
            json.dumps({"data": fields}).encode(), b"",
        ]) as fetch, patch.object(query_financials.tencent_kline, "fetch_kline_json",
                                  side_effect=RuntimeError("offline")):
            result = query_financials.query_financial_profile("600000")
        self.assertIn("f55", fetch.call_args_list[0].args[0])
        return result

    def test_eps_and_profit_growth_are_not_profit_margins(self):
        result = self.profile({"f55": 1.25, "f185": -8.5, "f186": 42, "f187": 18})
        self.assertEqual(result["eps"], 1.25)
        self.assertEqual(result["net_profit_growth"], -8.5)
        self.assertEqual(result["fin_color"], "green")

    def test_zero_and_negative_eps_are_preserved(self):
        for eps, color in [(0, "yellow"), (-0.25, "red")]:
            with self.subTest(eps=eps):
                result = self.profile({"f55": eps, "f185": 0, "f186": 42, "f187": 18})
                self.assertEqual(result["eps"], eps)
                self.assertEqual(result["net_profit_growth"], 0)
                self.assertEqual(result["fin_color"], color)

    def test_missing_fields_do_not_fall_back_to_margins(self):
        for missing in [None, "", "-"]:
            with self.subTest(missing=missing):
                result = self.profile({"f55": missing, "f185": missing, "f186": 42, "f187": 18})
                self.assertIsNone(result["eps"])
                self.assertIsNone(result["net_profit_growth"])
                self.assertEqual(result["fin_color"], "gray")


if __name__ == "__main__":
    unittest.main()
