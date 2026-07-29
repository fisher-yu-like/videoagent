"""Stage 7 closed-loop evaluation tests for ``videoactagent.closed_loop``.

Run: ``& $PY -m unittest tests.test_closed_loop -v`` (see
``docs/DEBUGGING.md``). Inputs are the real Kling MP4, Stage 4 report, station
ShotScript, and ``examples/stage7_s01_inspection.json``; the user-facing CLI
writes ``expectation.json``, ``feedback.json``, and ``revision.json`` under
``runs/stage7_closed_loop/kling_s01``. Temporary/tampered fixtures test binding
mechanics and are not new video-generation evidence.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
from unittest import mock
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHOTSCRIPT = ROOT / "examples" / "station_shotscript.json"
VIDEO = ROOT / "runs" / "stage3_api" / "20260729T012120Z_kling_d8bde2ef" / "result.mp4"
CAMERA_REPORT = ROOT / "runs" / "stage4_camera_eval" / "kling_s01_camera_eval.json"
INSPECTION = ROOT / "examples" / "stage7_s01_inspection.json"


class ClosedLoopTests(unittest.TestCase):
    def test_compiles_real_s01_expectation(self):
        from videoactagent.closed_loop import compile_expectation

        result = compile_expectation(SHOTSCRIPT, "s01")

        self.assertEqual(result["shot_id"], "s01")
        self.assertEqual(result["camera"]["motion"], "truck_right")
        self.assertEqual(result["camera"]["start"], [-1.0, -10.0, 6.0])
        self.assertEqual(result["camera"]["end"], [1.0, -10.0, 6.0])
        self.assertEqual(result["actors"][0]["actor_id"], "actor_a")
        self.assertEqual(result["actors"][0]["action"], "walk")
        self.assertEqual(result["continuity"]["screen_direction"], "left_to_right")

    def test_builds_feedback_from_real_kling_and_stage4_evidence(self):
        from videoactagent.closed_loop import build_feedback, compile_expectation

        expectation = compile_expectation(SHOTSCRIPT, "s01")
        feedback = build_feedback(
            expectation, VIDEO, CAMERA_REPORT, INSPECTION
        )

        self.assertEqual(feedback["video"]["sha256"], "f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3")
        self.assertEqual(feedback["automatic_camera"]["heuristic_verdict"], "matched")
        self.assertEqual(feedback["automatic_camera"]["verdict"], "inconclusive")
        self.assertEqual(
            feedback["automatic_camera"]["strict_reference"]["verdict"],
            "inconclusive",
        )
        self.assertEqual(feedback["automatic_camera"]["source"], "stage4_camera_eval")
        self.assertEqual(feedback["manual_visual_inspection"]["actor_a_action"], "matched")
        self.assertEqual(feedback["manual_visual_inspection"]["actors_facing"], "matched")

    def test_rejects_non_manual_inspection_source(self):
        from videoactagent.closed_loop import ClosedLoopError, build_feedback, compile_expectation

        inspection = json.loads(INSPECTION.read_text(encoding="utf-8"))
        inspection["source"] = "automatic_detector"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "inspection.json"
            path.write_text(json.dumps(inspection), encoding="utf-8")
            with self.assertRaisesRegex(ClosedLoopError, "manual_visual_inspection"):
                build_feedback(compile_expectation(SHOTSCRIPT, "s01"), VIDEO, CAMERA_REPORT, path)

    def test_rejects_tampered_strict_camera_gate_and_verdict(self):
        from videoactagent.closed_loop import ClosedLoopError, build_feedback, compile_expectation

        report = json.loads(CAMERA_REPORT.read_text(encoding="utf-8"))
        report["strict_reference"]["minimum_confidence"] = 1.01
        report["strict_reference"]["verdict"] = "matched"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "camera.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ClosedLoopError, "strict camera"):
                build_feedback(compile_expectation(SHOTSCRIPT, "s01"), VIDEO, path, INSPECTION)

    def test_rejects_malformed_camera_measurements_as_closed_loop_evidence(self):
        from videoactagent.closed_loop import ClosedLoopError, build_feedback, compile_expectation

        for field, value in (
            ("dx", None),
            ("dy", True),
            ("confidence", float("nan")),
            ("dx", 10**400),
        ):
            with self.subTest(field=field):
                report = json.loads(CAMERA_REPORT.read_text(encoding="utf-8"))
                report["measurements"]["first_to_middle"][field] = value
                with tempfile.TemporaryDirectory() as root:
                    path = Path(root) / "camera.json"
                    path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaisesRegex(ClosedLoopError, "finite numeric"):
                        build_feedback(compile_expectation(SHOTSCRIPT, "s01"), VIDEO, path, INSPECTION)

    def test_rejects_inspection_not_bound_to_real_video(self):
        from videoactagent.closed_loop import ClosedLoopError, build_feedback, compile_expectation

        inspection = json.loads(INSPECTION.read_text(encoding="utf-8"))
        inspection["video_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "inspection.json"
            path.write_text(json.dumps(inspection), encoding="utf-8")
            with self.assertRaisesRegex(ClosedLoopError, "video hash"):
                build_feedback(
                    compile_expectation(SHOTSCRIPT, "s01"),
                    VIDEO,
                    CAMERA_REPORT,
                    path,
                )

    def test_proposes_only_bounded_revision_for_inconclusive_camera(self):
        from videoactagent.closed_loop import propose_revision

        feedback = {
            "feedback_sha256": "a" * 64,
            "automatic_camera": {"verdict": "inconclusive"},
            "manual_visual_inspection": {
                "actor_a_action": "matched",
                "actor_b_action": "matched",
                "actors_facing": "matched",
                "screen_direction": "matched",
            },
        }
        revision = propose_revision(feedback)

        self.assertEqual(
            revision["operations"],
            [
                {"op": "enable_structural_proxy", "channel": "vace_src_video"},
                {"op": "preserve_camera_trajectory", "source": "shotscript"},
            ],
        )

    def test_cli_writes_three_hash_linked_records_without_network(self):
        from videoactagent.closed_loop import main, sha256_file

        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "evaluate",
                        "--shotscript", str(SHOTSCRIPT),
                        "--shot", "s01",
                        "--video", str(VIDEO),
                        "--camera-report", str(CAMERA_REPORT),
                        "--inspection", str(INSPECTION),
                        "--output-dir", str(output),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("CLOSED_LOOP_EVALUATED", stdout.getvalue())
            for name in ("expectation.json", "feedback.json", "revision.json"):
                self.assertTrue((output / name).is_file(), name)
                self.assertEqual(len(sha256_file(output / name)), 64)

            feedback = json.loads((output / "feedback.json").read_text(encoding="utf-8"))
            revision = json.loads((output / "revision.json").read_text(encoding="utf-8"))
            self.assertEqual(feedback["automatic_camera"]["verdict"], "inconclusive")
            self.assertEqual(
                revision["operations"],
                [
                    {"op": "enable_structural_proxy", "channel": "vace_src_video"},
                    {"op": "preserve_camera_trajectory", "source": "shotscript"},
                ],
            )

    def test_cli_group_publish_restores_previous_generation_on_failure(self):
        from videoactagent.closed_loop import main

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "closed-loop"
            output.mkdir()
            previous = {}
            for name in ("expectation.json", "feedback.json", "revision.json"):
                payload = ("old-" + name).encode("utf-8")
                (output / name).write_bytes(payload)
                previous[name] = payload

            real_replace = __import__("os").replace
            calls = 0

            def fail_publish(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected group publish failure")
                return real_replace(source, destination)

            with mock.patch("videoactagent.closed_loop.os.replace", side_effect=fail_publish):
                with self.assertRaisesRegex(OSError, "injected group publish failure"):
                    main([
                        "evaluate", "--shotscript", str(SHOTSCRIPT), "--shot", "s01",
                        "--video", str(VIDEO), "--camera-report", str(CAMERA_REPORT),
                        "--inspection", str(INSPECTION), "--output-dir", str(output),
                    ])

            self.assertEqual(
                previous,
                {name: (output / name).read_bytes() for name in previous},
            )

    def test_cli_rejects_output_directory_containing_input_evidence(self):
        from videoactagent.closed_loop import ClosedLoopError, main

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "closed-loop"
            output.mkdir()
            inspection = output / "inspection.json"
            inspection.write_bytes(INSPECTION.read_bytes())
            before = inspection.read_bytes()
            with self.assertRaisesRegex(ClosedLoopError, "contains input evidence"):
                main([
                    "evaluate", "--shotscript", str(SHOTSCRIPT), "--shot", "s01",
                    "--video", str(VIDEO), "--camera-report", str(CAMERA_REPORT),
                    "--inspection", str(inspection), "--output-dir", str(output),
                ])
            self.assertEqual(before, inspection.read_bytes())


if __name__ == "__main__":
    unittest.main()
