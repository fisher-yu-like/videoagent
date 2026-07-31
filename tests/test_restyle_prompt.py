from __future__ import annotations

from dataclasses import FrozenInstanceError
from copy import deepcopy
import inspect
import json
import math
from pathlib import Path
import unittest

from videoactagent.restyle_prompt import (
    RESTYLE_COMPILER_VERSION,
    RestyleProfile,
    RestylePromptError,
    SubjectProfile,
    compile_restyle_prompt,
    load_restyle_profile,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "configs" / "restyle" / "station_reunion.json"
HEADINGS = (
    "Subjects and wardrobe",
    "Driving motion from the approved proxy",
    "Environment",
    "Lighting",
    "Camera motion and framing",
    "Photoreal quality",
    "Must preserve / must replace / must avoid",
)
TRAJECTORY_PROMPT = (
    "Natural proportions and consistent station lighting. "
    "K0 to K1, actor_a moves right; "
    "K0 to K1, actor_b holds position; "
    "K0 to K1, actor_a and actor_b move closer; "
    "K0 to K1, camera moves along the approved path; "
    "K0 to K1, camera look-at changes; "
    "K0 to K1, focal length changes from 35 mm to 50 mm; "
    "framing starts as wide at 35 mm and ends as medium at 50 mm. "
    "One continuous 5-second take, no cuts, no time jumps, and no teleporting."
)


def profile_value() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "scene_id": "station_reunion",
        "subjects": [
            {
                "actor_id": "actor_a",
                "description": (
                    "an adult traveler wearing an orange coat and subdued travel clothes"
                ),
            },
            {
                "actor_id": "actor_b",
                "description": (
                    "an adult friend wearing a blue coat and subdued travel clothes"
                ),
            },
        ],
        "environment": "a grounded railway platform with stable station architecture",
        "lighting": "consistent natural station lighting",
        "quality": (
            "photoreal live-action people, natural anatomy, realistic skin and cloth"
        ),
    }


def compiled(profile: RestyleProfile | None = None, **overrides: object) -> str:
    values = {
        "profile": profile or load_restyle_profile(profile_value()),
        "trajectory_prompt": TRAJECTORY_PROMPT,
        "duration_seconds": 5.0,
    }
    values.update(overrides)
    return compile_restyle_prompt(**values)  # type: ignore[arg-type]


def section(prompt: str, heading: str) -> str:
    start = prompt.index(heading) + len(heading)
    later = [prompt.index(item, start) for item in HEADINGS if item in prompt[start:]]
    end = min(later) if later else len(prompt)
    return prompt[start:end]


class RestyleProfileTests(unittest.TestCase):
    def test_real_station_profile_loads_exact_source_content(self) -> None:
        value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

        profile = load_restyle_profile(value)

        self.assertEqual(profile.schema_version, "1.0")
        self.assertEqual(profile.scene_id, "station_reunion")
        self.assertEqual(
            [(item.actor_id, item.description) for item in profile.subjects],
            [
                (
                    "actor_a",
                    "an adult traveler wearing an orange coat and subdued travel clothes",
                ),
                (
                    "actor_b",
                    "an adult friend wearing a blue coat and subdued travel clothes",
                ),
            ],
        )
        self.assertEqual(
            profile.environment,
            "a grounded railway platform with stable station architecture",
        )
        self.assertEqual(profile.lighting, "consistent natural station lighting")
        self.assertEqual(
            profile.quality,
            "photoreal live-action people, natural anatomy, realistic skin and cloth",
        )

    def test_profile_value_objects_are_immutable(self) -> None:
        profile = load_restyle_profile(profile_value())

        with self.assertRaises(FrozenInstanceError):
            profile.scene_id = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            profile.subjects[0].description = "changed"  # type: ignore[misc]
        self.assertIsInstance(profile, RestyleProfile)
        self.assertIsInstance(profile.subjects[0], SubjectProfile)

    def test_loader_rejects_unknown_missing_and_malformed_structures(self) -> None:
        cases: list[object] = []
        unknown = profile_value()
        unknown["story_prompt"] = "A stale story."
        cases.append(unknown)
        missing = profile_value()
        del missing["lighting"]
        cases.append(missing)
        cases.extend(([], "profile", None))
        bad_subjects = profile_value()
        bad_subjects["subjects"] = {"actor_a": "orange coat"}
        cases.append(bad_subjects)
        bad_subject_shape = profile_value()
        bad_subject_shape["subjects"] = [
            {"actor_id": "actor_a", "description": "orange coat", "extra": "no"}
        ]
        cases.append(bad_subject_shape)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(RestylePromptError):
                    load_restyle_profile(value)  # type: ignore[arg-type]

    def test_loader_rejects_bad_actor_ids_duplicates_and_blank_text(self) -> None:
        cases: list[dict[str, object]] = []
        for actor_id in ("", " actor_a", "actor a", "actor-a", 7):
            value = profile_value()
            value["subjects"][0]["actor_id"] = actor_id  # type: ignore[index]
            cases.append(value)
        duplicate = profile_value()
        duplicate["subjects"][1]["actor_id"] = "actor_a"  # type: ignore[index]
        cases.append(duplicate)
        for field in ("scene_id", "environment", "lighting", "quality"):
            value = profile_value()
            value[field] = " "
            cases.append(value)
        blank_description = profile_value()
        blank_description["subjects"][0]["description"] = ""  # type: ignore[index]
        cases.append(blank_description)
        bad_version = profile_value()
        bad_version["schema_version"] = 1.0
        cases.append(bad_version)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(RestylePromptError):
                    load_restyle_profile(value)


