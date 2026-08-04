"""Strict Prompt-to-ShotScript scene-plan contract and deterministic compilers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from typing import Any


SCHEMA_VERSION = "scene-plan-1.0"
ACTOR_ACTIONS = ("walk", "wait", "stand", "approach", "cross", "follow", "carry")
ENVIRONMENT_PRESETS = frozenset({
    "station",
    "city_crosswalk",
    "forest_path",
    "studio_room",
    "cafe",
    "warehouse",
    "generic",
})
_ROOT_REQUIRED = frozenset({
    "schema_version",
    "scene_id",
    "environment_preset",
    "duration_seconds",
    "fps",
    "world_bounds",
    "actors",
    "initial_camera",
    "explanation",
})
_ROOT_OPTIONAL = frozenset({"objects"})
_ACTOR_FIELDS = frozenset({"id", "color", "action", "start", "end", "facing"})
_OBJECT_FIELDS = frozenset({"id", "primitive", "semantic", "start", "end"})
_CAMERA_FIELDS = frozenset({
    "shot_size", "focal_length_mm", "motion", "start", "end", "look_at"
})
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SHOT_SIZES = frozenset({"extreme_wide", "wide", "medium", "close", "extreme_close"})
_CAMERA_MOTIONS = frozenset({
    "static", "follow", "arc", "dolly", "truck", "dolly_in", "dolly_out",
    "truck_left", "truck_right", "pan_left", "pan_right",
})
_OBJECT_PRIMITIVES = frozenset({"cube", "sphere", "cylinder"})
_KEYFRAME_TIMES = (0.0, 0.2, 0.5, 0.8, 1.0)


class ScenePlanError(ValueError):
    """Raised when a scene-plan draft violates its closed contract."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenePlanError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ScenePlanError(f"{label} field names must be strings")
    return value


def _exact_fields(
    value: Mapping[str, Any], allowed: frozenset[str], label: str,
    *, required: frozenset[str] | None = None,
) -> None:
    required_fields = allowed if required is None else required
    unknown = set(value) - allowed
    missing = required_fields - set(value)
    if unknown:
        raise ScenePlanError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ScenePlanError(f"{label} is missing fields: {sorted(missing)}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenePlanError(f"{label} must be a non-empty string")
    return value


def _identifier(value: object, label: str) -> str:
    result = _string(value, label)
    if not _ID_PATTERN.fullmatch(result):
        raise ScenePlanError(f"{label} must be a safe identifier")
    return result


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenePlanError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        qualifier = "positive " if positive else ""
        raise ScenePlanError(f"{label} must be a finite {qualifier}number")
    return result


def _vector(value: object, size: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ScenePlanError(f"{label} must contain exactly {size} coordinates")
    return tuple(_finite(item, f"{label}[{index}]") for index, item in enumerate(value))


def _enum(value: object, allowed: frozenset[str], label: str) -> str:
    result = _string(value, label)
    if result not in allowed:
        raise ScenePlanError(f"{label} must be one of {sorted(allowed)}")
    return result


def _inside(point: tuple[float, ...], bounds: tuple[float, float, float, float]) -> bool:
    min_x, max_x, min_y, max_y = bounds
    return min_x <= point[0] <= max_x and min_y <= point[1] <= max_y


@dataclass(frozen=True)
class SceneActor:
    actor_id: str
    color: str
    action: str
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    facing: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.actor_id,
            "color": self.color,
            "action": self.action,
            "start": list(self.start),
            "end": list(self.end),
            "facing": self.facing,
        }


@dataclass(frozen=True)
class SceneObject:
    object_id: str
    primitive: str
    semantic: str
    start: tuple[float, float]
    end: tuple[float, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.object_id,
            "primitive": self.primitive,
            "semantic": self.semantic,
            "start": list(self.start),
            "end": list(self.end),
        }


@dataclass(frozen=True)
class InitialCamera:
    shot_size: str
    focal_length_mm: float
    motion: str
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    look_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "shot_size": self.shot_size,
            "focal_length_mm": self.focal_length_mm,
            "motion": self.motion,
            "start": list(self.start),
            "end": list(self.end),
            "look_at": self.look_at,
        }


