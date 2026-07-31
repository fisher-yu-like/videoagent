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
    return {
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
    }


def payload() -> dict:
    keys = contract()["keyframes"]
    return {
        "schema_version": "1.0",
        "author_id": "sy",
        "iteration_id": "D1",
        "parent_iteration_id": "D0",
        "auto_filled_values": 0,
        "keyframes": [
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
                "visible_state": f"K{index} visible station state.",
            }
            for index, item in enumerate(keys)
        ],
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
        self.assertIn("K0 (t=0)", result.compiled_prompt)
        self.assertIn("K4 (t=1)", result.compiled_prompt)
        self.assertEqual(json.loads(result.canonical_annotation)["author_id"], "sy")
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

    def test_rejects_empty_keyframe_prompt(self) -> None:
        value = payload()
        value["keyframes"][3]["visible_state"] = "  "
        with self.assertRaisesRegex(DirectorAnnotationError, "visible_state"):
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


if __name__ == "__main__":
    unittest.main()