class RestyleCompilerTests(unittest.TestCase):
    def test_sections_have_exact_order_and_repeated_output_is_identical(self) -> None:
        first = compiled()
        second = compiled()

        self.assertEqual(RESTYLE_COMPILER_VERSION, "human-restyle-v1")
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        positions = [first.index(heading) for heading in HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            [line for line in first.splitlines() if line in HEADINGS],
            list(HEADINGS),
        )

    def test_subjects_are_sorted_by_actor_id_with_explicit_wardrobe(self) -> None:
        value = profile_value()
        value["subjects"] = list(reversed(value["subjects"]))  # type: ignore[arg-type]

        prompt = compiled(load_restyle_profile(value))
        subjects = section(prompt, HEADINGS[0])

        self.assertLess(subjects.index("actor_a"), subjects.index("actor_b"))
        self.assertIn("orange coat and subdued travel clothes", subjects)
        self.assertIn("blue coat and subdued travel clothes", subjects)

    def test_actual_v2_facts_are_partitioned_without_stale_story_prose(self) -> None:
        prompt = compiled(
            trajectory_prompt=(
                "A reunion with a locked static camera. " + TRAJECTORY_PROMPT
            )
        )
        driving = section(prompt, HEADINGS[1])
        camera = section(prompt, HEADINGS[4])

        for fact in (
            "K0 to K1, actor_a moves right",
            "K0 to K1, actor_b holds position",
            "K0 to K1, actor_a and actor_b move closer",
        ):
            self.assertIn(fact, driving)
            self.assertNotIn(fact, camera)
        for fact in (
            "K0 to K1, camera moves along the approved path",
            "K0 to K1, camera look-at changes",
            "K0 to K1, focal length changes from 35 mm to 50 mm",
            "framing starts as wide at 35 mm and ends as medium at 50 mm",
        ):
            self.assertIn(fact, camera)
            self.assertNotIn(fact, driving)
        self.assertNotIn("locked static camera", prompt.lower())

    def test_instruction_is_duration_bound_and_preserves_proxy_controls(self) -> None:
        prompt = compiled(duration_seconds=5.0)
        final = section(prompt, HEADINGS[6]).lower()

        self.assertIn("one complete, approximately 5-second continuous take", prompt)
        self.assertIn(
            "blocking, timing, occlusion, composition, and camera movement",
            final,
        )
        self.assertIn("two distinguishable subjects", final)
        self.assertIn("explicit wardrobe profile", final)
        self.assertIn(
            "replace every clay/low-poly body with a complete photoreal human",
            final,
        )
        for forbidden in (
            "cylinders",
            "mannequins",
            "plastic",
            "clay",
            "labels",
            "trajectory lines",
            "path lines",
            "cg residue",
        ):
            self.assertIn(forbidden, final)

    def test_does_not_accept_story_prompt_or_invent_actions(self) -> None:
        self.assertNotIn("story_prompt", inspect.signature(compile_restyle_prompt).parameters)

        prompt = compiled().lower()
        for invented in ("embrace", "hug", "wave", "smile", "gesture", "contact"):
            self.assertNotIn(invented, prompt)

    def test_rejects_nonpositive_nonfinite_or_coerced_duration(self) -> None:
        for duration in (0, -1, math.nan, math.inf, -math.inf, True, "5"):
            with self.subTest(duration=duration):
                with self.assertRaises(RestylePromptError):
                    compiled(duration_seconds=duration)

    def test_rejects_wrong_profile_or_missing_approved_trajectory_facts(self) -> None:
        with self.assertRaises(RestylePromptError):
            compile_restyle_prompt(
                profile=profile_value(),  # type: ignore[arg-type]
                trajectory_prompt=TRAJECTORY_PROMPT,
                duration_seconds=5.0,
            )
        for trajectory_prompt in ("", " ", "A traveler hugs a friend.", 5):
            with self.subTest(trajectory_prompt=trajectory_prompt):
                with self.assertRaises(RestylePromptError):
                    compiled(trajectory_prompt=trajectory_prompt)


if __name__ == "__main__":
    unittest.main()
