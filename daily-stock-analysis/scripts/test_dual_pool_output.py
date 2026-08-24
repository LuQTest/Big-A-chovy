import unittest

from a_share_daily_screen import render_markdown


class DualPoolOutputTests(unittest.TestCase):
    def test_strict_mode_always_shows_both_pools_when_trend_pool_is_empty(self):
        result = {
            "meta": {"timestamp": "2026-07-17 13:10:00", "status": "盘中", "elapsed_seconds": 1, "source": "test"},
            "breadth": {"total_rows": 2, "valid_change": 2, "invalid_change": 0, "adv": 1, "dec": 1, "flat": 0, "main_limit_up": 0, "main_limit_down": 0, "degraded": False, "resonance_usable": True, "quality_reason": "完整"},
            "indices": [],
            "errors": [],
            "warnings": [],
            "announcement_errors": [],
            "strict_enabled": True,
            "strict_ultra": [{
                "code": "000001", "name": "测试超短", "price": 10.0, "change": 3.0,
                "turnover": 4.0, "amount": 500_000_000, "volume_ratio": 2.0,
                "industry": "测试板块", "main_pct": 1.0, "flow_5m_inc": 0,
                "flow_status": "数据不足", "announcement_risk": "clean",
            }],
            "trend_observation": [],
            "strict_trend": [],
            "dual_pool": [],
            "trend_diagnostics": [{
                "code": "000001", "name": "测试超短", "ma_state": "MA5/10/20上方",
                "ma20_slope": "上行", "day_change": "3.00%", "five_day": "5.00%",
                "dist60": "8.00%", "turnover_amount": "4.00% / 5.00亿",
                "sector": "数据可用", "announcement_risk": "clean",
                "first_failure": "无", "all_failures": "无", "near_match": False,
            }],
            "capital_rank": [],
            "low_ultra": [],
            "low_trend": [],
            "watchlist": [],
            "sector_indices": [],
            "flow_detail": [],
        }

        output = render_markdown(result)

        self.assertIn("双池运行：超短池 1 只；趋势观察池 0 只；趋势确认池 0 只；交集 0 只。", output)
        self.assertIn("## 超短池", output)
        self.assertIn("## 趋势观察池", output)
        self.assertIn("## 趋势确认池", output)
        self.assertIn("## 超短候选的趋势条件淘汰诊断", output)
        self.assertIn("观察首个淘汰", output)
        self.assertIn("确认首个淘汰", output)
        self.assertIn("无（当前没有满足趋势观察条件的标的）", output)

    def test_report_renders_intersection_state_machine_in_v2_format(self):
        result = {
            "meta": {"timestamp": "2026-07-20 10:05:00", "status": "盘中", "elapsed_seconds": 1, "source": "test"},
            "breadth": {"total_rows": 2, "valid_change": 2, "invalid_change": 0, "adv": 1, "dec": 1, "flat": 0, "main_limit_up": 0, "main_limit_down": 0, "degraded": False, "resonance_usable": True, "quality_reason": "完整"},
            "indices": [], "errors": [], "warnings": [], "announcement_errors": [],
            "strict_enabled": True,
            "strict_ultra": [], "trend_observation": [], "strict_trend": [],
            "dual_pool": [],
            "dual_pool_raw": [{
                "code": "000001", "name": "测试股", "price": 10.0, "change": 3.0,
                "turnover": 4.0, "amount": 500_000_000, "industry": "测试板块",
                "main_pct": 1.0, "flow_status": "有效流入", "announcement_risk": "clean",
            }],
            "pre_intersection": [{
                "code": "000001", "name": "测试股", "price": 10.0, "change": 3.0,
                "intersection_phase": "准交集",
                "preintersection_missing": "距60日高点",
                "trigger_price": 10.50,
                "main_pct": 1.0, "flow_status": "有效流入",
                "resonance": "是", "announcement_risk": "clean",
                "risk_note": "",
            }],
            "intersection_states": [],
            "intersection_config": {"confirmation_snapshots": 2, "morning_cutoff": "11:00", "afternoon_buy_deadline": "14:20", "intersection_latch_minutes": 15, "late_change_pct": 4.6},
            "intersection_config_meta": {"version": "default-v2", "source": "default"},
            "trend_diagnostics": [], "capital_rank": [], "low_ultra": [], "low_trend": [],
            "watchlist": [], "sector_indices": [], "flow_detail": [],
        }

        output = render_markdown(result)

        self.assertIn("## 交集四阶段状态机", output)
        self.assertIn("【准交集预警】", output)
        self.assertIn("距趋势确认仅差1项", output)
        self.assertIn("default-v2", output)
        self.assertIn("迟到涨幅阈值 4.6%", output)


if __name__ == "__main__":
    unittest.main()
