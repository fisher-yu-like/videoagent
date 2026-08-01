import unittest

from videoactagent.director_annotation import CameraKeyframe
from videoactagent.multicam_prompt import compile_multicam_prompt


def state(key, t, *, position, look_at, focal=35.0, roll=0.0):
    return CameraKeyframe(
        keyframe_id=key,
        t=t,
        position=position,
        look_at=look_at,
        focal_length_mm=focal,
        shot_size="wide",
        interpolation="linear",
        roll_degrees=roll,
    )


class MulticamPromptTests(unittest.TestCase):
    def setUp(self):
        self.bounds = (-3.0, 3.0, -4.0, 4.0)

    def compile_pair(self, first, second):
        return compile_multicam_prompt(
            world_bounds=self.bounds,
            camera_states={
                "camera_a": (first, second),
                "camera_b": (first, second),
                "camera_c": (first, second),
            },
        )

    def test_small_numeric_drift_compiles_as_fixed(self):
        first = state("K0", 0.0, position=(0.0, -8.0, 3.0), look_at=(0.0, 0.0, 1.25))
        second = state(
            "K1", 1.0,
            position=(0.039, -8.0, 3.0),
            look_at=(0.089, 0.0, 1.25),
            focal=35.9,
            roll=0.4,
        )
        prompt = self.compile_pair(first, second)
        self.assertIn("camera_a holds position and keeps a fixed look-at", prompt)
        self.assertNotIn("look-at changes", prompt)
        self.assertNotIn("focal length changes", prompt)
        self.assertNotIn("roll changes", prompt)

    def test_changes_above_tolerance_are_reported(self):
        first = state("K0", 0.0, position=(0.0, -8.0, 3.0), look_at=(0.0, 0.0, 1.25))
        second = state(
            "K1", 1.0,
            position=(0.06, -8.0, 3.0),
            look_at=(0.11, 0.0, 1.25),
            focal=36.1,
            roll=0.6,
        )
        prompt = self.compile_pair(first, second)
        self.assertIn("camera moves", prompt)
        self.assertIn("look-at changes", prompt)
        self.assertIn("focal length changes", prompt)
        self.assertIn("roll changes", prompt)


if __name__ == "__main__":
    unittest.main()
