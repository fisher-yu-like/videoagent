"""Stage 4 camera-motion heuristic tests for ``videoactagent.camera_eval``.

Run: ``& $PY -m unittest tests.test_camera_eval -v`` (see
``docs/DEBUGGING.md``). Real inputs include the persisted Kling first/middle/last
frames under ``runs/stage3_api/20260729T012120Z_kling_d8bde2ef/frames``; the
persistent CLI output is ``runs/stage4_camera_eval/kling_s01_camera_eval.json``.
Synthetic phase-correlation cases are mechanics only; only the real-frame case
describes observed video, and even ``matched`` is heuristic rather than pose GT.
"""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np


class PhaseCorrelationTests(unittest.TestCase):
    def test_estimates_observed_content_translation_with_documented_sign(self):
        from videoactagent.camera_eval import estimate_translation

        rng = np.random.default_rng(20260729)
        reference = rng.normal(size=(96, 128))
        reference[20:45, 35:70] += 4.0
        observed = np.roll(reference, shift=(3, -7), axis=(0, 1))

        translation = estimate_translation(reference, observed)

        self.assertAlmostEqual(translation.dx, -7.0, delta=0.25)
        self.assertAlmostEqual(translation.dy, 3.0, delta=0.25)
        self.assertGreater(translation.confidence, 1.0)

    def test_identical_images_have_zero_shift_and_finite_confidence(self):
        from videoactagent.camera_eval import estimate_translation

        image = np.arange(64 * 64, dtype=np.float64).reshape(64, 64)
        translation = estimate_translation(image, image.copy())

        self.assertAlmostEqual(translation.dx, 0.0, delta=0.01)
        self.assertAlmostEqual(translation.dy, 0.0, delta=0.01)
        self.assertTrue(np.isfinite(translation.confidence))


