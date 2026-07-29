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


if __name__ == "__main__":
    unittest.main()
