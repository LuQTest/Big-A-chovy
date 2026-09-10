import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
for path in (PROJECT_ROOT, PROJECT_ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rule_config import RULE_CONFIG, hhmm_to_minutes, is_complete_shadow_result, shadow_targets  # noqa: E402
from validate_consistency import (  # noqa: E402
    check_config,
    check_framework_progress,
    check_shadow_database,
    validate_workspace,
)


class RuleConsistencyTests(unittest.TestCase):
    def test_shared_config_has_strict_double_track_boundaries(self):
        result = check_config()
        self.assertFalse(result["fail"])
        self.assertEqual(len(shadow_targets()), 4)

    def test_shared_config_registers_execution_and_risk_policy(self):
        execution = RULE_CONFIG["execution"]
        self.assertIn("observe_only", execution["time_windows"])
        self.assertIn("fallback_window", execution)
        t1_window = execution["t1_exit_window"]
        self.assertLessEqual(
            hhmm_to_minutes(t1_window["start"]),
            hhmm_to_minutes(t1_window["target"]),
        )
        self.assertLessEqual(
            hhmm_to_minutes(t1_window["target"]),
            hhmm_to_minutes(t1_window["end"]),
        )

        risk = RULE_CONFIG["risk"]
        self.assertEqual(
            set(risk["statuses"]),
            {"clean", "watch_risk", "avoid", "unknown"},
        )
        self.assertTrue(risk["announcement"]["hard_keywords"])
        self.assertIn("600664", risk["hard_blacklist"])

    def test_complete_shadow_result_requires_daily_kline_and_all_metrics(self):
        incomplete = {
            "checked": True,
            "extremes_complete": True,
            "source": "report_snapshots_only",
            "t1_0945_price": 10.0,
            "t1_0945_return_pct": 1.0,
            "t1_max_gain_pct": 2.0,
            "t1_max_drawdown_pct": -1.0,
            "is_false_breakout": False,
        }
        self.assertFalse(is_complete_shadow_result(incomplete))

        complete = {**incomplete, "source": "daily_kline"}
        self.assertTrue(is_complete_shadow_result(complete))

    def test_checked_but_incomplete_sample_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow_samples.json"
            db = {
                "targets": shadow_targets(),
                "samples": {category: [] for category in shadow_targets()},
            }
            db["samples"]["coalition"] = [{
                "id": "COAL_TEST_000001",
                "code": "000001",
                "date": "20260827",
                "t1_result": {
                    "checked": True,
                    "extremes_complete": False,
                    "source": "report_snapshots_only",
                },
            }]
            path.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
            result = check_shadow_database(PROJECT_ROOT, db_path=path)
            self.assertTrue(result["fail"])
            self.assertIn("checked=true", result["fail"][0]["message"])

    def test_progress_uses_completed_results_not_collected_signals(self):
        db = {"targets": shadow_targets(), "samples": {key: [] for key in shadow_targets()}}
        db["samples"]["coalition"] = [{"t1_result": None} for _ in range(4)]
        text = (PROJECT_ROOT / "选股框架.md").read_text()
        # 用实际文档结构构造4条未结算信号，正确完成数为0。
        db["samples"]["divergence"] = [{"t1_result": None} for _ in range(4)]
        text = text.replace("已采集4；完整结算1/20", "已采集4；完整结算0/20")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "选股框架.md").write_text(text)
            self.assertFalse(check_framework_progress(root, db)["fail"])
            (root / "选股框架.md").write_text(text.replace("已采集4；完整结算0/20", "已采集4；完整结算4/20"))
            self.assertTrue(check_framework_progress(root, db)["fail"])

    def test_current_workspace_has_no_consistency_failures(self):
        result = validate_workspace(PROJECT_ROOT)
        self.assertFalse(result["fail"], result["fail"])


if __name__ == "__main__":
    unittest.main()
