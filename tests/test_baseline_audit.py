"""Stage 5 pinned-source audit tests for ``videoactagent.baseline_audit``.

Run: ``& $PY -m unittest tests.test_baseline_audit -v`` (see
``docs/DEBUGGING.md``). Inputs are ``baselines/manifest.json`` and the real
``third_party/VACE``/``third_party/ReCamMaster`` checkouts; generated reports
use temporary directories. This verifies source provenance, not model weights,
CUDA inference, or generated-video quality.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MANIFEST = Path("baselines/manifest.json")
THIRD_PARTY = Path("third_party")


class BaselineAuditTests(unittest.TestCase):
    def test_git_head_uses_checkout_scoped_safe_directory_without_global_config(self):
        from videoactagent.baseline_audit import _git_head

        checkout = (THIRD_PARTY / "VACE").resolve()
        expected_head = "a" * 40
        with patch("videoactagent.baseline_audit.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = expected_head + "\n"

            actual_head = _git_head(checkout)

        self.assertEqual(actual_head, expected_head)
        run.assert_called_once_with(
            [
                "git",
                "-c",
                f"safe.directory={checkout}",
                "-C",
                str(checkout),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    def test_manifest_pins_official_baseline_commits(self):
        self.assertTrue(MANIFEST.is_file(), "baseline manifest is missing")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        baselines = {item["name"]: item for item in manifest["baselines"]}

        self.assertEqual(
            baselines["VACE"]["commit"],
            "48eb44f1c4be87cc65a98bff985a26976841e9f3",
        )
        self.assertEqual(
            baselines["ReCamMaster"]["commit"],
            "fcf98bc86e876bb534518cd99e8a65b282f0f16e",
        )
        self.assertEqual(baselines["VACE"]["role"], "primary_structural_control")
        self.assertEqual(
            baselines["ReCamMaster"]["role"],
            "camera_rerender_comparator",
        )

    def test_actual_checkouts_match_manifest_and_required_files(self):
        from videoactagent.baseline_audit import audit_baselines

        report = audit_baselines(MANIFEST, THIRD_PARTY)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        expected_hash_files = {
            baseline["name"]: set(baseline["hash_files"])
            for baseline in manifest["baselines"]
        }

        self.assertEqual(report["status"], "verified_source_only")
        self.assertFalse(report["weights_downloaded"])
        self.assertFalse(report["inference_verified"])
        for baseline in report["baselines"]:
            self.assertTrue(baseline["checkout_exists"], baseline["name"])
            self.assertTrue(baseline["commit_match"], baseline["name"])
            self.assertTrue(all(baseline["required_files"].values()))
            self.assertEqual(
                set(baseline["hashed_files"]),
                expected_hash_files[baseline["name"]],
            )
            for record in baseline["hashed_files"].values():
                self.assertEqual(
                    set(record),
                    {"exists", "is_file", "bytes", "sha256"},
                )
                self.assertTrue(record["exists"])
                self.assertTrue(record["is_file"])
                self.assertEqual(len(record["sha256"]), 64)
                self.assertGreater(record["bytes"], 0)

    def test_missing_hash_file_makes_source_incomplete_and_is_reported(self):
        from videoactagent.baseline_audit import audit_baselines

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        baseline = dict(manifest["baselines"][0])
        missing_name = "missing-audit-evidence.txt"
        baseline["hash_files"] = [*baseline["hash_files"], missing_name]

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"baselines": [baseline]}),
                encoding="utf-8",
            )
            report = audit_baselines(manifest_path, THIRD_PARTY)

        self.assertEqual(report["status"], "source_incomplete")
        missing_record = report["baselines"][0]["hashed_files"][missing_name]
        self.assertEqual(
            missing_record,
            {
                "exists": False,
                "is_file": False,
                "bytes": None,
                "sha256": None,
            },
        )

    def test_directory_hash_path_is_reported_as_existing_non_file(self):
        from videoactagent.baseline_audit import audit_baselines

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        baseline = dict(manifest["baselines"][0])
        directory_name = "vace"
        baseline["hash_files"] = [directory_name]

        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"baselines": [baseline]}),
                encoding="utf-8",
            )
            report = audit_baselines(manifest_path, THIRD_PARTY)

        self.assertEqual(report["status"], "source_incomplete")
        self.assertEqual(
            report["baselines"][0]["hashed_files"][directory_name],
            {
                "exists": True,
                "is_file": False,
                "bytes": None,
                "sha256": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
