import unittest

from a_share_daily_screen import (
    Enriched,
    apply_announcement_pool_gates,
    market_summary,
    rank_capital_candidates,
    strict_trend,
    trend_observation,
    trend_condition_diagnosis,
)


def stock(code="000001", **overrides):
    values = dict(
        code=code, name="测试股", price=10.0, change=0.8, turnover=3.0,
        amount=500_000_000, volume_ratio=1.6, high=10.2, low=9.7,
        open=9.9, prev_close=9.92, total_mv=10_000_000_000,
        float_mv=8_000_000_000, industry="测试板块", timestamp=0, volume=1,
        kdate="2026-07-17", k_source="test", adj_close=10.0, ma5=9.9,
        ma10=9.8, ma20=9.5, prev_ma5=9.85, prev_ma10=9.75,
        prev_ma20=9.45, five_ret=0.05, dist60=0.08, ma20_dist=0.05,
        high_pull=0.6, cur_to_high=0.02, vol_vs_avg5=1.2, vwap=9.9,
        vwap_state="均价线上方", prior_high=9.8, prior_low=9.2,
    )
    values.update(overrides)
    return Enriched(**values)


class TrendPoolAndQualityTests(unittest.TestCase):
    def test_repairing_trend_enters_observation_but_not_confirmation(self):
        repairing = stock(change=0.8)

        self.assertTrue(trend_observation(repairing))
        self.assertFalse(strict_trend(repairing))

    def test_broad_weak_market_can_have_observation_without_forced_intersection(self):
        candidates = [stock("000001", change=-0.5), stock("000002", change=0.6)]

        observation = [e for e in candidates if trend_observation(e)]
        confirmation = [e for e in candidates if strict_trend(e)]
        ultra = []

        self.assertEqual(len(observation), 2)
        self.assertEqual(confirmation, [])
        self.assertEqual([e for e in ultra if e.code in {x.code for x in observation}], [])

    def test_diagnosis_lists_all_failed_confirmation_conditions(self):
        item = stock(change=0.8, turnover=1.0, dist60=0.14)

        diagnosis = trend_condition_diagnosis(item)

        self.assertEqual(diagnosis["first_failure"], "当日涨幅")
        self.assertIn("当日涨幅", diagnosis["all_failures"])
        self.assertIn("换手率", diagnosis["all_failures"])
        self.assertIn("距60日高点", diagnosis["all_failures"])
        self.assertFalse(diagnosis["near_match"])

    def test_diagnosis_separates_observation_failures_from_confirmation_failures(self):
        low_price = stock(price=7.5)

        diagnosis = trend_condition_diagnosis(low_price)

        self.assertEqual(diagnosis["observation_first_failure"], "价格区间")
        self.assertIn("价格区间", diagnosis["observation_all_failures"])
        self.assertIn("价格区间", diagnosis["all_failures"])

    def test_market_breadth_counts_declines_and_marks_missing_change_invalid(self):
        rows = [
            {"f12": "000001", "f3": 1.2, "f5": 100},
            {"f12": "000002", "f3": -2.3, "f5": 100},
            {"f12": "000003", "f3": 0.0, "f5": 100},
            {"f12": "000004", "f3": None, "f5": 100},
        ]

        summary = market_summary(rows, provider_total=4)

        self.assertEqual(summary["adv"], 1)
        self.assertEqual(summary["dec"], 1)
        self.assertEqual(summary["flat"], 1)
        self.assertEqual(summary["invalid_change"], 1)
        self.assertFalse(summary["resonance_usable"])

    def test_incomplete_provider_sample_disables_resonance_bonus(self):
        rows = [{"f12": "000001", "f3": 1.0, "f5": 100}]

        summary = market_summary(rows, provider_total=100)

        self.assertTrue(summary["degraded"])
        self.assertFalse(summary["resonance_usable"])

    def test_market_breadth_excludes_non_a_share_rows_from_its_denominator(self):
        rows = [
            {"f12": "000001", "f3": 1.0, "f5": 100},
            {"f12": "600001", "f3": -1.0, "f5": 100},
            {"f12": "300001", "f3": 0.0, "f5": 100},
            {"f12": "688001", "f3": 2.0, "f5": 100},
            {"f12": "830001", "f3": None, "f5": 100},
        ]
        complete_snapshot = {"complete": True, "expected_pages": 1, "received_pages": 1}

        summary = market_summary(rows, provider_total=5, fetch_status=complete_snapshot)

        self.assertEqual(summary["raw_total_rows"], 5)
        self.assertEqual(summary["total_rows"], 4)
        self.assertEqual(summary["adv"], 2)
        self.assertEqual(summary["dec"], 1)
        self.assertEqual(summary["flat"], 1)
        self.assertEqual(summary["invalid_change"], 0)
        self.assertFalse(summary["degraded"])

    def test_degraded_breadth_does_not_award_sector_resonance_points(self):
        item = stock(main_pct=1.0, super_pct=0.0, main_net=1, super_net=0, big_net=0)
        stats = {
            "测试板块": {"strong": 3},
            "__meta__": {"resonance_usable": False},
        }

        ranked = rank_capital_candidates([item], stats)

        self.assertEqual(ranked[0]["resonance"], "数据质量降级")
        self.assertIn("未加分", ranked[0]["capital_reason"])
        self.assertLess(ranked[0]["capital_score"], 30)

    def test_avoid_announcement_cannot_upgrade_an_observation_candidate(self):
        row = {"code": "000001", "announcement_risk": "avoid"}
        result = {
            "trend_observation": [dict(row)],
            "strict_trend": [dict(row)],
            "dual_pool": [dict(row)],
            "capital_rank": [dict(row)],
            "trend_diagnostics": [dict(row)],
        }

        apply_announcement_pool_gates(result)

        self.assertEqual(result["trend_observation"], [])
        self.assertEqual(result["strict_trend"], [])
        self.assertEqual(result["dual_pool"], [])
        self.assertEqual(result["capital_rank"], [])
        self.assertEqual(result["trend_diagnostics"][0]["upgrade_status"], "公告avoid，禁止升级")


if __name__ == "__main__":
    unittest.main()
