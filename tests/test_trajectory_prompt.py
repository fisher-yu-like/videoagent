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
        self.assertEqual(PROMPT_COMPILER_VERSION, "trajectory-facts-v1")

    def test_compiles_observed_motion_and_decreasing_distance(self) -> None:
        prompt = compile_prompt()
        self.assertIn("actor_a follows the authored path", prompt)
        self.assertIn("actor_b remains stationary", prompt)
        self.assertIn("actor_a and actor_b move closer", prompt)
        self.assertIn("camera remains static", prompt)

    def test_increasing_distance_cannot_compile_as_decreasing(self) -> None:
        frames = keyframes()
        for index, frame in enumerate(frames):
            frame["actors"]["actor_a"]["x"] = 0.1 - index * 0.05
        prompt = compile_prompt(frames)
        self.assertIn("actor_a and actor_b move farther apart", prompt)
        self.assertNotIn("actor_a and actor_b move closer", prompt)

    def test_moving_camera_and_enums_are_explicit(self) -> None:
        frames = keyframes()
        frames[-1]["camera"]["position"][0] = 1.0
        frames[-1]["camera"]["look_at"][0] = 0.5
        prompt = compile_prompt(
            frames, visual_style="documentary", mood="warm"
        )
        self.assertIn("camera follows the authored camera path", prompt)
        self.assertIn("documentary visual style", prompt)
        self.assertIn("warm mood", prompt)

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
