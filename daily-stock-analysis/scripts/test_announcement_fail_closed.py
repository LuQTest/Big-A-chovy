import time
import unittest
from unittest.mock import patch

from a_share_daily_screen import (
    apply_announcement_pool_gates,
    attach_announcement_risks,
    render_markdown,
)


def row(code, risk=None):
    value = {
        "code": code,
        "name": f"测试{code}",
        "price": 10.0,
        "change": 2.0,
        "turnover": 3.0,
        "amount": 500_000_000,
        "volume_ratio": 1.5,
        "industry": "测试板块",
        "announcement_risk": risk,
    }
    if risk is not None:
        value["risk_status"] = risk
    return value


def result_for(*rows):
    return {
        "trend_observation": [dict(r) for r in rows],
        "strict_trend": [dict(r) for r in rows],
        "dual_pool": [dict(r) for r in rows],
        "capital_rank": [dict(r) for r in rows],
        "trend_diagnostics": [dict(r) for r in rows],
        "announcement_errors": [],
    }


class AnnouncementFailClosedTests(unittest.TestCase):
    def test_fresh_cache_skips_network_and_reports_progress(self):
        result = result_for(row("000001"))
        progress = []
        cache = {
            "000001": {
                "status": "clean",
                "keywords": [],
                "titles": ["已缓存公告"],
                "checked_at": time.time(),
            }
        }
        with patch(
            "a_share_daily_screen.fetch_announcements",
            side_effect=AssertionError("fresh cache should avoid a network request"),
        ):
            errors = attach_announcement_risks(
                result,
                page_size=8,
                workers=1,
                risk_cache=cache,
                progress_callback=lambda *item: progress.append(item),
            )

        self.assertEqual(errors, [])
        self.assertEqual(result["announcement_cached_count"], 1)
        self.assertEqual(result["announcement_requested_count"], 0)
        self.assertEqual(result["trend_observation"][0]["risk_status"], "clean")
        self.assertEqual(result["trend_observation"][0]["announcement_risk_source"], "cache")
        self.assertEqual(progress, [(1, 1, "000001", "clean", "cache")])

    def test_diagnostics_do_not_expand_announcement_request_set(self):
        result = result_for(row("000001"))
        result["trend_diagnostics"] = [row(f"{index:06d}") for index in range(100, 150)]
        cache = {
            "000001": {"status": "clean", "checked_at": time.time()}
        }
        requested = []

        def fake_fetch(code, page_size):
            requested.append(code)
            return []

        with patch("a_share_daily_screen.fetch_announcements", side_effect=fake_fetch):
            attach_announcement_risks(
                result,
                page_size=8,
                workers=1,
                risk_cache=cache,
            )

        self.assertEqual(requested, [])
        self.assertEqual(result["announcement_total_count"], 1)

    def test_last_known_avoid_survives_announcement_source_failure(self):
        cache = {}
        initial = result_for(row("000948"))
        with patch(
            "a_share_daily_screen.fetch_announcements",
            return_value=["关于诉讼事项的公告"],
        ):
            attach_announcement_risks(initial, page_size=8, workers=1, risk_cache=cache)
        self.assertEqual(initial["trend_observation"][0]["risk_status"], "avoid")
        # Force a refresh so this test still exercises fail-closed fallback;
        # a fresh cache is intentionally allowed to skip the network.
        cache["000948"]["checked_at"] = time.time() - 8 * 24 * 60 * 60

        failed = result_for(row("000948"))
        with patch(
            "a_share_daily_screen.fetch_announcements",
            side_effect=RuntimeError("公告源不可用"),
        ):
            errors = attach_announcement_risks(
                failed, page_size=8, workers=1, risk_cache=cache
            )

        self.assertEqual(errors, ["000948"])
        self.assertEqual(failed["trend_observation"][0]["risk_status"], "avoid")
        self.assertEqual(failed["trend_observation"][0]["announcement_risk"], "avoid")
        apply_announcement_pool_gates(failed)
        self.assertEqual(failed["dual_pool"], [])
        self.assertEqual(failed["strict_trend"], [])
        self.assertEqual(failed["capital_rank"], [])

    def test_unknown_announcement_cannot_enter_tradeable_upgrades(self):
        result = result_for(row("000948"), row("603466"))
        result["trend_observation"][0]["risk_status"] = "avoid"
        result["trend_observation"][0]["announcement_risk"] = "avoid"
        result["strict_trend"][0]["risk_status"] = "avoid"
        result["strict_trend"][0]["announcement_risk"] = "avoid"
        result["dual_pool"][0]["risk_status"] = "avoid"
        result["dual_pool"][0]["announcement_risk"] = "avoid"
        result["capital_rank"][0]["risk_status"] = "avoid"
        result["capital_rank"][0]["announcement_risk"] = "avoid"

        with patch(
            "a_share_daily_screen.fetch_announcements",
            side_effect=RuntimeError("公告源不可用"),
        ):
            attach_announcement_risks(
                result, page_size=8, workers=1, risk_cache={"000948": {"status": "avoid"}}
            )
        apply_announcement_pool_gates(result)

        self.assertEqual(result["dual_pool"], [])
        self.assertEqual(result["strict_trend"], [])
        self.assertEqual(result["capital_rank"], [])
        self.assertEqual(
            [r["code"] for r in result["trend_observation"]], ["603466"]
        )
        self.assertEqual(result["trend_observation"][0]["risk_status"], "unknown")

    def test_report_distinguishes_unknown_from_clean_and_explains_gate(self):
        result = {
            "meta": {
                "timestamp": "2026-07-20 11:12:00",
                "status": "盘中",
                "elapsed_seconds": 1,
                "source": "test",
            },
            "breadth": {
                "total_rows": 1,
                "provider_total": 1,
                "valid_change": 1,
                "invalid_change": 0,
                "adv": 1,
                "dec": 0,
                "flat": 0,
                "main_limit_up": 0,
                "main_limit_down": 0,
                "degraded": False,
                "resonance_usable": True,
            },
            "indices": [],
            "errors": [],
            "warnings": [],
            "announcement_errors": ["603466"],
            "announcement_check_available": False,
            "announcement_unknown_codes": ["603466"],
            "strict_enabled": True,
            "strict_ultra": [row("603466")],
            "trend_observation": [row("603466")],
            "strict_trend": [],
            "dual_pool": [],
            "trend_diagnostics": [],
            "capital_rank": [],
            "low_ultra": [],
            "low_trend": [],
            "watchlist": [],
            "sector_indices": [],
            "flow_detail": [],
        }

        output = render_markdown(result)

        self.assertIn("公告检查不可用", output)
        self.assertIn("unknown", output)
        self.assertNotIn("公告风险 | clean", output)


if __name__ == "__main__":
    unittest.main()
