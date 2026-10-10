"""公告风险词优先级和冲突 fail-closed 行为回归。"""
import unittest

from tools.data_sources.announcements import (
    classify_announcement_risk,
    classify_announcement_titles,
)


class AnnouncementKeywordPrecedenceTests(unittest.TestCase):
    def test_hard_risk_survives_document_type_ignore_keyword(self):
        title = "关于股份冻结事项的法律意见书"
        result = classify_announcement_risk([title])

        self.assertEqual(result["announcement_risk"], "avoid")
        self.assertIn(title, result["announcement_titles"])
        self.assertIn(title, result["classification"]["avoid"])
        self.assertFalse(result["announcement_review_required"])

    def test_resolution_conflict_is_unknown_for_review_not_clean(self):
        title = "关于解除股份冻结的法律意见书"
        result = classify_announcement_risk([title])

        self.assertEqual(result["announcement_risk"], "unknown")
        self.assertIn(title, result["announcement_titles"])
        self.assertIn(title, result["classification"]["review"])
        self.assertTrue(result["announcement_review_required"])

    def test_pure_ignore_title_is_retained_as_ignored_evidence(self):
        title = "关于权益分派实施的法律意见书"
        result = classify_announcement_risk([title])

        self.assertEqual(result["announcement_risk"], "clean")
        self.assertIn(title, result["announcement_titles"])
        self.assertIn(title, result["announcement_ignored_titles"])
        self.assertIn(title, result["classification"]["ignored"])

    def test_hard_risk_is_classified_before_ignore_filter(self):
        title = "关于股份冻结事项的法律意见书"
        result = classify_announcement_titles([title])

        self.assertIn(title, result["avoid"])
        self.assertNotIn(title, result["ignored"])


if __name__ == "__main__":
    unittest.main()
