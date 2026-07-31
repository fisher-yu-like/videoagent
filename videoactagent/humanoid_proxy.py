"""Pure geometry and path-orientation contracts for Blender proxy actors."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


HEADING_TOLERANCE = 1e-9
ACTOR_GEOMETRY_PROFILE = "humanoid_v1"


@dataclass(frozen=True)
class HumanoidPartSpec:
    """One low-poly body part, positioned relative to an articulated anchor."""

    name: str
    primitive: str
    parent: str
    anchor: tuple[float, float, float]
    local_center: tuple[float, float, float]
    local_scale: tuple[float, float, float]


HUMANOID_PARTS = (
    HumanoidPartSpec("head", "ico_sphere", "torso", (0.0, 0.0, 0.83), (0.0, 0.0, 0.0), (0.28, 0.25, 0.30)),
    HumanoidPartSpec("torso", "cube", "root", (0.0, 0.0, 1.75), (0.0, 0.0, 0.0), (0.45, 0.24, 0.44)),
    HumanoidPartSpec("pelvis", "cube", "torso", (0.0, 0.0, -0.52), (0.0, 0.0, 0.0), (0.36, 0.22, 0.20)),
    HumanoidPartSpec("upper_arm.L", "cylinder", "torso", (0.53, 0.0, 0.30), (0.0, 0.0, -0.29), (0.14, 0.14, 0.29)),
    HumanoidPartSpec("lower_arm.L", "cylinder", "upper_arm.L", (0.0, 0.0, -0.58), (0.0, 0.0, -0.27), (0.12, 0.12, 0.27)),
    HumanoidPartSpec("upper_arm.R", "cylinder", "torso", (-0.53, 0.0, 0.30), (0.0, 0.0, -0.29), (0.14, 0.14, 0.29)),
    HumanoidPartSpec("lower_arm.R", "cylinder", "upper_arm.R", (0.0, 0.0, -0.58), (0.0, 0.0, -0.27), (0.12, 0.12, 0.27)),
    HumanoidPartSpec("upper_leg.L", "cylinder", "pelvis", (0.22, 0.0, -0.08), (0.0, 0.0, -0.32), (0.16, 0.17, 0.32)),
    HumanoidPartSpec("lower_leg.L", "cylinder", "upper_leg.L", (0.0, 0.0, -0.64), (0.0, 0.0, -0.25), (0.13, 0.15, 0.25)),
    HumanoidPartSpec("upper_leg.R", "cylinder", "pelvis", (-0.22, 0.0, -0.08), (0.0, 0.0, -0.32), (0.16, 0.17, 0.32)),
    HumanoidPartSpec("lower_leg.R", "cylinder", "upper_leg.R", (0.0, 0.0, -0.64), (0.0, 0.0, -0.25), (0.13, 0.15, 0.25)),
)

REQUIRED_ACTOR_PARTS = tuple(sorted(part.name for part in HUMANOID_PARTS))


def _xy_points(points: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    if not points:
        raise ValueError("points must not be empty")
    converted = []
    for point in points:
        try:
            x, y = float(point[0]), float(point[1])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("each point must contain two finite numeric coordinates") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("point coordinates must be finite")
        converted.append((x, y))
    return tuple(converted)


def path_heading_degrees(points: Sequence[Sequence[float]], index: int) -> float:
    """Return an XY path tangent, preferring future motion at ``index``.

    Duplicate and sub-tolerance segments are deterministic stationary samples.
    When no future segment moves, the most recent prior moving segment is used.
    """

    converted = _xy_points(points)
    if type(index) is not int or not 0 <= index < len(converted):
        raise ValueError("index must identify a point")

    tolerance_squared = HEADING_TOLERANCE * HEADING_TOLERANCE

    def segment_heading(segment_index: int) -> float | None:
        start = converted[segment_index]
        end = converted[segment_index + 1]
        dx, dy = end[0] - start[0], end[1] - start[1]
        if dx * dx + dy * dy <= tolerance_squared:
            return None
        return math.degrees(math.atan2(dy, dx))

    for segment_index in range(index, len(converted) - 1):
        heading = segment_heading(segment_index)
        if heading is not None:
            return heading
    for segment_index in range(min(index - 1, len(converted) - 2), -1, -1):
        heading = segment_heading(segment_index)
        if heading is not None:
            return heading
    return 0.0


def unwrap_heading_degrees(headings: Sequence[float]) -> tuple[float, ...]:
    """Unwrap headings so linear interpolation always takes the short turn."""

    converted = tuple(float(value) for value in headings)
    if any(not math.isfinite(value) for value in converted):
        raise ValueError("headings must be finite")
    if not converted:
        return ()
    unwrapped = [converted[0]]
    for heading in converted[1:]:
        delta = (heading - unwrapped[-1] + 180.0) % 360.0 - 180.0
        unwrapped.append(unwrapped[-1] + delta)
    return tuple(unwrapped)
