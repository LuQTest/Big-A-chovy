import unittest
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
for p in (str(PROJECT_ROOT), str(TOOLS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from detect_divergence_leader import (
    evaluate_history,
    detect_day,
    D1_LOOKBACK,
)


def make_snap(i, price=40.0, pull=0.4, vwap_up=True, mainp=2.0, xl=+300.0,
              dom="✗", amt_wan=300000.0, reso=True, n_sec=2, ann="clean",
              mainp_override=None):
    return {
        "time": f"{9 + i // 12:02d}:{(30 + i * 5) % 60:02d}",
        "price": price,
        "pull": pull,
        "vwap_up": vwap_up,
        "mainp": mainp_override if mainp_override is not None else mainp,
        "xl": xl,
        "dom": dom,
        "amt": amt_wan,
        "reso": reso,
        "n_sec": n_sec,
        "ann": ann,
        "report_file": f"A股筛选结果_20260821_{i:04d}.md",
    }


def rising_snaps(mainp_seq, **kwargs):
    """按主力净占比升序构造一串合格快照，其余条件全部满足。"""
    return [make_snap(i, mainp=mp, **kwargs) for i, mp in enumerate(mainp_seq)]


class DivergenceEvaluateHistoryTests(unittest.TestCase):
    CODE, NAME, PLATE, DATE = "000426", "兴业银锡", "贵金属", "20260821"

    def test_xingye_style_scenario_b_triggers(self):
        """正例：主力占比创新高破5%+贴顶+共振，主导标签✗且比值不足 -> 场景B触发。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0, 6.8]
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq), self.DATE)
        self.assertIsNotNone(tg)
        self.assertEqual(tg["code"], self.CODE)
        self.assertIn("B比值不足", tg["scenario"])
        self.assertGreater(tg["mainp"], 5.0)

    def test_jushi_style_blocked_below_5pct(self):
        """反证锚：中国巨石式散户堆量（主力占比始终<5%）必须零触发。"""
        seq = [0.9, 1.6, 2.3, 3.0, 3.7, 4.4]
        tg = evaluate_history("600176", "中国巨石", "玻璃玻纤",
                              rising_snaps(seq), "20260807")
        self.assertIsNone(tg)

    def test_insufficient_history_blocked_then_allowed(self):
        """P1-4回归：快照数不足 LOOKBACK+1 时不得触发；达到后允许。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2]
        self.assertEqual(len(seq), D1_LOOKBACK)
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq), self.DATE)
        self.assertIsNone(tg)
        tg6 = evaluate_history(self.CODE, self.NAME, self.PLATE,
                               rising_snaps(seq + [6.0]), self.DATE)
        self.assertIsNotNone(tg6)

    def test_avoid_any_snapshot_is_stock_level_veto(self):
        """公告avoid当日任一快照出现即整股隔离，即使后续快照转clean。"""
        snaps = rising_snaps([2.0, 2.8, 3.6, 4.4, 5.2, 6.0])
        snaps[1]["ann"] = "avoid(资金占用)"
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE, snaps, self.DATE)
        self.assertIsNone(tg)

    def test_scenario_a_negative_superorder(self):
        """场景A：结构三条件全真但超大单为负 -> 否决旁路记录（分歧承接）。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0]
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq, xl=-8000.0), self.DATE)
        self.assertIsNotNone(tg)
        self.assertIn("A否决旁路", tg["scenario"])

    def test_formal_dominance_labels_never_enter_bypass_b(self):
        """语义修正回归：✓/✓(绝对)/✓(合力) 均为正式主导，只有显式 ✗ 进B场景。"""
        for label in ("✓", "✓(绝对)", "✓(合力)", ""):
            tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                                  rising_snaps([2.0, 2.8, 3.6, 4.4, 5.2, 6.0],
                                               dom=label),
                                  self.DATE)
            self.assertIsNone(tg, f"标签 {label!r} 不应进入旁路B")

    def test_first_trigger_only(self):
        """同股同日只记首个触发快照：返回的 trigger_time 是序列中最早合格点。"""
        seq = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0, 6.5]
        tg = evaluate_history(self.CODE, self.NAME, self.PLATE,
                              rising_snaps(seq), self.DATE)
        above5 = next(i for i, mp in enumerate(seq) if mp > 5.0)
        expected_idx = max(above5, D1_LOOKBACK)
        self.assertEqual(tg["trigger_time"], make_snap(expected_idx)["time"])

    def test_resonance_required(self):
        """无板块共振或板内候选不足时不得触发。"""
        base = [2.0, 2.8, 3.6, 4.4, 5.2, 6.0]
        self.assertIsNone(evaluate_history(
            self.CODE, self.NAME, self.PLATE,
            rising_snaps(base, reso=False), self.DATE))
        self.assertIsNone(evaluate_history(
            self.CODE, self.NAME, self.PLATE,
            rising_snaps(base, n_sec=1), self.DATE))


class DivergenceArchiveIntegrationTests(unittest.TestCase):
    ARCHIVE = PROJECT_ROOT / "筛选结果"

    def test_real_0821_positive_and_0807_anchor(self):
        """真实档案冒烟：0821 兴业银锡必须触发；0807 巨石反证日不得出现巨石。"""
        day0821 = self.ARCHIVE / "20260821"
        day0807 = self.ARCHIVE / "20260807"
        if not (day0821.exists() and day0807.exists()):
            self.skipTest("报告档案缺失，跳过集成冒烟")

        tgs21 = detect_day("20260821", verbose=False)
        codes21 = {t["code"] for t in tgs21}
        self.assertIn("000426", codes21)

        tgs07 = detect_day("20260807", verbose=False)
        codes07 = {t["code"] for t in tgs07}
        self.assertNotIn("600176", codes07)


if __name__ == "__main__":
    unittest.main()
