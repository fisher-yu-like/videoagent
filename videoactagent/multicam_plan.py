"""Strict high-level contract for a three-camera director plan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any


_PLAN_FIELDS = {
    "schema_version", "scene_id", "director_intent", "actor_staging",
    "locked_through_keyframe", "cameras",
}
_CAMERA_FIELDS = {
    "camera_id", "role", "target", "side", "shot_size", "motion",
    "look_at_policy", "responsibility_segments", "constraints", "rationale",
}
_CAMERA_IDS = ("camera_a", "camera_b", "camera_c")
_ROLES = {"master", "follow", "reverse"}
_SIDES = {
    "north", "south", "east", "west", "north_east", "north_west",
    "south_east", "south_west",
}
_SHOT_SIZES = {"extreme_wide", "wide", "medium", "close", "extreme_close"}
_MOTIONS = {"static", "follow", "arc", "dolly", "truck"}
_LOOK_POLICIES = {"actors_midpoint", "target_actor", "fixed_world_point"}
_LOCKS = {None, "K0", "K1", "K2", "K3"}


class MulticamPlanError(ValueError):
    """Raised when an Agent plan cannot safely drive deterministic compilation."""


@dataclass(frozen=True)
class ResponsibilitySegment:
    start: float
    end: float

    def to_list(self) -> list[float]:
        return [self.start, self.end]


@dataclass(frozen=True)
class CameraAssignment:
    camera_id: str
    role: str
    target: str
    side: str
    shot_size: str
    motion: str
    look_at_policy: str
    responsibility_segments: tuple[ResponsibilitySegment, ...]
    constraints: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "role": self.role,
            "target": self.target,
            "side": self.side,
            "shot_size": self.shot_size,
            "motion": self.motion,
            "look_at_policy": self.look_at_policy,
            "responsibility_segments": [
                segment.to_list() for segment in self.responsibility_segments
            ],
            "constraints": list(self.constraints),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class MulticamPlan:
    scene_id: str
    director_intent: str
    actor_staging: tuple[str, ...]
    locked_through_keyframe: str | None
    cameras: tuple[CameraAssignment, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "scene_id": self.scene_id,
            "director_intent": self.director_intent,
            "actor_staging": list(self.actor_staging),
            "locked_through_keyframe": self.locked_through_keyframe,
            "cameras": [camera.to_dict() for camera in self.cameras],
        }


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise MulticamPlanError(
            f"{label} fields are invalid: missing={sorted(fields - set(value))}, "
            f"unknown={sorted(set(value) - fields)}"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MulticamPlanError(f"{label} must be non-empty text")
    result = value.strip()
    if len(result) > 1000 or any(ord(character) < 32 for character in result):
        raise MulticamPlanError(f"{label} contains unsafe text")
    return result


def _text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MulticamPlanError(f"{label} must be a non-empty list")
    return tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))


def _segments(value: object, label: str) -> tuple[ResponsibilitySegment, ...]:
    if not isinstance(value, list) or not value:
        raise MulticamPlanError(f"{label} must be a non-empty list")
    result = []
    for index, raw in enumerate(value):
        if not isinstance(raw, list) or len(raw) != 2:
            raise MulticamPlanError(f"{label}[{index}] must be [start, end]")
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw):
            raise MulticamPlanError(f"{label}[{index}] endpoints must be finite numbers")
        start, end = float(raw[0]), float(raw[1])
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= 1:
            raise MulticamPlanError(f"{label}[{index}] must satisfy 0 <= start < end <= 1")
        result.append(ResponsibilitySegment(start, end))
    return tuple(result)


def load_multicam_plan(
    value: Mapping[str, Any], *, scene_id: str, actors: Sequence[str],
    locked_through_keyframe: str | None = None,
) -> MulticamPlan:
    if not isinstance(value, Mapping):
        raise MulticamPlanError("plan must be one object")
    _exact(value, _PLAN_FIELDS, "plan")
    if value.get("schema_version") != "1.0":
        raise MulticamPlanError("schema_version must be 1.0")
    if value.get("scene_id") != scene_id:
        raise MulticamPlanError("scene_id differs from the source scene")
    lock = value.get("locked_through_keyframe")
    if lock not in _LOCKS or lock != locked_through_keyframe:
        raise MulticamPlanError("locked prefix differs from the planning request")
    actor_ids = tuple(actors)
    if not actor_ids or len(set(actor_ids)) != len(actor_ids):
        raise MulticamPlanError("source actors must be unique and non-empty")
    raw_cameras = value.get("cameras")
    if not isinstance(raw_cameras, list) or len(raw_cameras) != 3:
        raise MulticamPlanError("plan must contain exactly three cameras")
    cameras = []
    for index, raw in enumerate(raw_cameras):
        if not isinstance(raw, Mapping):
            raise MulticamPlanError(f"cameras[{index}] must be an object")
        _exact(raw, _CAMERA_FIELDS, f"cameras[{index}]")
        camera_id = raw.get("camera_id")
        role = raw.get("role")
        target = raw.get("target")
        side = raw.get("side")
        shot_size = raw.get("shot_size")
        motion = raw.get("motion")
        look_policy = raw.get("look_at_policy")
        if camera_id not in _CAMERA_IDS:
            raise MulticamPlanError("camera IDs must be camera_a, camera_b and camera_c")
        if role not in _ROLES:
            raise MulticamPlanError(f"{camera_id} role is invalid")
        if target != "all_actors" and target not in actor_ids:
            raise MulticamPlanError(f"{camera_id} target is not a source actor")
        if side not in _SIDES:
            raise MulticamPlanError(f"{camera_id} side is invalid")
        if shot_size not in _SHOT_SIZES:
            raise MulticamPlanError(f"{camera_id} shot_size is invalid")
        if motion not in _MOTIONS:
            raise MulticamPlanError(f"{camera_id} motion is invalid")
        if look_policy not in _LOOK_POLICIES:
            raise MulticamPlanError(f"{camera_id} look_at_policy is invalid")
        cameras.append(CameraAssignment(
            camera_id=str(camera_id), role=str(role), target=str(target), side=str(side),
            shot_size=str(shot_size), motion=str(motion), look_at_policy=str(look_policy),
            responsibility_segments=_segments(
                raw.get("responsibility_segments"), f"{camera_id}.responsibility_segments"
            ),
            constraints=_text_list(raw.get("constraints"), f"{camera_id}.constraints"),
            rationale=_text(raw.get("rationale"), f"{camera_id}.rationale"),
        ))
    if tuple(camera.camera_id for camera in cameras) != _CAMERA_IDS:
        raise MulticamPlanError("camera IDs must be ordered camera_a, camera_b and camera_c")
    coverage = sorted(
        [
            segment
            for camera in cameras
            for segment in camera.responsibility_segments
        ],
        key=lambda segment: (segment.start, segment.end),
    )
    cursor = 0.0
    for segment in coverage:
        if not math.isclose(segment.start, cursor, rel_tol=0.0, abs_tol=1e-9):
            raise MulticamPlanError("camera responsibility coverage has a gap or overlap")
        cursor = segment.end
    if not math.isclose(cursor, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise MulticamPlanError("camera responsibility coverage must end at 1.0")
    return MulticamPlan(
        scene_id=scene_id,
        director_intent=_text(value.get("director_intent"), "director_intent"),
        actor_staging=_text_list(value.get("actor_staging"), "actor_staging"),
        locked_through_keyframe=lock,
        cameras=tuple(cameras),
    )
