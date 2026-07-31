"""Smoke tests for the installed console dispatcher."""

import io
from contextlib import redirect_stdout, redirect_stderr
import unittest


class ConsoleDispatcherTests(unittest.TestCase):
    def test_help_lists_commands_and_returns_zero(self):
        from videoactagent.cli import main

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--help"])
        self.assertEqual(result, 0)
        self.assertIn("trajectory-observe", output.getvalue())
        self.assertIn("jd-smoke", output.getvalue())
        self.assertIn("director-loop", output.getvalue())

    def test_unknown_command_returns_two_without_importing_a_module(self):
        from videoactagent.cli import main

        output = io.StringIO()
        with redirect_stderr(output):
            result = main(["not-a-command"])
        self.assertEqual(result, 2)
        self.assertIn("unknown command", output.getvalue())

    def test_dispatches_module_help(self):
        from videoactagent.cli import main

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["trajectory", "--help"])
        self.assertEqual(result, 0)
        self.assertIn("usage:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
