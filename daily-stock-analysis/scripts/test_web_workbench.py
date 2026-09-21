"""Security regression tests for the browser workbench HTTP boundary."""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


class _FakeDashboardHandler(BaseHTTPRequestHandler):
    """Minimal dashboard surface so the workbench can be tested offline."""

    def _serve_json(self, data: object) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _serve_text(self, text: str, content_type: str) -> None:
        content = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _send_cors_headers(self) -> None:
        pass

    def do_GET(self) -> None:
        self.send_error(404)

    def do_POST(self) -> None:
        self.send_error(404)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


def _load_workbench():
    fake_dash = types.ModuleType("realtime_dashboard")
    fake_dash.DashboardHandler = _FakeDashboardHandler
    fake_dash.MIME_TYPES = {}
    fake_dash.PORT = 8765
    fake_dash._sanitize_json = lambda obj: obj
    fake_dash.is_trading_hours = lambda: False
    fake_dash.scheduler = types.SimpleNamespace(
        is_prewarming=False,
        _screening_lock=threading.Lock(),
        latest_result=None,
        latest_md_path=None,
    )
    fake_dash.ScreeningScheduler = types.SimpleNamespace(
        _render_min5_table=lambda _result: "",
    )

    old_dash = sys.modules.get("realtime_dashboard")
    sys.modules["realtime_dashboard"] = fake_dash
    module_name = "web_workbench_security_test_target"
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPT_DIR / "web_workbench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    if old_dash is not None:
        sys.modules["realtime_dashboard"] = old_dash
    else:
        sys.modules.pop("realtime_dashboard", None)
    return module


class WorkbenchSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbench = _load_workbench()

    def test_report_path_rejects_absolute_and_escape_paths(self):
        with tempfile.TemporaryDirectory(dir=self.workbench.PROJECT_ROOT) as tmp:
            reports = Path(tmp) / "筛选结果"
            reports.mkdir()
            self.workbench.REPORTS_DIR = reports

            safe = reports / "report.md"
            safe.write_text("# safe", encoding="utf-8")
            self.assertEqual(
                self.workbench._resolve_report_path(
                    str(safe.relative_to(self.workbench.PROJECT_ROOT))
                ),
                safe.resolve(),
            )
            self.assertIsNone(self.workbench._resolve_report_path("/etc/passwd"))
            self.assertIsNone(self.workbench._resolve_report_path("筛选结果/../README.md"))

    def test_cross_origin_api_request_is_rejected_without_wildcard_cors(self):
        with tempfile.TemporaryDirectory(dir=self.workbench.PROJECT_ROOT) as tmp:
            reports = Path(tmp) / "筛选结果"
            reports.mkdir()
            self.workbench.REPORTS_DIR = reports
            server = ThreadingHTTPServer(("127.0.0.1", 0), self.workbench.WorkbenchHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/api/wb/reports", headers={"Origin": "http://evil.example"})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request(
                    "GET",
                    "/api/wb/reports",
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
                response.read()
                conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
