import unittest
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from shadow_tracker import (
    collect_samples_from_report,
    fetch_t1_day_kline_extremes,
    find_next_trading_day_reports,
    calculate_t1_for_sample,
    generate_report,
)
from a_share_daily_screen import _parse_sina_kline
from scan_reports import evaluate_low_absorb_candidate


class ShadowTrackerTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_find_next_trading_day_reports_and_exact_0945_metrics(self):
        """测试精准定位次日报告目录，并在多快照(09:40, 09:45, 10:30, 15:00)中精确核算09:45收益与日内最大浮盈/回撤。"""
        # 创建 T 日与 T+1 日报告目录与文件
        day1_dir = os.path.join(self.test_dir, "20260821")
        day2_dir = os.path.join(self.test_dir, "20260824")
        os.makedirs(day1_dir, exist_ok=True)
        os.makedirs(day2_dir, exist_ok=True)

        f_t0 = os.path.join(day1_dir, "A股筛选结果_20260821_1455.md")
        # 构造次日多个不同时间点与价格的报告
        f_t1_0940 = os.path.join(day2_dir, "A股筛选结果_20260824_0940.md")
        f_t1_0945 = os.path.join(day2_dir, "A股筛选结果_20260824_0945.md")
        f_t1_1030 = os.path.join(day2_dir, "A股筛选结果_20260824_1030.md")
        f_t1_1500 = os.path.join(day2_dir, "A股筛选结果_20260824_1500.md")

        def make_report(filepath, time_str, price):
            with open(filepath, "w", encoding="utf-8") as fp:
                fp.write(f"数据时间：{time_str}，状态：运行。\n\n## 低吸超短线 A/B/C\n| 类 | 代码 | 名称 | 现价 | 涨幅 | 换手率 | 成交额 | 量比 | 板块 | 板块内候选 | 共振 | 高位回落 | 均价线 | 主力净占比 | 超大单 | 超单主导 | 5分钟增量 | 资金状态 | 风险 | 公告风险 |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n| A | 000603 | 盛达资源 | {price:.2f} | 4.0% | 5.0% | 15.00亿 | 1.5 | 贵金属 | 2 | 是 | 0.5pct | 上方 | 6.0% | +3500万 | ✓(合力) | +1200万 | 有效流入 | 无 | clean |\n")

        make_report(f_t0, "2026-08-21 14:55:00", 35.0)
        make_report(f_t1_0940, "2026-08-24 09:40:00", 35.70) # 09:40 价格 +2.0%
        make_report(f_t1_0945, "2026-08-24 09:45:00", 36.40) # 09:45 价格 +4.0% (应精准选取此价格)
        make_report(f_t1_1030, "2026-08-24 10:30:00", 37.80) # 10:30 日内最高价 +8.0%
        make_report(f_t1_1500, "2026-08-24 15:00:00", 34.30) # 15:00 日内最低价 -2.0% (跌破 35.0*0.985=34.475 -> 假突破)

        # 1. 验证查找 20260821 的下一个交易日报告（找到 4 份报告）
        next_reports = find_next_trading_day_reports(self.test_dir, "20260821")
        self.assertEqual(len(next_reports), 4)

        # 2. 真实端到端核算 T+1 表现
        sample = {
            "code": "000603",
            "name": "盛达资源",
            "trigger_price": 35.0,
        }
        with patch(
            "a_share_daily_screen.fetch_kline",
            return_value=([{"date": "2026-08-24", "high": 37.80, "low": 34.30}], "test"),
        ) as fetch_kline:
            res = calculate_t1_for_sample(sample, next_reports)
        self.assertIsNotNone(res)
        self.assertTrue(res["checked"])
        self.assertTrue(res["extremes_complete"])
        self.assertEqual(res["source"], "daily_kline")
        fetch_kline.assert_called_once_with("000603")
        # 精确选取 09:45 价格 (36.40 而非 09:40 的 35.70)
        self.assertEqual(res["t1_0945_price"], 36.40)
        self.assertAlmostEqual(res["t1_0945_return_pct"], 4.0, places=2)
        # 真实计算最大浮盈 (+8.0%)
        self.assertAlmostEqual(res["t1_max_gain_pct"], 8.0, places=2)
        # 真实计算最大回撤 (-2.0%)
        self.assertAlmostEqual(res["t1_max_drawdown_pct"], -2.0, places=2)
        # 跌破 34.475 判定为假突破
        self.assertTrue(res["is_false_breakout"])

    def test_missing_day_kline_never_completes_extreme_settlement(self):
        """日K失败或找不到目标交易日时，极值字段必须保持待补算。"""
        day_dir = os.path.join(self.test_dir, "20260824")
        os.makedirs(day_dir, exist_ok=True)
        report = os.path.join(day_dir, "A股筛选结果_20260824_0945.md")
        with open(report, "w", encoding="utf-8") as fp:
            fp.write(
                "## 低吸超短线 A/B/C\n"
                "| 类 | 代码 | 名称 | 现价 | 涨幅 | 换手率 | 成交额 | 量比 | 板块 | 板块内候选 | 共振 | 高位回落 | 均价线 | 主力净占比 | 超大单 | 超单主导 | 5分钟增量 | 资金状态 | 风险 | 公告风险 |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| A | 000603 | 盛达资源 | 36.40 | 4.0% | 5.0% | 15.00亿 | 1.5 | 贵金属 | 2 | 是 | 0.5pct | 上方 | 6.0% | +3500万 | ✓(合力) | +1200万 | 有效流入 | 无 | clean |\n"
            )

        sample = {"code": "000603", "trigger_price": 35.0}
        with patch(
            "a_share_daily_screen.fetch_kline",
            return_value=([{"date": "2026-08-23", "high": 99.0, "low": 1.0}], "test"),
        ):
            result = calculate_t1_for_sample(sample, [report])

        self.assertIsNotNone(result)
        self.assertFalse(result["checked"])
        self.assertFalse(result["extremes_complete"])
        self.assertEqual(result["source"], "report_snapshots_only")
        self.assertEqual(result["t1_0945_price"], 36.40)
        self.assertEqual(result["t1_max_gain_pct"], "待补算")
        self.assertEqual(result["t1_max_drawdown_pct"], "待补算")
        self.assertEqual(result["is_false_breakout"], "待补算")

    def test_sina_kline_parser_preserves_trade_date(self):
        """新浪备用日K必须保留日期，才能匹配指定T+1交易日。"""
        rows = _parse_sina_kline([{
            "day": "2026-08-24",
            "open": "35.0",
            "close": "36.0",
            "high": "37.0",
            "low": "34.0",
            "volume": "1000",
        }])

        self.assertEqual(rows[0]["date"], "2026-08-24")

    def test_fetch_t1_day_kline_extremes_uses_real_fetch_kline_tuple(self):
        """日K极值必须接通生产 fetch_kline() 的 (rows, source) 返回值。"""
        with patch(
            "a_share_daily_screen.fetch_kline",
            return_value=(
                [
                    {"date": "2026-08-23", "high": 99.0, "low": 1.0},
                    {"date": "2026-08-24", "high": 41.20, "low": 33.80},
                ],
                "eastmoney_qfq",
            ),
        ) as fetch_kline:
            extremes = fetch_t1_day_kline_extremes("000603", "20260824")

        self.assertEqual(extremes, (41.20, 33.80))
        fetch_kline.assert_called_once_with("000603")

    def test_coalition_collection_requires_explicit_strict_label(self):
        """数值门槛满足但报告标签为✗时，不得采集为合力样本。"""
        report = os.path.join(self.test_dir, "A股筛选结果_20260821_1002.md")
        with open(report, "w", encoding="utf-8") as fp:
            fp.write(
                "## 低吸超短线 A/B/C\n"
                "| 类 | 代码 | 名称 | 现价 | 涨幅 | 换手率 | 成交额 | 量比 | 板块 | 板块内候选 | 共振 | 高位回落 | 均价线 | 主力净占比 | 超大单 | 超单主导 | 5分钟增量 | 资金状态 | 风险 | 公告风险 |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| C | 000603 | 盛达资源 | 35.41 | 6.4% | 6.8% | 15.78亿 | 5.85 | 贵金属 | 1 | 是 | 1.47pct | 均价线上方 | 5.9% | +3294万 | ✗ | +2762万 | 有效流入 | 无 | clean |\n"
                "| C | 000605 | 裸标签样本 | 18.00 | 3.0% | 5.0% | 10.00亿 | 2.0 | 测试板块 | 1 | 是 | 0.5pct | 均价线上方 | 8.0% | +2500万 | 合力 | +1200万 | 有效流入 | 无 | clean |\n"
                "| A | 000604 | 严格样本 | 20.00 | 3.0% | 5.0% | 10.00亿 | 2.0 | 测试板块 | 1 | 是 | 0.5pct | 均价线上方 | 8.0% | +2500万 | ✓(合力) | +1200万 | 有效流入 | 无 | clean |\n"
            )

        db = {"samples": {"coalition": [], "breakout": [], "sector_boost": []}}
        added = collect_samples_from_report(report, db)

        self.assertEqual(added, 1)
        self.assertEqual([s["code"] for s in db["samples"]["coalition"]], ["000604"])

    def test_diagnostic_scanner_never_rederives_coalition_from_numbers(self):
        """诊断扫描器即使数值达标，也必须以生产报告严格标签为准。"""
        row = {
            "代码": "000603",
            "名称": "数值达标但无标签",
            "现价": "35.00",
            "涨幅": "3.0%",
            "板块": "测试板块",
            "板块内候选": "3",
            "共振": "是",
            "高位回落": "0.5pct",
            "均价线": "均价线上方",
            "主力净占比": "12.0%",
            "主力净额": "+6000万",
            "成交额": "10.00亿",
            "5分钟增量": "+1500万",
            "超大单": "+2500万",
            "超单主导": "✗",
            "公告风险": "clean",
        }

        result = evaluate_low_absorb_candidate(row)

        self.assertEqual(result["super_lead"], "✗")
        self.assertFalse(result["is_5_of_5"])
        self.assertIn("生产报告未给出严格超单主导标签", result["fails"])

    def test_report_does_not_mark_target_reached_until_all_samples_are_evaluated(self):
        """20个样本中仅1个完成日K结算时，报表不得提前显示验证达标。"""
        def sample(code, completed):
            if completed:
                result = {
                    "checked": True,
                    "extremes_complete": True,
                    "source": "daily_kline",
                    "t1_date": "20260824",
                    "t1_0945_price": 36.4,
                    "t1_0945_return_pct": 4.0,
                    "t1_max_gain_pct": 8.0,
                    "t1_max_drawdown_pct": -2.0,
                    "is_false_breakout": True,
                }
            else:
                result = {
                    "checked": False,
                    "extremes_complete": False,
                    "source": "report_snapshots_only",
                    "t1_date": "20260824",
                    "t1_0945_price": 36.4,
                    "t1_0945_return_pct": 4.0,
                    "t1_max_gain_pct": "待补算",
                    "t1_max_drawdown_pct": "待补算",
                    "is_false_breakout": "待补算",
                }
            return {
                "id": f"COAL_20260824_{code}",
                "code": code,
                "name": "测试样本",
                "date": "20260824",
                "trigger_time": "09:45",
                "trigger_price": 35.0,
                "plate": "测试板块",
                "super_wan": 2500.0,
                "main_net_wan": 8000.0,
                "super_ratio": 31.25,
                "t1_result": result,
            }

        samples = [sample("000001", True)] + [sample(f"000{index:03d}", False) for index in range(2, 21)]
        db = {
            "last_updated": "test",
            "targets": {"coalition": {"name": "合力主升主导", "target_samples": 20}},
            "samples": {"coalition": samples},
        }

        output = generate_report(db)

        self.assertIn("| **合力主升主导** | 20 | **20** |", output)
        self.assertIn("🟡 影子数据采集中", output)
        self.assertNotIn("🟢 验证达标", output)


if __name__ == "__main__":
    unittest.main()
