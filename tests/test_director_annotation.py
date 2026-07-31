from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from videoactagent.director_annotation import (
    DirectorAnnotationError,
    camera_trajectory_from_path,
    compile_director_annotation,
)


def contract() -> dict:
    value = {
        "story_id": "station_reunion",
        "shot_id": "whole",
        "duration_seconds": 5.0,
        "sample_count": 120,
        "actors": ["actor_a", "actor_b"],
        "keyframes": [
            {"id": "K0", "t": 0.0},
            {"id": "K1", "t": 0.2},
            {"id": "K2", "t": 0.5},
            {"id": "K3", "t": 0.8},
            {"id": "K4", "t": 1.0},
        ],
        "story_prompt": "A continuous station reunion shot.",
        "appearance_instruction": "Natural proportions and consistent station lighting.",
    }
    inherited = payload_keyframes(value["keyframes"])
    value["inheritance_sha256"] = "a" * 64
    value["inherited_keyframes"] = inherited
    return value


def payload_keyframes(keys: list[dict]) -> list[dict]:
    return [
        {
            "id": item["id"],
            "t": item["t"],
            "actors": {
                "actor_a": {"x": 0.16 + index * 0.11, "y": 0.5},
                "actor_b": {"x": 0.67, "y": 0.5},
            },
            "camera": {
                "position": [0.0 + index * 0.1, -10.0, 6.0],
                "look_at": [0.0 + index * 0.1, 0.0, 1.0],
                "focal_length_mm": 35.0 + index,
                "shot_size": "wide",
                "interpolation": "linear",
                "roll_degrees": 0.0,
            },
        }
        for index, item in enumerate(keys)
    ]


def payload() -> dict:
    expected = contract()
    frames = payload_keyframes(expected["keyframes"])
    for frame in frames:
        frame["camera_source"] = "inherited"
    return {
        "schema_version": "1.0",
        "author_id": "sy",
        "iteration_id": "D1",
        "parent_iteration_id": "D0",
        "auto_filled_values": 0,
        "visual_style": "source_default",
        "mood": "source_default",
        "frozen_through_keyframe": "K0",
        "inheritance_sha256": expected["inheritance_sha256"],
        "inherited_locked_values": deepcopy(expected["inherited_keyframes"][:1]),
        "keyframes": frames,
    }


