from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class ShotScriptError(ValueError):
    """Raised when a ShotScript cannot be parsed or violates continuity."""


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ShotScriptError(f"{name} must be an object")
    return value


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ShotScriptError(f"{name} must be a positive number")
    return float(value)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShotScriptError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float

    @classmethod
    def from_value(cls, value: Any, name: str) -> "Vec3":
        if not isinstance(value, list) or len(value) != 3:
            raise ShotScriptError(f"{name} must contain exactly three coordinates")
        coordinates = []
        for coordinate in value:
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                raise ShotScriptError(f"{name} coordinates must be numeric")
            coordinates.append(float(coordinate))
        return cls(*coordinates)

    def as_list(self) -> list[float]:
        return [self.x, self.y, self.z]


@dataclass(frozen=True)
class CameraPlan:
    shot_size: str
    focal_length_mm: float
    motion: str
    start: Vec3
    end: Vec3
    look_at: str

    @classmethod
    def from_dict(cls, value: Any, prefix: str) -> "CameraPlan":
        data = _mapping(value, prefix)
        return cls(
            shot_size=_string(data.get("shot_size"), f"{prefix}.shot_size"),
            focal_length_mm=_positive_number(
                data.get("focal_length_mm"), f"{prefix}.focal_length_mm"
            ),
            motion=_string(data.get("motion"), f"{prefix}.motion"),
            start=Vec3.from_value(data.get("start"), f"{prefix}.start"),
            end=Vec3.from_value(data.get("end"), f"{prefix}.end"),
            look_at=_string(data.get("look_at"), f"{prefix}.look_at"),
        )


@dataclass(frozen=True)
class ActorPlan:
    actor_id: str
    color: str
    start: Vec3
    end: Vec3
    action: str
    facing: str

    @classmethod
    def from_dict(cls, value: Any, prefix: str) -> "ActorPlan":
        data = _mapping(value, prefix)
        return cls(
            actor_id=_string(data.get("id"), f"{prefix}.id"),
            color=_string(data.get("color"), f"{prefix}.color"),
            start=Vec3.from_value(data.get("start"), f"{prefix}.start"),
            end=Vec3.from_value(data.get("end"), f"{prefix}.end"),
            action=_string(data.get("action"), f"{prefix}.action"),
            facing=_string(data.get("facing"), f"{prefix}.facing"),
        )


@dataclass(frozen=True)
class ContinuityPlan:
    previous_shot: str | None
    screen_direction: str
    axis_side: str

    @classmethod
    def from_dict(cls, value: Any, prefix: str) -> "ContinuityPlan":
        data = _mapping(value, prefix)
        previous = data.get("previous_shot")
        if previous is not None:
            previous = _string(previous, f"{prefix}.previous_shot")
        return cls(
            previous_shot=previous,
            screen_direction=_string(
                data.get("screen_direction"), f"{prefix}.screen_direction"
            ),
            axis_side=_string(data.get("axis_side"), f"{prefix}.axis_side"),
        )


@dataclass(frozen=True)
class Shot:
    shot_id: str
    duration: float
    prompt: str
    camera: CameraPlan
    actors: tuple[ActorPlan, ...]
    continuity: ContinuityPlan

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "Shot":
        prefix = f"shots[{index}]"
        data = _mapping(value, prefix)
        actors_value = data.get("actors")
        if not isinstance(actors_value, list) or not actors_value:
            raise ShotScriptError(f"{prefix}.actors must be a non-empty list")
        return cls(
            shot_id=_string(data.get("shot_id"), f"{prefix}.shot_id"),
            duration=_positive_number(data.get("duration"), f"{prefix}.duration"),
            prompt=_string(data.get("prompt"), f"{prefix}.prompt"),
            camera=CameraPlan.from_dict(data.get("camera"), f"{prefix}.camera"),
            actors=tuple(
                ActorPlan.from_dict(actor, f"{prefix}.actors[{actor_index}]")
                for actor_index, actor in enumerate(actors_value)
            ),
            continuity=ContinuityPlan.from_dict(
                data.get("continuity"), f"{prefix}.continuity"
            ),
        )


