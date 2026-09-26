"""分钟序列兜底基准的单元测试。

不需要盘中数据：用合成累计序列验证取数、端点作差、日期校验、
以及兜底基准与资金门禁分类的联动。
"""
import math
import unittest
from unittest.mock import patch

import a_share_daily_screen as screen
from a_share_daily_screen import Enriched


def stock(code="600000", **overrides):
    values = dict(
        code=code, name=code, price=10.0, change=3.0, turnover=4.0,
        amount=500_000_000, volume_ratio=2.0, high=10.2, low=9.8,
        open=9.9, prev_close=9.7, total_mv=10_000_000_000,
        float_mv=8_000_000_000, industry="测试板块", timestamp=0, volume=1,
        kdate="2026-09-25", k_source="test", adj_close=10.0, ma5=9.8,
        ma10=9.6, ma20=9.4, prev_ma5=9.7, prev_ma10=9.5, prev_ma20=9.3,
        five_ret=0.05, dist60=0.1, ma20_dist=0.06, high_pull=0.8,
        cur_to_high=0.02, vol_vs_avg5=1.2, vwap=9.9, vwap_state="均价线上方",
        prior_high=9.9, prior_low=9.3, main_net=20_000_000, main_pct=8.0,
        super_net=10_000_000, super_pct=2.0, big_net=10_000_000, big_pct=2.0,
        mid_net=-2_000_000, mid_pct=-0.4, small_net=-3_000_000, small_pct=-0.6,
        flow_5m_inc=float("nan"), flow_15m_inc=float("nan"),
        price_above_vwap=True, flow_status="数据不足",
    )
    values.update(overrides)
    return Enriched(**values)


def series(count, step=100_000.0, start_minute=570):
    """构造 count 根 1 分钟累计序列，默认自 09:30 起每分钟累加 step。"""
    out = []
    for i in range(count):
        minutes = start_minute + i
        out.append((f"{minutes // 60:02d}:{minutes % 60:02d}", step * i))
    return out


