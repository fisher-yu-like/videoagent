"""Stage 3 run-recording unit test for ``videoactagent.run_record.RunDirectory``.

Run: ``& $PY -m unittest tests.test_run_directory -v`` (see
``docs/DEBUGGING.md``). A request JSON is written to a temporary directory and
checked for authorization redaction. This is filesystem/security mechanics;
there is no API call and no real generation artifact.
"""

import json
from pathlib import Path
import tempfile
import unittest


class RunDirectoryTests(unittest.TestCase):
    def test_writes_actual_json_file_and_redacts_authorization(self):
        try:
            from videoactagent.run_record import RunDirectory
        except ModuleNotFoundError as exc:
            self.fail(f"RunDirectory module is missing: {exc}")
        with tempfile.TemporaryDirectory() as root:
            run = RunDirectory.create(Path(root), "seedance")
            path = run.write_json(
                "request.json",
                {"Authorization": "Bearer local-test-value", "prompt": "hello"},
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(path.is_file())
            self.assertEqual(saved["Authorization"], "[REDACTED]")
            self.assertEqual(saved["prompt"], "hello")


if __name__ == "__main__":
    unittest.main()
