"""腾讯日 K 主机故障转移的行为测试（不需要联网）。"""
import json
import unittest
from unittest.mock import patch

import tencent_kline


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload_with(code):
    return {"data": {tencent_kline._symbol(code): {"qfqday": [["2026-09-24", "1", "2"]]}}}


class TencentHostFailoverTests(unittest.TestCase):
    def test_empty_envelope_falls_through_to_next_host(self):
        """有 data 但没有目标股票的 K 线 → 不算成功，必须继续试下一台主机。"""
        responses = [
            _FakeResponse({"data": {"sh999999": {"qfqday": [["2026-09-24", "1", "2"]]}}}),
            _FakeResponse(_payload_with("600519")),
        ]
        with patch.object(tencent_kline.urllib.request, "urlopen", side_effect=responses):
            payload, host = tencent_kline.fetch_kline_json("600519", 30)
        self.assertEqual(host, tencent_kline.TENCENT_KLINE_URLS[1])
        self.assertTrue(tencent_kline.kline_rows(payload, "600519"))

    def test_all_hosts_exhausted_raises_with_reasons(self):
        empty = _FakeResponse({"data": {}})
        with patch.object(tencent_kline.urllib.request, "urlopen",
                          side_effect=[empty] * len(tencent_kline.TENCENT_KLINE_URLS)):
            with self.assertRaises(RuntimeError) as ctx:
                tencent_kline.fetch_kline_json("600519", 30)
        self.assertIn("全部主机失败", str(ctx.exception))

    def test_first_host_success_keeps_order(self):
        with patch.object(tencent_kline.urllib.request, "urlopen",
                          return_value=_FakeResponse(_payload_with("600519"))):
            _, host = tencent_kline.fetch_kline_json("600519", 30)
        self.assertEqual(host, tencent_kline.TENCENT_KLINE_URLS[0])

    def test_symbol_normalization(self):
        self.assertEqual(tencent_kline._symbol("600519"), "sh600519")
        self.assertEqual(tencent_kline._symbol("000859"), "sz000859")
        self.assertEqual(tencent_kline._symbol("sh000001"), "sh000001")


class AdjustmentTypeTests(unittest.TestCase):
    """复权口径不得静默回退：要求前复权时不能把未复权的 day 当 qfq 用。"""

    RAW_ONLY = {"data": {"sh600519": {"day": [["2026-09-24", "1", "2"]]}}}

    def test_require_qfq_rejects_raw_only_payload(self):
        self.assertEqual(tencent_kline.kline_rows(self.RAW_ONLY, "600519", require_qfq=True), [])
        # 不要求时可以回退（指数等无复权概念的场景）
        self.assertEqual(len(tencent_kline.kline_rows(self.RAW_ONLY, "600519", )), 1)

    def test_adjustment_type_reports_actual_source(self):
        self.assertEqual(tencent_kline.adjustment_type(self.RAW_ONLY, "600519"), "raw")
        self.assertEqual(tencent_kline.adjustment_type(_payload_with("600519"), "600519"), "qfq")
        self.assertEqual(tencent_kline.adjustment_type({"data": {}}, "600519"), "")

    def test_fetch_tries_next_host_when_only_raw_available(self):
        """要求前复权时，只给 day 的主机不算成功，必须继续试下一台。"""
        responses = [
            _FakeResponse(self.RAW_ONLY),
            _FakeResponse(_payload_with("600519")),
        ]
        with patch.object(tencent_kline.urllib.request, "urlopen", side_effect=responses):
            payload, host = tencent_kline.fetch_kline_json("600519", 30, require_qfq=True)
        self.assertEqual(host, tencent_kline.TENCENT_KLINE_URLS[1])
        self.assertEqual(tencent_kline.adjustment_type(payload, "600519"), "qfq")

    def test_fetch_raises_when_no_host_has_qfq(self):
        with patch.object(tencent_kline.urllib.request, "urlopen",
                          return_value=_FakeResponse(self.RAW_ONLY)):
            with self.assertRaises(RuntimeError) as ctx:
                tencent_kline.fetch_kline_json("600519", 30, require_qfq=True)
        self.assertIn("只有未复权数据", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
