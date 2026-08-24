# -*- coding: utf-8 -*-
"""default-v2 严格顺序状态机单元测试。

覆盖：严格相位顺序（禁止跳级）、snapshot_id 连续确认去重、分钟K新鲜度
硬否决、公告风险统一 risk_map、市场环境 CASH 否决、14:20 截止、
迟到交集、失效不复活、锁存过期。
"""
import unittest
from datetime import datetime

import a_share_daily_screen as screen
from a_share_daily_screen import (
    DEFAULT_INTERSECTION_CONFIG,
    PHASE_ENTRY,
    PHASE_EXPIRED,
    PHASE_INVALID,
    PHASE_LATCHED,
    PHASE_LATE,
    PHASE_OBSERVING,
    PHASE_PRE,
    PHASE_RETEST_READY,
    PHASE_WAIT_RETEST,
    evaluate_intersection_states,
)

CFG = dict(DEFAULT_INTERSECTION_CONFIG)


def inter_row(code="000001", price=10.0, high=10.1, **overrides):
    """正式交集行（双池成员）。默认不过热、共振、资金正。"""
    row = {
        "code": code,
        "name": "测试股",
        "price": price,
        "high": high,
        "change": 3.2,
        "turnover": 4.0,
        "volume_ratio": 2.0,
        "high_pull": 0.3,
        "vwap": round(price * 0.994, 2),
        "main_net": 20_000_000,
        "main_pct": 1.5,
        "flow_5m_inc": 5_000_000,
        "flow_15m_inc": 9_000_000,
        "price_above_vwap": True,
        "resonance": "是",
    }
    row.update(overrides)
    return row


def pre_row(code="000002", eligible=True, failures=None, **overrides):
    """准交集候选行（compute_pre_intersection 的输出结构）。"""
    failures = failures or []
    row = {
        "code": code,
        "name": "准测试股",
        "price": 8.0,
        "change": 2.8,
        "intersection_phase": "准交集" if eligible else "观察中",
        "pre_intersection_eligible": eligible,
        "preintersection_missing": "当日涨幅",
        "trigger_price": 8.1,
        "gate_failures": failures,
        "gate_failure_text": "；".join(failures) if failures else "全部通过",
        "resonance": "是",
    }
    row.update(overrides)
    return row


def fresh_min(vol=4000, close=10.0, vwap=9.95, age=30.0):
    return {"status": "fresh", "age_seconds": age, "last_bar_at": "10:00",
            "close_5m": close, "vwap_5m": vwap, "vol_5m": vol}


def stale_min(vol=4000, close=10.0, vwap=9.95, age=400.0):
    return {"status": "stale", "age_seconds": age, "last_bar_at": "09:50",
            "close_5m": close, "vwap_5m": vwap, "vol_5m": vol}


def at(hour, minute, second=0):
    return datetime(2026, 7, 28, hour, minute, second)


def run(inter, pre, state, now, snap=None, risk=None, minute=None, mctx=None):
    return evaluate_intersection_states(
        inter, pre, state, now, CFG,
        snapshot_id=snap or now.strftime("%H:%M:%S"),
        risk_map=risk if risk is not None else {"000001": "clean", "000002": "clean"},
        minute_map=minute or {},
        market_context=mctx or {"market_mode": "NORMAL", "breadth_pct": 58.0},
    )


def phase_of(state, code):
    return screen._canonical_phase(state[code]["phase"])


class StrictOrderTests(unittest.TestCase):
    """需求5：严格相位顺序，每快照最多前进一级。"""

    def latch(self, minute=None):
        rows, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                          minute=minute or {"000001": fresh_min(vol=8000, close=10.0, vwap=9.95)})
        return rows, state

    def test_first_intersection_only_latches(self):
        rows, state = self.latch()
        self.assertEqual(phase_of(state, "000001"), PHASE_LATCHED)
        self.assertFalse(rows[0]["new_open_eligible"])
        self.assertEqual(rows[0]["intersection_phase"], "首次交集")

    def test_no_skip_from_latched_to_ready(self):
        # 第2快照条件全好：也只能 LATCHED→WAIT_RETEST
        _, state = self.latch()
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 2),
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        self.assertEqual(phase_of(state, "000001"), PHASE_WAIT_RETEST)

    def full_path_state(self):
        """LATCHED(10:00)→WAIT(10:02)→READY(10:04)→ENTRY(10:06)"""
        _, state = self.latch()
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 2),
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 4),
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        rows, state = run([inter_row(price=9.92, high=10.0)], [], state, at(10, 6),
                          minute={"000001": fresh_min(vol=4100, close=9.92, vwap=9.86)})
        return rows, state

    def test_full_path_reaches_entry(self):
        rows, state = self.full_path_state()
        self.assertEqual(phase_of(state, "000001"), PHASE_ENTRY)
        self.assertTrue(rows[0]["new_open_eligible"])
        self.assertEqual(state["000001"]["consecutive_confirmations"], 2)


