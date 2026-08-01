"""Deterministic prompt facts derived from authored actor and camera trajectories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


PROMPT_COMPILER_VERSION = "trajectory-facts-v2"
_TOLERANCE = 0.01
_STYLES = {
    "source_default": None,
    "cinematic_realism": "cinematic realistic visual style",
    "documentary": "documentary visual style",
}
_MOODS = {
    "source_default": None,
    "warm": "warm mood",
    "neutral": "neutral mood",
    "tense": "tense mood",
}


class TrajectoryPromptError(ValueError):
    """Raised when source-bound trajectory facts cannot be compiled safely."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrajectoryPromptError(f"{label} must be non-empty")
    return value.strip()


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrajectoryPromptError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise TrajectoryPromptError(f"{label} must be a finite number")
    return result


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _screen_motion(
    first: Sequence[float], second: Sequence[float],
) -> str:
    directions: list[str] = []
    horizontal = second[0] - first[0]
    vertical = second[1] - first[1]
    if abs(horizontal) > _TOLERANCE:
        directions.append("right" if horizontal > 0 else "left")
    if abs(vertical) > _TOLERANCE:
        directions.append("down" if vertical > 0 else "up")
    if not directions:
        return "holds position"
    return "moves " + " and ".join(directions)


def _spacing_change(
    first_a: Sequence[float], first_b: Sequence[float],
    second_a: Sequence[float], second_b: Sequence[float],
) -> str:
    delta = _distance(second_a, second_b) - _distance(first_a, first_b)
    if delta < -_TOLERANCE:
        return "move closer"
    if delta > _TOLERANCE:
        return "move farther apart"
    return "keep similar spacing"


def _actor_point(frame: Mapping[str, Any], actor: str) -> tuple[float, float]:
    actors = frame.get("actors")
    point = actors.get(actor) if isinstance(actors, Mapping) else None
    if not isinstance(point, Mapping):
        raise TrajectoryPromptError(f"actor point is missing for {actor}")
    return (
        _number(point.get("x"), f"{actor}.x"),
        _number(point.get("y"), f"{actor}.y"),
    )


def _camera_vector(frame: Mapping[str, Any], field: str) -> tuple[float, float, float]:
    camera = frame.get("camera")
    value = camera.get(field) if isinstance(camera, Mapping) else None
    if not isinstance(value, list) or len(value) != 3:
        raise TrajectoryPromptError(f"camera {field} must be a 3D point")
    return tuple(_number(item, f"camera.{field}") for item in value)  # type: ignore[return-value]


def _camera_segment(
    first: Mapping[str, Any], second: Mapping[str, Any],
) -> list[str]:
    first_camera = first["camera"]
    second_camera = second["camera"]
    facts: list[str] = []
    if (
        _distance(
            _camera_vector(first, "position"),
            _camera_vector(second, "position"),
        )
        > _TOLERANCE
    ):
        facts.append("camera moves along the approved path")
    if (
        _distance(
            _camera_vector(first, "look_at"),
            _camera_vector(second, "look_at"),
        )
        > _TOLERANCE
    ):
        facts.append("camera look-at changes")

    first_focal = _number(first_camera.get("focal_length_mm"), "focal")
    second_focal = _number(second_camera.get("focal_length_mm"), "focal")
    if abs(second_focal - first_focal) > _TOLERANCE:
        facts.append(
            f"focal length changes from {first_focal:g} mm to {second_focal:g} mm"
        )

    first_shot = first_camera.get("shot_size")
    second_shot = second_camera.get("shot_size")
    if first_shot != second_shot:
        facts.append(f"shot size changes from {first_shot} to {second_shot}")

    first_roll = _number(first_camera.get("roll_degrees"), "roll")
    second_roll = _number(second_camera.get("roll_degrees"), "roll")
    if abs(second_roll - first_roll) > _TOLERANCE:
        facts.append(
            f"roll changes from {first_roll:g} degrees to {second_roll:g} degrees"
        )
    return facts