@dataclass(frozen=True)
class ScenePlanDraft:
    scene_id: str
    environment_preset: str
    duration_seconds: float
    fps: int
    world_bounds: tuple[float, float, float, float]
    actors: tuple[SceneActor, ...]
    objects: tuple[SceneObject, ...]
    initial_camera: InitialCamera
    explanation: str
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: object) -> "ScenePlanDraft":
        root = _object(value, "scene plan")
        _exact_fields(
            root,
            _ROOT_REQUIRED | _ROOT_OPTIONAL,
            "scene plan",
            required=_ROOT_REQUIRED,
        )
        if root["schema_version"] != SCHEMA_VERSION:
            raise ScenePlanError(f"schema_version must be {SCHEMA_VERSION!r}")
        scene_id = _identifier(root["scene_id"], "scene_id")
        preset = _enum(root["environment_preset"], ENVIRONMENT_PRESETS, "environment_preset")
        duration = _finite(root["duration_seconds"], "duration_seconds", positive=True)
        fps_value = root["fps"]
        if type(fps_value) is not int or fps_value <= 0:
            raise ScenePlanError("fps must be a positive integer")
        frame_count = int(round(duration * fps_value))
        if frame_count < 1:
            raise ScenePlanError(
                f"duration_seconds {duration:g}s at {fps_value} fps "
                f"rounds to {frame_count} frames"
            )
        bounds_value = _vector(root["world_bounds"], 4, "world_bounds")
        bounds = (bounds_value[0], bounds_value[1], bounds_value[2], bounds_value[3])
        if bounds[0] >= bounds[1] or bounds[2] >= bounds[3]:
            raise ScenePlanError("world_bounds minimums must be smaller than maximums")

        actor_values = root["actors"]
        if not isinstance(actor_values, list) or not 1 <= len(actor_values) <= 3:
            raise ScenePlanError("actors must contain one to three entries")
        actors: list[SceneActor] = []
        for index, raw_actor in enumerate(actor_values):
            label = f"actors[{index}]"
            data = _object(raw_actor, label)
            _exact_fields(data, _ACTOR_FIELDS, label)
            color = _string(data["color"], f"{label}.color")
            if not _COLOR_PATTERN.fullmatch(color):
                raise ScenePlanError(f"{label}.color must be a six-digit hexadecimal color")
            start_value = _vector(data["start"], 3, f"{label}.start")
            end_value = _vector(data["end"], 3, f"{label}.end")
            start = (start_value[0], start_value[1], start_value[2])
            end = (end_value[0], end_value[1], end_value[2])
            if not _inside(start, bounds):
                raise ScenePlanError(f"{label}.start must be inside world_bounds")
            if not _inside(end, bounds):
                raise ScenePlanError(f"{label}.end must be inside world_bounds")
            actors.append(SceneActor(
                actor_id=_identifier(data["id"], f"{label}.id"),
                color=color,
                action=_enum(
                    data["action"], frozenset(ACTOR_ACTIONS), f"{label}.action"
                ),
                start=start,
                end=end,
                facing=_string(data["facing"], f"{label}.facing"),
            ))

        object_values = root.get("objects", [])
        if not isinstance(object_values, list):
            raise ScenePlanError("objects must be a list")
        objects: list[SceneObject] = []
        for index, raw_object in enumerate(object_values):
            label = f"objects[{index}]"
            data = _object(raw_object, label)
            _exact_fields(data, _OBJECT_FIELDS, label)
            start_value = _vector(data["start"], 2, f"{label}.start")
            end_value = _vector(data["end"], 2, f"{label}.end")
            start = (start_value[0], start_value[1])
            end = (end_value[0], end_value[1])
            if not _inside(start, bounds):
                raise ScenePlanError(f"{label}.start must be inside world_bounds")
            if not _inside(end, bounds):
                raise ScenePlanError(f"{label}.end must be inside world_bounds")
            objects.append(SceneObject(
                object_id=_identifier(data["id"], f"{label}.id"),
                primitive=_enum(data["primitive"], _OBJECT_PRIMITIVES, f"{label}.primitive"),
                semantic=_enum(data["semantic"], frozenset({"move"}), f"{label}.semantic"),
                start=start,
                end=end,
            ))

        ids = [actor.actor_id for actor in actors] + [item.object_id for item in objects]
        if len(ids) != len(set(ids)):
            raise ScenePlanError("actor and object IDs must be globally unique")
        actor_ids = {actor.actor_id for actor in actors}
        for index, actor in enumerate(actors):
            if actor.facing not in actor_ids - {actor.actor_id} and actor.facing not in {
                "camera", "movement_direction"
            }:
                raise ScenePlanError(f"actors[{index}].facing must identify another actor")

        camera_data = _object(root["initial_camera"], "initial_camera")
        _exact_fields(camera_data, _CAMERA_FIELDS, "initial_camera")
        look_at = _string(camera_data["look_at"], "initial_camera.look_at")
        if look_at not in actor_ids and look_at not in {"actors_midpoint", "fixed_actors_midpoint"}:
            raise ScenePlanError("initial_camera.look_at must identify an actor or actors_midpoint")
        camera_start = _vector(camera_data["start"], 3, "initial_camera.start")
        camera_end = _vector(camera_data["end"], 3, "initial_camera.end")
        camera = InitialCamera(
            shot_size=_enum(camera_data["shot_size"], _SHOT_SIZES, "initial_camera.shot_size"),
            focal_length_mm=_finite(
                camera_data["focal_length_mm"], "initial_camera.focal_length_mm", positive=True
            ),
            motion=_enum(camera_data["motion"], _CAMERA_MOTIONS, "initial_camera.motion"),
            start=(camera_start[0], camera_start[1], camera_start[2]),
            end=(camera_end[0], camera_end[1], camera_end[2]),
            look_at=look_at,
        )
        return cls(
            schema_version=SCHEMA_VERSION,
            scene_id=scene_id,
            environment_preset=preset,
            duration_seconds=duration,
            fps=fps_value,
            world_bounds=bounds,
            actors=tuple(actors),
            objects=tuple(objects),
            initial_camera=camera,
            explanation=_string(root["explanation"], "explanation"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scene_id": self.scene_id,
            "environment_preset": self.environment_preset,
            "duration_seconds": self.duration_seconds,
            "fps": self.fps,
            "world_bounds": list(self.world_bounds),
            "actors": [actor.to_dict() for actor in self.actors],
            "objects": [item.to_dict() for item in self.objects],
            "initial_camera": self.initial_camera.to_dict(),
            "explanation": self.explanation,
        }

    def to_shotscript(self, story_prompt: str) -> dict[str, object]:
        prompt = _string(story_prompt, "story_prompt")
        delta_x = self.actors[0].end[0] - self.actors[0].start[0]
        direction = "left_to_right" if delta_x > 0 else "right_to_left" if delta_x < 0 else "static"
        return {
            "scene_id": self.scene_id,
            "environment_preset": self.environment_preset,
            "fps": self.fps,
            "world_bounds": list(self.world_bounds),
            "shots": [{
                "shot_id": "whole",
                "duration": self.duration_seconds,
                "prompt": prompt,
                "camera": self.initial_camera.to_dict(),
                "actors": [actor.to_dict() for actor in self.actors],
                "continuity": {
                    "previous_shot": None,
                    "screen_direction": direction,
                    "axis_side": "north",
                },
            }],
        }

    def to_trajectory(self) -> dict[str, object]:
        min_x, max_x, min_y, max_y = self.world_bounds

        def normalized_points(start: tuple[float, ...], end: tuple[float, ...]):
            return [{
                "t": time_value,
                "x": ((start[0] + (end[0] - start[0]) * time_value) - min_x) / (max_x - min_x),
                "y": (max_y - (start[1] + (end[1] - start[1]) * time_value)) / (max_y - min_y),
                "visible": True,
            } for time_value in _KEYFRAME_TIMES]

        tracks: list[dict[str, object]] = []
        for actor in self.actors:
            tracks.append({
                "track_id": f"actor_{actor.actor_id}_path",
                "target": {"type": "actor", "id": actor.actor_id},
                "primitive": "polyline",
                "semantic": "move",
                "points": normalized_points(actor.start, actor.end),
            })
        for item in self.objects:
            tracks.append({
                "track_id": f"object_{item.object_id}_path",
                "target": {"type": "object", "id": item.object_id},
                "primitive": "polyline",
                "semantic": "move",
                "points": normalized_points(item.start, item.end),
            })
        return {
            "schema_version": "0.1",
            "scene_id": self.scene_id,
            "shot_id": "whole",
            "coordinate_space": "normalized_0_1_top_left",
            "duration_seconds": self.duration_seconds,
            "sample_count": max(1, round(self.duration_seconds * self.fps)),
            "tracks": tracks,
        }
