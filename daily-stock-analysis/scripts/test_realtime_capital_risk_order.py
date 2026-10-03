"""运行实时筛选主流程，隔离网络、缓存和持仓读写，验证公告先于资金评分。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import a_share_daily_screen as screen
from test_capital_flow_ranking import stock


class RealtimeCapitalRiskOrderTests(unittest.TestCase):
    def run_screen(self, risk="clean", skip_announcements=False, skip_capital_ranking=False):
        # realtime_engine 导入时会替换 fetch_kline；测试退出后恢复，避免污染其它用例。
        with ExitStack() as stack:
            stack.enter_context(patch.object(screen, "fetch_kline", screen.fetch_kline))
            import realtime_engine as engine
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(patch.object(screen, "SCRIPT_DIR", Path(directory)))
            items = [stock(f"00000{i}", main_pct=6, super_pct=3.6,
                           flow_5m_inc=5_000_000, high_pull=0.5) for i in (1, 2, 3)]
            items[0].amount = 2_500_000_000
            for item in items:
                item.main_net = item.amount * 0.06
                item.super_net = item.main_net * 0.6
                item.big_net = item.main_net - item.super_net
                item.flow_15m_inc = 10_000_000
            stats = {"测试板块": {"strong": 3, "n": 3, "adv": 3, "sum": 9.0},
                     "__meta__": {"resonance_usable": True}}
            history = {"000001": [{"main_net": 10_000_000, "super_net": 6_000_000}]}
            for name, value in {
                "set_network_mode": None,
                "fetch_market": ([{"f124": 1790733600}], 3),
                "get_market_fetch_status": {"complete": True, "source": "eastmoney"},
                "fetch_indices": [], "fetch_sector_indices": [], "filter_prefetch": [],
                "enrich_all": (items, []),
                "market_summary": {"adv": 58, "dec": 42, "valid_change": 100,
                                   "main_limit_up": 20, "main_limit_down": 0},
                "sector_stats": stats, "load_flow_history": history,
                "apply_flow_increments": None, "fill_flow_increments_from_fflow": 0,
                "save_flow_history": None, "load_intersection_state": {},
                "save_intersection_state": None, "load_intersection_calibration": None,
            }.items():
                stack.enter_context(patch.object(screen, name, return_value=value))
            for name, value in {"_save_kline_cache": None, "build_minute_map": {}, "enrich_min5": None}.items():
                stack.enter_context(patch.object(engine, name, return_value=value))
            titles = {"clean": ["日常经营公告"], "watch_risk": ["股东质押公告"],
                      "avoid": ["关于诉讼事项的公告"]}
            announcements = stack.enter_context(patch.object(
                screen, "fetch_announcements", return_value=titles.get(risk, []),
                side_effect=RuntimeError("offline") if risk == "unknown" else None))
            attach = screen.attach_announcement_risks
            stack.enter_context(patch.object(screen, "attach_announcement_risks",
                side_effect=lambda *args, **kwargs: attach(*args, **kwargs, risk_cache={})))
            ranking = stack.enter_context(patch.object(screen, "rank_capital_candidates",
                                                        wraps=screen.rank_capital_candidates))
            result = engine.run_screening(modes={"strict"}, network_mode="direct",
                                          skip_announcements=skip_announcements,
                                          skip_capital_ranking=skip_capital_ranking)
            if not skip_capital_ranking:
                self.assertEqual(ranking.call_count, 1)
                self.assertIs(ranking.call_args.args[2], history)
            if skip_announcements:
                announcements.assert_not_called()
            return result

    def test_clean_candidates_receive_sector_boost_after_announcements(self):
        result = self.run_screen()
        self.assertEqual(len(result["capital_rank"]), 3)
        for row in result["capital_rank"]:
            self.assertEqual(row["risk_status"], "clean")
            self.assertEqual(row["sector_boost"], 15)
            self.assertIn("pool_source", row)

    def test_watch_risk_does_not_receive_clean_only_boost(self):
        result = self.run_screen("watch_risk")
        self.assertEqual(len(result["capital_rank"]), 3)
        self.assertTrue(all(row["risk_status"] == "watch_risk" for row in result["capital_rank"]))
        self.assertTrue(all(row["sector_boost"] == 0 for row in result["capital_rank"]))

    def test_unverified_and_avoided_candidates_do_not_reappear_after_ranking(self):
        for risk in ("avoid", "unknown"):
            with self.subTest(risk=risk):
                self.assertEqual(self.run_screen(risk)["capital_rank"], [])

    def test_skip_options_remain_fail_closed(self):
        self.assertEqual(self.run_screen(skip_announcements=True)["capital_rank"], [])
        self.assertEqual(self.run_screen(skip_capital_ranking=True)["capital_rank"], [])


if __name__ == "__main__":
    unittest.main()
