from copy import deepcopy
import unittest

from videoactagent.multicam_plan import MulticamPlanError, load_multicam_plan


VALID_PLAN = {
    "schema_version": "1.0",
    "scene_id": "station_reunion",
    "director_intent": "Keep both people spatially legible throughout the reunion.",
    "actor_staging": ["actor_a approaches actor_b", "actor_b remains waiting"],
    "locked_through_keyframe": None,
    "cameras": [
        {
            "camera_id": "camera_a",
            "role": "master",
            "target": "all_actors",
            "side": "south",
            "shot_size": "wide",
            "motion": "static",
            "look_at_policy": "actors_midpoint",
            "responsibility_segments": [[0.0, 0.4]],
            "constraints": ["keep both actors visible"],
            "rationale": "establish space",
        },
        {
            "camera_id": "camera_b",
            "role": "follow",
            "target": "actor_a",
            "side": "south_west",
            "shot_size": "medium",
            "motion": "follow",
            "look_at_policy": "target_actor",
            "responsibility_segments": [[0.4, 0.75]],
            "constraints": ["preserve headroom"],
            "rationale": "cover approach",
        },
        {
            "camera_id": "camera_c",
            "role": "reverse",
            "target": "actor_b",
            "side": "north_east",
            "shot_size": "medium",
            "motion": "arc",
            "look_at_policy": "target_actor",
            "responsibility_segments": [[0.75, 1.0]],
            "constraints": ["do not cross actor path"],
            "rationale": "cover response",
        },
    ],
}


class MulticamPlanTests(unittest.TestCase):
    def load(self, value=VALID_PLAN, *, locked=None):
        return load_multicam_plan(
            value,
            scene_id="station_reunion",
            actors=("actor_a", "actor_b"),
            locked_through_keyframe=locked,
        )

    def test_accepts_exact_three_camera_full_coverage_plan(self):
        plan = self.load()
        self.assertEqual(plan.scene_id, "station_reunion")
        self.assertEqual([camera.camera_id for camera in plan.cameras], [
            "camera_a", "camera_b", "camera_c"
        ])
        self.assertEqual(plan.cameras[1].target, "actor_a")

    def test_rejects_unknown_fields_and_camera_ids(self):
        unknown = deepcopy(VALID_PLAN)
        unknown["unexpected"] = True
        with self.assertRaisesRegex(MulticamPlanError, "fields"):
            self.load(unknown)
        wrong_id = deepcopy(VALID_PLAN)
        wrong_id["cameras"][2]["camera_id"] = "camera_d"
        with self.assertRaisesRegex(MulticamPlanError, "camera IDs"):
            self.load(wrong_id)

    def test_rejects_coverage_gaps_and_overlaps(self):
        gap = deepcopy(VALID_PLAN)
        gap["cameras"][1]["responsibility_segments"] = [[0.41, 0.75]]
        with self.assertRaisesRegex(MulticamPlanError, "coverage"):
            self.load(gap)
        overlap = deepcopy(VALID_PLAN)
        overlap["cameras"][1]["responsibility_segments"] = [[0.39, 0.75]]
        with self.assertRaisesRegex(MulticamPlanError, "coverage"):
            self.load(overlap)

    def test_rejects_unknown_actor_target(self):
        value = deepcopy(VALID_PLAN)
        value["cameras"][1]["target"] = "actor_x"
        with self.assertRaisesRegex(MulticamPlanError, "target"):
            self.load(value)

    def test_suffix_replan_cannot_change_locked_prefix(self):
        value = deepcopy(VALID_PLAN)
        value["locked_through_keyframe"] = "K1"
        with self.assertRaisesRegex(MulticamPlanError, "locked"):
            self.load(value, locked="K2")


if __name__ == "__main__":
    unittest.main()
