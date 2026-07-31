"""Regression tests for the VACE shell runner's nullable reference contract."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_vace_coded_draft import _make_coded_draft


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_vace_full_chain.sh"
GIT_BASH = Path(r"D:\Git\bin\bash.exe")


class VaceRunnerNullableRefTests(unittest.TestCase):
    def test_new_schema_dry_run_validates_and_omits_reference_flag(self):
        if not GIT_BASH.is_file():
            self.skipTest("Git Bash is unavailable")
        from videoactagent.vace_coded_draft import build_vace_coded_draft_job

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            job = build_vace_coded_draft_job(bundle, manifest, root / "job")
            output = root / "dry-run"
            environment = os.environ.copy()
            environment.update(
                {
                    "VACE_DRY_RUN": "1",
                    "PROJECT_ROOT": ROOT.as_posix(),
                    "VACE_PYTHON": (ROOT / ".venv" / "Scripts" / "python.exe").as_posix(),
                }
            )
            completed = subprocess.run(
                [
                    str(GIT_BASH),
                    SCRIPT.as_posix(),
                    job.as_posix(),
                    output.as_posix(),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            command = (output / "command.txt").read_text(encoding="utf-8")
            self.assertIn("--src_video", command)
            self.assertIn("--src_mask", command)
            self.assertNotIn("--src_ref_images", command)
            self.assertIn("VACE_DRY_RUN_OK", completed.stdout)

    def test_script_keeps_legacy_reference_but_never_indexes_null_mapping(self):
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("videoactagent.vace_coded_draft verify --job", script)
        self.assertIn("videoactagent.vace_full_chain validate", script)
        self.assertNotIn('job["mapping"]["src_ref_images"][0]', script)
        self.assertIn('if [ -n "$REF_IMAGE" ]; then', script)


if __name__ == "__main__":
    unittest.main()