def _validated_frames(keyframes: object) -> tuple[list[Mapping[str, Any]], list[str]]:
    if not isinstance(keyframes, list) or len(keyframes) < 2:
        raise TrajectoryPromptError("at least two structural keyframes are required")
    frames: list[Mapping[str, Any]] = []
    actor_ids: list[str] | None = None
    for index, raw in enumerate(keyframes):
        if not isinstance(raw, Mapping):
            raise TrajectoryPromptError(f"K{index} must be an object")
        actors = raw.get("actors")
        if not isinstance(actors, Mapping) or not actors:
            raise TrajectoryPromptError(f"K{index} actors are missing")
        current_ids = sorted(str(actor) for actor in actors)
        if actor_ids is None:
            actor_ids = current_ids
        elif current_ids != actor_ids:
            raise TrajectoryPromptError("actor IDs must be stable across keyframes")
        for actor in current_ids:
            _actor_point(raw, actor)
        _camera_vector(raw, "position")
        _camera_vector(raw, "look_at")
        camera = raw.get("camera")
        if not isinstance(camera, Mapping):
            raise TrajectoryPromptError(f"K{index} camera is missing")
        _number(camera.get("focal_length_mm"), "camera.focal_length_mm")
        _number(camera.get("roll_degrees"), "camera.roll_degrees")
        _text(camera.get("shot_size"), "camera.shot_size")
        frames.append(raw)
    return frames, actor_ids or []


def compile_trajectory_prompt(
    *, story_prompt: object, appearance_instruction: object,
    duration_seconds: object, keyframes: object, visual_style: object, mood: object,
) -> str:
    """Compile only observable trajectory/camera facts into stable English text."""
    _text(story_prompt, "story_prompt")
    appearance = _text(appearance_instruction, "appearance_instruction")
    duration = _number(duration_seconds, "duration_seconds")
    if duration <= 0:
        raise TrajectoryPromptError("duration_seconds must be positive")
    if visual_style not in _STYLES:
        raise TrajectoryPromptError("visual_style is invalid")
    if mood not in _MOODS:
        raise TrajectoryPromptError("mood is invalid")
    frames, actors = _validated_frames(keyframes)

    facts: list[str] = []
    for segment_index, (first, second) in enumerate(zip(frames, frames[1:])):
        segment = f"K{segment_index} to K{segment_index + 1}"
        for actor in actors:
            facts.append(
                f"{segment}, {actor} "
                f"{_screen_motion(_actor_point(first, actor), _actor_point(second, actor))}"
            )

        for first_index, first_actor in enumerate(actors):
            for second_actor in actors[first_index + 1:]:
                facts.append(
                    f"{segment}, {first_actor} and {second_actor} "
                    f"{_spacing_change(
                        _actor_point(first, first_actor),
                        _actor_point(first, second_actor),
                        _actor_point(second, first_actor),
                        _actor_point(second, second_actor),
                    )}"
                )

        facts.extend(
            f"{segment}, {camera_fact}"
            for camera_fact in _camera_segment(first, second)
        )

    first_camera = frames[0]["camera"]
    last_camera = frames[-1]["camera"]
    facts.append(
        f"framing starts as {first_camera['shot_size']} at "
        f"{_number(first_camera.get('focal_length_mm'), 'focal'):g} mm and ends as "
        f"{last_camera['shot_size']} at "
        f"{_number(last_camera.get('focal_length_mm'), 'focal'):g} mm"
    )

    creative = [value for value in (_STYLES[visual_style], _MOODS[mood]) if value]
    parts = [
        appearance.rstrip(". ") + ".",
        "; ".join(facts) + ".",
    ]
    if creative:
        parts.append("Use " + " and ".join(creative) + ".")
    parts.append(
        f"One continuous {duration:g}-second take, no cuts, no time jumps, "
        "and no teleporting."
    )
    return " ".join(parts)
