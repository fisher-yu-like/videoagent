from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CodegenLabPanelTests(unittest.TestCase):
    def test_page_has_truthful_controls_and_evidence_panels(self):
        html = (ROOT / "static" / "codegen_lab.html").read_text(encoding="utf-8")
        for marker in (
            'id="prepareButton"', 'id="generateButton"', 'id="smokeButton"', 'id="fullButton"',
            'id="statusTimeline"', 'id="resultVideo"', 'id="evidencePanel"', 'id="failurePanel"',
        ):
            self.assertIn(marker, html)
        self.assertIn("真实 DeepSeek 调用", html)
        self.assertIn("不会自动重试", html)
        self.assertIn("/api/prepare", html)
        self.assertNotIn("api-key", html.lower())


if __name__ == "__main__":
    unittest.main()
