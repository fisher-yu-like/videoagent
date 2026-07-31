"""Strict schema tests for semantic story plans."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from videoactagent.semantic_plan import SemanticPlanError, SemanticStoryPlan


PLAN_PATH = Path("plans/station_reunion.semantic.json")


def valid_document() -> dict[str, object]:
    keyframes = [
        {
            "id": "K0",
            "t": 0.0,
            "visible_state": "The two people are separated.",
            "actor_states": ["actor_a waits", "actor_b stands at a distance"],
            "camera_state": "locked wide shot",
            "must_not_show": ["physical contact"],
        },
        {
            "id": "K1",
            "t": 0.3,
            "visible_state": "Actor A begins walking toward actor B.",
            "actor_states": ["actor_a approaches", "actor_b remains still"],
            "camera_state": "locked wide shot",
            "must_not_show": ["physical contact"],
        },
        {
            "id": "K2",
            "t": 0.7,
            "visible_state": "The separation has visibly narrowed.",
            "actor_states": ["actor_a continues approaching", "actor_b remains still"],
            "camera_state": "locked wide shot",
            "must_not_show": ["physical contact"],
        },
        {
            "id": "K3",
            "t": 1.0,
            "visible_state": "The people finish at reunion distance.",
            "actor_states": ["actor_a stops nearby", "actor_b remains still"],
            "camera_state": "locked wide shot",
            "must_not_show": ["body overlap"],
        },
    ]
    return {
        "schema_version": "1.0",
        "story_id": "example_story",
        "duration_seconds": 5.0,
        "appearance_instruction": "Render the event as a natural station reunion.",
        "semantic_keyframes": keyframes,
        "transitions": [
            {
                "from": "K0",
                "to": "K1",
                "cause": "actor_a notices actor_b",
                "continuous_change": "actor_a starts moving forward",
                "should_not_jump": "actor_a position",
            },
            {
                "from": "K1",
                "to": "K2",
                "cause": "actor_a continues the approach",
                "continuous_change": "the distance steadily narrows",
                "should_not_jump": "actor_a position",
            },
            {
                "from": "K2",
                "to": "K3",
                "cause": "actor_a reaches reunion distance",
                "continuous_change": "actor_a slows to a stop",
                "should_not_jump": "remaining separation",
            },
        ],
        "causal_constraints": ["approach must precede near-arrival"],
        "must_show": ["visible reduction in distance"],
        "must_avoid": ["contact before the final state"],
        "uncertain_assumptions": ["the waiting actor remains stationary"],
    }


class SemanticStoryPlanTests(unittest.TestCase):
    def test_from_dict_builds_immutable_dataclasses_and_canonical_dict(self):
        document = valid_document()

        plan = SemanticStoryPlan.from_dict(document)

        self.assertEqual(plan.story_id, "example_story")
        self.assertEqual(plan.duration_seconds, 5.0)
        self.assertEqual(tuple(frame.id for frame in plan.semantic_keyframes), ("K0", "K1", "K2", "K3"))
        self.assertEqual(plan.transitions[0].from_id, "K0")
        self.assertEqual(plan.to_dict(), document)
        with self.assertRaises((AttributeError, TypeError)):
            plan.story_id = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            plan.semantic_keyframes[0] = plan.semantic_keyframes[1]  # type: ignore[index]

    def test_exact_keys_are_required_at_every_object_level(self):
        mutations = []
        for location, key in (
            ((), "story_id"),
            (("semantic_keyframes", 0), "visible_state"),
            (("transitions", 0), "cause"),
        ):
            missing = valid_document()
            target = missing
            for part in location:
                target = target[part]  # type: ignore[index,assignment]
            target.pop(key)  # type: ignore[union-attr]
            mutations.append((f"missing {key}", missing))

            unknown = valid_document()
            target = unknown
            for part in location:
                target = target[part]  # type: ignore[index,assignment]
            target["unknown"] = "not allowed"  # type: ignore[index]
            mutations.append((f"unknown beside {key}", unknown))

        for label, document in mutations:
            with self.subTest(label=label), self.assertRaisesRegex(SemanticPlanError, "keys"):
                SemanticStoryPlan.from_dict(document)

    def test_from_dict_reports_non_string_unknown_keys_as_schema_errors(self):
        document = valid_document()
        document["extra"] = "not allowed"
        document[7] = "not a JSON object key"  # type: ignore[index]

        with self.assertRaisesRegex(SemanticPlanError, "keys"):
            SemanticStoryPlan.from_dict(document)

    def test_keyframes_are_four_to_six_unique_strictly_ordered_states(self):
        documents = []

        too_few = valid_document()
        too_few["semantic_keyframes"] = too_few["semantic_keyframes"][:3]  # type: ignore[index]
        too_few["transitions"] = too_few["transitions"][:2]  # type: ignore[index]
        documents.append(("four to six", too_few))

        duplicate_id = valid_document()
        duplicate_id["semantic_keyframes"][1]["id"] = "K0"  # type: ignore[index]
        documents.append(("unique", duplicate_id))

        unordered = valid_document()
        unordered["semantic_keyframes"][2]["t"] = 0.2  # type: ignore[index]
        documents.append(("strictly increasing", unordered))

        wrong_start = valid_document()
        wrong_start["semantic_keyframes"][0]["t"] = 0.1  # type: ignore[index]
        documents.append(("start at 0", wrong_start))

        wrong_end = valid_document()
        wrong_end["semantic_keyframes"][-1]["t"] = 0.9  # type: ignore[index]
        documents.append(("end at 1", wrong_end))

        for message, document in documents:
            with self.subTest(message=message), self.assertRaisesRegex(SemanticPlanError, message):
                SemanticStoryPlan.from_dict(document)

    def test_transitions_cover_each_adjacent_keyframe_exactly(self):
        missing = valid_document()
        missing["transitions"] = missing["transitions"][:-1]  # type: ignore[index]

        skipped = valid_document()
        skipped["transitions"][1]["to"] = "K3"  # type: ignore[index]

        reversed_edge = valid_document()
        reversed_edge["transitions"][0]["from"] = "K1"  # type: ignore[index]

        for document in (missing, skipped, reversed_edge):
            with self.subTest(document=document), self.assertRaisesRegex(SemanticPlanError, "adjacent"):
                SemanticStoryPlan.from_dict(document)

    def test_strings_and_requirement_lists_are_strictly_non_empty(self):
        invalid_documents = []

        blank_appearance = valid_document()
        blank_appearance["appearance_instruction"] = "  "
        invalid_documents.append(blank_appearance)

        empty_constraint_list = valid_document()
        empty_constraint_list["causal_constraints"] = []
        invalid_documents.append(empty_constraint_list)

        non_string_actor_state = valid_document()
        non_string_actor_state["semantic_keyframes"][0]["actor_states"][0] = {"pose": "wait"}  # type: ignore[index]
        invalid_documents.append(non_string_actor_state)

        blank_transition_requirement = valid_document()
        blank_transition_requirement["transitions"][0]["should_not_jump"] = ""  # type: ignore[index]
        invalid_documents.append(blank_transition_requirement)

        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(SemanticPlanError):
                SemanticStoryPlan.from_dict(document)

    def test_schema_numbers_and_container_types_are_strict(self):
        invalid_documents = []
        for version in (1.0, "0.9", None):
            document = valid_document()
            document["schema_version"] = version
            invalid_documents.append(document)
        for duration in (True, 0, -1, "5"):
            document = valid_document()
            document["duration_seconds"] = duration
            invalid_documents.append(document)
        tuple_list = valid_document()
        tuple_list["must_show"] = ("distance narrows",)
        invalid_documents.append(tuple_list)

        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(SemanticPlanError):
                SemanticStoryPlan.from_dict(document)

    def test_unrepresentably_large_numbers_raise_semantic_plan_error(self):
        document = valid_document()
        document["duration_seconds"] = 10**400

        with self.assertRaisesRegex(SemanticPlanError, "finite"):
            SemanticStoryPlan.from_dict(document)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "huge-duration.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SemanticPlanError, "finite"):
                SemanticStoryPlan.from_path(path)

    def test_from_path_wraps_json_integer_digit_limit_error(self):
        text = json.dumps(valid_document()).replace(
            '"duration_seconds": 5.0',
            f'"duration_seconds": {"9" * 5000}',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "huge-json-integer.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(SemanticPlanError, "cannot read semantic plan"):
                SemanticStoryPlan.from_path(path)

    def test_from_path_rejects_duplicate_key_in_otherwise_valid_document(self):
        text = json.dumps(valid_document()).replace(
            '"story_id": "example_story"',
            '"story_id": "example_story", "story_id": "example_story"',
            1,
        )
        self.assertEqual(
            SemanticStoryPlan.from_dict(json.loads(text)).story_id,
            "example_story",
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-key.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                SemanticPlanError, "duplicate JSON key: story_id"
            ):
                SemanticStoryPlan.from_path(path)

    def test_from_path_rejects_nonstandard_json_numbers(self):
        text = json.dumps(valid_document()).replace(
            '"duration_seconds": 5.0', '"duration_seconds": NaN'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonstandard-number.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(SemanticPlanError):
                SemanticStoryPlan.from_path(path)

    def test_station_reunion_pilot_has_five_locked_camera_states(self):
        plan = SemanticStoryPlan.from_path(PLAN_PATH)

        self.assertEqual(plan.story_id, "station_reunion")
        self.assertEqual(plan.duration_seconds, 5.0)
        self.assertEqual(len(plan.semantic_keyframes), 5)
        self.assertTrue(all(frame.camera_state == "locked camera" for frame in plan.semantic_keyframes))
        self.assertTrue(any("premature contact" in item for item in plan.must_avoid))
        self.assertEqual(plan.to_dict(), json.loads(PLAN_PATH.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
