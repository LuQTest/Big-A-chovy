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

    def test_screening_thresholds_have_one_shared_source_and_experiment_is_closed(self):
        screening = RULE_CONFIG["screening"]
        for section in (
            "resonance",
            "strict_ultra",
            "strict_trend",
            "trend_observation",
            "low_ultra",
            "low_trend",
            "watchlist",
            "capital_rank",
        ):
            self.assertIsInstance(screening[section], dict)
        experiment = screening["low_absorb"]["experimental_retest_gate"]
        self.assertFalse(experiment["enabled"])
        self.assertEqual(experiment["permission"], "simulated_only")
        self.assertEqual(screening["watchlist"]["score_dist60_scale"], 1.0)
        self.assertEqual(RULE_CONFIG["realtime"]["entry_exit"]["take_profit_pct"], 2.0)
        self.assertEqual(RULE_CONFIG["realtime"]["entry_exit"]["stop_loss_pct"], 3.0)

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
            "t1_date": "20260824",
            "target_time": "09:45",
            "target_snapshot_found": True,
            "t1_date_verified": True,
        }
        self.assertFalse(is_complete_shadow_result(incomplete))

        complete = {**incomplete, "source": "daily_kline"}
        self.assertTrue(is_complete_shadow_result(complete))
        self.assertFalse(is_complete_shadow_result({key: value for key, value in complete.items() if key != "target_time"}))
        self.assertFalse(is_complete_shadow_result({**complete, "target_time": "09:44"}))
        self.assertFalse(is_complete_shadow_result({**complete, "t1_date_verified": False}))

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

    def test_custom_shadow_category_is_retained_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow_samples.json"
            db = {
                "targets": {**shadow_targets(), "custom_external": {"name": "外部记录", "target_samples": 7}},
                "samples": {**{category: [] for category in shadow_targets()}, "custom_external": [{"id": "EXT-1", "t1_result": None}]},
            }
            path.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
            result = check_shadow_database(PROJECT_ROOT, db_path=path)
            self.assertFalse(result["fail"])
            self.assertTrue(any("custom_external 结构有效" in issue["message"] for issue in result["pass"]))

    def _framework_text(self) -> str:
        return (PROJECT_ROOT / "选股框架.md").read_text(encoding="utf-8")

    def _db(self, **samples_by_category) -> dict:
        db = {"targets": shadow_targets(), "samples": {key: [] for key in shadow_targets()}}
        for category, rows in samples_by_category.items():
            db["samples"][category] = rows
        return db

    def _progress(self, framework_text: str, db: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "选股框架.md").write_text(framework_text, encoding="utf-8")
            return check_framework_progress(root, db)

    def _pass_text(self, result: dict) -> str:
        return "；".join(issue["message"] for issue in result["pass"])

    def _fail_text(self, result: dict) -> str:
        return "；".join(issue["message"] for issue in result["fail"])

    def test_progress_is_read_from_local_shadow_db_not_the_framework(self):
        """进度只在本地影子库：未结算信号不得计入 x/20，框架本身不需要写进度。"""
        db = self._db(
            coalition=[{"t1_result": None} for _ in range(4)],
            divergence=[{"t1_result": None} for _ in range(4)],
        )
        result = self._progress(self._framework_text(), db)
        self.assertFalse(result["fail"], result["fail"])
        passes = self._pass_text(result)
        self.assertIn("coalition 实际进度（本地影子库）：完整结算 0/20；已采集 4", passes)
        self.assertIn("divergence 实际进度（本地影子库）：完整结算 0/20；已采集 4", passes)

    def test_checked_but_unsettled_sample_still_counts_zero_progress(self):
        """checked=true 但未达完整结算口径的样本只能算“已采集”，不能算完成。"""
        db = self._db(coalition=[{"t1_result": {
            "checked": True, "extremes_complete": False, "source": "report_snapshots_only",
        }}])
        result = self._progress(self._framework_text(), db)
        self.assertIn("coalition 实际进度（本地影子库）：完整结算 0/20；已采集 1", self._pass_text(result))

    def test_framework_must_not_publish_local_progress_numbers(self):
        """把本地进度写回公开框架应判 FAIL——框架只列目标与方法。"""
        leaked = self._framework_text() + "\n- ⑥合力主升主导验证：已采集4；完整结算1/20\n"
        result = self._progress(leaked, self._db())
        self.assertIn("本地进度数字", self._fail_text(result))

    def test_framework_must_keep_target_method_and_permission_statements(self):
        base = self._framework_text()
        dropped_item = "".join(
            line for line in base.splitlines(keepends=True) if "⑦观察池突破状态机" not in line
        )
        wrong_target = base.replace("| ⑦观察池突破状态机 | 20个完整结算样本 |",
                                    "| ⑦观察池突破状态机 | 15个完整结算样本 |")
        no_local_only = base.replace("只在本地影子库维护", "由报告维护")
        cases = (
            (dropped_item, "缺少待验证项：breakout"),
            (wrong_target, "breakout 未声明验证目标"),
            (no_local_only, "进度只在本地影子库维护"),
        )
        for text, needle in cases:
            with self.subTest(needle=needle):
                self.assertNotEqual(text, base)
                result = self._progress(text, self._db())
                self.assertIn(needle, self._fail_text(result))

    def test_shadow_db_target_must_match_shared_config(self):
        db = self._db()
        db["targets"]["breakout"]["target_samples"] = 15
        result = self._progress(self._framework_text(), db)
        self.assertIn("影子库目标值为 15", self._fail_text(result))

    def test_missing_shadow_db_warns_instead_of_failing(self):
        """影子库还没建立时只能 WARN：不能因为本地没数据把一致性检查判死。"""
        result = self._progress(self._framework_text(), None)
        self.assertFalse(result["fail"], result["fail"])
        self.assertTrue(result["warn"])

    def test_current_workspace_has_no_consistency_failures(self):
        result = validate_workspace(PROJECT_ROOT)
        self.assertFalse(result["fail"], result["fail"])


if __name__ == "__main__":
    unittest.main()
