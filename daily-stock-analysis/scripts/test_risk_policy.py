# -*- coding: utf-8 -*-
"""公告风险 / 超大单为负的政策矩阵回归（R3 / R4）。

权威语义（选股框架.md「一、信号与建仓门禁」）：
- 公告 avoid / unknown 一票否决；watch_risk 仅减分不否决；
- 超大单为负一票否决；
- 实验性升级（sector_boost / A_STRICT）可以额外要求 clean —— 这是“未获得加分”，
  与“整只股票被一票否决”是两回事。

两个状态机（双池交集、观察池突破）与资金优选都必须遵守同一套语义。
"""

import unittest
from datetime import datetime

import a_share_daily_screen as screen
import testing_fixtures as fx

STATS = {
    "半导体": {"n": 10, "adv": 8, "strong": 5, "sum": 20.0},
    "__meta__": {"resonance_usable": True, "quality_reason": "", "strong_scope_boards": ["main"]},
}


def _watchlist_item(code: str) -> dict:
    return {
        "code": code, "name": f"观察{code}", "trigger": 12.0, "buy_zone": "11.8-12.1",
        "invalid": 11.6, "no_chase": ">12.3不追", "industry": "半导体",
        "structure": "突破前观察", "reason": "合成", "score": 90.0,
    }


def _breakout_enriched(code: str, **overrides):
    base = dict(
        price=12.1, high=12.15, change=4.0, main_pct=6.0, high_pull=0.5,
        super_net=30_000_000.0, main_net=50_000_000.0, big_net=20_000_000.0,
        flow_5m_inc=8_000_000.0, price_above_vwap=True, buy_ratio=1.6,
    )
    base.update(overrides)
    return fx.make_enriched(code, f"突破{code}", **base)


class BreakoutRiskMatrixTests(unittest.TestCase):
    """观察池突破：避免/数据不足不得升级；软风险只丢实验升级。"""

    def _run(self, risk: str):
        code = "600301"
        enriched = {code: _breakout_enriched(code)}
        source_time = int(datetime(2026, 10, 2, 10, 4).timestamp())
        enriched[code].timestamp = source_time
        previous = {code: {
            "code": code, "phase": "TRIGGERED", "confirm_count": 1,
            "trigger_price": 12.0, "no_chase_price": 12.3, "buy_zone": "11.8-12.1",
            "invalid": 11.6, "no_chase": ">12.3不追", "industry": "半导体",
            "last_snapshot_id": source_time - 60,
        }}
        rows, _state = screen.evaluate_watchlist_breakout_states(
            [_watchlist_item(code)], enriched, STATS, None, previous,
            datetime(2026, 10, 2, 10, 5),  # 已过 09:30-09:40 观察期
            risk_map={code: risk},
        )
        return rows[0]

    def test_clean_can_reach_a_strict(self):
        row = self._run("clean")
        self.assertEqual(row["breakout_phase"], "A_STRICT")

    def test_watch_risk_loses_experiment_upgrade_but_still_advances(self):
        row = self._run("watch_risk")
        self.assertEqual(row["breakout_phase"], "B_BREAKOUT")
        self.assertEqual(row["risk_status"], "watch_risk")

    def test_avoid_is_invalidated(self):
        row = self._run("avoid")
        self.assertEqual(row["breakout_phase"], "INVALID")
        self.assertEqual(row["confirm_count"], 0)

    def test_unknown_cannot_upgrade(self):
        row = self._run("unknown")
        self.assertIn(row["breakout_phase"], ("WATCHING",))
        self.assertEqual(row["confirm_count"], 0)
        self.assertIn("unknown", row["status_note"])

    def test_unknown_is_not_confirmed_across_repeated_snapshots(self):
        """跨多快照反复出现也不得升级（数据不足不是“站稳”）。"""
        code = "600301"
        enriched = {code: _breakout_enriched(code)}
        state = {code: {
            "code": code, "phase": "TRIGGERED", "confirm_count": 3,
            "trigger_price": 12.0, "no_chase_price": 12.3,
        }}
        rows, _ = screen.evaluate_watchlist_breakout_states(
            [_watchlist_item(code)], enriched, STATS, None, state,
            datetime(2026, 10, 2, 10, 5), risk_map={code: "unknown"},
        )
        self.assertEqual(rows[0]["breakout_phase"], "WATCHING")


class SectorBoostRiskTests(unittest.TestCase):
    """sector_boost 是实验加分：clean 才获得；watch_risk 不否决但拿不到加分。"""

    def _candidates(self, target_risk: str):
        anchor = fx.make_enriched(
            "600401", "锚点", amount=2_500_000_000.0, main_pct=6.0,
            main_net=100_000_000.0, super_net=60_000_000.0, big_net=40_000_000.0,
            high_pull=0.5, price_above_vwap=True, risk_status="clean",
        )
        peers = [
            fx.make_enriched("600402", "共振1", risk_status="clean"),
            fx.make_enriched("600403", "共振2", risk_status="clean"),
        ]
        target = fx.make_enriched("600404", "目标", risk_status=target_risk)
        return [anchor, *peers, target]

    def test_clean_gets_boost_watch_risk_stays_without_it(self):
        for risk, expected_boost in (("clean", 15.0), ("watch_risk", 0.0)):
            with self.subTest(risk=risk):
                ranked = screen.rank_capital_candidates(self._candidates(risk), STATS, None)
                by_code = {r["code"]: r for r in ranked}
                self.assertIn("600404", by_code, "watch_risk 不得被资金优选剔除")
                self.assertEqual(by_code["600404"]["sector_boost"], expected_boost)


class FlowVetoTests(unittest.TestCase):
    """超大单为负一票否决：标注 + 禁止正式新开仓。"""

    def test_negative_super_marks_and_blocks_intersection_entry(self):
        cfg = dict(screen.DEFAULT_INTERSECTION_CONFIG)
        e = fx.make_enriched("600501", "负超单", super_net=-8_000_000.0, main_net=30_000_000.0)
        self.assertTrue(screen.flow_veto_reason(e))

        row = {
            "code": "600501", "name": "负超单", "price": 12.0, "high": 12.0,
            "change": 3.0, "turnover": 4.0, "volume_ratio": 2.0, "high_pull": 0.3,
            "vwap": 11.9, "main_net": 30_000_000.0, "main_pct": 6.0,
            "flow_5m_inc": 5_000_000.0, "flow_15m_inc": 9_000_000.0,
            "price_above_vwap": True, "resonance": "是",
            "flow_veto": screen.flow_veto_reason(e),
        }
        minute = {"status": "fresh", "age_seconds": 30, "last_bar_at": "10:00",
                  "close_5m": 12.0, "vwap_5m": 11.9, "vol_5m": 8000}
        state = {}
        phases = []
        for i in range(4):
            rows, state = screen.evaluate_intersection_states(
                [dict(row)], [], state, datetime(2026, 10, 2, 10, i * 2), cfg,
                snapshot_id=f"S{i}", risk_map={"600501": "clean"},
                minute_map={"600501": minute},
                market_context={"market_mode": "NORMAL", "breadth_pct": 58.0},
            )
            phases.append(rows[0]["phase_code"])
        self.assertNotIn(screen.PHASE_ENTRY, phases)
        self.assertFalse(rows[0]["new_open_eligible"])
        self.assertIn("超大单为负", rows[0]["entry_block_reason"])


if __name__ == "__main__":
    unittest.main()