class FlowMinuteBaselineTests(unittest.TestCase):
    def setUp(self):
        screen._FLOW_MINUTE_CACHE.clear()
        # 请求预算是模块级且按轮计，用例之间必须重置，否则后面的用例会因预算耗尽拿不到序列
        screen.reset_flow_minute_round()

    # ---------- 端点作差 ----------
    def test_increment_uses_five_and_fifteen_minute_endpoints(self):
        inc_5m, inc_15m = screen.flow_increments_from_minutes(series(26, step=100_000))
        self.assertEqual(inc_5m, 500_000)
        self.assertEqual(inc_15m, 1_500_000)

    def test_insufficient_samples_stay_nan(self):
        inc_5m, inc_15m = screen.flow_increments_from_minutes(series(3))
        self.assertTrue(math.isnan(inc_5m))
        self.assertTrue(math.isnan(inc_15m))

    def test_ten_bars_fill_five_minute_only(self):
        inc_5m, inc_15m = screen.flow_increments_from_minutes(series(10))
        self.assertEqual(inc_5m, 500_000)
        self.assertTrue(math.isnan(inc_15m))

    # ---------- 补齐行为 ----------
    def test_fill_sets_baseline_and_marks_source(self):
        row = stock()
        with patch.object(screen, "fetch_flow_minutes", return_value=series(26)):
            filled = screen.fill_flow_increments_from_fflow([row])
        self.assertEqual(filled, 1)
        self.assertEqual(row.flow_baseline_source, "fflow")
        self.assertEqual(row.flow_5m_inc, 500_000)
        self.assertEqual(row.flow_15m_inc, 1_500_000)

    def test_rows_with_snapshot_baseline_are_not_refetched(self):
        row = stock(flow_5m_inc=1_000_000, flow_15m_inc=2_000_000)
        with patch.object(screen, "fetch_flow_minutes") as fetch:
            filled = screen.fill_flow_increments_from_fflow([row])
        self.assertEqual(filled, 0)
        fetch.assert_not_called()

    def test_partial_baseline_keeps_existing_and_fills_gap(self):
        row = stock(flow_5m_inc=7_000_000,
                    flow_baseline_source="snapshot")
        with patch.object(screen, "fetch_flow_minutes", return_value=series(26)):
            filled = screen.fill_flow_increments_from_fflow([row])
        self.assertEqual(filled, 1)
        self.assertEqual(row.flow_5m_inc, 7_000_000)   # 已有快照基准不被覆盖
        self.assertEqual(row.flow_15m_inc, 1_500_000)  # 缺的 15 分钟由序列补上

    def test_failure_keeps_nan_and_never_zero(self):
        row = stock()
        with patch.object(screen, "fetch_flow_minutes", side_effect=RuntimeError("boom")):
            filled = screen.fill_flow_increments_from_fflow([row])
        self.assertEqual(filled, 0)
        self.assertTrue(math.isnan(row.flow_5m_inc))
        self.assertTrue(math.isnan(row.flow_15m_inc))
        self.assertEqual(row.flow_baseline_source, "")

    def test_round_cap_limits_requests(self):
        rows = [stock(code=f"6000{i:02d}") for i in range(25)]
        with patch.object(screen, "fetch_flow_minutes", return_value=series(26)) as fetch:
            filled = screen.fill_flow_increments_from_fflow(rows)
        self.assertEqual(filled, screen.FLOW_MINUTE_MAX_CODES_PER_ROUND)
        self.assertEqual(fetch.call_count, screen.FLOW_MINUTE_MAX_CODES_PER_ROUND)

    def test_fill_recomputes_buy_ratio(self):
        row = stock(flow_5m_inc=float("nan"), flow_15m_inc=float("nan"))
        before = screen.compute_buy_ratio(row, screen.SCREENING_CONFIG["flow"])
        with patch.object(screen, "fetch_flow_minutes", return_value=series(26)):
            screen.fill_flow_increments_from_fflow([row])
        after = screen.compute_buy_ratio(row, screen.SCREENING_CONFIG["flow"])
        self.assertEqual(row.buy_ratio, after)
        self.assertNotEqual(before, after)   # 有正向 5 分钟增量后 surge 生效

    # ---------- 日期与时效校验 ----------
    def test_stale_series_from_another_day_is_rejected(self):
        payload = {"data": {"klines": [
            "2026-09-24 14:56,-1000,-800", "2026-09-24 14:57,-900,-700",
        ]}}
        with patch.object(screen, "fetch_json", return_value=payload):
            got = screen.fetch_flow_minutes("600000", expected_date="2026-09-26")
        self.assertEqual(got, [])

    def test_series_matching_snapshot_date_is_accepted(self):
        """盘后/周末跑快照时，序列日期只要等于快照交易日就算有效，不按自然日拒绝。"""
        payload = {"data": {"klines": [
            "2026-09-24 09:30,0,0", "2026-09-24 09:31,100000,40000",
            "2026-09-24 09:32,200000,90000",
        ]}}
        with patch.object(screen, "fetch_json", return_value=payload):
            got = screen.fetch_flow_minutes("600000", expected_date="2026-09-24")
        self.assertEqual(len(got), 3)
        self.assertEqual(got[-1], ("09:32", 200_000.0, 90_000.0))

    def test_fresh_series_accepted_outside_trading_hours(self):
        today = screen.datetime.now(screen.TZ).strftime("%Y-%m-%d")
        payload = {"data": {"klines": [
            f"{today} 09:30,0,0", f"{today} 09:31,100000,40000",
            f"{today} 09:32,200000,90000",
        ]}}
        with patch.object(screen, "fetch_json", return_value=payload):
            got = screen.fetch_flow_minutes("600000")
        self.assertEqual(len(got), 3)
        self.assertEqual(got[-1], ("09:32", 200_000.0, 90_000.0))

    def test_flow_minutes_request_includes_super_order_column(self):
        """coalition 连续性要同时核验主力与超大单，因此序列必须请求 f56。"""
        with patch.object(screen, "fetch_json", return_value={"data": {"klines": []}}) as fetch:
            screen.fetch_flow_minutes("600000")
        _, kwargs = fetch.call_args
        params = kwargs.get("params") or fetch.call_args.args[1]
        self.assertIn("f56", params["fields2"])

    # ---------- 与门禁分类的联动 ----------
    def test_classify_flow_accepts_fallback_baseline(self):
        row = stock(flow_baseline_source="fflow")
        self.assertNotEqual(screen.classify_flow(row, {}, has_snapshot=False), "数据不足")

    def test_classify_flow_still_reports_missing_without_baseline(self):
        row = stock(flow_baseline_source="")
        self.assertEqual(screen.classify_flow(row, {}, has_snapshot=False), "数据不足")

    def test_capital_data_label_has_three_states(self):
        self.assertEqual(screen.capital_data_label(stock(flow_baseline_source=""), False), "仅当前快照")
        self.assertEqual(
            screen.capital_data_label(stock(flow_baseline_source="snapshot"), True), "含连续快照")
        self.assertEqual(
            screen.capital_data_label(stock(flow_baseline_source="fflow"), True), "含分钟序列")


