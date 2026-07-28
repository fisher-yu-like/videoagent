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
        payload = self.run_cli("seedance_demo.py", "t2v")
        self.assertEqual(payload["model"], "Doubao-Seedance-2.0")
        self.assertEqual(payload["parameters"]["duration"], 5)

    def test_kling_t2v_dry_run_prints_payload_builder_output(self):
        payload = self.run_cli("kling_demo.py", "t2v")
        self.assertEqual(payload["model"], "Kling-V2-5-Turbo")
        self.assertEqual(payload["parameters"]["duration"], 5)


if __name__ == "__main__":
    unittest.main()
