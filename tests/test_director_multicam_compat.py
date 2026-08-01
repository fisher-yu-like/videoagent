import io
from contextlib import redirect_stdout
from pathlib import Path
import unittest

from videoactagent.cli import COMMANDS, main


class DirectorMulticamCompatibilityTests(unittest.TestCase):
    def test_legacy_and_multicam_are_separate_commands(self):
        self.assertEqual(COMMANDS["director-loop"], "videoactagent.director_loop")
        self.assertEqual(
            COMMANDS["director-multicam"], "videoactagent.director_multicam"
        )

    def test_both_help_pages_are_available(self):
        for command in ("director-loop", "director-multicam"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([command, "--help"])
            self.assertEqual(code, 0)
            self.assertIn("usage:", output.getvalue().lower())

    def test_legacy_panel_is_not_replaced(self):
        html = Path("static/director_panel.html").read_text(encoding="utf-8")
        self.assertIn("人工导演循环", html)
        self.assertNotIn("Agent 多视角导演", html)


if __name__ == "__main__":
    unittest.main()
