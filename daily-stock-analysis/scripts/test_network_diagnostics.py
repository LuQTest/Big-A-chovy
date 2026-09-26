import contextlib
import io
import unittest
from unittest.mock import patch

import a_share_daily_screen as screen
import realtime_engine as realtime
from a_share_daily_screen import (
    MARKET_WARNINGS,
    NetworkUnavailable,
    build_url_opener,
    fetch_market,
    format_network_failure,
    get_market_fetch_status,
    main,
)


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        # 熔断、主机失败记录、K 线缓存都是模块级状态，测试之间必须复位，否则用例顺序会影响结果
        screen._tencent_kline_fail_streak = 0
        screen._tencent_kline_blocked_until = 0.0
        screen._sina_kline_fallback_warned = True
        screen._HOST_FAILURES.clear()
        realtime._kline_cache = {}
        realtime._kline_cache_date = realtime._today()

    def test_known_blocked_list_endpoints_use_webguest_routes(self):
        self.assertTrue(all("/webguest/api/qt/clist/get" in url for url in screen.CLIST_URLS))
        self.assertTrue(all("/webguest/api/qt/clist/get" in url for url in screen.CLIST_STARTUP_URLS))
        self.assertTrue(all("/webguest/api/qt/ulist.np/get" in url for url in screen.INDEX_URLS))
        self.assertTrue(all("push2delay.eastmoney.com" not in url for url in screen.CLIST_URLS + screen.INDEX_URLS))
        self.assertTrue(all("/webguest/api/qt/stock/fflow/kline/get" in url
                            for url in screen.EM_FFLOW_MINUTE_URLS))
        self.assertIn("/webguest/api/qt/stock/trends2/get", realtime.EM_TRENDS_URL)

    def test_fetch_kline_prefers_tencent_qfq(self):
        rows = [f"2026-06-{i:02d},10,10.5,11,9.5,1000" for i in range(1, 71)]
        response = {"data": {"sh600519": {"qfqday": rows}}}
        with patch.object(screen, "fetch_json", return_value=response) as fetch:
            parsed, source = screen.fetch_kline("600519", 90)

        self.assertEqual(source, "tencent_qfq")
        self.assertEqual(len(parsed), 70)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], screen.TENCENT_KLINE_URL)

    def test_fetch_kline_falls_back_to_sina_when_tencent_fails(self):
        """东财日 K 已于 2026-09-25 下线，腾讯各主机失败后直接走新浪末档。"""
        rows = [{"day": f"2026-06-{i:02d}", "open": "10", "high": "11",
                 "low": "9.5", "close": "10.5", "volume": "1000"} for i in range(1, 71)]

        def fake_fetch(url, params=None, **kwargs):
            if "gtimg" in url or "qq.com" in url:
                raise NetworkUnavailable(url, {})
            return rows

        with patch.object(screen, "fetch_json", side_effect=fake_fetch) as fetch:
            parsed, source = screen.fetch_kline("600519", 90)

        self.assertEqual(source, "sina_daily")
        self.assertEqual(len(parsed), 70)
        self.assertEqual(fetch.call_args_list[-1].args[0], screen.SINA_KLINE_URL)
        tencent_hosts = {c.args[0] for c in fetch.call_args_list
                         if c.args[0].endswith("/appstock/app/fqkline/get")}
        self.assertEqual(tencent_hosts, set(screen.TENCENT_KLINE_URLS))

    def test_tencent_kline_circuit_opens_after_repeated_failures(self):
        """连续失败后熔断腾讯日 K：避免 WAF 拦截页把一轮 76 只放大成上百次请求。"""
        with patch.object(screen, "fetch_json",
                          side_effect=NetworkUnavailable("tencent", {})) as fetch:
            for _ in range(5):
                with self.assertRaises(RuntimeError):
                    screen.fetch_kline("600519", 90)

        tencent_calls = [c for c in fetch.call_args_list
                         if c.args and c.args[0] == screen.TENCENT_KLINE_URL]
        self.assertEqual(len(tencent_calls), screen.TENCENT_KLINE_FAIL_STREAK_LIMIT)

    def test_tencent_kline_circuit_resets_on_success(self):
        rows = [f"2026-06-{i:02d},10,10.5,11,9.5,1000" for i in range(1, 71)]
        response = {"data": {"sh600519": {"qfqday": rows}}}
        screen._tencent_kline_fail_streak = screen.TENCENT_KLINE_FAIL_STREAK_LIMIT - 1
        with patch.object(screen, "fetch_json", return_value=response):
            parsed, source = screen.fetch_kline("600519", 90)

        self.assertEqual(source, "tencent_qfq")
        self.assertEqual(screen._tencent_kline_fail_streak, 0)
        self.assertEqual(len(parsed), 70)

    def test_tencent_kline_fails_over_between_hosts(self):
        """2026-09-26：web.ifzq.gtimg.cn 被 WAF 拦截，同一接口在 ifzq.gtimg.cn 上仍可用。"""
        rows = [f"2026-06-{i:02d},10,10.5,11,9.5,1000" for i in range(1, 71)]
        response = {"data": {"sh600519": {"qfqday": rows}}}
        with patch.object(screen, "fetch_json",
                          side_effect=[NetworkUnavailable("waf", {}), response]) as fetch:
            parsed, source = screen.fetch_kline("600519", 90)

        self.assertEqual(source, "tencent_qfq")
        self.assertEqual(len(parsed), 70)
        self.assertIn(fetch.call_args_list[1].args[0], screen.TENCENT_KLINE_URLS)
        self.assertNotEqual(fetch.call_args_list[0].args[0], fetch.call_args_list[1].args[0])

    def test_failure_message_keeps_proxy_and_direct_evidence(self):
        error = NetworkUnavailable(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            {
                "系统代理": "ProxyError: Cannot connect to proxy",
                "直连": "ConnectionError: Name or service not known",
            },
        )

        message = format_network_failure(error)

        self.assertIn("系统代理", message)
        self.assertIn("直连", message)
        self.assertIn("网络连接失败", message)
        self.assertNotIn("Traceback", message)

    @patch("time.sleep", return_value=None)
    @patch("a_share_daily_screen.fetch_sina_market", create=True)
    @patch("a_share_daily_screen.fetch_json")
    def test_fetch_market_preserves_network_failure_for_cli_handling(self, fetch_json, fetch_sina_market, _sleep):
        failure = NetworkUnavailable("https://example.invalid", {"直连": "ConnectionError"})
        fetch_json.side_effect = failure
        fetch_sina_market.side_effect = failure

        with self.assertRaises(NetworkUnavailable) as caught:
            fetch_market()

        self.assertIs(caught.exception, failure)

    @patch("time.sleep", return_value=None)
    @patch("a_share_daily_screen.fetch_sina_market", create=True)
    @patch("a_share_daily_screen.fetch_json")
    def test_fetch_market_uses_independent_fallback_after_push2_failure(self, fetch_json, fetch_sina_market, _sleep):
        """push2 host aliases must not prevent a separate provider fallback."""
        MARKET_WARNINGS.clear()
        failure = NetworkUnavailable("https://push2delay.eastmoney.com/api/qt/clist/get", {"直连": "RemoteDisconnected"})
        fallback_rows = [{"f12": "600000", "f14": "浦发银行", "_source": "sina_fallback"}]
        fetch_json.side_effect = failure
        fetch_sina_market.return_value = (fallback_rows, 1)

        rows, total = fetch_market()
        status = get_market_fetch_status()

        self.assertEqual(rows, fallback_rows)
        self.assertIsNone(total)
        self.assertEqual(status["source"], "sina_fallback")
        self.assertFalse(status["complete"])
        self.assertIsNone(status["provider_total"])
        self.assertTrue(any("新浪备用行情" in warning for warning in MARKET_WARNINGS))

    @patch("time.sleep", return_value=None)
    @patch("a_share_daily_screen._em_in_cooldown", return_value=False)
    @patch("a_share_daily_screen.fetch_sina_market", return_value=([], 0), create=True)
    @patch("a_share_daily_screen.fetch_json")
    def test_page_one_has_one_controlled_host_failover(self, fetch_json, _fetch_sina_market, _cooldown, _sleep):
        """A page-one CDN fault gets controlled host failover."""
        MARKET_WARNINGS.clear()
        failure = NetworkUnavailable("https://push2delay.eastmoney.com/api/qt/clist/get", {"直连": "RemoteDisconnected"})
        fetch_json.side_effect = [failure, {"data": {"total": 0, "diff": []}}]

        rows, total = fetch_market()

        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertGreaterEqual(fetch_json.call_count, 2)

    @patch("a_share_daily_screen.filter_prefetch", return_value=[])
    @patch("a_share_daily_screen.save_intersection_state")
    @patch("a_share_daily_screen.load_intersection_state", return_value={})
    @patch("a_share_daily_screen.save_flow_history")
    @patch("a_share_daily_screen.load_flow_history", return_value={})
    @patch("a_share_daily_screen.enrich_all", return_value=([], []))
    @patch("a_share_daily_screen.fetch_sector_indices", return_value=[])
    @patch("a_share_daily_screen.fetch_indices", return_value=[])
    @patch("a_share_daily_screen.get_market_fetch_status", return_value={"source": "eastmoney_push2", "complete": True})
    @patch("a_share_daily_screen.fetch_market", return_value=([], 0))
    @patch("sys.argv", ["a_share_daily_screen.py", "--format", "json", "--skip-announcements"])
    def test_cli_builds_result_with_resolved_intersection_config(self, *_mocks):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)

    def test_direct_urllib_opener_can_be_constructed(self):
        opener = build_url_opener("direct")

        self.assertTrue(callable(opener.open))

    @patch("time.sleep", return_value=None)
    @patch("a_share_daily_screen.fetch_json")
    def test_fetch_market_marks_incomplete_pages_as_partial_snapshot(self, fetch_json, _sleep):
        """A few successful top-gainer pages must never look like a full market."""
        MARKET_WARNINGS.clear()

        def response_for(url, params):
            page = params["pn"]
            if page == 3:
                raise RuntimeError("page 3 unavailable")
            start = (page - 1) * 100
            return {"data": {"total": 300, "diff": [{"f12": str(start + i)} for i in range(100)]}}

        fetch_json.side_effect = response_for

        rows, total = fetch_market()
        status = get_market_fetch_status()

        self.assertEqual(total, 300)
        self.assertEqual(len(rows), 200)
        self.assertFalse(status["complete"])
        self.assertEqual(status["expected_pages"], 3)
        self.assertEqual(status["received_pages"], 2)
        self.assertEqual(status["failed_pages"], [3])
        self.assertTrue(any("局部快照" in warning for warning in MARKET_WARNINGS))

    @patch("time.sleep", return_value=None)
    @patch("a_share_daily_screen.fetch_json")
    def test_fetch_market_uses_exact_page_count_and_code_order(self, fetch_json, _sleep):
        """A complete two-page response must not request a phantom third page."""
        MARKET_WARNINGS.clear()
        seen_pages = []

        def response_for(url, params):
            seen_pages.append(params["pn"])
            self.assertEqual(params["fid"], "f12")
            self.assertEqual(params["pz"], 100)
            page = params["pn"]
            start = (page - 1) * 100
            return {"data": {"total": 200, "diff": [{"f12": str(start + i)} for i in range(100)]}}

        fetch_json.side_effect = response_for

        rows, total = fetch_market()
        status = get_market_fetch_status()

        self.assertEqual(total, 200)
        self.assertEqual(set(seen_pages), {1, 2})
        self.assertTrue(status["complete"])
        self.assertEqual(status["failed_pages"], [])
        self.assertEqual(len(rows), 200)


if __name__ == "__main__":
    unittest.main()
