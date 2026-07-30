"""Payload-preview tests for the isolated API examples.

Run: ``python -m unittest tests.test_dry_run -v`` (see ``docs/USAGE.md``).
Inputs are the scripts' built-in dry-run defaults and output is JSON captured
from stdout. ``--dry-run`` deliberately performs no API call; these assertions
only validate payload construction and are never real generation acceptance.
"""

import json
import locale
import subprocess
import sys
import unittest


class DryRunTests(unittest.TestCase):
    def run_cli(self, script, *args):
        completed = subprocess.run(
            [sys.executable, script, "--dry-run", *args],
            check=False,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_seedance_t2v_dry_run_prints_payload_builder_output(self):
        payload = self.run_cli("examples/api/seedance_demo.py", "t2v")
        self.assertEqual(payload["model"], "Doubao-Seedance-2.0")
        self.assertEqual(payload["parameters"]["duration"], 5)

    def test_kling_t2v_dry_run_prints_payload_builder_output(self):
        payload = self.run_cli("examples/api/kling_demo.py", "t2v")
        self.assertEqual(payload["model"], "Kling-V2-5-Turbo")
        self.assertEqual(payload["parameters"]["duration"], 5)


if __name__ == "__main__":
    unittest.main()
