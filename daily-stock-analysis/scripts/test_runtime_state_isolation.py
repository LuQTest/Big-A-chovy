#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行状态隔离的回归测试（不联网、不启动服务）。

背景：2026-10-01 交易板范围验收期间的实跑覆盖了盘中运行状态文件
（资金快照 / 交集状态 / 观察池突破状态），盘中原始内容不可恢复。这些文件是
交集锁存、等待回踩与突破确认次数的载体，被覆盖后当天剩余快照的确认链会断裂。

因此这里锁住两件事：
1. 实跑验证可以把**所有**运行状态定向到临时目录（``A_SHARE_STATE_DIR``／
   ``A_SHARE_REPORT_DIR``），配置与输入数据不跟随；
2. 三个入口不再各自硬编码状态路径，避免有人绕开隔离机制。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import runtime_paths  # noqa: E402

STATE_FILES = (
    "flow_snapshot.json",
    "intersection_state.json",
    "watchlist_breakout_state.json",
    ".kline_cache.json",
    ".announcement_risk_cache.json",
    ".em_cooldown",
    "last_valid_result.json",
)
# 配置与输入：端到端验证必须继续用真实文件，否则验证的不是用户那套口径。
NOT_REDIRECTED = (
    "dashboard_settings.json",
    "holdings.json",
    "proxy_ports.json",
    "intersection_calibration.json",
)


class RuntimePathsTests(unittest.TestCase):
    def test_default_matches_historical_location(self):
        """不设环境变量时路径必须与历史行为逐字节一致。

        本套件按要求在隔离环境下运行（``A_SHARE_STATE_DIR`` 指向临时目录），所以
        不能在进程内断言默认值——那会把"隔离是否生效"和"默认路径是否正确"混在一起。
        这里用一个**不继承隔离变量**的子进程验证默认解析。
        """
        snippet = (
            "import runtime_paths, pathlib\n"
            "print(runtime_paths.state_dir())\n"
            "print(runtime_paths.state_file('x.json'))\n"
            "print(runtime_paths.report_dir(pathlib.Path('/proj/筛选结果')))\n"
        )
        env = {
            k: v for k, v in os.environ.items()
            if k not in (runtime_paths.STATE_DIR_ENV, runtime_paths.REPORT_DIR_ENV)
        }
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=str(SCRIPT_DIR), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(lines[0], str(SCRIPT_DIR))
        self.assertEqual(lines[1], str(SCRIPT_DIR / "x.json"))
        self.assertEqual(lines[2], str(Path("/proj/筛选结果")))

    def test_report_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get(runtime_paths.REPORT_DIR_ENV)
            os.environ[runtime_paths.REPORT_DIR_ENV] = tmp
            try:
                self.assertEqual(runtime_paths.report_dir(PROJECT_ROOT / "筛选结果"), Path(tmp))
            finally:
                if old is None:
                    os.environ.pop(runtime_paths.REPORT_DIR_ENV, None)
                else:
                    os.environ[runtime_paths.REPORT_DIR_ENV] = old

    def test_blank_override_keeps_default(self):
        with mock.patch.object(runtime_paths.os, "environ",
                               {runtime_paths.STATE_DIR_ENV: "   "}):
            self.assertIsNone(runtime_paths._override(runtime_paths.STATE_DIR_ENV))

    def test_unusable_state_dir_fails_loudly_instead_of_silently_polluting(self):
        """状态目录不可用时必须报错：静默回退到真实状态目录会让“隔离”变成假象。"""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            bad_paths = (blocker, Path("\0bad"))
            for bad in bad_paths:
                with self.subTest(state_dir=repr(bad)):
                    with mock.patch.object(runtime_paths, "STATE_DIR", bad):
                        with self.assertRaises(RuntimeError) as ctx:
                            runtime_paths.state_file("flow_snapshot.json")
                    self.assertIn(runtime_paths.STATE_DIR_ENV, str(ctx.exception))


class EntrypointIsolationTests(unittest.TestCase):
    """三个入口的状态路径必须都跟着 A_SHARE_STATE_DIR 走。

    环境变量在导入时读取，所以用子进程验证真实解析结果，而不是在进程内改环境。
    """

    def _resolved(self, tmp: str, snippet: str) -> str:
        env = dict(os.environ)
        env[runtime_paths.STATE_DIR_ENV] = tmp
        env[runtime_paths.REPORT_DIR_ENV] = tmp + "/reports"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=str(SCRIPT_DIR), env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return proc.stdout.strip()

    def test_all_entrypoints_follow_the_state_dir(self):
        snippet = (
            "import a_share_daily_screen as s, realtime_engine as e, realtime_dashboard as d, web_workbench as w\n"
            "print(s.FLOW_SNAPSHOT_PATH); print(s.INTERSECTION_STATE_FILE)\n"
            "print(s.WATCHLIST_BREAKOUT_STATE_PATH); print(s._EM_COOLDOWN_FILE)\n"
            "print(s.ANNOUNCEMENT_CACHE_FILE); print(e.KLINE_CACHE_FILE)\n"
            "print(d.LAST_VALID_RESULT_PATH); print(d.MD_OUTPUT_DIR); print(w.REPORTS_DIR)"
        )
        with tempfile.TemporaryDirectory() as tmp:
            lines = self._resolved(tmp, snippet).splitlines()
        self.assertEqual(len(lines), 9, lines)
        expected_state = {Path(tmp) / name for name in STATE_FILES}
        for line in lines[:7]:
            with self.subTest(path=line):
                self.assertIn(Path(line), expected_state)
        self.assertEqual(Path(lines[7]), Path(tmp) / "reports")
        self.assertEqual(Path(lines[8]), Path(tmp) / "reports")

    def test_reports_and_state_share_one_override(self):
        """看板与工作台的归档目录必须是同一个来源，否则验证产物会漏一处。"""
        workbench = (SCRIPT_DIR / "web_workbench.py").read_text(encoding="utf-8")
        self.assertIn("REPORTS_DIR = dash.MD_OUTPUT_DIR", workbench)


class NoHardcodedStatePathsTests(unittest.TestCase):
    """状态路径只能来自 runtime_paths；防止绕过隔离机制（含回退写法）。"""

    SOURCES = ("a_share_daily_screen.py", "realtime_engine.py", "realtime_dashboard.py")

    def test_state_files_are_not_hardcoded(self):
        for name in self.SOURCES:
            src = (SCRIPT_DIR / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("SCRIPT_DIR / \"flow_snapshot.json\"", src)
                self.assertNotIn("SCRIPT_DIR / \"intersection_state.json\"", src)
                self.assertNotIn("SCRIPT_DIR / \"watchlist_breakout_state.json\"", src)
                self.assertNotIn("SCRIPT_DIR / \".kline_cache.json\"", src)
                self.assertNotIn("SCRIPT_DIR / \"last_valid_result.json\"", src)

    def test_config_and_holdings_stay_in_place(self):
        """配置与持仓不跟随隔离变量，否则验证跑用的不是用户那套口径。"""
        src = (SCRIPT_DIR / "a_share_daily_screen.py").read_text(encoding="utf-8")
        self.assertIn('SCRIPT_DIR / "holdings.json"', src)
        settings = (SCRIPT_DIR / "dashboard_settings.py").read_text(encoding="utf-8")
        self.assertIn('SETTINGS_PATH = SCRIPT_DIR / "dashboard_settings.json"', settings)
        for name in NOT_REDIRECTED:
            with self.subTest(name=name):
                self.assertNotIn(f"state_file(\"{name}\")", src)


if __name__ == "__main__":
    unittest.main()
