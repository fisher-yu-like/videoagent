from copy import deepcopy
import math
import unittest

from videoactagent.scene_plan import ScenePlanDraft, ScenePlanError
from videoactagent.shotscript import ShotScript, ShotScriptError
from videoactagent.trajectory import TrajectoryInstruction


VALID_DRAFT = {
    "schema_version": "scene-plan-1.0",
    "scene_id": "new_station_story",
    "environment_preset": "station",
    "duration_seconds": 5.0,
    "fps": 3,
    "world_bounds": [-5.0, 5.0, -4.0, 4.0],
    "actors": [
        {
            "id": "traveler",
            "color": "#F28E2B",
            "action": "walk",
            "start": [-3.0, 0.0, 0.0],
            "end": [-0.5, 0.0, 0.0],
            "facing": "friend",
        },
        {
            "id": "friend",
            "color": "#4E79A7",
            "action": "wait",
            "start": [1.5, 0.0, 0.0],
            "end": [1.5, 0.0, 0.0],
            "facing": "traveler",
        },
    ],
    "objects": [
        {
            "id": "suitcase",
            "primitive": "cube",
            "semantic": "move",
            "start": [-2.8, 0.2],
            "end": [-0.3, 0.2],
        }
    ],
    "initial_camera": {
        "shot_size": "wide",
        "focal_length_mm": 35,
        "motion": "static",
        "start": [0.0, -10.0, 6.0],
        "end": [0.0, -10.0, 6.0],
        "look_at": "actors_midpoint",
    },
    "explanation": "Use one wide view to inspect the reunion staging.",
}


