from __future__ import annotations

from copy import deepcopy
import unittest

from videoactagent.trajectory_prompt import (
    PROMPT_COMPILER_VERSION,
    TrajectoryPromptError,
    compile_trajectory_prompt,
)


def keyframes() -> list[dict]:
    frames = []
    for index, (time, actor_a_x) in enumerate(((0.0, 0.1), (0.25, 0.25), (0.5, 0.4), (0.75, 0.55), (1.0, 0.65))):
        frames.append({
            "id": f"K{index}",
            "t": time,
            "actors": {
                "actor_a": {"x": actor_a_x, "y": 0.5},
                "actor_b": {"x": 0.8, "y": 0.5},
            },
            "camera": {
                "position": [0.0, -10.0, 6.0],
                "look_at": [0.0, 0.0, 1.25],
                "focal_length_mm": 35.0,
                "shot_size": "wide",
                "interpolation": "linear",
                "roll_degrees": 0.0,
            },
        })
    return frames


def compile_prompt(frames: list[dict] | None = None, **overrides: object) -> str:
    values = {
        "story_prompt": "A traveler approaches a waiting friend on a station platform.",
        "appearance_instruction": "Natural proportions and consistent station lighting.",
        "duration_seconds": 5.0,
        "keyframes": frames or keyframes(),
        "visual_style": "source_default",
        "mood": "source_default",
    }
    values.update(overrides)
    return compile_trajectory_prompt(**values)


class TrajectoryPromptTests(unittest.TestCase):
    def test_repeated_compilation_is_byte_identical(self) -> None:
        first = compile_prompt()
        second = compile_prompt()
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        self.assertEqual(PROMPT_COMPILER_VERSION, "trajectory-facts-v2")

    def test_compiles_observed_motion_and_decreasing_distance(self) -> None:
        prompt = compile_prompt()
        self.assertIn("K0 to K1, actor_a moves right", prompt)
        self.assertIn("K0 to K1, actor_b holds position", prompt)
        self.assertIn("K0 to K1, actor_a and actor_b move closer", prompt)
        self.assertNotIn("camera moves along the approved path", prompt)

    def test_increasing_distance_cannot_compile_as_decreasing(self) -> None:
        frames = keyframes()
        for index, frame in enumerate(frames):
            frame["actors"]["actor_a"]["x"] = 0.1 - index * 0.05
        prompt = compile_prompt(frames)
        self.assertIn("K0 to K1, actor_a and actor_b move farther apart", prompt)
        self.assertNotIn("actor_a and actor_b move closer", prompt)

    def test_compiles_each_segment_instead_of_hiding_reversal(self) -> None:
        frames = keyframes()
        xs = [0.10, 0.35, 0.65, 0.45, 0.20]
        for frame, x in zip(frames, xs):
            frame["actors"]["actor_a"]["x"] = x

        prompt = compile_prompt(frames)

        self.assertIn("K1 to K2, actor_a moves right", prompt)
        self.assertIn("K2 to K3, actor_a moves left", prompt)
        self.assertIn("K1 to K2, actor_a and actor_b move closer", prompt)
        self.assertIn("K2 to K3, actor_a and actor_b move farther apart", prompt)
        self.assertLess(prompt.index("K1 to K2"), prompt.index("K2 to K3"))

    def test_screen_motion_reports_both_axes_and_holds_within_tolerance(self) -> None:
        frames = keyframes()
        frames[1]["actors"]["actor_a"] = {"x": 0.3, "y": 0.7}
        frames[2]["actors"]["actor_a"] = {"x": 0.305, "y": 0.695}

        prompt = compile_prompt(frames)

        self.assertIn("K0 to K1, actor_a moves right and down", prompt)
        self.assertIn("K1 to K2, actor_a holds position", prompt)

    def test_moving_camera_and_enums_are_explicit(self) -> None:
        frames = keyframes()
        frames[-1]["camera"]["position"][0] = 1.0
        frames[-1]["camera"]["look_at"][0] = 0.5
        prompt = compile_prompt(
            frames, visual_style="documentary", mood="warm"
        )
        self.assertIn("K3 to K4, camera moves along the approved path", prompt)
        self.assertIn("K3 to K4, camera look-at changes", prompt)
        self.assertIn("documentary visual style", prompt)
        self.assertIn("warm mood", prompt)

    def test_camera_changes_are_emitted_per_segment_only_when_observed(self) -> None:
        frames = keyframes()
        for frame in frames[2:]:
            frame["camera"].update({
                "position": [1.0, -9.0, 6.5],
                "look_at": [0.5, 0.25, 1.5],
                "focal_length_mm": 50.0,
                "shot_size": "medium",
                "roll_degrees": 3.0,
            })

        prompt = compile_prompt(frames)

        expected = [
            "K1 to K2, camera moves along the approved path",
            "K1 to K2, camera look-at changes",
            "K1 to K2, focal length changes from 35 mm to 50 mm",
            "K1 to K2, shot size changes from wide to medium",
            "K1 to K2, roll changes from 0 degrees to 3 degrees",
        ]
        for fact in expected:
            with self.subTest(fact=fact):
                self.assertIn(fact, prompt)
        self.assertEqual(prompt.count("camera moves along the approved path"), 1)
        self.assertEqual(prompt.count("camera look-at changes"), 1)
        self.assertEqual(prompt.count("focal length changes"), 1)
        self.assertEqual(prompt.count("shot size changes"), 1)
        self.assertEqual(prompt.count("roll changes"), 1)

        fact_positions = [prompt.index(fact) for fact in expected]
        self.assertEqual(fact_positions, sorted(fact_positions))

    def test_source_story_is_validated_but_not_copied_into_motion_facts(self) -> None:
        frames = keyframes()
        frames[2]["camera"]["position"][0] = 0.5

        prompt = compile_prompt(
            frames, story_prompt="A locked static camera watches a traveler."
        )

        self.assertNotIn("locked static camera", prompt.lower())
        self.assertIn("K1 to K2, camera moves along the approved path", prompt)
        self.assertIn("Natural proportions and consistent station lighting", prompt)

    def test_does_not_invent_unobserved_actions(self) -> None:
        prompt = compile_prompt().lower()
        for invented in ("embrace", "hug", "wave", "contact", "smile", "gesture"):
            self.assertNotIn(invented, prompt)

    def test_rejects_empty_sources_and_unknown_enums(self) -> None:
        for changes in (
            {"story_prompt": " "},
            {"appearance_instruction": ""},
            {"visual_style": "anime"},
            {"mood": "angry"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(TrajectoryPromptError):
                    compile_prompt(deepcopy(keyframes()), **changes)


if __name__ == "__main__":
    unittest.main()
