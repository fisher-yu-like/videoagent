"""Stage 6 local/server diagnostic interface tests for ``stage6_debug``.

Run: ``& $PY -m unittest tests.test_stage6_debug -v`` (see
``docs/DEBUGGING.md``). Real inputs are ``runs/stage6_vace_inputs/s01/vace_job.json``,
the Stage 2 bundle, and ``third_party/VACE``; commands emit JSON to stdout and
``print-probe-command`` only prints a server command. Passing locally does not
prove CUDA readiness, upstream preprocessing, weights, or VACE inference.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from videoactagent.stage6_debug import (
    DEFAULT_PROBE_COMMAND,
    build_probe_command,
    doctor,
    verify_inputs,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_JOB = ROOT / "runs" / "stage6_vace_inputs" / "s01" / "vace_job.json"
REAL_BUNDLE = ROOT / "runs" / "stage2_control_bridge" / "control_bundle.json"
VACE_ROOT = ROOT / "third_party" / "VACE"


class Stage6DoctorTests(unittest.TestCase):
    def test_doctor_reports_independent_boolean_checks(self) -> None:
        report = doctor(VACE_ROOT)

        check_names = (
            "python_ok",
            "ffmpeg_ok",
            "torch_ok",
            "cuda_ok",
            "vace_checkout_ok",
            "vace_commit_ok",
        )
        for name in check_names:
            self.assertIs(type(report[name]), bool, name)
        self.assertEqual(report["ok"], all(report[name] for name in check_names))
        self.assertEqual(
            report["expected_vace_commit"],
            "48eb44f1c4be87cc65a98bff985a26976841e9f3",
        )

    def test_doctor_cli_json_and_exit_code_match_real_readiness(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "videoactagent.stage6_debug",
                "doctor",
                "--vace-root",
                str(VACE_ROOT),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        report = json.loads(completed.stdout)
        self.assertEqual(completed.returncode == 0, report["ok"])
        self.assertIn(completed.returncode, (0, 1))


class Stage6InputVerificationTests(unittest.TestCase):
    def test_verify_real_stage6_inputs_reports_decoded_media(self) -> None:
        report = verify_inputs(REAL_JOB, REAL_BUNDLE)

        self.assertTrue(report["ok"])
        self.assertEqual(report["selected_shot_id"], "s01")
        self.assertEqual(
            report["job"]["sha256"],
            "2ecc5fe51b9fe9d2e9bf608a0829c68b316fb45ae1b8a444bc661375a6cd5063",
        )
        self.assertEqual(report["bundle"]["sha256"], "febf93a5ed5e1a3e8611322c280126239b57164ec56e429708f283d03f7aa617")
        self.assertEqual(report["proxy_video"]["frame_count"], 15)
        self.assertEqual(report["proxy_video"]["dimensions"], [960, 540])
        self.assertEqual(report["proxy_video"]["fps"], 3.0)
        self.assertEqual(report["mask_video"]["frame_count"], 15)
        self.assertEqual(report["mask_video"]["dimensions"], [960, 540])
        self.assertFalse(report["evidence"]["source_validation_passed"])
        self.assertFalse(report["evidence"]["inference_success"])

    def test_verify_inputs_cli_emits_machine_readable_json(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "videoactagent.stage6_debug",
                "verify-inputs",
                "--job",
                str(REAL_JOB),
                "--bundle",
                str(REAL_BUNDLE),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["ok"])


class Stage6ProbeCommandTests(unittest.TestCase):
    def test_probe_command_is_exact_pinned_server_command(self) -> None:
        self.assertEqual(
            build_probe_command(),
            "cd /root/videoactagent && "
            "/root/venvs/vace/bin/python -m videoactagent.vace_preprocess_probe "
            "--job runs/stage6_vace_inputs/s01/vace_job.json "
            "--vace-root third_party/VACE "
            "--output runs/stage6_vace_inputs/s01/source_validation.json "
            "--validated-job-output "
            "runs/stage6_vace_inputs/s01/vace_job.validated.json",
        )
        self.assertEqual(DEFAULT_PROBE_COMMAND, build_probe_command())

    def test_print_probe_command_does_not_execute_or_create_report(self) -> None:
        report_path = ROOT / "runs" / "stage6_vace_inputs" / "s01" / "source_validation.json"
        existed_before = report_path.exists()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "videoactagent.stage6_debug",
                "print-probe-command",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), DEFAULT_PROBE_COMMAND)
        self.assertEqual(report_path.exists(), existed_before)


if __name__ == "__main__":
    unittest.main()
