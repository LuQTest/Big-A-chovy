"""实时行情涨跌停字段的离线回归测试。"""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import query_quote


def quote_response(symbol, upper="11.00", lower="9.00", field_count=52):
    """构造腾讯格式响应；零基下标 47 为涨停价，48 为跌停价。"""
    fields = ["0"] * 52
    fields[1:6] = ["测试股票", symbol[2:], "10.20", "10.00", "10.10"]
    fields[30] = "20260930150000"
    fields[47:49] = [upper, lower]
    return f'v_{symbol}="' + "~".join(fields[:field_count]) + '";'


class QuotePriceLimitTests(unittest.TestCase):
    def fetch(self, content, codes):
        with patch.object(query_quote.urllib.request, "urlopen",
                          return_value=io.BytesIO(content.encode("gbk"))):
            return query_quote.fetch_realtime_quotes(codes)

    def test_batch_quotes_keep_upper_and_lower_limits_in_correct_order(self):
        content = (quote_response("sh600000") + "\n"
                   + quote_response("sz000001", upper="22.00", lower="18.00"))
        quotes = self.fetch(content, ["600000", "000001"])
        self.assertEqual(set(quotes), {"600000", "000001"})
        for code, upper, lower in [("600000", 11.0, 9.0), ("000001", 22.0, 18.0)]:
            with self.subTest(code=code):
                self.assertEqual(quotes[code]["zt"], upper)
                self.assertEqual(quotes[code]["dt"], lower)

    def test_empty_limit_fields_default_independently_to_zero(self):
        for upper, lower, expected in [
            ("", "9.00", (0.0, 9.0)),
            ("11.00", "", (11.0, 0.0)),
            ("", "", (0.0, 0.0)),
        ]:
            with self.subTest(upper=upper, lower=lower):
                quote = self.fetch(quote_response("sh600000", upper, lower),
                                   ["600000"])["600000"]
                self.assertEqual((quote["zt"], quote["dt"]), expected)

    def test_short_response_without_limit_fields_defaults_to_zero(self):
        quote = self.fetch(quote_response("sh600000", field_count=40),
                           ["600000"])["600000"]
        self.assertEqual((quote["zt"], quote["dt"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
