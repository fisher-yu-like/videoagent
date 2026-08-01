from copy import deepcopy
import math
import unittest

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.multicam_eval import evaluate_multicam_iteration
from videoactagent.multicam_plan import load_multicam_plan


def _direction(position, target=(0.0, 0.0, 0.8)):
    values = [target[i] - position[i] for i in range(3)]
    length = math.sqrt(sum(value * value for value in values))
    return [value / length for value in values]


def fixture():
    cameras = {}
    positions = {
        "camera_a": [0.0, -7.0, 2.4],
        "camera_b": [-2.0, -5.0, 2.0],
        "camera_c": [2.0, 5.0, 2.0],
    }
    for camera_id in positions:
        cameras[camera_id] = {
            "video": {"frame_count": 3, "fps": 3, "resolution": [160, 90]},
        }
    frames = []
    for frame in range(1, 4):
        frames.append({
            "frame": frame,
            "actors": {"actor_a": [-0.5, 0.0, 0.0], "actor_b": [0.5, 0.0, 0.0]},
            "cameras": {
                camera_id: {
                    "position": position,
                    "view_direction": _direction(position),
                    "focal_length_mm": 35.0,
                }
                for camera_id, position in positions.items()
            },
        })
    return {"shared_world_frames": frames, "cameras": cameras}


class MulticamEvalTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_multicam_plan(
            VALID_PLAN, scene_id="station_reunion", actors=["actor_a", "actor_b"]
        )

    def test_measures_sync_visibility_and_orientation(self):
        report = evaluate_multicam_iteration(plan=self.plan, render_manifest=fixture())
        self.assertEqual(report["sync"], {
            "frame_count_equal": True, "fps_equal": True, "duration_equal": True,
        })
        self.assertEqual(report["coverage"], {
            "camera_a": 1.0, "camera_b": 1.0, "camera_c": 1.0,
        })
        self.assertEqual(report["responsibility_target_visibility"]["minimum"], 1.0)
        self.assertTrue(report["orientation"]["passed"])
        self.assertEqual(report["human_composition_status"], "unknown")
        self.assertTrue(report["automatic_passed"])

    def test_missing_frame_and_abrupt_turn_fail(self):
        value = fixture()
        value["cameras"]["camera_b"]["video"]["frame_count"] = 2
        value["shared_world_frames"][1]["cameras"]["camera_a"]["view_direction"] = [1, 0, 0]
        report = evaluate_multicam_iteration(plan=self.plan, render_manifest=value)
        self.assertFalse(report["sync"]["frame_count_equal"])
        self.assertFalse(report["orientation"]["passed"])
        self.assertFalse(report["automatic_passed"])


if __name__ == "__main__":
    unittest.main()
