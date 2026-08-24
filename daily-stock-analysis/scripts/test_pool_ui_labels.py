import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


class PoolUiLabelTests(unittest.TestCase):
    def test_gui_uses_unambiguous_dual_pool_label(self):
        source = (SCRIPT_DIR / "a_share_screen_gui.py").read_text(encoding="utf-8")

        self.assertIn("开始双池筛选", source)
        self.assertIn("趋势观察池", source)
        self.assertIn("趋势确认池", source)

    def test_legacy_streamlit_and_html_entrypoints_are_removed(self):
        self.assertFalse((SCRIPT_DIR / "streamlit_app.py").exists())
        self.assertFalse((SCRIPT_DIR.parent / "运行A股筛选Web.command").exists())
        source = (SCRIPT_DIR / "a_share_screen_gui.py").read_text(encoding="utf-8")
        self.assertNotIn("webbrowser", source)
        self.assertNotIn("open_html_preview", source)



if __name__ == "__main__":
    unittest.main()
