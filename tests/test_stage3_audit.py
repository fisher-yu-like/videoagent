"""Stage 3 offline audit tests for the persisted real Kling API run.

Run: ``& $PY -m unittest tests.test_stage3_audit -v`` (see
``docs/DEBUGGING.md``). Real input/output evidence lives in
``runs/stage3_api/20260729T012120Z_kling_d8bde2ef`` (including ``result.mp4``
and ``audit.json``), with ``D:\\blender\\blender.exe`` used for decode metadata.
Network entry points are blocked; tampered fixtures test rejection mechanics,
while only the persisted run constitutes prior real API evidence.
"""

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch
import urllib.request


REAL_RUN = Path(
    "runs/stage3_api/20260729T012120Z_kling_d8bde2ef"
)
REAL_SEEDANCE_RUN = Path(
    "runs/trajectory_api_pilot/real/20260729T171238Z_seedance_cad24559"
)
BLENDER = Path(r"D:\blender\blender.exe")
EXPECTED_SHA256 = (
    "f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3"
)


class Stage3AuditTests(unittest.TestCase):
    def test_audits_actual_persisted_seedance_run_without_network(self):
        from videoactagent.stage3_audit import audit_existing_run

        audit = audit_existing_run(REAL_SEEDANCE_RUN, BLENDER)

        self.assertEqual(audit["backend"], "seedance")
        self.assertEqual(audit["status_sequence"], ["pending", "success"])
        self.assertEqual(audit["result"]["sha256"], "0cc51800c70f45f94314210ac590041a1e3f1ce3a913a375c4f4d24ce960bae2")
        self.assertEqual(audit["result"]["bytes"], 2_990_207)
        self.assertEqual(audit["result"]["blender_media"]["frames"], 121)
        self.assertFalse(audit["network_called"])

    def test_audits_actual_persisted_kling_run_without_network(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        audit = audit_existing_kling_run(REAL_RUN, BLENDER)

        self.assertEqual(
            audit["evidence_source"],
            "persisted_gateway_records_internal_consistency",
        )
        self.assertFalse(audit["network_called"])
        self.assertEqual(audit["backend"], "kling")
        self.assertEqual(
            audit["status_sequence"],
            ["pending", "running", "running", "success"],
        )
        self.assertEqual(audit["query_count"], 4)
        self.assertTrue(audit["checks"]["task_ids_match"])
        self.assertTrue(audit["checks"]["terminal_status_success"])
        self.assertTrue(audit["checks"]["download_record_matches_file"])
        self.assertEqual(audit["result"]["sha256"], EXPECTED_SHA256)
        self.assertEqual(audit["result"]["bytes"], 7_207_519)
        self.assertEqual(
            audit["result"]["blender_media"],
            {
                "blender_version": "5.1.2",
                "width": 1280,
                "height": 720,
                "frames": 121,
                "fps": 24.0,
                "duration_seconds": 121 / 24,
            },
        )
        self.assertEqual(
            audit["seedance"],
            {
                "outcome": "unknown",
                "source": "project_context_not_part_of_run",
            },
        )
        self.assertFalse(audit["billing_interpreted"])
        self.assertFalse(audit["quality_evaluated"])

    def test_audit_binds_every_json_evidence_file_to_its_exact_bytes(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        audit = audit_existing_kling_run(REAL_RUN, BLENDER)
        evidence_paths = [
            REAL_RUN / "metadata.json",
            REAL_RUN / "request.json",
            REAL_RUN / "response.json",
            REAL_RUN / "state.json",
            REAL_RUN / "download.json",
            *sorted(REAL_RUN.glob("query_*.json")),
        ]

        self.assertEqual(
            set(audit["evidence_files"]),
            {path.name for path in evidence_paths},
        )
        for path in evidence_paths:
            contents = path.read_bytes()
            self.assertEqual(
                audit["evidence_files"][path.name],
                {
                    "path": path.name,
                    "bytes": len(contents),
                    "sha256": hashlib.sha256(contents).hexdigest(),
                },
            )

    def test_rejects_download_manifest_hash_that_does_not_match_real_file(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            download_path = copied / "download.json"
            download = json.loads(download_path.read_text(encoding="utf-8"))
            download["sha256"] = "0" * 64
            download_path.write_text(json.dumps(download), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "download sha256 mismatch"):
                audit_existing_kling_run(copied, BLENDER)

    def test_cli_prints_hash_of_exact_bytes_written_on_windows(self):
        from videoactagent.stage3_audit import main

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "audit.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--run-dir",
                        str(REAL_RUN),
                        "--blender",
                        str(BLENDER),
                        "--output",
                        str(output),
                    ]
                )
            actual_hash = hashlib.sha256(output.read_bytes()).hexdigest()

            self.assertEqual(exit_code, 0)
            self.assertIn(f"sha256={actual_hash}", stdout.getvalue())

    def test_cli_rejects_output_that_would_overwrite_run_evidence(self):
        from videoactagent.stage3_audit import main

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            result = copied / "result.mp4"
            before = hashlib.sha256(result.read_bytes()).hexdigest()

            with self.assertRaisesRegex(ValueError, "reserved audit.json"):
                main(
                    [
                        "--run-dir",
                        str(copied),
                        "--blender",
                        str(BLENDER),
                        "--output",
                        str(result),
                    ]
                )

            self.assertEqual(hashlib.sha256(result.read_bytes()).hexdigest(), before)

    def test_cli_atomic_failure_preserves_existing_reserved_audit(self):
        from videoactagent.stage3_audit import main

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            output = copied / "audit.json"
            original = b"existing audit must survive\n"
            output.write_bytes(original)

            with (
                patch.object(Path, "replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                main(
                    [
                        "--run-dir",
                        str(copied),
                        "--blender",
                        str(BLENDER),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(copied.glob(".audit.json.*.tmp")), [])

    def test_rejects_request_endpoint_on_non_official_host(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            request_path = copied / "request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["endpoint"] = "https://example.invalid/v1/task/submit"
            request_path.write_text(json.dumps(request), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "request endpoint"):
                audit_existing_kling_run(copied, BLENDER)

    def test_rejects_non_kling_request_model(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            request_path = copied / "request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["payload"]["model"] = "Doubao-Seedance-2.0"
            request_path.write_text(json.dumps(request), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "request model"):
                audit_existing_kling_run(copied, BLENDER)

    def test_rejects_state_base_url_on_non_official_host(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            state_path = copied / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["base_url"] = "https://example.invalid"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "state base_url"):
                audit_existing_kling_run(copied, BLENDER)

    def test_rejects_terminal_video_urls_that_disagree_with_state(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / "run"
            shutil.copytree(REAL_RUN, copied, ignore=shutil.ignore_patterns("frames"))
            state_path = copied / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["video_urls"][0]["url"] = "https://example.invalid/other.mp4"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "terminal video URLs"):
                audit_existing_kling_run(copied, BLENDER)

    def test_real_audit_remains_offline_when_network_entry_points_fail(self):
        from videoactagent.stage3_audit import audit_existing_kling_run

        def network_forbidden(*args, **kwargs):
            raise AssertionError("network access is forbidden during offline audit")

        with (
            patch.object(socket, "socket", side_effect=network_forbidden),
            patch.object(socket, "create_connection", side_effect=network_forbidden),
            patch.object(urllib.request, "urlopen", side_effect=network_forbidden),
        ):
            audit = audit_existing_kling_run(REAL_RUN, BLENDER)

        self.assertFalse(audit["network_called"])


if __name__ == "__main__":
    unittest.main()
