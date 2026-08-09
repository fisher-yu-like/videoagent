"""Validated, hashable shared world-state contract for pipeline v2."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any


SCHEMA_VERSION = "world-state-1.0"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ROOT_KEYS = {
    "schema_version",
    "scene_plan",
    "physical_state_plan",
    "character_trajectory_plan",
    "object_trajectory_plan",
    "camera_trajectory_plan",
}


class WorldStateError(ValueError):
    """Raised when a world-state document cannot drive a shared proxy."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorldStateError(f"{label} must be an object")
    return value


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        raise WorldStateError(
            f"{label} fields invalid: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise WorldStateError(f"{label} must be a safe identifier")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorldStateError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise WorldStateError(f"{label} must be a finite number")
    return result


def _vector(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise WorldStateError(f"{label} must contain three coordinates")
    return [_number(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _points(value: object, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) < 2:
        raise WorldStateError(f"{label} must contain at least two points")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        point = _mapping(raw, f"{label}[{index}]")
        _exact(point, {"frame", "position", "rotation"}, f"{label}[{index}]")
        frame = point["frame"]
        if type(frame) is not int or frame < 0:
            raise WorldStateError(f"{label}[{index}].frame must be a non-negative integer")
        result.append({
            "frame": frame,
            "position": _vector(point["position"], f"{label}[{index}].position"),
            "rotation": _vector(point["rotation"], f"{label}[{index}].rotation"),
        })
    frames = [item["frame"] for item in result]
    if frames != sorted(set(frames)):
        raise WorldStateError(f"{label} frame indices must be strictly increasing")
    return tuple(result)


def _track_group(
    value: object,
    label: str,
    *,
    allowed_ids: set[str],
    expected_kind: str,
    frame_count: int,
) -> tuple[dict[str, Any], ...]:
    group = _mapping(value, label)
    _exact(group, {"tracks"}, label)
    raw_tracks = group["tracks"]
    if not isinstance(raw_tracks, list):
        raise WorldStateError(f"{label}.tracks must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tracks):
        track = _mapping(raw, f"{label}.tracks[{index}]")
        _exact(track, {"target_id", "points"}, f"{label}.tracks[{index}]")
        target_id = _id(track["target_id"], f"{label}.tracks[{index}].target_id")
        if target_id not in allowed_ids:
            raise WorldStateError(f"unknown target {target_id!r} in {label}")
        if target_id in seen:
            raise WorldStateError(f"duplicate target {target_id!r} in {label}")
        seen.add(target_id)
        points = _points(track["points"], f"{label}.tracks[{index}].points")
        if any(item["frame"] >= frame_count for item in points):
            raise WorldStateError(f"{label} contains a frame outside frame_count")
        # ``kind`` is an internal validation fact, not part of the public
        # trajectory schema. Keeping it out makes WorldState.to_dict() safely
        # round-trip through WorldState.from_dict().
        result.append({"target_id": target_id, "points": list(points)})
    return tuple(result)


def _camera_group(
    value: object,
    *,
    entity_ids: set[str],
    frame_count: int,
) -> tuple[dict[str, Any], ...]:
    group = _mapping(value, "camera_trajectory_plan")
    _exact(group, {"cameras"}, "camera_trajectory_plan")
    raw_cameras = group["cameras"]
    if not isinstance(raw_cameras, list):
        raise WorldStateError("camera_trajectory_plan.cameras must be a list")
    if len(raw_cameras) > 8:
        raise WorldStateError("at most 8 cameras are supported")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cameras):
        camera = _mapping(raw, f"cameras[{index}]")
        _exact(
            camera,
            {"id", "role", "target", "lens_mm", "roll_deg", "points"},
            f"cameras[{index}]",
        )
        camera_id = _id(camera["id"], f"cameras[{index}].id")
        if camera_id in seen:
            raise WorldStateError(f"duplicate camera id {camera_id!r}")
        seen.add(camera_id)
        target = _mapping(camera["target"], f"cameras[{index}].target")
        if set(target) == {"object_id"}:
            target_value = {"object_id": _id(target["object_id"], "camera target.object_id")}
            if target_value["object_id"] not in entity_ids:
                raise WorldStateError(f"unknown camera target {target_value['object_id']!r}")
        elif set(target) == {"point"}:
            target_value = {"point": _vector(target["point"], "camera target.point")}
        else:
            raise WorldStateError("camera target must contain object_id or point")
        points = _points(camera["points"], f"cameras[{index}].points")
        if any(item["frame"] >= frame_count for item in points):
            raise WorldStateError(f"camera {camera_id!r} contains a frame outside frame_count")
        lens = _number(camera["lens_mm"], f"cameras[{index}].lens_mm")
        if lens <= 0:
            raise WorldStateError("camera lens_mm must be positive")
        result.append({
            "id": camera_id,
            "role": str(camera["role"]),
            "target": target_value,
            "lens_mm": lens,
            "roll_deg": _number(camera["roll_deg"], f"cameras[{index}].roll_deg"),
            "points": list(points),
        })
    if not result:
        raise WorldStateError("at least one camera is required")
    return tuple(result)


def _frame_indices(groups: tuple[tuple[dict[str, Any], ...], ...], cameras: tuple[dict[str, Any], ...]) -> tuple[int, ...]:
    sequences: list[tuple[int, ...]] = []
    for group in groups:
        sequences.extend(tuple(item["frame"] for item in track["points"]) for track in group)
    sequences.extend(tuple(item["frame"] for item in camera["points"]) for camera in cameras)
    if not sequences:
        raise WorldStateError("trajectory plans must contain at least one track")
    first = sequences[0]
    if any(sequence != first for sequence in sequences[1:]):
        raise WorldStateError("frame indices must be shared by all trajectories")
    return first


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class WorldState:
    """Immutable normalized world state used by all downstream modules."""

    _document: dict[str, Any]
    scene_id: str
    fps: int
    frame_count: int
    frame_indices: tuple[int, ...]
    camera_count: int

    @classmethod
    def from_dict(cls, value: object) -> "WorldState":
        root = _mapping(value, "world state")
        _exact(root, _ROOT_KEYS, "world state")
        if root["schema_version"] != SCHEMA_VERSION:
            raise WorldStateError(f"schema_version must be {SCHEMA_VERSION!r}")

        scene = _mapping(root["scene_plan"], "scene_plan")
        _exact(
            scene,
            {"scene_id", "environment_preset", "duration_seconds", "fps", "frame_count", "entities"},
            "scene_plan",
        )
        scene_id = _id(scene["scene_id"], "scene_plan.scene_id")
        fps = scene["fps"]
        if type(fps) is not int or fps <= 0:
            raise WorldStateError("scene_plan.fps must be a positive integer")
        frame_count = scene["frame_count"]
        if type(frame_count) is not int or frame_count <= 1:
            raise WorldStateError("scene_plan.frame_count must be greater than one")
        duration = _number(scene["duration_seconds"], "scene_plan.duration_seconds")
        if duration <= 0:
            raise WorldStateError("scene_plan.duration_seconds must be positive")
        expected_frame_count = int(round(duration * fps))
        if expected_frame_count != frame_count:
            raise WorldStateError(
                "duration_seconds * fps must equal frame_count"
            )
        entities = scene["entities"]
        if not isinstance(entities, list) or not entities:
            raise WorldStateError("scene_plan.entities must be a non-empty list")
        entity_ids: set[str] = set()
        entity_kinds: dict[str, str] = {}
        for index, raw in enumerate(entities):
            entity = _mapping(raw, f"scene_plan.entities[{index}]")
            _exact(entity, {"id", "kind", "asset"}, f"scene_plan.entities[{index}]")
            entity_id = _id(entity["id"], f"scene_plan.entities[{index}].id")
            if entity_id in entity_ids:
                raise WorldStateError(f"duplicate entity id {entity_id!r}")
            if entity["kind"] not in {"character", "object"}:
                raise WorldStateError("entity kind must be character or object")
            entity_ids.add(entity_id)
            entity_kinds[entity_id] = str(entity["kind"])

        physical = _mapping(root["physical_state_plan"], "physical_state_plan")
        unknown_physical = set(physical) - {"events", "parameters"}
        if unknown_physical or "events" not in physical:
            raise WorldStateError(
                "physical_state_plan fields invalid: "
                f"missing={sorted({ 'events' } - set(physical))}, "
                f"unknown={sorted(unknown_physical)}"
            )
        events = physical["events"]
        if not isinstance(events, list):
            raise WorldStateError("physical_state_plan.events must be a list")
        parameters = physical.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise WorldStateError("physical_state_plan.parameters must be an object")
        try:
            json.dumps(parameters, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise WorldStateError("physical_state_plan.parameters must be JSON-safe") from exc
        normalized_events: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        for index, raw in enumerate(events):
            event = _mapping(raw, f"physical_state_plan.events[{index}]")
            _exact(event, {"id", "frame", "type", "participants"}, f"physical_state_plan.events[{index}]")
            event_id = _id(event["id"], f"physical_state_plan.events[{index}].id")
            if event_id in event_ids:
                raise WorldStateError(f"duplicate physical event id {event_id!r}")
            frame = event["frame"]
            if type(frame) is not int or not 0 <= frame < frame_count:
                raise WorldStateError("physical event frame is outside frame_count")
            participants = event["participants"]
            if not isinstance(participants, list) or any(item not in entity_ids for item in participants):
                raise WorldStateError("physical event participants must reference entities")
            event_ids.add(event_id)
            normalized_events.append({
                "id": event_id,
                "frame": frame,
                "type": str(event["type"]),
                "participants": list(participants),
            })

        character_tracks = _track_group(
            root["character_trajectory_plan"],
            "character_trajectory_plan",
            allowed_ids={item for item, kind in entity_kinds.items() if kind == "character"},
            expected_kind="character",
            frame_count=frame_count,
        )
        object_tracks = _track_group(
            root["object_trajectory_plan"],
            "object_trajectory_plan",
            allowed_ids={item for item, kind in entity_kinds.items() if kind == "object"},
            expected_kind="object",
            frame_count=frame_count,
        )
        cameras = _camera_group(
            root["camera_trajectory_plan"],
            entity_ids=entity_ids,
            frame_count=frame_count,
        )
        frames = _frame_indices((character_tracks, object_tracks), cameras)
        document = {
            "schema_version": SCHEMA_VERSION,
            "scene_plan": {
                **dict(scene),
                "duration_seconds": duration,
                "entities": [dict(item) for item in entities],
            },
            "physical_state_plan": {
                "events": normalized_events,
                "parameters": copy.deepcopy(dict(parameters)),
            },
            "character_trajectory_plan": {"tracks": list(character_tracks)},
            "object_trajectory_plan": {"tracks": list(object_tracks)},
            "camera_trajectory_plan": {"cameras": list(cameras)},
        }
        return cls(
            _document=copy.deepcopy(document),
            scene_id=scene_id,
            fps=fps,
            frame_count=frame_count,
            frame_indices=frames,
            camera_count=len(cameras),
        )

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._document)

    def canonical_bytes(self) -> bytes:
        return _canonical(self._document)

    def world_state_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def object_state_hash(self) -> str:
        object_document = {
            "scene_plan": self._document["scene_plan"],
            "physical_state_plan": self._document["physical_state_plan"],
            "character_trajectory_plan": self._document["character_trajectory_plan"],
            "object_trajectory_plan": self._document["object_trajectory_plan"],
        }
        return hashlib.sha256(_canonical(object_document)).hexdigest()
