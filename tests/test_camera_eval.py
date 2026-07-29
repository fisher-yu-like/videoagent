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
            "inconclusive",
        )

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
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["evidence_type"], "heuristic_phase_correlation")
        self.assertEqual(result["minimum_confidence"], 1.25)
        self.assertEqual(result["dimensions"], [1280, 720])
        self.assertEqual(result["crop_bounds"], [0, 0, 1280, 302])
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


if __name__ == "__main__":
    unittest.main()