class FlowVetoTests(unittest.TestCase):
    """框架一票否决：超大单为负，引擎必须在行上打标（而非只在诊断工具里判）。"""

    def test_negative_super_order_raises_veto(self):
        row = stock(super_net=-1_250_000.0)
        self.assertEqual(screen.flow_veto_reason(row), "超大单为负·一票否决")

    def test_positive_or_missing_super_order_no_veto(self):
        self.assertEqual(screen.flow_veto_reason(stock(super_net=1_000_000.0)), "")
        self.assertEqual(screen.flow_veto_reason(stock(super_net=float("nan"))), "")

    def test_absolute_dominance_requires_minimum_amount(self):
        """2026-09-26：absolute 原先只要求超大单>0，低成交额股票几十万即可取得该标签。"""
        small = stock(main_net=5_000_000.0, super_net=3_000_000.0, big_net=1_000_000.0)
        self.assertEqual(screen.evaluate_dominance_type(small, {})[0], "none")

        big = stock(main_net=20_000_000.0, super_net=12_000_000.0, big_net=5_000_000.0)
        self.assertEqual(screen.evaluate_dominance_type(big, {})[0], "absolute")


class SeriesBasedGateTests(unittest.TestCase):
    """分钟序列用于高位派发核验与 coalition 连续性（快照不足时的兜底）。"""

    def setUp(self):
        screen._FLOW_MINUTE_CACHE.clear()
        screen.reset_flow_minute_round()
        self.addCleanup(screen.reset_flow_minute_round)

    @staticmethod
    def _series(count, main_step=100_000.0, super_step=40_000.0, start_minute=570):
        return [
            (f"{(start_minute + i) // 60:02d}:{(start_minute + i) % 60:02d}",
             main_step * i, super_step * i)
            for i in range(count)
        ]

    # ---------- 窗口工具 ----------
    def test_window_deltas_for_both_columns(self):
        series = self._series(12, main_step=100_000.0, super_step=40_000.0)
        self.assertEqual(screen.flow_window_deltas(series, 5, column=1)[-1], 500_000.0)
        self.assertEqual(screen.flow_window_deltas(series, 5, column=2)[-1], 200_000.0)

    def test_partial_column_gap_does_not_compress_window(self):
        """单根 bar 缺值必须占位，窗口按时间跨度取。

        只修正前的行为：先把该列的 NaN 过滤掉再按条数取 [-6]，缺值会压缩位置，
        让相隔更久的两个端点被当成 5 分钟增量。
        """
        series = [
            ("10:01", 100.0, 10.0),
            ("10:02", 200.0, float("nan")),   # 超大单列在这根缺值
            ("10:03", 300.0, 30.0),
            ("10:04", 400.0, 40.0),
            ("10:05", 500.0, 50.0),
            ("10:06", 600.0, 60.0),
            ("10:07", 700.0, 70.0),
        ]
        super_deltas = screen.flow_window_deltas(series, 5, column=2)
        # 两个窗口：(10:01→10:06) 两端有效 = 50；(10:02→10:07) 起点缺值 = NaN
        self.assertEqual(len(super_deltas), 2)
        self.assertEqual(super_deltas[0], 50.0)
        self.assertTrue(math.isnan(super_deltas[1]))

        # 主力列完整：两端分别 100→600、200→700，都是 500
        self.assertEqual(screen.flow_window_deltas(series, 5, column=1), [500.0, 500.0])

    def test_continuity_is_unknown_when_column_gap_hits_window(self):
        series = [("10:01", 100.0, 10.0), ("10:02", 200.0, float("nan")),
                  ("10:03", 300.0, 30.0), ("10:04", 400.0, 40.0),
                  ("10:05", 500.0, 50.0), ("10:06", 600.0, 60.0), ("10:07", 700.0, 70.0)]
        # 超大单列：最近两个窗口里有一个端点缺值 → 无法核验，而不是"已衰减"
        self.assertIsNone(screen.flow_continuity_from_minutes(series, 2, column=2))
        # 主力列完整且递增 → True
        self.assertTrue(screen.flow_continuity_from_minutes(series, 2, column=1))

    def test_sampling_returns_none_when_sampled_point_missing(self):
        series = [("10:01", -1.0, 0.0), ("10:02", -2.0, 0.0), ("10:03", -3.0, 0.0),
                  ("10:04", -4.0, 0.0), ("10:05", float("nan"), 0.0), ("10:06", -6.0, 0.0)]
        # 5 分钟间隔最近 2 点 = 10:06、10:01；均有效 → 2 个 ≤0
        self.assertEqual(screen.flow_negative_samples_from_minutes(series, 2), 2)
        # 采样点落在缺值处 → 无法核验
        series2 = [("10:01", -1.0, 0.0), ("10:02", -2.0, 0.0), ("10:03", -3.0, 0.0),
                   ("10:04", -4.0, 0.0), ("10:05", -5.0, 0.0), ("10:06", float("nan"), 0.0)]
        self.assertIsNone(screen.flow_negative_samples_from_minutes(series2, 1, spacing_minutes=5))

    def test_negative_samples_counts_cumulative_negatives(self):
        """口径对齐快照：数的是累计主力净额 ≤ 0 的采样点个数。"""
        values = (-1.0, -2.0, -3.0, 4.0, 6.0, 8.0)
        series = [(f"09:{31 + i:02d}", v, 0.0) for i, v in enumerate(values)]
        # 倒序取最近 5 点 = 8,6,4,-3,-2 → 2 个 ≤0
        self.assertEqual(screen.flow_negative_samples_from_minutes(series, 5, spacing_minutes=1), 2)

    def test_negative_samples_with_five_minute_spacing(self):
        """按 5 分钟间隔采样时，样本数不足应返回 None（无法核验），不猜。"""
        short = [("09:31", -1.0, 0.0), ("09:36", -1.0, 0.0)]
        self.assertIsNone(screen.flow_negative_samples_from_minutes(short, 3))

        long_series = [(f"{(570 + i) // 60:02d}:{(570 + i) % 60:02d}", float(i - 10), 0.0)
                       for i in range(16)]   # 采样点（倒序、步长5）= 5,0,-5,-10
        self.assertEqual(screen.flow_negative_samples_from_minutes(long_series, 3), 2)

    # ---------- coalition 连续性兜底 ----------
    def _coalition_stock(self):
        return stock(main_net=60_000_000.0, main_pct=8.0, super_net=20_000_000.0,
                     big_net=10_000_000.0, flow_5m_inc=12_000_000.0, buy_ratio=1.8)

    def test_coalition_uses_series_when_snapshots_missing(self):
        screen.reset_flow_minute_round("2026-09-24")
        with patch.object(screen, "fetch_flow_minutes", return_value=self._series(20)):
            dom, label = screen.evaluate_dominance_type(self._coalition_stock(), {})
        self.assertEqual(dom, "coalition")

    def test_coalition_rejects_decaying_series(self):
        screen.reset_flow_minute_round("2026-09-24")
        decaying = [("09:31", 0.0, 0.0), ("09:36", 5_000_000.0, 2_000_000.0),
                    ("09:41", 4_000_000.0, 1_500_000.0), ("09:46", 4_500_000.0, 1_800_000.0)]
        with patch.object(screen, "fetch_flow_minutes", return_value=decaying):
            dom, _ = screen.evaluate_dominance_type(self._coalition_stock(), {})
        self.assertEqual(dom, "none")

    def test_coalition_rejects_when_super_column_missing(self):
        """只核验主力等于放松门槛，超大单列缺失时必须拒绝。"""
        screen.reset_flow_minute_round("2026-09-24")
        no_super = [("09:31", 0.0, float("nan")), ("09:36", 5_000_000.0, float("nan")),
                    ("09:41", 9_000_000.0, float("nan"))]
        with patch.object(screen, "fetch_flow_minutes", return_value=no_super):
            dom, _ = screen.evaluate_dominance_type(self._coalition_stock(), {})
        self.assertEqual(dom, "none")

    def test_coalition_without_round_date_stays_offline(self):
        """未进入筛选轮（未设置 round date）时不得联网兜底。"""
        stock_row = self._coalition_stock()
        with patch.object(screen, "fetch_flow_minutes") as fetch:
            dom, _ = screen.evaluate_dominance_type(stock_row, {})
        fetch.assert_not_called()
        self.assertEqual(dom, "none")

    # ---------- 高位派发降权兜底 ----------
    def _high_position_stock(self):
        return stock(five_ret=0.15, main_pct=-1.0)

    def test_low_absorb_discharge_verified_from_series(self):
        screen.reset_flow_minute_round("2026-09-24")
        # 规则要求 6 期、其中 ≥3 期净流出；5 分钟间隔采样至少需要 26 根 bar
        negative = [(f"{(570 + i) // 60:02d}:{(570 + i) % 60:02d}",
                     -9_000_000.0 + i * 100_000.0, -4_000_000.0) for i in range(31)]
        with patch.object(screen, "fetch_flow_minutes", return_value=negative):
            excluded, reason = screen._should_exclude_from_low_absorb(self._high_position_stock(), {})
        self.assertTrue(excluded)
        self.assertIn("高位派发降权", reason)

    def test_low_absorb_unverified_is_counted_not_silently_skipped(self):
        screen.reset_flow_minute_round("2026-09-24")
        with patch.object(screen, "fetch_flow_minutes", side_effect=RuntimeError("no data")):
            excluded, reason = screen._should_exclude_from_low_absorb(self._high_position_stock(), {})
        self.assertFalse(excluded)
        self.assertEqual(reason, "")
        self.assertEqual(screen.low_absorb_unverified_count(), 1)

    def test_low_absorb_uses_snapshots_when_available(self):
        row = self._high_position_stock()
        history = {row.code: [{"main_net": -float(i)} for i in range(1, 7)]}
        with patch.object(screen, "fetch_flow_minutes") as fetch:
            excluded, reason = screen._should_exclude_from_low_absorb(row, history)
        fetch.assert_not_called()
        self.assertTrue(excluded)
        self.assertIn("高位派发降权", reason)