class SnapshotDedupTests(unittest.TestCase):
    """需求6：同一 snapshot_id 不得重复计数。"""

    def test_same_snapshot_not_double_counted(self):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0), snap="S1",
                       minute={"000001": fresh_min(vol=8000, close=10.0, vwap=9.95)})
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 2), snap="S2",
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        # WAIT_RETEST。用同一 snapshot_id 跑两次确认：计数只能到1 → 停在 READY
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 4), snap="S3",
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        self.assertEqual(phase_of(state, "000001"), PHASE_RETEST_READY)
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 6), snap="S3",
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        self.assertEqual(phase_of(state, "000001"), PHASE_RETEST_READY)
        self.assertEqual(state["000001"]["consecutive_confirmations"], 1)


class MinuteFreshnessTests(unittest.TestCase):
    """需求3：分钟K超180秒不得推进，也不伪装资金流出。"""

    def test_stale_minute_blocks_ready(self):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                       minute={"000001": fresh_min(vol=8000, close=10.0, vwap=9.95)})
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 2),
                       minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        rows, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 4),
                          minute={"000001": stale_min(vol=4000, close=9.9, vwap=9.85)})
        self.assertEqual(phase_of(state, "000001"), PHASE_WAIT_RETEST)
        self.assertTrue(rows[0]["minute_data_stale"])
        self.assertIn("分钟K过期", rows[0]["data_block_reason"])
        # 过期不推断失效：锁存保留（价格在VWAP上、资金正 → 不 INVALID）
        self.assertNotEqual(phase_of(state, "000001"), PHASE_INVALID)


class RiskMapTests(unittest.TestCase):
    """需求2：统一 risk_map；watch_risk 不新开仓；unknown 否决。"""

    def _entry_rows(self, risk_map):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                       risk=risk_map, minute={"000001": fresh_min(vol=8000)})
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 2),
                       risk=risk_map, minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        _, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 4),
                       risk=risk_map, minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        rows, state = run([inter_row(price=9.9, high=10.0)], [], state, at(10, 6),
                          risk=risk_map, minute={"000001": fresh_min(vol=4000, close=9.9, vwap=9.85)})
        return rows, state

    def test_watch_risk_blocks_entry(self):
        rows, state = self._entry_rows({"000001": "watch_risk"})
        self.assertNotEqual(phase_of(state, "000001"), PHASE_ENTRY)
        self.assertFalse(rows[0]["new_entry_allowed"])
        self.assertIn("公告风险", rows[0]["entry_block_reason"])

    def test_missing_risk_is_unknown_and_blocks(self):
        rows, _ = self._entry_rows({})   # 无 risk_map 且行内无风险字段 → unknown
        self.assertEqual(rows[0]["announcement_risk"], "unknown")
        self.assertFalse(rows[0]["new_entry_allowed"])


class MarketModeDeadlineTests(unittest.TestCase):
    """需求8/9：CASH 禁止新开仓；14:20 后禁止新开仓。"""

    def test_cash_blocks_entry(self):
        mctx = {"market_mode": "CASH", "breadth_pct": 38.0}
        rows, _ = run([inter_row()], [], {}, at(10, 0), mctx=mctx,
                      minute={"000001": fresh_min(vol=8000)})
        self.assertFalse(rows[0]["new_entry_allowed"])
        self.assertIn("CASH", rows[0]["entry_block_reason"])

    def test_past_deadline_blocks_entry(self):
        rows, _ = run([inter_row()], [], {}, at(14, 25),
                      minute={"000001": fresh_min(vol=8000)})
        self.assertTrue(rows[0]["past_entry_deadline"])
        self.assertFalse(rows[0]["new_entry_allowed"])
        self.assertIn("仅供明日观察", rows[0]["entry_block_reason"])


