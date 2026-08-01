"""Deterministic camera rig compilation for approved semantic roles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from videoactagent.director_annotation import CameraKeyframe
from videoactagent.multicam_plan import CameraAssignment, MulticamPlan, MulticamPlanError


_SIDE_VECTORS = {
    "north": (0.0, 1.0),
    "south": (0.0, -1.0),
    "east": (1.0, 0.0),
    "west": (-1.0, 0.0),
    "north_east": (1.0, 1.0),
    "north_west": (-1.0, 1.0),
    "south_east": (1.0, -1.0),
    "south_west": (-1.0, -1.0),
}
_DISTANCE = {
    "extreme_wide": 0.80,
    "wide": 0.62,
    "medium": 0.40,
    "close": 0.24,
    "extreme_close": 0.16,
}
_HEIGHT = {
    "extreme_wide": 0.30,
    "wide": 0.25,
    "medium": 0.18,
    "close": 0.14,
    "extreme_close": 0.12,
}
_FOCAL = {
    "extreme_wide": 24.0,
    "wide": 35.0,
    "medium": 50.0,
    "close": 70.0,
    "extreme_close": 85.0,
}


def _world_point(
    point: Mapping[str, Any], bounds: tuple[float, float, float, float], label: str,
) -> tuple[float, float]:
    x, y = point.get("x"), point.get("y")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (x, y)):
        raise MulticamPlanError(f"{label} must contain numeric x and y")
    nx, ny = float(x), float(y)
    if not 0 <= nx <= 1 or not 0 <= ny <= 1:
        raise MulticamPlanError(f"{label} must be normalized to [0, 1]")
    return (
        bounds[0] + nx * (bounds[1] - bounds[0]),
        bounds[3] - ny * (bounds[3] - bounds[2]),
    )


def _target(
    assignment: CameraAssignment,
    actor_points: Mapping[str, tuple[float, float]],
) -> tuple[float, float]:
    if assignment.target == "all_actors" or assignment.look_at_policy == "actors_midpoint":
        return (
            sum(point[0] for point in actor_points.values()) / len(actor_points),
            sum(point[1] for point in actor_points.values()) / len(actor_points),
        )
    return actor_points[assignment.target]


def _rotated(vector: tuple[float, float], radians: float) -> tuple[float, float]:
    cosine, sine = math.cos(radians), math.sin(radians)
    return (
        vector[0] * cosine - vector[1] * sine,
        vector[0] * sine + vector[1] * cosine,
    )


def compile_camera_rig(
    *, plan: MulticamPlan, world_bounds: tuple[float, float, float, float],
    actor_keyframes: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[CameraKeyframe, ...]]:
    if len(world_bounds) != 4 or world_bounds[0] >= world_bounds[1] or world_bounds[2] >= world_bounds[3]:
        raise MulticamPlanError("world_bounds must be [min_x, max_x, min_y, max_y]")
    if len(actor_keyframes) != 5:
        raise MulticamPlanError("camera rig requires exactly K0--K4")
    width = world_bounds[1] - world_bounds[0]
    height = world_bounds[3] - world_bounds[2]
    diagonal = max(math.hypot(width, height), 1e-6)
    margin = diagonal * 0.8
    legal = (
        world_bounds[0] - margin, world_bounds[1] + margin,
        world_bounds[2] - margin, world_bounds[3] + margin,
    )
    prepared = []
    for index, frame in enumerate(actor_keyframes):
        if frame.get("id") != f"K{index}" or frame.get("t") not in (0.0, 0.2, 0.5, 0.8, 1.0):
            raise MulticamPlanError("actor keyframe schedule must be K0--K4")
        raw_actors = frame.get("actors")
        if not isinstance(raw_actors, Mapping) or not raw_actors:
            raise MulticamPlanError(f"K{index} actors are missing")
        actor_points = {
            str(actor): _world_point(point, world_bounds, f"K{index}.{actor}")
            for actor, point in raw_actors.items()
            if isinstance(point, Mapping)
        }
        if set(actor_points) != set(raw_actors):
            raise MulticamPlanError(f"K{index} actor points are invalid")
        prepared.append((float(frame["t"]), actor_points))

    result: dict[str, tuple[CameraKeyframe, ...]] = {}
    for assignment in plan.cameras:
        raw_side = _SIDE_VECTORS[assignment.side]
        length = math.hypot(*raw_side)
        base_side = (raw_side[0] / length, raw_side[1] / length)
        first_target = _target(assignment, prepared[0][1])
        states = []
        for index, (time, actor_points) in enumerate(prepared):
            target = _target(assignment, actor_points)
            position_target = first_target if assignment.motion == "static" else target
            side = base_side
            if assignment.motion == "arc":
                side = _rotated(base_side, math.radians(-12.0 + 24.0 * time))
            distance = diagonal * _DISTANCE[assignment.shot_size]
            px = min(legal[1], max(legal[0], position_target[0] + side[0] * distance))
            py = min(legal[3], max(legal[2], position_target[1] + side[1] * distance))
            pz = max(1.7, diagonal * _HEIGHT[assignment.shot_size])
            states.append(CameraKeyframe(
                keyframe_id=f"K{index}",
                t=time,
                position=(round(px, 12), round(py, 12), round(pz, 12)),
                look_at=(round(target[0], 12), round(target[1], 12), 1.25),
                focal_length_mm=_FOCAL[assignment.shot_size],
                shot_size=assignment.shot_size,
                interpolation="linear",
                roll_degrees=0.0,
            ))
        result[assignment.camera_id] = tuple(states)
    return result