class LunchGapTests(unittest.TestCase):
    """午休/缺 bar 缺口：窗口必须按时间取，不能按记录条数取。

    2026-09-26 实测真实序列为 11:30 → 13:01（午休），在 13:03 用 series[-6] 会取到 11:28，
    把约 95 分钟的累计变化当成"5 分钟增量"，进而影响资金门槛与连续性判断。
    """

    @staticmethod
    def _bar(hhmm, value):
        return (hhmm, value, value)

    def _morning_plus_afternoon(self, afternoon_bars=4):
        series = [self._bar(f"11:{m:02d}", 1_000_000.0 * m) for m in (26, 27, 28, 29, 30)]
        series += [self._bar(f"13:{m:02d}", 2_000_000.0 * m) for m in range(1, afternoon_bars + 1)]
        return series

    def test_increment_does_not_cross_lunch_gap(self):
        series = self._morning_plus_afternoon(3)   # 下午只有 13:01–13:03，连续段长度 3
        inc_5m, inc_15m = screen.flow_increments_from_minutes(series)
        self.assertTrue(math.isnan(inc_5m), "跨午休时不得给出 5 分钟增量")
        self.assertTrue(math.isnan(inc_15m))

    def test_increment_uses_afternoon_only_once_enough_bars(self):
        series = self._morning_plus_afternoon(6)   # 13:01–13:06，连续段 6 根
        inc_5m, inc_15m = screen.flow_increments_from_minutes(series)
        # 5 分钟增量 = 13:06 累计 - 13:01 累计 = 2.0e7*6 - 2.0e7*1
        self.assertEqual(inc_5m, 2_000_000.0 * 6 - 2_000_000.0 * 1)
        self.assertTrue(math.isnan(inc_15m), "连续段不足 16 根时 15 分钟增量仍为基准不足")

    def test_missing_bar_mid_session_is_treated_as_gap(self):
        series = [self._bar(f"10:{m:02d}", 1000.0 * m) for m in (1, 2, 3, 5, 6, 7)]
        inc_5m, _ = screen.flow_increments_from_minutes(series)
        self.assertTrue(math.isnan(inc_5m), "缺 bar 造成的断档同样不能跨越")

    def test_continuity_returns_none_when_tail_shorter_than_periods(self):
        series = self._morning_plus_afternoon(2)
        self.assertIsNone(screen.flow_continuity_from_minutes(series, 2))

    def test_negative_samples_use_contiguous_tail(self):
        """采样点必须落在连续段内：午休前的负值不应被算进下午的采样。"""
        series = [self._bar("11:29", -5_000_000.0), self._bar("11:30", -4_000_000.0)]
        series += [self._bar(f"13:{m:02d}", 1_000_000.0 * m) for m in range(1, 12)]
        # 连续段内最近 2 个 5 分钟采样点 = 13:11、13:06（均为正）→ 0 个 ≤0
        self.assertEqual(screen.flow_negative_samples_from_minutes(series, 2), 0)