class CameraMotionVerdictTests(unittest.TestCase):
    REAL_FRAMES = Path(
        "runs/stage3_api/20260729T012120Z_kling_d8bde2ef/frames"
    )

    def test_truck_right_verdict_uses_negative_background_translation(self):
        from videoactagent.camera_eval import classify_translation

        self.assertEqual(
            classify_translation(-8.0, "truck_right", confidence=2.0),
            "matched",
        )
        self.assertEqual(
            classify_translation(-2.0, "truck_right", confidence=2.0),
            "insufficient",
        )
        self.assertEqual(
            classify_translation(8.0, "truck_right", confidence=2.0),
            "opposite",
        )
        self.assertEqual(
            classify_translation(-59.0, "truck_right", confidence=1.08),
            "matched",
        )
        self.assertEqual(
            classify_translation(
                -59.0,
                "truck_right",
                confidence=1.08,
                minimum_confidence=1.25,
            ),
            "inconclusive",
        )

    def test_direction_and_confidence_are_reported_separately(self):
        from videoactagent.camera_eval import decompose_translation_evidence

        evidence = decompose_translation_evidence(
            dx=-59.0,
            expected_motion="truck_right",
            confidence=1.08,
        )

        self.assertEqual(evidence["observed_background_direction"], "negative")
        self.assertEqual(evidence["direction_verdict"], "matched")
        self.assertTrue(evidence["confidence_gate_passed"])
        self.assertEqual(evidence["confidence_verdict"], "passed")
        self.assertEqual(evidence["overall_verdict"], "matched")

    def test_strict_confidence_override_reproduces_inconclusive_verdict(self):
        from videoactagent.camera_eval import decompose_translation_evidence

        evidence = decompose_translation_evidence(
            dx=-59.0,
            expected_motion="truck_right",
            confidence=1.0803985977360666,
            minimum_confidence=1.25,
        )

        self.assertEqual(evidence["direction_verdict"], "matched")
        self.assertFalse(evidence["confidence_gate_passed"])
        self.assertEqual(evidence["overall_verdict"], "inconclusive")

    def test_minimum_confidence_must_be_finite_and_greater_than_one(self):
        from videoactagent.camera_eval import decompose_translation_evidence

        for minimum_confidence in (1.0, 0.9, float("nan"), float("inf")):
            with self.subTest(minimum_confidence=minimum_confidence):
                with self.assertRaisesRegex(
                    ValueError,
                    "minimum_confidence must be finite and greater than 1.0",
                ):
                    decompose_translation_evidence(
                        dx=-59.0,
                        expected_motion="truck_right",
                        confidence=1.08,
                        minimum_confidence=minimum_confidence,
                    )

    def test_direction_evidence_does_not_claim_camera_success(self):
        from videoactagent.camera_eval import decompose_translation_evidence

        evidence = decompose_translation_evidence(
            dx=-8.0,
            expected_motion="truck_right",
            confidence=2.0,
        )

        self.assertEqual(evidence["direction_verdict"], "matched")
        self.assertEqual(evidence["overall_verdict"], "matched")

    def test_threshold_boundary_is_consistently_near_zero_and_insufficient(self):
        from videoactagent.camera_eval import decompose_translation_evidence

        for dx in (-5.0, 5.0):
            with self.subTest(dx=dx):
                evidence = decompose_translation_evidence(
                    dx=dx,
                    expected_motion="truck_right",
                    confidence=2.0,
                    threshold_px=5.0,
                )

                self.assertEqual(
                    evidence["observed_background_direction"], "near_zero"
                )
                self.assertEqual(evidence["direction_verdict"], "insufficient")
                self.assertEqual(evidence["overall_verdict"], "insufficient")

    def test_evaluates_actual_kling_frames_as_heuristic_evidence(self):
        from videoactagent.camera_eval import evaluate_camera_motion

        if not self.REAL_FRAMES.is_dir():
            raise AssertionError("real Stage 3 Kling frames are required")
        result = evaluate_camera_motion(
            self.REAL_FRAMES / "first.png",
            self.REAL_FRAMES / "middle.png",
            self.REAL_FRAMES / "last.png",
            expected_motion="truck_right",
            crop_fraction=0.42,
        )

        self.assertEqual(result["expected_background_dx"], "negative")
        self.assertEqual(result["verdict"], "matched")
        self.assertEqual(result["evidence_type"], "heuristic_phase_correlation")
        self.assertEqual(result["minimum_confidence"], 1.05)
        self.assertEqual(result["dimensions"], [1280, 720])
        self.assertEqual(result["crop_bounds"], [0, 0, 1280, 302])
        direction = result["directional_evidence"]["first_to_last"]
        self.assertEqual(direction["observed_background_direction"], "negative")
        self.assertEqual(direction["direction_verdict"], "matched")
        self.assertTrue(direction["confidence_gate_passed"])
        self.assertEqual(direction["overall_verdict"], "matched")
        strict = result["strict_reference"]
        self.assertEqual(strict["minimum_confidence"], 1.25)
        self.assertEqual(strict["verdict"], "inconclusive")
        self.assertEqual(
            strict["directional_evidence"]["first_to_last"]["overall_verdict"],
            "inconclusive",
        )
        self.assertEqual(
            set(result["measurements"]),
            {"first_to_middle", "middle_to_last", "first_to_last"},
        )
        for measurement in result["measurements"].values():
            self.assertEqual(set(measurement), {"dx", "dy", "confidence"})
        self.assertEqual(len(result["inputs"]), 3)
        for value in result["inputs"].values():
            self.assertEqual(len(value["sha256"]), 64)

    def test_cli_writes_actual_evaluation_json(self):
        from videoactagent.camera_eval import main

        if not self.REAL_FRAMES.is_dir():
            raise AssertionError("real Stage 3 Kling frames are required")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "camera_eval.json"
            main(
                [
                    "--first",
                    str(self.REAL_FRAMES / "first.png"),
                    "--middle",
                    str(self.REAL_FRAMES / "middle.png"),
                    "--last",
                    str(self.REAL_FRAMES / "last.png"),
                    "--shotscript",
                    "examples/station_shotscript.json",
                    "--shot",
                    "s01",
                    "--output",
                    str(output),
                ]
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["expected_motion"], "truck_right")
            self.assertEqual(result["minimum_confidence"], 1.05)
            self.assertEqual(result["verdict"], "matched")
            self.assertEqual(result["strict_reference"]["verdict"], "inconclusive")

    def test_cli_minimum_confidence_override_reproduces_old_result(self):
        from videoactagent.camera_eval import main

        if not self.REAL_FRAMES.is_dir():
            raise AssertionError("real Stage 3 Kling frames are required")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "strict_camera_eval.json"
            main(
                [
                    "--first",
                    str(self.REAL_FRAMES / "first.png"),
                    "--middle",
                    str(self.REAL_FRAMES / "middle.png"),
                    "--last",
                    str(self.REAL_FRAMES / "last.png"),
                    "--shotscript",
                    "examples/station_shotscript.json",
                    "--shot",
                    "s01",
                    "--output",
                    str(output),
                    "--minimum-confidence",
                    "1.25",
                ]
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["minimum_confidence"], 1.25)
            self.assertEqual(result["verdict"], "inconclusive")
            self.assertEqual(result["strict_reference"]["verdict"], "inconclusive")

    def test_cli_does_not_reuse_or_clobber_a_fixed_temporary_name(self):
        from videoactagent.camera_eval import main

        if not self.REAL_FRAMES.is_dir():
            raise AssertionError("real Stage 3 Kling frames are required")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "camera_eval.json"
            fixed_temporary = output.with_name(f".{output.name}.tmp")
            fixed_temporary.write_text("pre-existing sentinel", encoding="utf-8")
            main(
                [
                    "--first", str(self.REAL_FRAMES / "first.png"),
                    "--middle", str(self.REAL_FRAMES / "middle.png"),
                    "--last", str(self.REAL_FRAMES / "last.png"),
                    "--shotscript", "examples/station_shotscript.json",
                    "--shot", "s01",
                    "--output", str(output),
                ]
            )

            self.assertEqual(
                fixed_temporary.read_text(encoding="utf-8"),
                "pre-existing sentinel",
            )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["verdict"],
                "matched",
            )

    def test_cli_rejects_output_aliasing_an_input_without_clobbering_it(self):
        from videoactagent.camera_eval import main

        if not self.REAL_FRAMES.is_dir():
            raise AssertionError("real Stage 3 Kling frames are required")
        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "first.png"
            first.write_bytes((self.REAL_FRAMES / "first.png").read_bytes())
            before = first.read_bytes()
            with self.assertRaisesRegex(ValueError, "output must not alias"):
                main([
                    "--first", str(first),
                    "--middle", str(self.REAL_FRAMES / "middle.png"),
                    "--last", str(self.REAL_FRAMES / "last.png"),
                    "--shotscript", "examples/station_shotscript.json",
                    "--shot", "s01",
                    "--output", str(first),
                ])
            self.assertEqual(before, first.read_bytes())


if __name__ == "__main__":
    unittest.main()
