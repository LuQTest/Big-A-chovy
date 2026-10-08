import contextlib
import io
import json
import multiprocessing
import queue
import time
import unittest
import os
import sys
import tempfile
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import tools.shadow_tracker as tracker
sys.modules["shadow_tracker"] = tracker
import detect_divergence_leader as divergence
from shadow_tracker import (
    collect_samples_from_report,
    fetch_t1_day_kline_extremes,
    find_next_trading_day_reports,
    pending_t1_label,
    calculate_t1_for_sample,
    generate_report,
    init_db,
    save_db,
    scan_and_update,
    update_all_t1_metrics,
)
from a_share_daily_screen import _parse_sina_kline
from scan_reports import evaluate_low_absorb_candidate
from tools.rule_config import is_complete_shadow_result, shadow_targets
from tools.validate_consistency import check_shadow_database


class _ThreadOnlyFileLock:
    """Permit synthetic single-process tests without claiming process safety."""
    LOCK_EX = 1
    LOCK_UN = 2

    @staticmethod
    def flock(_fd, _operation):
        return None


def _shadow_process_scan(shadow_dir, reports_dir, date_str, simulate_no_fcntl, barrier, output_queue):
    """Independent-process worker; inputs are synthetic reports in a temp directory."""
    os.environ["A_SHARE_SHADOW_DATA_DIR"] = shadow_dir
    process_tracker = tracker
    process_tracker.SHADOW_DATA_DIR = Path(shadow_dir)
    process_tracker.SHADOW_DB_FILE = process_tracker.SHADOW_DATA_DIR / "shadow_samples.json"
    if simulate_no_fcntl:
        process_tracker.fcntl = None
        process_tracker.msvcrt = None
        try:
            with process_tracker._database_lock():
                pass
        except process_tracker.ShadowDatabaseError as exc:
            output_queue.put(("refused", str(exc)))
            return

        # On the old implementation, both processes read the same empty snapshot
        # before either can replace the database. This makes the lost-update
        # regression deterministic instead of relying on scheduler timing.
        original_init_db = process_tracker.init_db

        def synchronized_init_db():
            db = original_init_db()
            barrier.wait(timeout=20)
            return db

        process_tracker.init_db = synchronized_init_db

    process_tracker.update_all_t1_metrics = lambda *_args, **_kwargs: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            process_tracker.scan_and_update(date_str, reports_dir=reports_dir)
    except BaseException as exc:
        output_queue.put(("error", repr(exc)))
    else:
        output_queue.put(("ok", ""))


def _shadow_lock_holder(shadow_dir, ready_event, release_event, output_queue, raise_inside=False):
    """Hold the real platform lock so a second process can probe exclusion."""
    os.environ["A_SHARE_SHADOW_DATA_DIR"] = shadow_dir
    process_tracker = tracker
    process_tracker.SHADOW_DATA_DIR = Path(shadow_dir)
    process_tracker.SHADOW_DB_FILE = process_tracker.SHADOW_DATA_DIR / "shadow_samples.json"
    try:
        with process_tracker._database_lock():
            output_queue.put(("entered", ""))
            ready_event.set()
            if raise_inside:
                raise RuntimeError("synthetic transaction failure")
            if not release_event.wait(20):
                raise TimeoutError("lock holder was not released")
    except RuntimeError as exc:
        if raise_inside:
            output_queue.put(("raised", str(exc)))
        else:
            output_queue.put(("error", repr(exc)))
    except BaseException as exc:
        output_queue.put(("error", repr(exc)))
    else:
        output_queue.put(("released", ""))


def _shadow_lock_contender(shadow_dir, output_queue):
    """Acquire and release the same platform lock in an independent process."""
    os.environ["A_SHARE_SHADOW_DATA_DIR"] = shadow_dir
    process_tracker = tracker
    process_tracker.SHADOW_DATA_DIR = Path(shadow_dir)
    process_tracker.SHADOW_DB_FILE = process_tracker.SHADOW_DATA_DIR / "shadow_samples.json"
    started = time.monotonic()
    try:
        with process_tracker._database_lock():
            output_queue.put(("entered", time.monotonic() - started))
    except BaseException as exc:
        output_queue.put(("error", repr(exc)))


class ShadowTrackerTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.old_shadow_dir = tracker.SHADOW_DATA_DIR
        self.old_shadow_file = tracker.SHADOW_DB_FILE
        self.old_fcntl = tracker.fcntl
        self.old_msvcrt = tracker.msvcrt
        if tracker.fcntl is None and not tracker._msvcrt_lock_available():
            # Existing unit cases use only one process; cross-process safety is
            # tested separately and production still refuses writes without a
            # genuine lock implementation.
            tracker.fcntl = _ThreadOnlyFileLock
        tracker.SHADOW_DATA_DIR = Path(self.test_dir) / "shadow_data"
        tracker.SHADOW_DB_FILE = tracker.SHADOW_DATA_DIR / "shadow_samples.json"

    def tearDown(self):
        tracker.SHADOW_DATA_DIR = self.old_shadow_dir
        tracker.SHADOW_DB_FILE = self.old_shadow_file
        tracker.fcntl = self.old_fcntl
        tracker.msvcrt = self.old_msvcrt
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_coalition_report(self, date_str, time_str, codes, *, folder=None, reordered=False):
        path_dir = Path(folder or self.test_dir)
        path_dir.mkdir(parents=True, exist_ok=True)
        path = path_dir / f"A股筛选结果_{date_str}_{time_str}.md"
        fields = {
            "类": "A", "代码": "", "名称": "合成样本", "现价": "10.00", "涨幅": "3.0%",
            "换手率": "5.0%", "成交额": "10.00亿", "量比": "2.0", "板块": "测试板块",
            "板块内候选": "3", "共振": "是", "高位回落": "0.5pct", "均价线": "均价线上方",
            "主力净占比": "8.0%", "主力净额": "+8000万", "超大单": "+2500万",
            "超单主导": "✓(合力)", "5分钟增量": "+1200万", "资金状态": "有效流入",
            "风险": "无", "公告风险": "clean",
        }
        headers = list(fields)
        if reordered:
            headers = ["超单主导", "名称", "代码", *[key for key in headers if key not in {"超单主导", "名称", "代码"}]]
        rows = []
        for code in codes:
            fields["代码"] = code
            rows.append("| " + " | ".join(fields[header] for header in headers) + " |")
        content = (
            f"数据时间：{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {time_str[:2]}:{time_str[2:]}:00，状态：运行。\n\n"
            "## 低吸超短线 A/B/C\n"
            "| " + " | ".join(headers) + " |\n"
            "|" + "|".join(["---"] * len(headers)) + "|\n"
            + "\n".join(rows) + "\n"
        )
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_pending_t1_date_is_dynamic_and_migrates_old_fixed_label(self):
        """待结算提示不得硬编码历史日期；已有旧标签也应在更新时迁移。"""
        with patch("shadow_tracker._verified_next_trading_date", return_value=None):
            self.assertEqual(pending_t1_label(self.test_dir, "20260826"), "待下一个交易日")

        source_day_dir = os.path.join(self.test_dir, "20260826")
        os.makedirs(source_day_dir, exist_ok=True)
        next_day_dir = os.path.join(self.test_dir, "20260827")
        os.makedirs(next_day_dir, exist_ok=True)
        next_report = os.path.join(next_day_dir, "A股筛选结果_20260827_0930.md")
        with open(next_report, "w", encoding="utf-8") as fp:
            fp.write("数据时间：2026-08-27 09:30:00，状态：运行。\n")

        with patch("shadow_tracker._verified_next_trading_date", return_value="20260827"):
            self.assertEqual(pending_t1_label(self.test_dir, "20260826"), "待下一个交易日(20260827)")

        db = {
            "samples": {
                "coalition": [{
                    "date": "20260826",
                    "code": "600909",
                    "trigger_price": 8.34,
                    "t1_result": {
                        "checked": False,
                        "t1_date": "待下一个交易日(8/24)",
                        "source": "unavailable",
                    },
                }],
            },
        }
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260827"), patch(
            "shadow_tracker.calculate_t1_for_sample", return_value=None
        ):
            update_all_t1_metrics(db, reports_dir=self.test_dir)
        self.assertEqual(db["samples"]["coalition"][0]["t1_result"]["t1_date"], "待下一个交易日(20260827)")
        self.assertNotIn("8/24", db["samples"]["coalition"][0]["t1_result"]["t1_date"])

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

        # 1. 只有日历确认的 T+1 日期报告可以作为次日样本。
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"):
            next_reports = find_next_trading_day_reports(self.test_dir, "20260821")
        self.assertEqual(len(next_reports), 4)

        # 2. 真实端到端核算 T+1 表现
        sample = {
            "code": "000603",
            "name": "盛达资源",
            "trigger_price": 35.0,
            "date": "20260821",
        }
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"), patch(
            "a_share_daily_screen.fetch_kline",
            return_value=([{"date": "2026-08-24", "high": 37.80, "low": 34.30}], "test"),
        ) as fetch_kline:
            res = calculate_t1_for_sample(sample, next_reports)
        self.assertIsNotNone(res)
        self.assertTrue(res["checked"])
        self.assertTrue(res["extremes_complete"])
        self.assertEqual(res["source"], "daily_kline")
        self.assertEqual(res["target_time"], "09:45")
        self.assertTrue(res["target_snapshot_found"])
        self.assertTrue(is_complete_shadow_result(res))
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

        sample = {"code": "000603", "trigger_price": 35.0, "date": "20260821"}
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"), patch(
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

    def test_missing_exact_target_snapshot_never_uses_neighboring_times(self):
        reports = [
            self._write_coalition_report("20260824", time_str, ["000603"])
            for time_str in ("0944", "0946", "1400")
        ]
        sample = {"code": "000603", "date": "20260821", "trigger_price": 10.0}
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"), patch(
            "shadow_tracker.fetch_t1_day_kline_extremes", return_value=(12.0, 9.0)
        ):
            result = calculate_t1_for_sample(sample, reports)

        self.assertIsNotNone(result)
        self.assertFalse(result["checked"])
        self.assertTrue(result["extremes_complete"])
        self.assertFalse(result["target_snapshot_found"])
        self.assertIsNone(result["t1_0945_price"])
        self.assertIsNone(result["t1_0945_return_pct"])
        self.assertFalse(is_complete_shadow_result(result))

    def test_t_plus_two_report_is_not_substituted_when_t_plus_one_is_missing(self):
        self._write_coalition_report("20260825", "0945", ["000603"], folder=self.test_dir)
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"):
            self.assertEqual(find_next_trading_day_reports(self.test_dir, "20260821"), [])
            self.assertEqual(pending_t1_label(self.test_dir, "20260821"), "待下一个交易日")

    def test_t_plus_one_lookup_does_not_require_trigger_report_archive(self):
        target = self._write_coalition_report("20260824", "0945", ["000603"], folder=self.test_dir)
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"):
            self.assertEqual(find_next_trading_day_reports(self.test_dir, "20260821"), [target])

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

    def test_reordered_report_headers_are_parsed_by_name(self):
        report = self._write_coalition_report("20260821", "1002", ["000604"], reordered=True)
        db = {"samples": {"coalition": [], "breakout": [], "sector_boost": []}}
        self.assertEqual(collect_samples_from_report(report, db), 1)
        sample = db["samples"]["coalition"][0]
        self.assertEqual(sample["code"], "000604")
        self.assertEqual(sample["super_ratio"], 31.2)

    def test_scans_accumulate_history_and_date_scope_only_adds_selected_reports(self):
        db = init_db()
        settled = {
            "id": "COAL_20260801_600099", "code": "600099", "name": "first touch",
            "date": "20260801", "trigger_time": "09:30", "trigger_price": 10.0,
            "plate": "旧板块", "super_wan": 2500.0, "main_net_wan": 8000.0,
            "super_ratio": 31.25, "inc5_wan": 1200.0,
            "t1_result": {
                "checked": True, "extremes_complete": True, "source": "daily_kline",
                "t1_date": "20260803", "target_time": "09:45", "target_snapshot_found": True,
                "t1_date_verified": True,
                "t1_0945_price": 10.2, "t1_0945_return_pct": 2.0,
                "t1_max_gain_pct": 4.0, "t1_max_drawdown_pct": -1.0,
                "is_false_breakout": False,
            },
        }
        db["samples"]["coalition"].append(settled)
        divergence = {"id": "DIV-1", "code": "600088", "date": "20260801", "scenario": "分歧", "t1_result": {"legacy": "keep"}}
        db["samples"]["divergence"].append(divergence)
        db["targets"]["custom_external"] = {"name": "外部自定义机制", "target_samples": 7}
        custom = {"id": "CUSTOM-1", "code": "600077", "date": "20260801", "first_touch": "retain"}
        db["samples"]["custom_external"] = [custom]
        save_db(db)

        self._write_coalition_report("20260821", "1000", ["600001"], folder=self.test_dir)
        day_folder = Path(self.test_dir) / "20260824"
        self._write_coalition_report("20260824", "1000", ["600002"], folder=day_folder)
        self._write_coalition_report("20260824", "1010", ["600002"], folder=day_folder)
        self._write_coalition_report("20260825", "1000", ["600003"], folder=self.test_dir)

        with patch.object(tracker, "update_all_t1_metrics"), contextlib.redirect_stdout(io.StringIO()):
            scan_and_update("20260824", reports_dir=self.test_dir)
        scoped = init_db()
        scoped_codes = {row["code"] for row in scoped["samples"]["coalition"]}
        self.assertEqual(scoped_codes, {"600099", "600002"})

        with patch.object(tracker, "update_all_t1_metrics"), contextlib.redirect_stdout(io.StringIO()):
            scan_and_update(reports_dir=self.test_dir)
        accumulated = init_db()
        core = accumulated["samples"]["coalition"]
        self.assertEqual({row["code"] for row in core}, {"600099", "600001", "600002", "600003"})
        self.assertEqual(sum(row["code"] == "600002" for row in core), 1)
        self.assertEqual(next(row for row in core if row["code"] == "600099"), settled)
        self.assertEqual(accumulated["samples"]["divergence"], [divergence])
        self.assertEqual(accumulated["targets"]["custom_external"], {"name": "外部自定义机制", "target_samples": 7})
        self.assertEqual(accumulated["samples"]["custom_external"], [custom])

    def test_concurrent_date_scans_do_not_lose_each_others_additions(self):
        self._write_coalition_report("20260821", "1000", ["600001"], folder=self.test_dir)
        self._write_coalition_report("20260824", "1000", ["600002"], folder=Path(self.test_dir) / "20260824")
        with patch.object(tracker, "update_all_t1_metrics"), contextlib.redirect_stdout(io.StringIO()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(scan_and_update, date, self.test_dir)
                    for date in ("20260821", "20260824")
                ]
                for future in futures:
                    future.result(timeout=10)
        db = init_db()
        self.assertEqual({row["code"] for row in db["samples"]["coalition"]}, {"600001", "600002"})

    def _divergence_trigger(self, code="000012", date="20260821"):
        return {
            "code": code, "name": f"合成{code}", "date": date,
            "trigger_time": "09:35", "trigger_price": 10.0, "plate": "测试板块",
            "mainp": 6.0, "xl": 2500.0, "pull": 1.2, "scenario": "A",
        }

    def test_scan_and_divergence_record_interleave_preserves_both_categories(self):
        self._write_coalition_report("20260821", "1000", ["000011"], folder=self.test_dir)
        snapshot_taken = threading.Event()
        scan_finished = threading.Event()
        original_init_db = tracker.init_db

        def pause_after_stale_snapshot():
            db = original_init_db()
            snapshot_taken.set()
            if not scan_finished.wait(10):
                raise TimeoutError("synthetic interleave did not release divergence record")
            return db

        errors = []

        def record_divergence():
            try:
                divergence.record([self._divergence_trigger()])
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=record_divergence)
        with patch.object(divergence, "init_db", side_effect=pause_after_stale_snapshot, create=True):
            worker.start()
            # The legacy implementation reads outside the write lock and pauses
            # here. The transactional implementation does not use that entrypoint
            # and may commit first; either serialized order must retain both rows.
            snapshot_taken.wait(1.0)
            with patch.object(tracker, "update_all_t1_metrics"), contextlib.redirect_stdout(io.StringIO()):
                scan_and_update("20260821", reports_dir=self.test_dir)
            scan_finished.set()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive(), "divergence writer did not finish")
        self.assertEqual(errors, [])
        db = init_db()
        self.assertEqual({row["code"] for row in db["samples"]["coalition"]}, {"000011"})
        self.assertEqual({row["code"] for row in db["samples"]["divergence"]}, {"000012"})

    def test_divergence_record_is_idempotent_and_preserves_other_categories(self):
        db = init_db()
        db["targets"]["custom_external"] = {"name": "外部自定义机制", "target_samples": 7}
        db["samples"]["custom_external"] = [{"id": "C-1", "code": "000088", "date": "20260820", "first_touch": "retain"}]
        db["samples"]["coalition"].append({"id": "COAL-1", "code": "000077", "date": "20260820", "first_touch": "retain", "t1_result": None})
        save_db(db)

        trigger = self._divergence_trigger()
        with contextlib.redirect_stdout(io.StringIO()):
            divergence.record([trigger])
            divergence.record([trigger])

        updated = init_db()
        self.assertEqual(len(updated["samples"]["divergence"]), 1)
        self.assertEqual(updated["samples"]["divergence"][0]["code"], "000012")
        self.assertEqual(updated["samples"]["coalition"][0]["first_touch"], "retain")
        self.assertEqual(updated["samples"]["custom_external"][0]["first_touch"], "retain")
        self.assertEqual(updated["targets"]["custom_external"], {"name": "外部自定义机制", "target_samples": 7})

    def test_save_db_rejects_stale_snapshot_without_replacing_newer_history(self):
        original = init_db()
        save_db(original)
        stale = init_db()

        current = init_db()
        current["targets"]["custom_external"] = {"name": "外部自定义机制", "target_samples": 7}
        current["samples"]["custom_external"] = [{"id": "C-1", "code": "000088", "date": "20260820", "first_touch": "retain"}]
        save_db(current)

        stale["samples"]["divergence"].append({"id": "DIV-OLD", "code": "000012", "date": "20260821", "t1_result": None})
        with self.assertRaises(tracker.ShadowDatabaseConflict):
            save_db(stale)
        after = init_db()
        self.assertEqual(after["samples"]["custom_external"], current["samples"]["custom_external"])
        self.assertEqual(after["targets"]["custom_external"], current["targets"]["custom_external"])
        self.assertEqual(after["samples"]["divergence"], [])

    def _run_process_scans(self, *, simulate_no_fcntl):
        self._write_coalition_report("20260821", "1000", ["000011"], folder=self.test_dir)
        self._write_coalition_report("20260824", "1000", ["000012"], folder=self.test_dir)
        context = multiprocessing.get_context("spawn")
        output_queue = context.Queue()
        barrier = context.Barrier(2) if simulate_no_fcntl else None
        processes = [
            context.Process(
                target=_shadow_process_scan,
                args=(str(tracker.SHADOW_DATA_DIR), self.test_dir, date_str, simulate_no_fcntl, barrier, output_queue),
            )
            for date_str in ("20260821", "20260824")
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                self.fail("shadow scan subprocess did not exit")
        outcomes = [output_queue.get(timeout=5) for _ in processes]
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        output_queue.close()
        output_queue.join_thread()
        return outcomes

    @unittest.skipUnless(
        tracker.fcntl is not None or tracker._msvcrt_lock_available(),
        "platform has no real cross-process lock",
    )
    def test_independent_process_scans_preserve_both_dates(self):
        outcomes = self._run_process_scans(simulate_no_fcntl=False)
        self.assertEqual(outcomes, [("ok", ""), ("ok", "")])
        db = init_db()
        self.assertEqual({row["code"] for row in db["samples"]["coalition"]}, {"000011", "000012"})

    @unittest.skipUnless(
        tracker.fcntl is not None or tracker._msvcrt_lock_available(),
        "platform has no real cross-process lock",
    )
    def test_cross_process_lock_excludes_contender_and_releases_after_exception(self):
        context = multiprocessing.get_context("spawn")
        holder_ready = context.Event()
        holder_release = context.Event()
        holder_output = context.Queue()
        contender_output = context.Queue()
        holder = context.Process(
            target=_shadow_lock_holder,
            args=(str(tracker.SHADOW_DATA_DIR), holder_ready, holder_release, holder_output),
        )
        contender = context.Process(
            target=_shadow_lock_contender,
            args=(str(tracker.SHADOW_DATA_DIR), contender_output),
        )
        try:
            holder.start()
            self.assertTrue(holder_ready.wait(20), "lock holder did not enter")
            self.assertEqual(holder_output.get(timeout=5)[0], "entered")
            contender.start()
            with self.assertRaises(queue.Empty):
                contender_output.get(timeout=1.0)

            holder_release.set()
            self.assertEqual(contender_output.get(timeout=20)[0], "entered")
            holder.join(timeout=20)
            contender.join(timeout=20)
            self.assertFalse(holder.is_alive())
            self.assertFalse(contender.is_alive())
            self.assertEqual(holder.exitcode, 0)
            self.assertEqual(contender.exitcode, 0)
        finally:
            holder_release.set()
            for process in (holder, contender):
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

        raised_ready = context.Event()
        raised_release = context.Event()
        raised_output = context.Queue()
        raiser = context.Process(
            target=_shadow_lock_holder,
            args=(str(tracker.SHADOW_DATA_DIR), raised_ready, raised_release, raised_output, True),
        )
        after_exception_output = context.Queue()
        after_exception = context.Process(
            target=_shadow_lock_contender,
            args=(str(tracker.SHADOW_DATA_DIR), after_exception_output),
        )
        raiser.start()
        try:
            self.assertTrue(raised_ready.wait(20), "exception holder did not enter")
            self.assertEqual(raised_output.get(timeout=5)[0], "entered")
            raiser.join(timeout=20)
            self.assertFalse(raiser.is_alive())
            self.assertEqual(raiser.exitcode, 0)
            self.assertEqual(raised_output.get(timeout=5)[0], "raised")

            after_exception.start()
            self.assertEqual(after_exception_output.get(timeout=20)[0], "entered")
            after_exception.join(timeout=20)
            self.assertFalse(after_exception.is_alive())
            self.assertEqual(after_exception.exitcode, 0)
        finally:
            if raiser.is_alive():
                raiser.terminate()
                raiser.join(timeout=5)
            if after_exception.is_alive():
                after_exception.terminate()
                after_exception.join(timeout=5)

    def test_no_cross_process_lock_processes_refuse_unsafe_writes(self):
        outcomes = self._run_process_scans(simulate_no_fcntl=True)
        if all(kind == "refused" for kind, _detail in outcomes):
            self.assertTrue(all("跨进程" in detail or "拒绝" in detail for _, detail in outcomes))
            self.assertFalse(tracker.SHADOW_DB_FILE.exists())
            return

        # Implementations with another genuine cross-process primitive may
        # continue; silent success with only one date is never acceptable.
        self.assertEqual(outcomes, [("ok", ""), ("ok", "")])
        db = init_db()
        self.assertEqual({row["code"] for row in db["samples"]["coalition"]}, {"000011", "000012"})

    def test_windows_lock_acquisition_failure_refuses_write(self):
        class FailingMSVCRT:
            LK_LOCK = 1
            LK_UNLCK = 0

            @staticmethod
            def locking(_fd, _mode, _nbytes):
                raise OSError("synthetic Windows lock failure")

        tracker.fcntl = None
        tracker.msvcrt = FailingMSVCRT
        with self.assertRaises(tracker.ShadowDatabaseError) as context:
            with tracker._database_lock():
                self.fail("unreachable")
        self.assertIn("Windows", str(context.exception))
        self.assertFalse(tracker.SHADOW_DB_FILE.exists())

    def test_windows_lock_release_failure_refuses_normal_exit(self):
        class UnlockFailingMSVCRT:
            LK_LOCK = 1
            LK_UNLCK = 0
            calls = 0

            @classmethod
            def locking(cls, _fd, mode, _nbytes):
                cls.calls += 1
                if mode == cls.LK_UNLCK:
                    raise OSError("synthetic Windows unlock failure")

        tracker.fcntl = None
        tracker.msvcrt = UnlockFailingMSVCRT
        with self.assertRaises(tracker.ShadowDatabaseError) as context:
            with tracker._database_lock():
                pass
        self.assertIn("释放", str(context.exception))
        self.assertEqual(UnlockFailingMSVCRT.calls, 2)

    def test_malformed_database_and_atomic_write_failure_preserve_existing_file(self):
        tracker.SHADOW_DATA_DIR.mkdir(parents=True, exist_ok=True)
        tracker.SHADOW_DB_FILE.write_text("{malformed", encoding="utf-8")
        malformed = tracker.SHADOW_DB_FILE.read_text(encoding="utf-8")
        with self.assertRaises(tracker.ShadowDatabaseError):
            init_db()
        self.assertEqual(tracker.SHADOW_DB_FILE.read_text(encoding="utf-8"), malformed)

        bad_container = json.dumps({"targets": {}, "samples": []})
        tracker.SHADOW_DB_FILE.write_text(bad_container, encoding="utf-8")
        with self.assertRaises(tracker.ShadowDatabaseError):
            init_db()
        self.assertEqual(tracker.SHADOW_DB_FILE.read_text(encoding="utf-8"), bad_container)

        tracker.SHADOW_DB_FILE.unlink()
        db = init_db()
        save_db(db)
        before = tracker.SHADOW_DB_FILE.read_text(encoding="utf-8")
        db["samples"]["coalition"].append({"id": "new", "code": "600001", "date": "20260821"})
        with patch("shadow_tracker.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                save_db(db)
        self.assertEqual(tracker.SHADOW_DB_FILE.read_text(encoding="utf-8"), before)
        self.assertEqual(list(tracker.SHADOW_DATA_DIR.glob(".*.tmp")), [])

    def test_legacy_checked_daily_kline_without_target_time_is_quarantined(self):
        report = self._write_coalition_report("20260824", "0944", ["000603"])
        legacy = {
            "checked": True, "extremes_complete": True, "source": "daily_kline",
            "t1_date": "20260824", "t1_0945_price": 10.2, "t1_0945_return_pct": 2.0,
            "t1_max_gain_pct": 4.0, "t1_max_drawdown_pct": -1.0, "is_false_breakout": False,
        }
        db = init_db()
        db["samples"]["coalition"].append({
            "id": "COAL_20260821_000603", "code": "000603", "name": "legacy",
            "date": "20260821", "trigger_time": "10:00", "trigger_price": 10.0,
            "plate": "测试", "super_wan": 2500.0, "main_net_wan": 8000.0,
            "super_ratio": 31.25, "t1_result": dict(legacy),
        })
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"), patch(
            "shadow_tracker.find_next_trading_day_reports", return_value=[report]
        ), patch("shadow_tracker.fetch_t1_day_kline_extremes", return_value=None):
            update_all_t1_metrics(db, reports_dir=self.test_dir)

        result = db["samples"]["coalition"][0]["t1_result"]
        self.assertFalse(result["checked"])
        self.assertFalse(result["extremes_complete"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["legacy_evidence"], legacy)
        self.assertIsNone(result["t1_0945_price"])
        self.assertIsNone(result["t1_0945_return_pct"])
        self.assertFalse(is_complete_shadow_result(result))
        self.assertIn("旧结算缺少目标时点", generate_report(db))

        save_db(db)
        validation = check_shadow_database(db_path=tracker.SHADOW_DB_FILE)
        self.assertTrue(any("完整结算 0" in issue["message"] for issue in validation["pass"]))

    def test_verified_complete_settlement_survives_later_source_failure(self):
        report = self._write_coalition_report("20260824", "0945", ["000603"])
        verified = {
            "checked": True, "extremes_complete": True, "source": "daily_kline",
            "t1_date": "20260824", "target_time": "09:45", "target_snapshot_found": True,
            "t1_date_verified": True,
            "t1_0945_price": 10.2, "t1_0945_return_pct": 2.0,
            "t1_max_gain_pct": 4.0, "t1_max_drawdown_pct": -1.0,
            "is_false_breakout": False,
        }
        db = init_db()
        db["samples"]["coalition"].append({
            "id": "COAL_20260821_000603", "code": "000603", "name": "verified",
            "date": "20260821", "trigger_time": "10:00", "trigger_price": 10.0,
            "plate": "测试", "super_wan": 2500.0, "main_net_wan": 8000.0,
            "super_ratio": 31.25, "t1_result": dict(verified),
        })
        with patch("shadow_tracker._verified_next_trading_date", return_value="20260824"), patch(
            "shadow_tracker.find_next_trading_day_reports", return_value=[report]
        ), patch("shadow_tracker.fetch_t1_day_kline_extremes", return_value=None):
            update_all_t1_metrics(db, reports_dir=self.test_dir)
        self.assertEqual(db["samples"]["coalition"][0]["t1_result"], verified)

    def test_twenty_legacy_results_without_target_time_do_not_reach_threshold(self):
        db = init_db()
        for index in range(20):
            db["samples"]["coalition"].append({
                "id": f"LEGACY-{index}", "code": f"60{index:04d}", "name": "旧样本",
                "date": "20260821", "trigger_time": "10:00", "trigger_price": 10.0,
                "plate": "测试", "super_wan": 2500.0, "main_net_wan": 8000.0,
                "super_ratio": 31.25,
                "t1_result": {
                    "checked": True, "extremes_complete": True, "source": "daily_kline",
                    "t1_date": "20260824", "t1_0945_price": 10.2, "t1_0945_return_pct": 2.0,
                    "t1_max_gain_pct": 4.0, "t1_max_drawdown_pct": -1.0,
                    "is_false_breakout": False,
                },
            })
        output = generate_report(db)
        self.assertIn("| **合力主升主导** | 20 | **20** |", output)
        self.assertIn("0.0% (待补精确09:45快照)", output)
        self.assertIn("🟡 影子数据采集中", output)
        self.assertNotIn("🟢 验证达标", output)
        self.assertFalse(is_complete_shadow_result(db["samples"]["coalition"][0]["t1_result"]))
        save_db(db)
        validation = check_shadow_database(db_path=tracker.SHADOW_DB_FILE)
        self.assertTrue(any("完整结算 0" in issue["message"] for issue in validation["pass"]))

    def test_report_does_not_mark_target_reached_until_all_samples_are_evaluated(self):
        """20个样本中仅1个完成日K结算时，报表不得提前显示验证达标。"""
        def sample(code, completed):
            if completed:
                result = {
                    "checked": True,
                    "extremes_complete": True,
                    "source": "daily_kline",
                    "t1_date": "20260824",
                    "target_time": "09:45",
                    "target_snapshot_found": True,
                    "t1_date_verified": True,
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