class FlowStatusCellTests(unittest.TestCase):
    """资金状态单元格必须带上超大单为负的否决标记（池表/低吸表/观察池都要能一眼看到）。"""

    def test_veto_row_gets_marker(self):
        row = {"flow_status": "有效流入", "flow_veto": "超大单为负·一票否决"}
        self.assertEqual(screen.flow_status_cell(row), "有效流入；❌超大单为负·一票否决")

    def test_clean_row_has_no_marker(self):
        self.assertEqual(screen.flow_status_cell({"flow_status": "有效流入"}), "有效流入")
        self.assertEqual(screen.flow_status_cell({}), "")

    def test_veto_suffix_is_empty_or_marker(self):
        self.assertEqual(screen.flow_veto_suffix({"flow_veto": "超大单为负·一票否决"}),
                         "；❌超大单为负·一票否决")
        self.assertEqual(screen.flow_veto_suffix({}), "")


class RangePositionTests(unittest.TestCase):
    """现价在当日区间的分位：把"是不是买在半山腰"变成报告里可见的数字。"""

    def test_range_pct_is_computed_from_high_low(self):
        # 当日 9.0–11.0，现价 10.0 → 50%
        row = stock(price=10.0, low=9.0, high=11.0)
        payload = {"price": 10.0, "low": 9.0, "high": 11.0}
        value = (payload["price"] - payload["low"]) / (payload["high"] - payload["low"]) * 100
        self.assertEqual(value, 50.0)
        self.assertEqual(screen.range_position_cell({"range_pct": value}), "50%")

    def test_warn_marker_above_threshold(self):
        self.assertEqual(
            screen.range_position_cell({"range_pct": screen.RANGE_POSITION_WARN_PCT}), "80%⚠")
        self.assertEqual(screen.range_position_cell({"range_pct": 79.9}), "80%")
        self.assertEqual(screen.range_position_cell({"range_pct": 85.0}), "85%⚠")

    def test_top_marker_above_top_threshold(self):
        """2026-09-24 实测 ≥80% 占 21%、≥90% 占 7%，两档才有区分度（单档 70% 会 86% 命中）。"""
        self.assertEqual(
            screen.range_position_cell({"range_pct": screen.RANGE_POSITION_TOP_PCT}), "90%⚠顶")
        self.assertEqual(screen.range_position_cell({"range_pct": 100.0}), "100%⚠顶")

    def test_missing_or_flat_range_renders_dash(self):
        self.assertEqual(screen.range_position_cell({"range_pct": float("nan")}), "-")
        self.assertEqual(screen.range_position_cell({}), "-")


