from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CodegenLabPanelTests(unittest.TestCase):
    def test_page_is_prompt_only_and_shows_final_video(self):
        html = (ROOT / "static" / "codegen_lab.html").read_text(encoding="utf-8")
        for marker in (
            'id="prompt"', 'id="runButton"', 'id="status"', 'id="resultVideo"', 'id="error"',
        ):
            self.assertIn(marker, html)
        self.assertIn("/api/prompt-run", html)
        self.assertIn("/api/prompt-status", html)
        self.assertIn("/api/prompt-latest", html)
        self.assertIn("video.load()", html)
        self.assertIn("onerror =", html)
        self.assertIn("ShotScript", html)
        self.assertIn("Blender", html)
        self.assertNotIn("prepareButton", html)
        self.assertNotIn("api/prepare", html)
        self.assertNotIn("api-key", html.lower())


if __name__ == "__main__":
    unittest.main()
