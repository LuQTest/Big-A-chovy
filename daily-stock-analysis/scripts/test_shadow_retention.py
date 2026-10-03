"""只在临时目录验证影子库增量扫描与结算，不访问真实样本和行情。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import shadow_tracker as tracker


class ShadowRetentionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.db_file = self.root / "db" / "shadow_samples.json"
        self.stack.enter_context(patch.object(tracker, "SHADOW_DATA_DIR", self.db_file.parent))
        self.stack.enter_context(patch.object(tracker, "SHADOW_DB_FILE", self.db_file))
        self.stack.enter_context(patch.object(tracker, "fetch_t1_day_kline_extremes", return_value=None))

    def report(self, date, time="0945", nested=False):
        folder = self.reports / date if nested else self.reports
        folder.mkdir(exist_ok=True)
        path = folder / f"A股筛选结果_{date}_{time}.md"
        path.write_text(
            "## 低吸超短线 A/B/C\n"
            "| 代码 | 名称 | 现价 | 超单主导 |\n|---|---|---|---|\n"
            "| 000001 | 人工样本 | 10.00 | ✓(合力) |\n\n"
            "## 明日观察池\n"
            "| 代码 | 名称 | 当前价 | 突破状态 |\n|---|---|---|---|\n"
            "| 000002 | 人工样本 | 20.00 | CONFIRMED |\n\n"
            "## 主力资金优选\n"
            "| 代码 | 名称 | 现价 | sector_boost |\n|---|---|---|---|\n"
            "| 000003 | 人工样本 | 30.00 | 15 |\n", encoding="utf-8")
        return str(path)

    def scan(self, date=None):
        with redirect_stdout(io.StringIO()):
            tracker.scan_and_update(date, reports_dir=str(self.reports))
        return json.loads(self.db_file.read_text(encoding="utf-8"))

    def test_all_history_accumulates_and_repeated_scans_are_idempotent(self):
        self.report("20260928", "1000")
        self.report("20260928", "0945")
        self.report("20260929", nested=True)
        # 同一快照出现在平铺与子目录时也不能重复入库。
        self.report("20260929")
        db = self.scan()
        for category in ("coalition", "breakout", "sector_boost"):
            self.assertEqual([s["date"] for s in db["samples"][category]], ["20260928", "20260929"])
            self.assertEqual(db["samples"][category][0]["trigger_time"], "09:45")
        self.assertEqual(self.scan()["samples"], db["samples"])

    def test_date_scan_retains_old_samples_results_targets_and_other_categories(self):
        self.report("20260928")
        db = self.scan()
        old = db["samples"]["coalition"][0]
        old["t1_result"] = {"checked": False, "note": "保留已有证据"}
        db["targets"]["coalition"]["target_samples"] = 23
        db["samples"]["external"] = [{"date": "20260928", "code": "000009",
                                          "trigger_price": 1, "t1_result": {"note": "旁路样本"}}]
        tracker.save_db(db)
        # 原报告归档移走时仍保留旧样本；新日期仅追加。
        for path in self.reports.iterdir():
            path.unlink()
        self.report("20260930", nested=True)
        updated = self.scan("20260930")
        for category in ("coalition", "breakout", "sector_boost"):
            self.assertEqual(len(updated["samples"][category]), 2)
            self.assertEqual(updated["samples"][category][0], db["samples"][category][0])
        self.assertEqual(updated["samples"]["external"], db["samples"]["external"])
        self.assertEqual(updated["targets"]["coalition"]["target_samples"], 23)

    def test_broken_database_is_not_overwritten(self):
        self.db_file.parent.mkdir()
        for content in ("{broken", "[]"):
            self.db_file.write_text(content)
            with self.assertRaises(ValueError):
                self.scan()
            self.assertEqual(self.db_file.read_text(), content)

    def test_failed_atomic_replace_keeps_previous_database(self):
        self.scan()
        original = self.db_file.read_bytes()
        with patch.object(tracker.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                tracker.save_db({"samples": {}})
        self.assertEqual(self.db_file.read_bytes(), original)
        self.assertEqual(list(self.db_file.parent.iterdir()), [self.db_file])

    def test_other_times_do_not_impersonate_0945_even_with_daily_extremes(self):
        for time in ("0944", "0946", "1400"):
            with self.subTest(time=time):
                path = self.report("20260929", time)
                with patch.object(tracker, "fetch_t1_day_kline_extremes", return_value=(11, 9)):
                    result = tracker.calculate_t1_for_sample({"code": "000001", "trigger_price": 10}, [path])
                self.assertFalse(result["checked"])
                self.assertTrue(result["extremes_complete"])
                self.assertIsNone(result["t1_0945_price"])
                self.assertIsNone(result["t1_0945_return_pct"])
                self.assertIsNone(result["t1_0945_time"])

    def test_verified_complete_result_survives_temporary_kline_failure(self):
        self.report("20260928")
        path = self.report("20260929", nested=True)
        sample = {"code": "000001", "date": "20260928", "trigger_price": 10}
        with patch.object(tracker, "fetch_t1_day_kline_extremes", return_value=(11, 9)):
            complete = tracker.calculate_t1_for_sample(sample, [path])
        self.assertTrue(complete["checked"])
        self.assertEqual(complete["t1_0945_time"], "09:45")
        sample["t1_result"] = complete
        tracker.update_all_t1_metrics({"samples": {"coalition": [sample]}}, str(self.reports))
        self.assertEqual(sample["t1_result"], complete)


if __name__ == "__main__":
    unittest.main()
