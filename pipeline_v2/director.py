"""Agent Director boundary for semantic plans and deterministic compilation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import copy
import json
import math
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .state import WorldState, WorldStateError
from .json_repair import JSONRepairError, parse_json_response


DIRECTOR_SCHEMA_VERSION = "director-plan-1.0"
DEFAULT_MODEL = "deepseek-v4-flash"
OPENAI_DEFAULT_MODEL = "gpt5.6luna"


class DirectorError(ValueError):
    """Raised when the Director response cannot become a valid WorldState."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DirectorError(f"{label} must be an object")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise DirectorError(
            f"{label} fields invalid: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DirectorError(f"{label} must be non-empty text")
    return value.strip()


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DirectorError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise DirectorError(f"{label} must be a finite number")
    return result


def _unit_time(value: object, label: str) -> float:
    result = _number(value, label)
    if not 0.0 <= result <= 1.0:
        raise DirectorError(f"{label} must be in [0, 1]")
    return result


def _vector(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise DirectorError(f"{label} must contain three coordinates")
    return [_number(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _compile_points(value: object, label: str, frame_count: int, *, include_rotation: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 2:
        raise DirectorError(f"{label} must contain at least two points")
    result: list[dict[str, Any]] = []
    times: list[float] = []
    frames: list[int] = []
    for index, raw in enumerate(value):
        point = _mapping(raw, f"{label}[{index}]")
        expected = {"t", "position", "rotation"} if include_rotation else {"t", "position"}
        _exact(point, expected, f"{label}[{index}]")
        t = _unit_time(point["t"], f"{label}[{index}].t")
        frame = int(round(t * (frame_count - 1)))
        times.append(t)
        frames.append(frame)
        compiled: dict[str, Any] = {"frame": frame, "position": _vector(point["position"], f"{label}[{index}].position")}
        if include_rotation:
            compiled["rotation"] = _vector(point["rotation"], f"{label}[{index}].rotation")
        result.append(compiled)
    if any(left >= right for left, right in zip(times, times[1:])):
        raise DirectorError(f"{label} times must be strictly increasing")
    if times[0] != 0.0 or times[-1] != 1.0:
        raise DirectorError(f"{label} must start at t=0 and end at t=1")
    if len(frames) != len(set(frames)):
        raise DirectorError(f"{label} times map to duplicate frame indices")
    return result


def _position_at(points: list[dict[str, Any]], t: float) -> list[float]:
    if t <= 0.0:
        return list(points[0]["position"])
    if t >= 1.0:
        return list(points[-1]["position"])
    # The semantic points have already been validated and are still in [0, 1].
    # Reconstruct normalized time from their evenly ordered frame positions.
    denominator = points[-1]["frame"] or 1
    frame = t * denominator
    for left, right in zip(points, points[1:]):
        if left["frame"] <= frame <= right["frame"]:
            span = right["frame"] - left["frame"] or 1
            alpha = (frame - left["frame"]) / span
            return [
                left["position"][axis] + alpha * (right["position"][axis] - left["position"][axis])
                for axis in range(3)
            ]
    return list(points[-1]["position"])


def _resample_points(
    points: list[dict[str, Any]],
    frames: list[int],
    *,
    include_rotation: bool,
) -> list[dict[str, Any]]:
    """Linearly resample one semantic track onto the shared frame grid."""
    result: list[dict[str, Any]] = []
    for frame in frames:
        if frame <= points[0]["frame"]:
            left = right = points[0]
            alpha = 0.0
        elif frame >= points[-1]["frame"]:
            left = right = points[-1]
            alpha = 0.0
        else:
            left = right = points[-1]
            alpha = 0.0
            for candidate_left, candidate_right in zip(points, points[1:]):
                if candidate_left["frame"] <= frame <= candidate_right["frame"]:
                    left, right = candidate_left, candidate_right
                    span = right["frame"] - left["frame"] or 1
                    alpha = (frame - left["frame"]) / span
                    break
        item: dict[str, Any] = {
            "frame": frame,
            "position": [
                left["position"][axis] + alpha * (right["position"][axis] - left["position"][axis])
                for axis in range(3)
            ],
        }
        if include_rotation:
            item["rotation"] = [
                left["rotation"][axis] + alpha * (right["rotation"][axis] - left["rotation"][axis])
                for axis in range(3)
            ]
        result.append(item)
    return result


def _look_at_rotation(camera: list[float], target: list[float], roll_deg: float) -> list[float]:
    dx = target[0] - camera[0]
    dy = target[1] - camera[1]
    dz = target[2] - camera[2]
    horizontal = math.hypot(dx, dy)
    if math.hypot(horizontal, dz) <= 1e-9:
        raise DirectorError("camera target is coincident with camera position")
    pitch = math.degrees(math.atan2(dz, horizontal))
    yaw = math.degrees(math.atan2(dy, dx))
    return [-pitch, yaw, roll_deg]


def compile_director_plan(value: object) -> WorldState:
    """Compile normalized semantic times into the canonical shared state."""

    root = _mapping(value, "director plan")
    _exact(
        root,
        {
            "schema_version",
            "scene_plan",
            "physical_state_plan",
            "character_trajectory_plan",
            "object_trajectory_plan",
            "camera_trajectory_plan",
        },
        "director plan",
    )
    if root["schema_version"] != DIRECTOR_SCHEMA_VERSION:
        raise DirectorError(f"schema_version must be {DIRECTOR_SCHEMA_VERSION!r}")

    scene = _mapping(root["scene_plan"], "scene_plan")
    _exact(
        scene,
        {"scene_id", "environment_preset", "duration_seconds", "fps", "entities"},
        "scene_plan",
    )
    duration = _number(scene["duration_seconds"], "scene_plan.duration_seconds")
    fps = scene["fps"]
    if type(fps) is not int or fps <= 0:
        raise DirectorError("scene_plan.fps must be a positive integer")
    if duration <= 0:
        raise DirectorError("scene_plan.duration_seconds must be positive")
    frame_count = int(round(duration * fps))
    if frame_count <= 1:
        raise DirectorError("scene plan must contain at least two frames")

    raw_entities = scene["entities"]
    if not isinstance(raw_entities, list) or not raw_entities:
        raise DirectorError("scene_plan.entities must be a non-empty list")
    entities: list[dict[str, str]] = []
    entity_ids: set[str] = set()
    entity_kinds: dict[str, str] = {}
    for index, raw in enumerate(raw_entities):
        entity = _mapping(raw, f"scene_plan.entities[{index}]")
        _exact(entity, {"id", "kind", "asset"}, f"scene_plan.entities[{index}]")
        entity_id = _text(entity["id"], f"scene_plan.entities[{index}].id")
        if entity_id in entity_ids:
            raise DirectorError(f"duplicate entity id {entity_id!r}")
        kind = entity["kind"]
        if kind not in {"character", "object"}:
            raise DirectorError("entity kind must be character or object")
        entity_ids.add(entity_id)
        entity_kinds[entity_id] = str(kind)
        entities.append({"id": entity_id, "kind": str(kind), "asset": _text(entity["asset"], f"scene_plan.entities[{index}].asset")})

    track_points: dict[str, list[dict[str, Any]]] = {}

    def compile_tracks(raw_group: object, label: str, kind: str) -> list[dict[str, Any]]:
        group = _mapping(raw_group, label)
        _exact(group, {"tracks"}, label)
        raw_tracks = group["tracks"]
        if not isinstance(raw_tracks, list):
            raise DirectorError(f"{label}.tracks must be a list")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_tracks):
            track = _mapping(raw, f"{label}.tracks[{index}]")
            _exact(track, {"target_id", "points"}, f"{label}.tracks[{index}]")
            target_id = _text(track["target_id"], f"{label}.tracks[{index}].target_id")
            if target_id not in entity_ids or entity_kinds[target_id] != kind:
                raise DirectorError(f"unknown target {target_id!r} in {label}")
            if target_id in track_points:
                raise DirectorError(f"duplicate trajectory target {target_id!r}")
            points = _compile_points(track["points"], f"{label}.tracks[{index}].points", frame_count, include_rotation=True)
            track_points[target_id] = points
            result.append({"target_id": target_id, "points": points})
        return result

    character_tracks = compile_tracks(root["character_trajectory_plan"], "character_trajectory_plan", "character")
    object_tracks = compile_tracks(root["object_trajectory_plan"], "object_trajectory_plan", "object")

    physical = _mapping(root["physical_state_plan"], "physical_state_plan")
    unknown_physical = set(physical) - {"events", "parameters"}
    if unknown_physical or "events" not in physical:
        raise DirectorError(
            "physical_state_plan fields invalid: "
            f"missing={sorted({'events'} - set(physical))}, unknown={sorted(unknown_physical)}"
        )
    raw_events = physical["events"]
    if not isinstance(raw_events, list):
        raise DirectorError("physical_state_plan.events must be a list")
    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for index, raw in enumerate(raw_events):
        event = _mapping(raw, f"physical_state_plan.events[{index}]")
        _exact(event, {"id", "t", "type", "participants"}, f"physical_state_plan.events[{index}]")
        event_id = _text(event["id"], f"physical_state_plan.events[{index}].id")
        if event_id in event_ids:
            raise DirectorError(f"duplicate physical event id {event_id!r}")
        participants = event["participants"]
        if not isinstance(participants, list) or any(item not in entity_ids for item in participants):
            raise DirectorError("physical event participants must reference entities")
        event_ids.add(event_id)
        events.append({
            "id": event_id,
            "frame": int(round(_unit_time(event["t"], f"physical_state_plan.events[{index}].t") * (frame_count - 1))),
            "type": _text(event["type"], f"physical_state_plan.events[{index}].type"),
            "participants": list(participants),
        })

    cameras_group = _mapping(root["camera_trajectory_plan"], "camera_trajectory_plan")
    _exact(cameras_group, {"cameras"}, "camera_trajectory_plan")
    raw_cameras = cameras_group["cameras"]
    if not isinstance(raw_cameras, list) or not 1 <= len(raw_cameras) <= 8:
        raise DirectorError("camera_trajectory_plan must contain one to eight cameras")
    camera_sources: list[dict[str, Any]] = []
    camera_ids: set[str] = set()
    for index, raw in enumerate(raw_cameras):
        camera = _mapping(raw, f"camera_trajectory_plan.cameras[{index}]")
        _exact(
            camera,
            {"id", "role", "target", "lens_mm", "roll_deg", "points"},
            f"camera_trajectory_plan.cameras[{index}]",
        )
        camera_id = _text(camera["id"], f"cameras[{index}].id")
        if camera_id in camera_ids:
            raise DirectorError(f"duplicate camera id {camera_id!r}")
        camera_ids.add(camera_id)
        target = _mapping(camera["target"], f"cameras[{index}].target")
        if set(target) == {"object_id"}:
            target_id = _text(target["object_id"], "camera target.object_id")
            if target_id not in entity_ids or target_id not in track_points:
                raise DirectorError(f"camera target {target_id!r} has no trajectory")
            target_value: dict[str, Any] = {"object_id": target_id}
        elif set(target) == {"point"}:
            target_value = {"point": _vector(target["point"], "camera target.point")}
            target_id = None
        else:
            raise DirectorError("camera target must contain object_id or point")
        lens = _number(camera["lens_mm"], f"cameras[{index}].lens_mm")
        if lens <= 0:
            raise DirectorError("camera lens_mm must be positive")
        roll = _number(camera["roll_deg"], f"cameras[{index}].roll_deg")
        positions = _compile_points(camera["points"], f"cameras[{index}].points", frame_count, include_rotation=False)
        camera_sources.append({
            "id": camera_id,
            "role": _text(camera["role"], f"cameras[{index}].role"),
            "target": target_value,
            "target_id": target_id,
            "lens_mm": lens,
            "roll_deg": roll,
            "positions": positions,
        })

    # A Director may place semantic keyframes at different times for each
    # entity/camera. The canonical WorldState intentionally uses one shared
    # frame grid, so compile once on the union and deterministically resample
    # every trajectory rather than rejecting an otherwise valid plan.
    all_sources = list(track_points.values()) + [item["positions"] for item in camera_sources]
    shared_frames = sorted({point["frame"] for source in all_sources for point in source})
    if not shared_frames:
        raise DirectorError("trajectory plans must contain at least one point")
    for target_id, points in list(track_points.items()):
        track_points[target_id] = _resample_points(points, shared_frames, include_rotation=True)
    for track in character_tracks + object_tracks:
        track["points"] = track_points[track["target_id"]]

    cameras: list[dict[str, Any]] = []
    for source in camera_sources:
        positions = _resample_points(source["positions"], shared_frames, include_rotation=False)
        compiled_points: list[dict[str, Any]] = []
        for point in positions:
            t = point["frame"] / (frame_count - 1)
            target_position = (
                source["target"]["point"]
                if source["target_id"] is None
                else _position_at(track_points[source["target_id"]], t)
            )
            compiled_points.append({
                "frame": point["frame"],
                "position": point["position"],
                "rotation": _look_at_rotation(point["position"], target_position, source["roll_deg"]),
            })
        cameras.append({
            "id": source["id"],
            "role": source["role"],
            "target": source["target"],
            "lens_mm": source["lens_mm"],
            "roll_deg": source["roll_deg"],
            "points": compiled_points,
        })

    world_document = {
        "schema_version": "world-state-1.0",
        "scene_plan": {
            "scene_id": _text(scene["scene_id"], "scene_plan.scene_id"),
            "environment_preset": _text(scene["environment_preset"], "scene_plan.environment_preset"),
            "duration_seconds": duration,
            "fps": fps,
            "frame_count": frame_count,
            "entities": entities,
        },
        "physical_state_plan": {
            "events": events,
            "parameters": copy.deepcopy(dict(physical.get("parameters", {}))),
        },
        "character_trajectory_plan": {"tracks": character_tracks},
        "object_trajectory_plan": {"tracks": object_tracks},
        "camera_trajectory_plan": {"cameras": cameras},
    }
    try:
        return WorldState.from_dict(world_document)
    except WorldStateError as exc:
        raise DirectorError(f"compiled world state is invalid: {exc}") from exc


def _normalization_points(
    value: object,
    label: str,
    *,
    include_rotation: bool,
    position_transform: Callable[[list[float]], list[float]] | None = None,
) -> list[dict[str, Any]]:
    """Convert common provider trajectory spellings to the semantic point shape."""
    if not isinstance(value, list):
        raise DirectorError(f"{label} must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        point = _mapping(raw, f"{label}[{index}]")
        if "t" not in point or "position" not in point:
            raise DirectorError(f"{label}[{index}] must contain t and position")
        position = list(_vector(point["position"], f"{label}[{index}].position"))
        if position_transform is not None:
            position = position_transform(position)
        item: dict[str, Any] = {
            "t": _number(point["t"], f"{label}[{index}].t"),
            "position": position,
        }
        if include_rotation:
            item["rotation"] = list(_vector(point.get("rotation", [0, 0, 0]), f"{label}[{index}].rotation"))
        result.append(item)
    return result


def _normalization_tracks(
    value: object,
    label: str,
    *,
    kind: str,
    position_transform: Callable[[list[float]], list[float]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Accept strict tracks or provider maps such as {traveler: {trajectory: [...]}}."""
    group = _mapping(value, label)
    raw_tracks: list[tuple[str | None, object]] = []
    if isinstance(group.get("tracks"), list):
        raw_tracks = [(None, item) for item in group["tracks"]]
    else:
        for key, item in group.items():
            if key in {"description", "notes"}:
                continue
            raw_tracks.append((str(key), item))
    tracks: list[dict[str, Any]] = []
    entities: list[dict[str, str]] = []
    for index, (map_id, raw) in enumerate(raw_tracks):
        item = _mapping(raw, f"{label}[{index}]")
        target_id = item.get("target_id", map_id or item.get("id"))
        target_id = _text(target_id, f"{label}[{index}].target_id")
        points_value = item.get("points", item.get("trajectory"))
        points = _normalization_points(
            points_value,
            f"{label}[{target_id}].trajectory",
            include_rotation=True,
            position_transform=position_transform,
        )
        tracks.append({"target_id": target_id, "points": points})
        entities.append({"id": target_id, "kind": kind, "asset": item.get("asset", "proxy_human" if kind == "character" else "proxy_object")})
    return tracks, entities


def normalize_director_plan(
    value: object,
    *,
    fallback_scene_id: str = "scene_001",
    fallback_environment_preset: str = "default",
    fallback_fps: int | None = None,
) -> dict[str, Any]:
    """Normalize provider-friendly JSON into the strict Director contract.

    The LLM is allowed to use readable maps (``traveler: {trajectory: ...}``) and
    descriptive scene/camera fields. This adapter preserves those semantics while
    keeping the downstream compiler deterministic and schema-strict.
    """
    root = _mapping(value, "director plan")
    if root.get("schema_version") not in {None, DIRECTOR_SCHEMA_VERSION}:
        raise DirectorError(f"schema_version must be {DIRECTOR_SCHEMA_VERSION!r}")
    scene = _mapping(root.get("scene_plan", {}), "scene_plan")
    scene_id = _text(scene.get("scene_id", fallback_scene_id), "scene_plan.scene_id")
    environment = _text(scene.get("environment_preset", fallback_environment_preset), "scene_plan.environment_preset")
    duration = _number(scene.get("duration_seconds"), "scene_plan.duration_seconds")
    fps = scene.get("fps", fallback_fps)
    if type(fps) is not int or fps <= 0:
        raise DirectorError("scene_plan.fps must be a positive integer")

    coordinate_convention = scene.get("coordinate_convention")
    if coordinate_convention is None and isinstance(scene.get("world"), Mapping):
        coordinate_convention = scene["world"].get("coordinate_convention")
    provider_y_up = isinstance(coordinate_convention, Mapping) and (
        str(coordinate_convention.get("vertical_axis", "")).lower() == "y"
        or str(coordinate_convention.get("ground_plane", "")).lower().startswith("y=")
        or str(coordinate_convention.get("y", "")).lower() == "vertical"
    )

    def position_transform(position: list[float]) -> list[float]:
        # pipeline_v2 is canonical Z-up (ground z=0). A Director provider may
        # explicitly describe a Y-up scene; convert positions once at this
        # boundary and record the decision in physical_state_plan.parameters.
        return [position[0], position[2], position[1]] if provider_y_up else position

    character_tracks, character_entities = _normalization_tracks(
        root.get("character_trajectory_plan", {}), "character_trajectory_plan", kind="character", position_transform=position_transform
    )
    object_tracks, object_entities = _normalization_tracks(
        root.get("object_trajectory_plan", {}), "object_trajectory_plan", kind="object", position_transform=position_transform
    )
    by_id = {item["id"]: item for item in character_entities + object_entities}
    explicit_entities = scene.get("entities")
    if isinstance(explicit_entities, list):
        for raw in explicit_entities:
            item = _mapping(raw, "scene_plan.entities")
            entity_id = _text(item.get("id"), "scene_plan.entities.id")
            by_id[entity_id] = {
                "id": entity_id,
                "kind": item.get("kind", by_id.get(entity_id, {}).get("kind", "object")),
                "asset": item.get("asset", by_id.get(entity_id, {}).get("asset", "proxy_object")),
            }
    if not by_id:
        raise DirectorError("Director plan contains no character or object trajectories")

    physical_raw = _mapping(root.get("physical_state_plan", {}), "physical_state_plan")
    parameters = dict(physical_raw.get("parameters", {})) if isinstance(physical_raw.get("parameters", {}), Mapping) else {}
    for key, item in physical_raw.items():
        if key not in {"events", "parameters"}:
            parameters[key] = copy.deepcopy(item)
    events: list[dict[str, Any]] = []
    raw_events = physical_raw.get("events", [])
    if not isinstance(raw_events, list):
        raw_events = []
    provider_event_records: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_events):
        item = _mapping(raw, f"physical_state_plan.events[{index}]")
        raw_text = item.get("event", item.get("description", ""))
        participants = item.get("participants")
        if not isinstance(participants, list):
            text_value = str(raw_text).lower()
            participants = [entity_id for entity_id in by_id if entity_id.lower() in text_value]
        event_time = item.get("t", item.get("t_start", 0.0))
        event_type = item.get("type", "action")
        events.append({
            "id": item.get("id", f"event_{index + 1:03d}"),
            "t": event_time,
            "type": event_type,
            "participants": participants,
        })
        if "t_start" in item or "t_end" in item or raw_text:
            provider_event_records.append(copy.deepcopy(dict(item)))
    if provider_event_records:
        parameters["provider_events"] = provider_event_records
    if provider_y_up:
        parameters["coordinate_transform"] = "provider_y_up_to_pipeline_z_up"

    camera_raw = _mapping(root.get("camera_trajectory_plan", {}), "camera_trajectory_plan")
    if isinstance(camera_raw.get("cameras"), list):
        raw_cameras = [(None, item) for item in camera_raw["cameras"]]
    else:
        raw_cameras = [(str(key), item) for key, item in camera_raw.items() if key not in {"description", "notes"}]
    cameras: list[dict[str, Any]] = []
    for index, (map_id, raw) in enumerate(raw_cameras):
        item = _mapping(raw, f"camera_trajectory_plan[{index}]")
        camera_id = _text(item.get("id", map_id or f"camera_{index + 1}"), f"camera[{index}].id")
        target_spec = item.get("target")
        if isinstance(target_spec, Mapping):
            target = dict(target_spec)
        elif isinstance(target_spec, str) and target_spec.strip() in by_id:
            # Providers often emit the readable shorthand ``"target": "traveler"``.
            # Preserve it as an entity target instead of silently degrading to a
            # fixed origin point; the compiled camera orientation must follow
            # the shared-world trajectory of that entity.
            target = {"object_id": target_spec.strip()}
        elif target_spec in {"character", "object", "entity"}:
            target_id = item.get("target_character", item.get("target_object", item.get("target_entity")))
            target = {"object_id": _text(target_id, f"camera[{index}].target")}
        elif "target_entity" in item or "target_character" in item or "target_object" in item:
            target_id = item.get("target_entity", item.get("target_character", item.get("target_object")))
            target = {"object_id": _text(target_id, f"camera[{index}].target")}
        elif target_spec in {"fixed_point", "point", "world"} or "target_point" in item:
            target_point = list(_vector(item.get("target_point", [0, 0, 0]), f"camera[{index}].target_point"))
            target = {"point": position_transform(target_point)}
        else:
            target = {"point": [0.0, 0.0, 0.0]}
        points = _normalization_points(
            item.get("points", item.get("trajectory")),
            f"camera[{camera_id}].trajectory",
            include_rotation=False,
            position_transform=position_transform,
        )
        cameras.append({
            "id": camera_id,
            "role": item.get("role", item.get("type", f"camera_{index + 1}")),
            "target": target,
            "lens_mm": item.get("lens_mm", item.get("focal_length", 50)),
            "roll_deg": item.get("roll_deg", item.get("roll", 0)),
            "points": points,
        })
    if not cameras:
        raise DirectorError("camera_trajectory_plan must contain at least one camera")
    return {
        "schema_version": DIRECTOR_SCHEMA_VERSION,
        "scene_plan": {
            "scene_id": scene_id,
            "environment_preset": environment,
            "duration_seconds": duration,
            "fps": fps,
            "entities": list(by_id.values()),
        },
        "physical_state_plan": {"events": events, "parameters": parameters},
        "character_trajectory_plan": {"tracks": character_tracks},
        "object_trajectory_plan": {"tracks": object_tracks},
        "camera_trajectory_plan": {"cameras": cameras},
    }


def build_request_payload(
    story_prompt: str,
    *,
    duration_seconds: float,
    fps: int,
    camera_count: int = 3,
    model: str = DEFAULT_MODEL,
    provider: str = "deepseek",
) -> dict[str, Any]:
    if not isinstance(story_prompt, str) or not story_prompt.strip():
        raise DirectorError("story_prompt must be non-empty text")
    if camera_count < 1 or camera_count > 8:
        raise DirectorError("camera_count must be between 1 and 8")
    system = (
        "You are the Agent Director for a controllable video pipeline. Return exactly one JSON object "
        "with schema_version director-plan-1.0. You describe scene semantics, physical events, "
        "character/object trajectories and camera responsibilities. Use normalized time t in [0,1]. "
        "Do not return Markdown, executable code, colors, or appearance instructions. Every camera "
        "must target an entity with a trajectory or a fixed 3D point. All trajectory point lists "
        "must start at t=0 and end at t=1. The deterministic compiler will assign frame indices "
        "and compute camera orientation."
    )
    user = {
        "story_prompt": story_prompt.strip(),
        "duration_seconds": float(duration_seconds),
        "fps": fps,
        "camera_count": camera_count,
        "required_top_level_fields": [
            "schema_version",
            "scene_plan",
            "physical_state_plan",
            "character_trajectory_plan",
            "object_trajectory_plan",
            "camera_trajectory_plan",
        ],
        "trajectory_point_shape": "{t, position:[x,y,z], rotation:[rx,ry,rz]}",
        "camera_point_shape": "{t, position:[x,y,z]}",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 8192,
        "stream": False,
    }
    if provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    return payload


def _endpoint(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DirectorError("provider base URL is invalid")
    path = parsed.path.rstrip("/")
    redacted = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/chat/completions", "", "")), redacted


def request_director_plan(
    *,
    story_prompt: str,
    duration_seconds: float,
    output_dir: Path | str,
    fps: int = 24,
    camera_count: int = 3,
    provider: str = "deepseek",
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> WorldState:
    """Make exactly one real Director request and persist redacted evidence."""

    output = Path(output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    started_at = _now()
    started = time.monotonic()
    environment = os.environ if environ is None else environ
    provider = provider.strip().lower()
    provider_env = {
        "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", DEFAULT_MODEL),
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", OPENAI_DEFAULT_MODEL),
    }
    if provider not in provider_env:
        raise DirectorError(f"unsupported Director provider: {provider!r}")
    key_name, base_name, model_name, default_model = provider_env[provider]
    api_key = environment.get(key_name, "").strip()
    raw_base = environment.get(base_name, "").strip()
    requested_model = model if model is not None else environment.get(model_name, default_model)
    model = requested_model.strip() or default_model
    api_calls = 0
    try:
        if not api_key or not raw_base:
            raise DirectorError(f"{provider} environment variables are unavailable")
        endpoint, redacted_base = _endpoint(raw_base)
        payload = build_request_payload(
            story_prompt,
            duration_seconds=duration_seconds,
            fps=fps,
            camera_count=camera_count,
            model=model,
            provider=provider,
        )
        _write_json(output / "request.json", {"endpoint": endpoint, "payload": payload, "authorization_saved": False})
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        api_calls = 1
        with transport(request, timeout=90) as response_stream:
            raw_response = response_stream.read()
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _write_bytes(output / "response.raw", raw_response.replace(api_key.encode(), b"[REDACTED]"))
            raise DirectorError(f"{provider} response is not JSON: {exc}") from exc
        _write_json(output / "response.json", _redact(response, api_key))
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise DirectorError(f"{provider} response choices are invalid")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise DirectorError(f"{provider} response did not finish normally: {choice.get('finish_reason')}")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise DirectorError(f"{provider} Director content is empty")
        try:
            director_document, repair_meta = parse_json_response(content, label=f"{provider} Director")
        except JSONRepairError as exc:
            raise DirectorError(str(exc)) from exc
        _write_json(output / "json_repair.json", repair_meta)
        _write_json(output / "director_plan.json", _redact(director_document, api_key))
        normalized_document = normalize_director_plan(
            director_document,
            fallback_scene_id="station_001",
            fallback_environment_preset="station",
            fallback_fps=fps,
        )
        _write_json(output / "director_plan_normalized.json", _redact(normalized_document, api_key))
        world = compile_director_plan(normalized_document)
        _write_json(output / "world_state.json", world.to_dict())
        _write_json(output / "evidence.json", {
            "schema_version": "1.0",
            "status": "succeeded",
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "api_call_count": api_calls,
            "retry_count": 0,
            "base_url": redacted_base,
            "provider": provider,
            "model": response.get("model", model),
            "response_id": response.get("id"),
            "usage": response.get("usage"),
            "json_repair": repair_meta,
            "scene_id": world.scene_id,
            "world_state_hash": world.world_state_hash(),
            "error": None,
        })
        return world
    except Exception as exc:
        try:
            _, redacted_base = _endpoint(raw_base)
        except DirectorError:
            redacted_base = None
        _write_json(output / "evidence.json", {
            "schema_version": "1.0",
            "status": "failed",
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "api_call_count": api_calls,
            "retry_count": 0,
            "base_url": redacted_base,
            "provider": provider,
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
        })
        if isinstance(exc, DirectorError):
            raise
        raise DirectorError(f"Director request failed: {exc}") from exc