@dataclass(frozen=True)
class ShotScript:
    scene_id: str
    environment_preset: str
    fps: int
    world_bounds: tuple[float, float, float, float]
    shots: tuple[Shot, ...]

    @classmethod
    def from_path(cls, path: Path | str) -> "ShotScript":
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShotScriptError(f"cannot read ShotScript {source}: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: object) -> "ShotScript":
        """Parse an already snapshotted ShotScript document."""

        root = _mapping(data, "root")
        fps_value = root.get("fps")
        if isinstance(fps_value, bool) or not isinstance(fps_value, int) or fps_value <= 0:
            raise ShotScriptError("fps must be a positive integer")
        bounds_value = root.get("world_bounds")
        if not isinstance(bounds_value, list) or len(bounds_value) != 4:
            raise ShotScriptError("world_bounds must contain four numbers")
        if any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in bounds_value
        ):
            raise ShotScriptError("world_bounds values must be numeric")
        shots_value = root.get("shots")
        if not isinstance(shots_value, list) or not shots_value:
            raise ShotScriptError("shots must be a non-empty list")
        preset_value = root.get("environment_preset")
        if preset_value is None and root.get("scene_id") == "station_platform":
            # Preserve the byte-bound Stage 1-7 station evidence while new
            # ShotScripts declare their environment explicitly.
            environment_preset = "station"
        else:
            environment_preset = _string(preset_value, "environment_preset")
        allowed_environment_presets = {
            "station",
            "city_crosswalk",
            "forest_path",
            "studio_room",
        }
        if environment_preset not in allowed_environment_presets:
            raise ShotScriptError(
                f"unsupported environment_preset: {environment_preset}"
            )
        script = cls(
            scene_id=_string(root.get("scene_id"), "scene_id"),
            environment_preset=environment_preset,
            fps=fps_value,
            world_bounds=tuple(float(item) for item in bounds_value),
            shots=tuple(Shot.from_dict(shot, index) for index, shot in enumerate(shots_value)),
        )
        script.validate()
        return script

    def validate(self) -> None:
        min_x, max_x, min_y, max_y = self.world_bounds
        if min_x >= max_x or min_y >= max_y:
            raise ShotScriptError("world_bounds minimums must be smaller than maximums")

        shot_ids = [shot.shot_id for shot in self.shots]
        if len(set(shot_ids)) != len(shot_ids):
            raise ShotScriptError("shot_id values must be unique")

        expected_actors = {actor.actor_id for actor in self.shots[0].actors}
        if len(expected_actors) != len(self.shots[0].actors):
            raise ShotScriptError("actor IDs must be unique within a shot")

        allowed_directions = {"left_to_right", "right_to_left", "static"}
        allowed_axis_sides = {"north", "south"}
        previous_actor_ends: dict[str, Vec3] | None = None

        for index, shot in enumerate(self.shots):
            expected_previous = None if index == 0 else self.shots[index - 1].shot_id
            if shot.continuity.previous_shot != expected_previous:
                raise ShotScriptError(
                    f"{shot.shot_id} previous_shot must be {expected_previous!r}"
                )
            if shot.continuity.screen_direction not in allowed_directions:
                raise ShotScriptError(
                    f"{shot.shot_id} has unsupported screen_direction"
                )
            if shot.continuity.axis_side not in allowed_axis_sides:
                raise ShotScriptError(f"{shot.shot_id} has unsupported axis_side")

            actor_ids = {actor.actor_id for actor in shot.actors}
            if len(actor_ids) != len(shot.actors):
                raise ShotScriptError(f"{shot.shot_id} contains duplicate actor IDs")
            if actor_ids != expected_actors:
                raise ShotScriptError(
                    f"{shot.shot_id} actor set differs from the first shot"
                )

            starts = {actor.actor_id: actor.start for actor in shot.actors}
            if previous_actor_ends is not None:
                for actor_id in expected_actors:
                    if starts[actor_id] != previous_actor_ends[actor_id]:
                        raise ShotScriptError(
                            f"{actor_id} is discontinuous before {shot.shot_id}"
                        )
            previous_actor_ends = {actor.actor_id: actor.end for actor in shot.actors}