class ScenePlanTests(unittest.TestCase):
    def test_valid_scene_plan_compiles_to_existing_contracts(self):
        draft = ScenePlanDraft.from_dict(VALID_DRAFT)
        script_value = draft.to_shotscript("An original reunion story")
        trajectory_value = draft.to_trajectory()

        script = ShotScript.from_dict(script_value)
        trajectory = TrajectoryInstruction.from_dict(trajectory_value)

        self.assertEqual(draft.to_dict(), VALID_DRAFT)
        self.assertEqual(script.scene_id, "new_station_story")
        self.assertEqual([shot.shot_id for shot in script.shots], ["whole"])
        self.assertEqual(
            [track.target_id for track in trajectory.tracks],
            ["traveler", "friend", "suitcase"],
        )
        self.assertEqual(
            [point.t for point in trajectory.tracks[0].points],
            [0.0, 0.2, 0.5, 0.8, 1.0],
        )
        self.assertEqual(
            [(point.x, point.y) for point in trajectory.tracks[0].points],
            [(0.2, 0.5), (0.25, 0.5), (0.325, 0.5), (0.4, 0.5), (0.45, 0.5)],
        )

    def test_trajectory_y_uses_top_left_coordinates_for_noncentral_motion(self):
        value = deepcopy(VALID_DRAFT)
        value["actors"][0]["start"] = [-3.0, -2.0, 0.0]
        value["actors"][0]["end"] = [-0.5, 2.0, 0.0]
        track = TrajectoryInstruction.from_dict(
            ScenePlanDraft.from_dict(value).to_trajectory()
        ).tracks[0]

        self.assertEqual(
            [point.y for point in track.points],
            [0.75, 0.65, 0.5, 0.35, 0.25],
        )

    def test_objects_are_optional_and_all_presets_are_supported(self):
        for preset in (
            "station",
            "city_crosswalk",
            "forest_path",
            "studio_room",
            "cafe",
            "warehouse",
            "generic",
        ):
            value = deepcopy(VALID_DRAFT)
            value["environment_preset"] = preset
            value.pop("objects")
            with self.subTest(preset=preset):
                draft = ScenePlanDraft.from_dict(value)
                self.assertEqual(draft.to_dict()["objects"], [])
                self.assertEqual(
                    ShotScript.from_dict(draft.to_shotscript("story")).environment_preset,
                    preset,
                )

    def test_static_objects_compile_to_stationary_existing_trajectory_contract(self):
        value = deepcopy(VALID_DRAFT)
        value["objects"][0].update({
            "semantic": "static",
            "start": [0.0, 0.0],
            "end": [0.0, 0.0],
        })

        draft = ScenePlanDraft.from_dict(value)
        trajectory = TrajectoryInstruction.from_dict(draft.to_trajectory())
        object_track = trajectory.tracks[-1]

        self.assertEqual(draft.objects[0].semantic, "static")
        self.assertEqual(object_track.semantic, "move")
        self.assertEqual(
            [(point.x, point.y) for point in object_track.points],
            [(0.5, 0.5)] * 5,
        )

    def test_static_objects_must_not_change_position(self):
        value = deepcopy(VALID_DRAFT)
        value["objects"][0]["semantic"] = "static"

        with self.assertRaisesRegex(ScenePlanError, "static.*same start and end"):
            ScenePlanDraft.from_dict(value)

    def test_scene_plan_requires_exact_fields_and_one_to_three_actors(self):
        for field in (
            "schema_version",
            "scene_id",
            "environment_preset",
            "duration_seconds",
            "fps",
            "world_bounds",
            "actors",
            "initial_camera",
            "explanation",
        ):
            value = deepcopy(VALID_DRAFT)
            value.pop(field)
            with self.subTest(missing=field), self.assertRaisesRegex(
                ScenePlanError, "missing fields"
            ):
                ScenePlanDraft.from_dict(value)

        unknown = deepcopy(VALID_DRAFT)
        unknown["invented"] = True
        with self.assertRaisesRegex(ScenePlanError, "unknown fields"):
            ScenePlanDraft.from_dict(unknown)

        for actors in ([], deepcopy(VALID_DRAFT["actors"]) * 2):
            value = deepcopy(VALID_DRAFT)
            value["actors"] = actors
            with self.subTest(actor_count=len(actors)), self.assertRaisesRegex(
                ScenePlanError, "one to three"
            ):
                ScenePlanDraft.from_dict(value)

    def test_scene_plan_rejects_invalid_identity_enum_and_relationships(self):
        cases = (
            ("schema_version", "1.0", "schema_version"),
            ("environment_preset", "city", "environment_preset"),
            ("fps", 3.5, "fps"),
        )
        for field, replacement, message in cases:
            value = deepcopy(VALID_DRAFT)
            value[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                ScenePlanError, message
            ):
                ScenePlanDraft.from_dict(value)

        duplicate = deepcopy(VALID_DRAFT)
        duplicate["objects"][0]["id"] = "traveler"
        with self.assertRaisesRegex(ScenePlanError, "IDs must be globally unique"):
            ScenePlanDraft.from_dict(duplicate)

        bad_facing = deepcopy(VALID_DRAFT)
        bad_facing["actors"][0]["facing"] = "stranger"
        with self.assertRaisesRegex(ScenePlanError, r"actors\[0\]\.facing"):
            ScenePlanDraft.from_dict(bad_facing)

        bad_look_at = deepcopy(VALID_DRAFT)
        bad_look_at["initial_camera"]["look_at"] = "stranger"
        with self.assertRaisesRegex(ScenePlanError, "initial_camera.look_at"):
            ScenePlanDraft.from_dict(bad_look_at)

    def test_scene_plan_rejects_nonfinite_or_out_of_bounds_numbers(self):
        cases = []
        for replacement in (math.nan, math.inf, -math.inf):
            value = deepcopy(VALID_DRAFT)
            value["duration_seconds"] = replacement
            cases.append((value, "duration_seconds"))
        value = deepcopy(VALID_DRAFT)
        value["world_bounds"] = [5.0, -5.0, -4.0, 4.0]
        cases.append((value, "world_bounds"))
        value = deepcopy(VALID_DRAFT)
        value["actors"][0]["start"] = [50.0, 0.0, 0.0]
        cases.append((value, r"actors\[0\]\.start"))
        value = deepcopy(VALID_DRAFT)
        value["objects"][0]["end"] = [-0.3, -20.0]
        cases.append((value, r"objects\[0\]\.end"))
        value = deepcopy(VALID_DRAFT)
        value["initial_camera"]["focal_length_mm"] = math.nan
        cases.append((value, "initial_camera.focal_length_mm"))

        for index, (document, message) in enumerate(cases):
            with self.subTest(index=index), self.assertRaisesRegex(ScenePlanError, message):
                ScenePlanDraft.from_dict(document)

    def test_duration_and_fps_must_round_to_at_least_one_effective_frame(self):
        value = deepcopy(VALID_DRAFT)
        value["duration_seconds"] = 0.01
        value["fps"] = 1
        with self.assertRaisesRegex(ScenePlanError, "rounds to 0 frames"):
            ScenePlanDraft.from_dict(value)

    def test_actor_actions_use_the_explicit_proxy_label_set(self):
        supported = ("walk", "wait", "stand", "approach", "cross", "follow", "carry")
        for action in supported:
            value = deepcopy(VALID_DRAFT)
            value["actors"][0]["action"] = action
            with self.subTest(action=action):
                self.assertEqual(
                    ScenePlanDraft.from_dict(value).actors[0].action,
                    action,
                )

        value = deepcopy(VALID_DRAFT)
        value["actors"][0]["action"] = "perform_backflip"
        with self.assertRaisesRegex(ScenePlanError, r"actors\[0\]\.action"):
            ScenePlanDraft.from_dict(value)

    def test_scene_plan_rejects_bad_nested_fields_and_colors(self):
        value = deepcopy(VALID_DRAFT)
        value["actors"][0]["extra"] = "not allowed"
        with self.assertRaisesRegex(ScenePlanError, r"actors\[0\].*unknown fields"):
            ScenePlanDraft.from_dict(value)

        value = deepcopy(VALID_DRAFT)
        value["actors"][0]["color"] = "orange"
        with self.assertRaisesRegex(ScenePlanError, r"actors\[0\]\.color"):
            ScenePlanDraft.from_dict(value)

    def test_generic_is_a_valid_shotscript_environment(self):
        value = deepcopy(VALID_DRAFT)
        value["environment_preset"] = "generic"
        script = ScenePlanDraft.from_dict(value).to_shotscript("generic story")
        self.assertEqual(ShotScript.from_dict(script).environment_preset, "generic")

        script["environment_preset"] = "freeform_llm_scene"
        with self.assertRaisesRegex(ShotScriptError, "unsupported environment_preset"):
            ShotScript.from_dict(script)


if __name__ == "__main__":
    unittest.main()
