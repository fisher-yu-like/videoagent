"""Scale-aware, deterministic prompt facts for multicamera trajectories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

from videoactagent.director_annotation import CameraKeyframe


LOOK_AT_POSITION_RATIO = 0.01
CAMERA_POSITION_RATIO = 0.005
LOOK_ANGLE_DEGREES = 1.0
ROLL_DEGREES = 0.5
FOCAL_MM = 1.0
MULTICAM_PROMPT_VERSION = "multicam-trajectory-facts-v1"


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def _view_angle(first: CameraKeyframe, second: CameraKeyframe) -> float:
    vectors = []
    for state in (first, second):
        vector = tuple(
            target - position
            for target, position in zip(state.look_at, state.position)
        )
        length = math.sqrt(sum(component * component for component in vector))
        if length <= 1e-12:
            raise ValueError("camera position and look-at cannot coincide")
        vectors.append(tuple(component / length for component in vector))
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(*vectors))))
    return math.degrees(math.acos(cosine))


def compile_multicam_prompt(
    *, world_bounds: tuple[float, float, float, float],
    camera_states: Mapping[str, Sequence[CameraKeyframe]],
) -> str:
    if len(world_bounds) != 4 or world_bounds[0] >= world_bounds[1] or world_bounds[2] >= world_bounds[3]:
        raise ValueError("world_bounds are invalid")
    if set(camera_states) != {"camera_a", "camera_b", "camera_c"}:
        raise ValueError("camera_states must contain camera_a, camera_b and camera_c")
    diagonal = max(math.hypot(
        world_bounds[1] - world_bounds[0],
        world_bounds[3] - world_bounds[2],
    ), 1e-6)
    lines = []
    for camera_id in ("camera_a", "camera_b", "camera_c"):
        states = tuple(camera_states[camera_id])
        if len(states) < 2:
            raise ValueError(f"{camera_id} requires at least two states")
        facts = []
        for first, second in zip(states, states[1:]):
            segment = f"{first.keyframe_id} to {second.keyframe_id}"
            if _distance(first.position, second.position) > CAMERA_POSITION_RATIO * diagonal:
                facts.append(f"{segment}, camera moves")
            look_fixed = (
                _distance(first.look_at, second.look_at) <= LOOK_AT_POSITION_RATIO * diagonal
                and _view_angle(first, second) <= LOOK_ANGLE_DEGREES
            )
            if not look_fixed:
                facts.append(f"{segment}, look-at changes")
            if abs(second.focal_length_mm - first.focal_length_mm) > FOCAL_MM:
                facts.append(
                    f"{segment}, focal length changes from {first.focal_length_mm:g} mm "
                    f"to {second.focal_length_mm:g} mm"
                )
            if abs(second.roll_degrees - first.roll_degrees) > ROLL_DEGREES:
                facts.append(
                    f"{segment}, roll changes from {first.roll_degrees:g} degrees "
                    f"to {second.roll_degrees:g} degrees"
                )
        if facts:
            lines.append(camera_id + ": " + "; ".join(facts) + ".")
        else:
            lines.append(f"{camera_id} holds position and keeps a fixed look-at.")
    return " ".join(lines)
