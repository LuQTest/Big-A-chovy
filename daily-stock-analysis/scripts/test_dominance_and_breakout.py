import unittest
from datetime import datetime
from unittest.mock import MagicMock

from a_share_daily_screen import (
    Enriched,
    _row_risk_status,
    evaluate_dominance_type,
    rank_capital_candidates,
    evaluate_watchlist_breakout_states,
)


class DominanceAndBreakoutTests(unittest.TestCase):
    def test_row_risk_status_with_enriched_instance(self):
        """_row_risk_status 传入 Enriched 数据类实例不会抛出 AttributeError。"""
        e = MagicMock(spec=Enriched)
        e.risk_status = "clean"
        self.assertEqual(_row_risk_status(e), "clean")

        e_avoid = MagicMock(spec=Enriched)
        e_avoid.risk_status = "avoid"
        self.assertEqual(_row_risk_status(e_avoid), "avoid")

    def test_evaluate_dominance_type_absolute(self):
        """超大单占主力 >= 50% 判定为 absolute 绝对主导。"""
        e = MagicMock()
        e.code = "000426"
        e.super_net = 60_000_000.0
        e.main_net = 100_000_000.0
        e.big_net = 40_000_000.0
        e.flow_5m_inc = 5_000_000.0

        dom_type, label = evaluate_dominance_type(e)
        self.assertEqual(dom_type, "absolute")
        self.assertEqual(label, "✓(绝对)")

    def test_buy_ratio_production_derivation(self):
        """测试由真实行情数据（主力净占比、超大单、大单与5分增量）生产级自动推导 buy_ratio 并触发合力主导。"""
        from a_share_daily_screen import apply_flow_increments

        import time as _t
        now_ts = _t.time()
        t_5m_ago = now_ts - 300

        e = Enriched(
            code="000603", name="盛达资源", price=35.0, change=6.0, turnover=5.0,
            amount=413_000_000.0, volume_ratio=1.5, high=36.0, low=34.0, open=34.5, prev_close=33.0,
            total_mv=30_000_000_000.0, float_mv=25_000_000_000.0, industry="贵金属", timestamp=int(now_ts),
            volume=11800000.0, kdate="2026-08-21", k_source="tencent", adj_close=35.0,
            ma5=34.0, ma10=33.0, ma20=32.0, prev_ma5=33.5, prev_ma10=32.5, prev_ma20=31.5,
            five_ret=0.08, dist60=0.15, ma20_dist=0.06, high_pull=0.5, cur_to_high=0.01,
            vol_vs_avg5=1.2, vwap=34.8, vwap_state="均价线上方", prior_high=35.5, prior_low=32.5,
            main_net=93_100_000.0, main_pct=22.5, super_net=32_940_000.0, super_pct=8.0,
            big_net=60_160_000.0, big_pct=14.5, mid_net=0, mid_pct=0, small_net=0, small_pct=0,
            price_above_vwap=True,
        )

        flow_history = {
            "000603": [
                {"ts": t_5m_ago, "main_net": 70_000_000.0, "super_net": 25_000_000.0, "amount": 350_000_000.0, "volume": 10000000.0},
                {"ts": now_ts, "main_net": 93_100_000.0, "super_net": 32_940_000.0, "amount": 413_000_000.0, "volume": 11800000.0},
            ]
        }

        apply_flow_increments([e], flow_history)
        # 验证自动推导出有效的 buy_ratio 且达到 >= 1.5
        import math
        self.assertFalse(math.isnan(e.buy_ratio))
        self.assertGreaterEqual(e.buy_ratio, 1.5)

        # 严格断言判定为 coalition 合力主升！
        dom_type, dom_label = evaluate_dominance_type(e, flow_history)
        self.assertEqual(dom_type, "coalition")
        self.assertEqual(dom_label, "✓(合力)")

    def test_render_markdown_with_non_empty_low_ultra_pool(self):
        """测试包含非空 low_ultra 与 low_trend 池时的 Markdown 渲染，验证不会发生 KeyError。"""
        from a_share_daily_screen import render_markdown

        mock_result = {
            "meta": {
                "timestamp": "2026-08-21 15:00:00",
                "status": "收盘",
                "source": "测试数据",
                "total_rows": 5000,
                "provider_total": 5000,
                "market_fetch_complete": True,
                "prefetch_rows": 50,
                "enriched_rows": 50,
                "elapsed_seconds": 1.0,
                "market_data_degraded": False,
            },
            "breadth": {},
            "market_fetch_status": {},
            "indices": [],
            "errors": [],
            "warnings": [],
            "announcement_errors": [],
            "strict_enabled": False,
            "capital_rank": [],
            "watchlist": [],
            "low_open_wash": [],
            "low_ultra": [{
                "class": "A",
                "code": "000603",
                "name": "盛达资源",
                "price": 35.40,
                "change": 6.0,
                "turnover": 5.0,
                "amount": 1_500_000_000.0,
                "volume_ratio": 1.5,
                "industry": "贵金属",
                "resonance": "是",
                "high_pull": 0.5,
                "vwap_state": "上方",
                "main_pct": 6.2,
                "super_net": 32_940_000.0,
                "main_net": 93_100_000.0,
                "flow_5m_inc": 27_620_000.0,
                "flow_status": "有效流入",
                "risk": "无",
                "dominance_type": "coalition",
                "dominance_label": "✓(合力)",
                "super_lead": "✓(合力)",
            }],
            "low_trend": [],
        }

        output_md = render_markdown(mock_result)
        self.assertIn("## 低吸超短线 A/B/C", output_md)
        self.assertIn("✓(合力)", output_md)
        self.assertIn("盛达资源", output_md)

    def test_evaluate_dominance_type_coalition_strict_requirements(self):
        """合力主升 (20% <= 占比 < 50%) 必须同时满足历史>=2期、未衰减、分笔主买比>=1.5。"""
        e = MagicMock()
        e.code = "000603"
        e.super_net = 32_940_000.0  # 3294万 >= 2000万
        e.main_net = 93_100_000.0   # 9310万 >= 5000万
        e.big_net = 60_160_000.0    # 大单 > 0
        e.flow_5m_inc = 27_620_000.0 # 5分 >= 1000万
        e.buy_ratio = 1.8           # 分笔主买比 >= 1.5

        flow_history_ok = {
            "000603": [
                {"main_net": 80_000_000.0, "super_net": 30_000_000.0},
                {"main_net": 93_100_000.0, "super_net": 32_940_000.0},
            ]
        }

        # 1. 完整条件全部满足 -> coalition
        dom_type, label = evaluate_dominance_type(e, flow_history=flow_history_ok)
        self.assertEqual(dom_type, "coalition")
        self.assertEqual(label, "✓(合力)")

        # 2. 缺失历史 (flow_history 为空或未包含) -> 严禁放行，返回 none
        dom_type_nohist, label_nohist = evaluate_dominance_type(e, flow_history=None)
        self.assertEqual(dom_type_nohist, "none")
        self.assertEqual(label_nohist, "✗")

        # 3. 历史不足 2 期 -> 严禁放行，返回 none
        dom_type_short, label_short = evaluate_dominance_type(e, flow_history={"000603": [{"main_net": 93100000.0}]})
        self.assertEqual(dom_type_short, "none")

        # 4. 缺失主买比 (buy_ratio 为 None) -> 严禁放行，返回 none
        e_nobuy = MagicMock()
        e_nobuy.code = "000603"
        e_nobuy.super_net = 32_940_000.0
        e_nobuy.main_net = 93_100_000.0
        e_nobuy.big_net = 60_160_000.0
        e_nobuy.flow_5m_inc = 27_620_000.0
        e_nobuy.buy_ratio = None
        dom_type_nobuy, _ = evaluate_dominance_type(e_nobuy, flow_history=flow_history_ok)
        self.assertEqual(dom_type_nobuy, "none")

        # 5. 主买比不足 (< 1.5) -> 返回 none
        e_lowbuy = MagicMock()
        e_lowbuy.code = "000603"
        e_lowbuy.super_net = 32_940_000.0
        e_lowbuy.main_net = 93_100_000.0
        e_lowbuy.big_net = 60_160_000.0
        e_lowbuy.flow_5m_inc = 27_620_000.0
        e_lowbuy.buy_ratio = 1.2
        dom_type_lowbuy, _ = evaluate_dominance_type(e_lowbuy, flow_history=flow_history_ok)
        self.assertEqual(dom_type_lowbuy, "none")

    def test_evaluate_dominance_type_decay_rejection(self):
        """最近2次快照主力或超大单衰减时，合力主导不成立。"""
        e = MagicMock()
        e.code = "000603"
        e.super_net = 25_000_000.0
        e.main_net = 60_000_000.0
        e.big_net = 35_000_000.0
        e.flow_5m_inc = 12_000_000.0
        e.buy_ratio = 1.6

        # 上一期主力为 8000万，本期 6000万 (衰减 > 10%)
        flow_history = {
            "000603": [
                {"main_net": 80_000_000.0, "super_net": 35_000_000.0},
                {"main_net": 60_000_000.0, "super_net": 25_000_000.0},
            ]
        }

        dom_type, label = evaluate_dominance_type(e, flow_history=flow_history)
        self.assertEqual(dom_type, "none")
        self.assertEqual(label, "✗")

    def test_sector_boost_anchor_and_clean_requirement(self):
        """20亿锚点 + 3只共振 + clean 触发 sector_boost (+15分) 与 B类优选资格。"""
        def make_enriched(code, name, price, change, amount, industry, super_net, main_net, flow_5m_inc, high_pull=0.5, risk="clean"):
            return Enriched(
                code=code, name=name, price=price, change=change, turnover=5.0, amount=amount,
                volume_ratio=1.5, high=price * 1.02, low=price * 0.98, open=price * 0.99, prev_close=price / (1 + change / 100),
                total_mv=30_000_000_000.0, float_mv=25_000_000_000.0, industry=industry, timestamp=1724210000,
                volume=amount / price, kdate="2026-08-21", k_source="tencent", adj_close=price,
                ma5=price * 0.98, ma10=price * 0.96, ma20=price * 0.94,
                prev_ma5=price * 0.97, prev_ma10=price * 0.95, prev_ma20=price * 0.93,
                five_ret=0.08, dist60=0.15, ma20_dist=0.06, high_pull=high_pull,
                cur_to_high=0.01, vol_vs_avg5=1.2, vwap=price * 0.99, vwap_state="上方",
                prior_high=price * 1.01, prior_low=price * 0.95,
                main_net=main_net, main_pct=(main_net / amount * 100) if amount > 0 else 0,
                super_net=super_net, super_pct=(super_net / amount * 100) if amount > 0 else 0,
                big_net=main_net - super_net, big_pct=0, mid_net=0, mid_pct=0, small_net=0, small_pct=0,
                flow_5m_inc=flow_5m_inc, flow_15m_inc=flow_5m_inc * 1.5,
                amount_5m_inc=0, volume_5m_inc=0, vol_ratio_vs_hist=1.0, vol_surge=False,
                price_above_vwap=True, flow_status="持续流入",
                risk_status=risk,
            )

        # 构造锚点股票（兴业银锡 30亿成交）
        anchor = make_enriched("000426", "兴业银锡", 40.0, 3.9, 3_000_000_000.0, "贵金属", 200_000_000.0, 300_000_000.0, 15_000_000.0)
        # 同板块第二只标的（盛达资源，clean）
        peer1 = make_enriched("000603", "盛达资源", 35.0, 6.0, 1_500_000_000.0, "贵金属", 50_000_000.0, 90_000_000.0, 25_000_000.0, risk="clean")
        # 同板块第三只标的（招金黄金，clean）
        peer2 = make_enriched("000506", "招金黄金", 20.0, 2.5, 800_000_000.0, "贵金属", 30_000_000.0, 50_000_000.0, 10_000_000.0, risk="clean")

        stats = {
            "贵金属": {"strong": 3, "n": 3, "adv": 3, "sum": 12.3},
            "__meta__": {"resonance_usable": True},
        }

        # 验证 clean 标的获得加分
        ranked = rank_capital_candidates([anchor, peer1, peer2], stats)
        peer1_row = next(r for r in ranked if r["code"] == "000603")
        self.assertEqual(peer1_row["sector_boost"], 15.0)
        self.assertTrue(peer1_row["b_preferred"])
        self.assertIn("主线板块协同(+15分,20亿锚点带动)", peer1_row["capital_reason"])

        # 验证 watch_risk 或 unknown 标的硬拒绝（不得加分）
        peer_risky = make_enriched("000603", "盛达资源", 35.0, 6.0, 1_500_000_000.0, "贵金属", 50_000_000.0, 90_000_000.0, 25_000_000.0, risk="watch_risk")
        ranked_risky = rank_capital_candidates([anchor, peer_risky, peer2], stats)
        peer_risky_row = next(r for r in ranked_risky if r["code"] == "000603")
        self.assertEqual(peer_risky_row["sector_boost"], 0.0)
        self.assertFalse(peer_risky_row["b_preferred"])

        peer_unknown = make_enriched("000603", "盛达资源", 35.0, 6.0, 1_500_000_000.0, "贵金属", 50_000_000.0, 90_000_000.0, 25_000_000.0, risk="unknown")
        ranked_unknown = rank_capital_candidates([anchor, peer_unknown, peer2], stats)
        peer_unknown_row = next(r for r in ranked_unknown if r["code"] == "000603")
        self.assertEqual(peer_unknown_row["sector_boost"], 0.0)

    def test_cross_day_watchlist_retention_when_today_raw_is_empty(self):
        """反例实测：昨日有标的、今日 raw_watchlist 为空时，昨日标的与触发基准必须完整保留继承。"""
        # 昨日持久化状态中的标的
        previous_state = {
            "000603": {
                "name": "盛达资源",
                "phase": "CONFIRMED",
                "confirm_count": 2,
                "trigger_price": 34.87,
                "no_chase_price": 35.74,
                "buy_zone": "33.20-33.80",
                "invalid": 32.50,
                "industry": "贵金属",
            }
        }

        # 今日新选出的 raw_watchlist 为空
        today_raw_watchlist = []
        enriched_map = {
            "000603": MagicMock(price=34.0, flow_5m_inc=0.0, price_above_vwap=False, risk_status="clean")
        }

        now = datetime(2026, 8, 24, 9, 35, 0)  # 次日开盘
        evaluated_wl, next_state = evaluate_watchlist_breakout_states(
            today_raw_watchlist, enriched_map, {}, None, previous_state, now, risk_map={"000603": "clean"}
        )

        # 核心断言：昨日标的没有丢失！
        self.assertEqual(len(evaluated_wl), 1)
        self.assertEqual(evaluated_wl[0]["code"], "000603")
        self.assertEqual(evaluated_wl[0]["trigger"], 34.87)
        self.assertEqual(evaluated_wl[0]["breakout_phase"], "WATCHING")

    def test_watchlist_breakout_a_strict_and_risk_control(self):
        """测试突破状态机与公告风控接通：avoid 一票否决；clean + 主买比>=1.5 升为 A_STRICT。"""
        watchlist_items = [{
            "code": "000603",
            "name": "盛达资源",
            "price": 34.0,
            "trigger": 34.87,
            "no_chase": ">35.74不追",
            "buy_zone": "33.20-33.80",
            "invalid": 32.50,
            "reason": "贵金属",
        }]

        stats_with_resonance = {
            "贵金属": {"strong": 3, "n": 3, "adv": 3, "sum": 10.0},
            "__meta__": {"resonance_usable": True},
        }

        # 站稳2期的高强度标的
        e = MagicMock()
        e.price = 35.40
        e.flow_5m_inc = 20_000_000.0
        e.price_above_vwap = True
        e.industry = "贵金属"
        e.super_net = 50_000_000.0
        e.main_net = 80_000_000.0
        e.big_net = 30_000_000.0
        e.main_pct = 6.5
        e.high_pull = 0.6
        e.buy_ratio = 2.0

        prev_state = {"000603": {"phase": "TRIGGERED", "confirm_count": 1, "trigger_price": 34.87}}
        now_1020 = datetime(2026, 8, 21, 10, 20, 0)

        # 1. 处于 clean 状态 -> 成功升级 A_STRICT
        eval_clean, _ = evaluate_watchlist_breakout_states(
            watchlist_items, {"000603": e}, stats_with_resonance, None, prev_state, now_1020, risk_map={"000603": "clean"}
        )
        self.assertEqual(eval_clean[0]["breakout_class"], "A_STRICT")

        # 2. 处于 avoid 风险状态 -> 立即一票否决为 INVALID
        eval_avoid, _ = evaluate_watchlist_breakout_states(
            watchlist_items, {"000603": e}, stats_with_resonance, None, prev_state, now_1020, risk_map={"000603": "avoid"}
        )
        self.assertEqual(eval_avoid[0]["breakout_class"], "INVALID")
        self.assertIn("avoid 否决", eval_avoid[0]["status_note"])

        # 3. 缺失主买比 (buy_ratio 为 None) -> 禁止升为 A_STRICT，降为 B_BREAKOUT
        e_nobuy = MagicMock()
        e_nobuy.price = 35.40
        e_nobuy.flow_5m_inc = 20_000_000.0
        e_nobuy.price_above_vwap = True
        e_nobuy.industry = "贵金属"
        e_nobuy.super_net = 50_000_000.0
        e_nobuy.main_net = 80_000_000.0
        e_nobuy.big_net = 30_000_000.0
        e_nobuy.main_pct = 6.5
        e_nobuy.high_pull = 0.6
        e_nobuy.buy_ratio = None
        eval_nobuy, _ = evaluate_watchlist_breakout_states(
            watchlist_items, {"000603": e_nobuy}, stats_with_resonance, None, prev_state, now_1020, risk_map={"000603": "clean"}
        )
        self.assertEqual(eval_nobuy[0]["breakout_class"], "B_BREAKOUT")


if __name__ == "__main__":
    unittest.main()
