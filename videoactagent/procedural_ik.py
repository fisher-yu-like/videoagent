"""Small deterministic two-link IK helpers for the local Proxy branch."""

from __future__ import annotations

import math
from typing import Sequence, Tuple


Point3 = Tuple[float, float, float]


def _sub(a: Point3, b: Point3) -> Point3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Point3, b: Point3) -> Point3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Point3, value: float) -> Point3:
    return (a[0] * value, a[1] * value, a[2] * value)


def _dot(a: Point3, b: Point3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Point3, b: Point3) -> Point3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(a: Point3) -> float:
    return math.sqrt(max(0.0, _dot(a, a)))


def _normalize(a: Point3, fallback: Point3) -> Point3:
    length = _length(a)
    if length < 1e-9:
        return fallback
    return _scale(a, 1.0 / length)


def _point(value: Sequence[float]) -> Point3:
    if len(value) != 3 or any(not math.isfinite(float(item)) for item in value):
        raise ValueError("IK points must be finite 3D coordinates")
    return (float(value[0]), float(value[1]), float(value[2]))


def clamp_target_to_reach(root: Sequence[float], target: Sequence[float], upper_length: float, lower_length: float) -> tuple[Point3, bool]:
    """Clamp a target to the annulus reachable by a two-link chain."""
    root_point = _point(root)
    target_point = _point(target)
    upper = float(upper_length)
    lower = float(lower_length)
    if upper <= 0.0 or lower <= 0.0 or not math.isfinite(upper + lower):
        raise ValueError("IK bone lengths must be positive finite values")
    vector = _sub(target_point, root_point)
    distance = _length(vector)
    minimum = abs(upper - lower) + 1e-7
    maximum = upper + lower
    if distance < 1e-9:
        direction = (0.0, 0.0, -1.0)
        return _add(root_point, _scale(direction, minimum)), False
    reachable = minimum <= distance <= maximum
    clamped = min(max(distance, minimum), maximum)
    return _add(root_point, _scale(_normalize(vector, (0.0, 0.0, -1.0)), clamped)), reachable


def solve_two_link_ik(root: Sequence[float], target: Sequence[float], upper_length: float, lower_length: float, pole_sign: float = 1.0) -> dict[str, object]:
    """Solve a two-link chain with a deterministic pole direction.

    The pole lies on the world Y axis, so positive/negative values choose the
    elbow side without changing the authored root or target trajectory.
    """
    root_point = _point(root)
    target_point = _point(target)
    upper = float(upper_length)
    lower = float(lower_length)
    clamped, reachable = clamp_target_to_reach(root_point, target_point, upper, lower)
    direction = _normalize(_sub(clamped, root_point), (0.0, 0.0, -1.0))
    pole = (0.0, 1.0 if float(pole_sign) >= 0.0 else -1.0, 0.0)
    pole_projection = _sub(pole, _scale(direction, _dot(pole, direction)))
    pole_axis = _normalize(pole_projection, (1.0, 0.0, 0.0))
    distance = _length(_sub(clamped, root_point))
    along = (upper * upper - lower * lower + distance * distance) / (2.0 * max(distance, 1e-9))
    height = math.sqrt(max(0.0, upper * upper - along * along))
    elbow = _add(root_point, _add(_scale(direction, along), _scale(pole_axis, height)))
    return {"root": root_point, "elbow": elbow, "target": clamped, "reachable": bool(reachable), "upper_length": upper, "lower_length": lower}