class DirectorAnnotationTests(unittest.TestCase):
    def test_complete_annotation_compiles_all_three_controls(self) -> None:
        result = compile_director_annotation(payload(), contract())

        self.assertEqual(
            [track.target_id for track in result.actor_trajectory.tracks],
            ["actor_a", "actor_b"],
        )
        self.assertEqual(
            [state.keyframe_id for state in result.camera_trajectory],
            ["K0", "K1", "K2", "K3", "K4"],
        )
        self.assertEqual(result.camera_trajectory[2].focal_length_mm, 37.0)
        self.assertIn("K0 to K1, actor_a moves right", result.compiled_prompt)
        self.assertIn(
            "K0 to K1, actor_a and actor_b move closer",
            result.compiled_prompt,
        )
        canonical = json.loads(result.canonical_annotation)
        self.assertEqual(canonical["author_id"], "sy")
        self.assertEqual(canonical["prompt_compiler_version"], "trajectory-facts-v2")
        self.assertNotIn("visible_state", canonical["keyframes"][0])
        self.assertEqual(json.loads(result.camera_document)["states"][4]["keyframe_id"], "K4")

    def test_canonical_annotation_can_be_revalidated(self) -> None:
        first = compile_director_annotation(payload(), contract())
        second = compile_director_annotation(json.loads(first.canonical_annotation), contract())
        self.assertEqual(second.canonical_annotation, first.canonical_annotation)

    def test_camera_document_round_trips_from_disk(self) -> None:
        compiled = compile_director_annotation(payload(), contract())
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "camera.json"
            path.write_bytes(compiled.camera_document)
            states = camera_trajectory_from_path(path)
        self.assertEqual(len(states), 5)
        self.assertEqual(states[0].position, (0.0, -10.0, 6.0))
        self.assertEqual(states[-1].keyframe_id, "K4")

    def test_rejects_missing_camera_state(self) -> None:
        value = payload()
        value["keyframes"][2].pop("camera")
        with self.assertRaisesRegex(DirectorAnnotationError, "camera"):
            compile_director_annotation(value, contract())

    def test_rejects_free_text_keyframe_prompt(self) -> None:
        value = payload()
        value["keyframes"][3]["visible_state"] = "move left"
        with self.assertRaisesRegex(DirectorAnnotationError, "fields"):
            compile_director_annotation(value, contract())

    def test_rejects_unknown_visual_style_or_mood(self) -> None:
        for field, invalid in (("visual_style", "anime"), ("mood", "angry")):
            value = payload()
            value[field] = invalid
            with self.subTest(field=field):
                with self.assertRaisesRegex(DirectorAnnotationError, field):
                    compile_director_annotation(value, contract())

    def test_rejects_camera_that_cannot_look_anywhere(self) -> None:
        value = payload()
        value["keyframes"][0]["camera"]["look_at"] = [0.0, -10.0, 6.0]
        with self.assertRaisesRegex(DirectorAnnotationError, "look_at"):
            compile_director_annotation(value, contract())

    def test_rejects_nonfinite_and_autofilled_values(self) -> None:
        nonfinite = payload()
        nonfinite["keyframes"][0]["camera"]["position"][0] = math.nan
        with self.assertRaisesRegex(DirectorAnnotationError, "finite"):
            compile_director_annotation(nonfinite, contract())

        autofilled = deepcopy(payload())
        autofilled["auto_filled_values"] = 1
        with self.assertRaisesRegex(DirectorAnnotationError, "auto_filled_values"):
            compile_director_annotation(autofilled, contract())

    def test_rejects_wrong_key_schedule_and_unknown_fields(self) -> None:
        wrong_key = payload()
        wrong_key["keyframes"][1]["t"] = 0.21
        with self.assertRaisesRegex(DirectorAnnotationError, "schedule"):
            compile_director_annotation(wrong_key, contract())

        unknown = payload()
        unknown["keyframes"][0]["surprise"] = True
        with self.assertRaisesRegex(DirectorAnnotationError, "fields"):
            compile_director_annotation(unknown, contract())

    def test_k2_boundary_accepts_exact_locked_prefix(self) -> None:
        value = payload()
        value["frozen_through_keyframe"] = "K2"
        value["inherited_locked_values"] = deepcopy(contract()["inherited_keyframes"][:3])
        compile_director_annotation(value, contract())

    def test_k2_boundary_rejects_changed_locked_actor(self) -> None:
        value = payload()
        value["frozen_through_keyframe"] = "K2"
        value["inherited_locked_values"] = deepcopy(contract()["inherited_keyframes"][:3])
        value["keyframes"][1]["actors"]["actor_a"]["x"] += 0.01
        with self.assertRaisesRegex(DirectorAnnotationError, "locked inherited"):
            compile_director_annotation(value, contract())

    def test_k2_boundary_rejects_changed_boundary_camera_or_prompt(self) -> None:
        for field in ("camera", "actors"):
            with self.subTest(field=field):
                value = payload()
                value["frozen_through_keyframe"] = "K2"
                value["inherited_locked_values"] = deepcopy(contract()["inherited_keyframes"][:3])
                if field == "camera":
                    value["keyframes"][2]["camera"]["roll_degrees"] = 2.0
                else:
                    value["keyframes"][2]["actors"]["actor_a"]["x"] += 0.01
                with self.assertRaisesRegex(DirectorAnnotationError, "locked inherited"):
                    compile_director_annotation(value, contract())

    def test_k4_cannot_be_frozen_boundary(self) -> None:
        value = payload()
        value["frozen_through_keyframe"] = "K4"
        value["inherited_locked_values"] = deepcopy(contract()["inherited_keyframes"])
        with self.assertRaisesRegex(DirectorAnnotationError, "editable suffix"):
            compile_director_annotation(value, contract())

    def test_changed_editable_camera_accepts_human_modified_source(self) -> None:
        value = payload()
        value["keyframes"][3]["camera"]["position"][0] += 0.5
        value["keyframes"][3]["camera_source"] = "human_modified"
        result = compile_director_annotation(value, contract())
        self.assertEqual(
            json.loads(result.canonical_annotation)["keyframes"][3]["camera_source"],
            "human_modified",
        )

    def test_rejects_inherited_claim_for_changed_camera(self) -> None:
        value = payload()
        value["keyframes"][3]["camera"]["position"][0] += 0.5
        with self.assertRaisesRegex(DirectorAnnotationError, "camera_source"):
            compile_director_annotation(value, contract())

    def test_rejects_human_modified_claim_for_unchanged_camera(self) -> None:
        value = payload()
        value["keyframes"][3]["camera_source"] = "human_modified"
        with self.assertRaisesRegex(DirectorAnnotationError, "camera_source"):
            compile_director_annotation(value, contract())


if __name__ == "__main__":
    unittest.main()
