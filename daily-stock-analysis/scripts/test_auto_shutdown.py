"""Regression tests for the dashboard's 15:15 auto-shutdown wiring.

Historical defect: web_workbench.py never registered its HTTP server with the
dashboard scheduler, so `_shutdown_server()` hit `_server is None` and did
nothing. The scheduler thread exited silently while the main thread stayed
blocked in serve_forever(), leaving an orphan process holding port 8765 until
the next launch killed it via _kill_stale_port_windows().

These tests pin the fix: the shutdown hook must be honoured, and a real server
must actually stop serving when it fires.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_dashboard():
    """Import realtime_dashboard by path (its directory is not a package)."""
    path = SCRIPT_DIR / "realtime_dashboard.py"
    spec = importlib.util.spec_from_file_location("realtime_dashboard_autoshutdown", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # keep test output clean
        return


class AutoShutdownHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dash = _load_dashboard()

    def _scheduler(self, hook=None):
        scheduler = self.dash.ScreeningScheduler.__new__(self.dash.ScreeningScheduler)
        scheduler.shutdown_hook = hook
        scheduler._archive_markdown = lambda: None  # avoid touching the real archive
        return scheduler

    def test_shutdown_hook_is_invoked(self):
        calls = []
        scheduler = self._scheduler(hook=lambda: calls.append("hook"))
        scheduler._auto_shutdown()
        self.assertEqual(calls, ["hook"], "shutdown_hook must be called on auto-shutdown")

    def test_hook_is_optional_and_does_not_raise(self):
        """Without a hook the scheduler must fall back, not crash."""
        scheduler = self._scheduler(hook=None)
        scheduler._auto_shutdown()  # module-level _server is None here -> no-op

    def test_failing_hook_falls_back_without_raising(self):
        def boom():
            raise RuntimeError("hook exploded")

        scheduler = self._scheduler(hook=boom)
        scheduler._auto_shutdown()  # must swallow and fall back

    def test_real_server_stops_serving_when_hook_fires(self):
        """End-to-end: the hook must unblock a real serve_forever() loop."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)

        def hook():
            threading.Thread(target=server.shutdown, daemon=True).start()

        scheduler = self._scheduler(hook=hook)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(thread.is_alive())

        scheduler._auto_shutdown()

        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "serve_forever() must return after auto-shutdown")
        server.server_close()


class WorkbenchWiringTests(unittest.TestCase):
    """Guard the workbench half of the fix by inspecting its source."""

    def test_workbench_registers_shutdown_hook(self):
        source = (SCRIPT_DIR / "web_workbench.py").read_text(encoding="utf-8")
        self.assertIn("dash.scheduler.shutdown_hook = _shutdown_workbench_server", source)
        self.assertIn("dash._server = server", source)
        self.assertIn("target=server.shutdown", source)


if __name__ == "__main__":
    unittest.main()