class BuyRatioProxyTests(unittest.TestCase):
    """主买比是代理值：缺数据必须为未知，不能靠常量通过硬门槛。"""

    def test_missing_amount_yields_nan_instead_of_constant(self):
        """2026-09-26 修正：amount 缺失时旧实现返回 1.6，可直接满足 >=1.5 门槛。"""
        row = stock(amount=0.0, main_net=20_000_000, flow_5m_inc=5_000_000)
        value = screen.compute_buy_ratio(row, screen.SCREENING_CONFIG["flow"])
        self.assertTrue(math.isnan(value))

    def test_missing_main_net_yields_nan(self):
        row = stock(main_net=float("nan"))
        value = screen.compute_buy_ratio(row, screen.SCREENING_CONFIG["flow"])
        self.assertTrue(math.isnan(value))

    def test_formula_unchanged_when_data_present(self):
        row = stock(amount=500_000_000, main_pct=8.0, flow_5m_inc=5_000_000)
        value = screen.compute_buy_ratio(row, screen.SCREENING_CONFIG["flow"])
        self.assertFalse(math.isnan(value))
        self.assertGreaterEqual(value, 1.0)

    def test_rows_without_history_still_get_proxy_value(self):
        """没有快照历史的行也要走同一公式，而不是沿用 enrich 阶段的临时值。"""
        row = stock(amount=500_000_000, main_pct=8.0, flow_5m_inc=float("nan"))
        expected = screen.compute_buy_ratio(row, screen.SCREENING_CONFIG["flow"])
        screen.apply_flow_increments([row], {})
        self.assertEqual(row.buy_ratio, expected)


if __name__ == "__main__":
    unittest.main()