class LateAndInvalidTests(unittest.TestCase):
    """迟到交集、失效条件、失效不复活。"""

    def test_overheat_first_intersection_is_late(self):
        rows, state = run([inter_row(change=5.2)], [], {}, at(10, 0))
        self.assertEqual(phase_of(state, "000001"), PHASE_LATE)
        self.assertTrue(rows[0]["late_flag"])

    def test_vwap_break_with_negative_flow_invalidates(self):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                       minute={"000001": fresh_min(vol=8000)})
        rows, state = run(
            [inter_row(price=9.8, high=10.0, price_above_vwap=False, vwap=9.9,
                       flow_5m_inc=-2_000_000)],
            [], state, at(10, 2),
            minute={"000001": fresh_min(vol=4000, close=9.8, vwap=9.9)})
        self.assertEqual(phase_of(state, "000001"), PHASE_INVALID)
        self.assertIn("VWAP", state["000001"]["invalid_reason"])

    def test_invalid_does_not_revive(self):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                       minute={"000001": fresh_min(vol=8000)})
        _, state = run(
            [inter_row(price=9.8, high=10.0, price_above_vwap=False, vwap=9.9,
                       flow_5m_inc=-2_000_000)],
            [], state, at(10, 2),
            minute={"000001": fresh_min(vol=4000, close=9.8, vwap=9.9)})
        rows, state = run([inter_row(price=10.0, high=10.0)], [], state, at(10, 4),
                          minute={"000001": fresh_min(vol=4000)})
        self.assertEqual(phase_of(state, "000001"), PHASE_INVALID)
        self.assertFalse(rows[0]["new_open_eligible"])

    def test_pullback_over_limit_invalidates(self):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                       minute={"000001": fresh_min(vol=8000)})
        _, state = run([inter_row(price=9.8, high=10.0)], [], state, at(10, 2),
                       minute={"000001": fresh_min(vol=4000, close=9.8, vwap=9.75)})
        self.assertEqual(phase_of(state, "000001"), PHASE_INVALID)
        self.assertIn("回撤超限", state["000001"]["invalid_reason"])


class PreIntersectionTests(unittest.TestCase):
    """需求1：准交集需连续确认；门槛失败只能观察。"""

    def test_pre_requires_consecutive_snapshots(self):
        rows, state = run([], [pre_row()], {}, at(10, 0), snap="P1")
        self.assertEqual(phase_of(state, "000002"), PHASE_OBSERVING)
        rows, state = run([], [pre_row()], state, at(10, 2), snap="P2")
        self.assertEqual(phase_of(state, "000002"), PHASE_PRE)
        self.assertEqual(rows[0]["intersection_phase"], "准交集")

    def test_gate_failure_stays_observing(self):
        failures = ["5分钟资金未为正(-800万)"]
        for i, t in enumerate([at(10, 0), at(10, 2), at(10, 4)]):
            rows, state = run([], [pre_row(eligible=False, failures=failures)],
                              {} if i == 0 else state, t, snap=f"G{i}")
            self.assertEqual(phase_of(state, "000002"), PHASE_OBSERVING)
        self.assertIn("5分钟资金未为正", rows[0]["gate_failure_text"])

    def test_pre_exit_records_reason(self):
        _, state = run([], [pre_row()], {}, at(10, 0), snap="P1")
        _, state = run([], [pre_row()], state, at(10, 2), snap="P2")
        failures = ["5分钟资金未为正(-500万)"]
        rows, state = run([], [pre_row(eligible=False, failures=failures)],
                          state, at(10, 4), snap="P3")
        self.assertEqual(phase_of(state, "000002"), PHASE_OBSERVING)
        self.assertIn("5分钟资金未为正", state["000002"]["pre_exit_reason"])


class ExpiryTests(unittest.TestCase):
    """锁存15分钟：掉出候选后窗口内保留，超期 EXPIRED。"""

    def test_latched_expires_after_window(self):
        _, state = run([inter_row(price=10.0, high=10.0)], [], {}, at(10, 0),
                       minute={"000001": fresh_min(vol=8000)})
        # 掉出候选，仍在窗口内 → 锁存保留
        rows, state = run([], [], state, at(10, 10))
        self.assertEqual(phase_of(state, "000001"), PHASE_LATCHED)
        self.assertTrue(any(r.get("latched_hold") for r in rows))
        # 超过15分钟 → EXPIRED
        _, state = run([], [], state, at(10, 20))
        self.assertEqual(phase_of(state, "000001"), PHASE_EXPIRED)


if __name__ == "__main__":
    unittest.main()
