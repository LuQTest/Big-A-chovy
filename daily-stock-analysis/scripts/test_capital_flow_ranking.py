import math
import unittest

from a_share_daily_screen import Enriched, rank_capital_candidates


def stock(code, *, main_pct, super_pct, change=3.0, above_vwap=True,
          flow_5m_inc=1_000_000, flow_status="有效流入", high_pull=0.8,
          industry="测试板块"):
    values = dict(
        code=code, name=code, price=10.0, change=change, turnover=4.0,
        amount=500_000_000, volume_ratio=2.0, high=10.2, low=9.8,
        open=9.9, prev_close=9.7, total_mv=10_000_000_000,
        float_mv=8_000_000_000, industry=industry, timestamp=0, volume=1,
        kdate="2026-07-17", k_source="test", adj_close=10.0, ma5=9.8,
        ma10=9.6, ma20=9.4, prev_ma5=9.7, prev_ma10=9.5,
        prev_ma20=9.3, five_ret=0.05, dist60=0.1, ma20_dist=0.06,
        high_pull=high_pull, cur_to_high=0.02, vol_vs_avg5=1.2, vwap=9.9,
        vwap_state="均价线上方" if above_vwap else "均价线下方",
        prior_high=9.9, prior_low=9.3, main_net=20_000_000,
        main_pct=main_pct, super_net=10_000_000, super_pct=super_pct,
        big_net=10_000_000, big_pct=2.0, mid_net=-2_000_000,
        mid_pct=-0.4, small_net=-3_000_000, small_pct=-0.6,
        flow_5m_inc=flow_5m_inc, flow_15m_inc=float("nan"),
        price_above_vwap=above_vwap, flow_status=flow_status,
    )
    return Enriched(**values)


class CapitalFlowRankingTests(unittest.TestCase):
    def test_prefers_sustained_main_flow_with_price_confirmation(self):
        strong = stock("000001", main_pct=7.0, super_pct=4.0)
        weak = stock("000002", main_pct=2.0, super_pct=0.5,
                     above_vwap=False, flow_5m_inc=-1_000_000,
                     flow_status="价量背离")

        ranked = rank_capital_candidates([weak, strong], {"测试板块": {"strong": 3, "n": 3, "adv": 3, "sum": 3.0}})

        self.assertEqual(ranked[0]["code"], "000001")
        self.assertEqual(ranked[0]["capital_class"], "资金A类")
        self.assertGreater(ranked[0]["capital_score"], ranked[1]["capital_score"])

    def test_does_not_award_missing_increment_as_sustained_flow(self):
        item = stock("000003", main_pct=5.0, super_pct=3.0,
                     flow_5m_inc=float("nan"), flow_status="数据不足")

        row = rank_capital_candidates([item], {"测试板块": {"strong": 3}})[0]

        self.assertEqual(row["capital_data"], "仅当前快照")
        self.assertNotIn("持续流入", row["capital_reason"])

    def test_distribution_signal_is_never_a_class(self):
        item = stock("000004", main_pct=6.0, super_pct=-3.0,
                     flow_status="疑似派发")
        item.super_net = -10_000_000
        item.big_net = -8_000_000
        item.small_net = 12_000_000

        row = rank_capital_candidates([item], {"测试板块": {"strong": 3}})[0]

        self.assertEqual(row["capital_class"], "资金C类")
        self.assertLess(row["capital_score"], 60)


if __name__ == "__main__":
    unittest.main()
