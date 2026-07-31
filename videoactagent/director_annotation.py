"""Strict human director annotations for actor, camera, and prompt control."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any

from videoactagent.trajectory import (
    TrajectoryInstruction,
    TrajectoryPoint,
    TrajectoryTarget,
    TrajectoryTrack,
)
from videoactagent.trajectory_prompt import (
    PROMPT_COMPILER_VERSION,
    TrajectoryPromptError,
    compile_trajectory_prompt,
)
from videoactagent.restyle_prompt import (
    RESTYLE_COMPILER_VERSION,
    RestylePromptError,
    compile_restyle_prompt,
    load_restyle_profile,
)


SCHEMA_VERSION = "1.0"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ITERATION = re.compile(r"^D(?:0|[1-9][0-9]*)$")
_SHOT_SIZES = frozenset({"extreme_wide", "wide", "medium", "close", "extreme_close"})
_INTERPOLATIONS = frozenset({"linear", "bezier", "constant"})
_PAYLOAD_FIELDS = frozenset({
    "schema_version", "author_id", "iteration_id", "parent_iteration_id",
    "auto_filled_values", "frozen_through_keyframe", "inheritance_sha256",
    "inherited_locked_values", "keyframes", "visual_style", "mood",
})
_INHERITED_KEYFRAME_FIELDS = frozenset({"id", "t", "actors", "camera"})
_KEYFRAME_FIELDS = _INHERITED_KEYFRAME_FIELDS | {"camera_source"}
_CAMERA_FIELDS = frozenset({
    "position", "look_at", "focal_length_mm", "shot_size", "interpolation",
    "roll_degrees",
})


class DirectorAnnotationError(ValueError):
    """Raised when human director input is incomplete or untrustworthy."""


@dataclass(frozen=True)
class CameraKeyframe:
    keyframe_id: str
    t: float
    position: tuple[float, float, float]
    look_at: tuple[float, float, float]
    focal_length_mm: float
    shot_size: str
    interpolation: str
    roll_degrees: float

    def to_dict(self) -> dict[str, object]:
        return {
            "keyframe_id": self.keyframe_id,
            "t": self.t,
            "position": list(self.position),
            "look_at": list(self.look_at),
            "focal_length_mm": self.focal_length_mm,
            "shot_size": self.shot_size,
            "interpolation": self.interpolation,
            "roll_degrees": self.roll_degrees,
        }


@dataclass(frozen=True)
class CompiledDirectorAnnotation:
    actor_trajectory: TrajectoryInstruction
    camera_trajectory: tuple[CameraKeyframe, ...]
    trajectory_prompt: str
    restyle_prompt: str
    canonical_annotation: bytes
    camera_document: bytes

    @property
    def compiled_prompt(self) -> str:
        """Compatibility alias for trajectory-only VACE consumers."""
        return self.trajectory_prompt


def _exact(value: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        missing = sorted(fields - set(value))
        unknown = sorted(set(value) - fields)
        raise DirectorAnnotationError(
            f"{label} fields are invalid: missing={missing}, unknown={unknown}"
        )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise DirectorAnnotationError(f"{label} must be a safe non-empty identifier")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DirectorAnnotationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise DirectorAnnotationError(f"{label} must be a finite number")
    return 0.0 if result == 0.0 else result


def _unit(value: object, label: str) -> float:
    result = _number(value, label)
    if not 0.0 <= result <= 1.0:
        raise DirectorAnnotationError(f"{label} must be in [0, 1]")
    return result


def _vector3(value: object, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise DirectorAnnotationError(f"{label} must contain exactly three finite numbers")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _canonical(value: object) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DirectorAnnotationError(f"annotation is not canonical JSON: {exc}") from exc


def _normalize_frame(
    raw: object, schedule: Mapping[str, object], actors: Sequence[str], label: str,
    *, require_camera_source: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DirectorAnnotationError(f"{label} must be an object")
    _exact(
        raw,
        _KEYFRAME_FIELDS if require_camera_source else _INHERITED_KEYFRAME_FIELDS,
        label,
    )
    time = _unit(raw.get("t"), f"{label}.t")
    if raw.get("id") != schedule["id"] or time != schedule["t"]:
        raise DirectorAnnotationError("annotation keyframe schedule differs from contract")
    actors_value = raw.get("actors")
    if not isinstance(actors_value, Mapping) or set(actors_value) != set(actors):
        raise DirectorAnnotationError(f"{label}.actors must contain every actor exactly once")
    normalized_actors: dict[str, dict[str, float]] = {}
    for actor in actors:
        point = actors_value[actor]
        if not isinstance(point, Mapping) or set(point) != {"x", "y"}:
            raise DirectorAnnotationError(f"{label}.{actor} point fields are invalid")
        normalized_actors[actor] = {
            "x": _unit(point.get("x"), f"{label}.{actor}.x"),
            "y": _unit(point.get("y"), f"{label}.{actor}.y"),
        }
    camera_value = raw.get("camera")
    if not isinstance(camera_value, Mapping):
        raise DirectorAnnotationError(f"{label}.camera is required")
    _exact(camera_value, _CAMERA_FIELDS, f"{label}.camera")
    position = _vector3(camera_value.get("position"), f"{label}.camera.position")
    look_at = _vector3(camera_value.get("look_at"), f"{label}.camera.look_at")
    if position == look_at:
        raise DirectorAnnotationError(f"{label}.camera.look_at must differ from position")
    focal = _number(camera_value.get("focal_length_mm"), f"{label}.camera.focal_length_mm")
    if not 1.0 <= focal <= 300.0:
        raise DirectorAnnotationError(f"{label}.camera.focal_length_mm must be in [1, 300]")
    shot_size = camera_value.get("shot_size")
    if shot_size not in _SHOT_SIZES:
        raise DirectorAnnotationError(f"{label}.camera.shot_size is invalid")
    interpolation = camera_value.get("interpolation")
    if interpolation not in _INTERPOLATIONS:
        raise DirectorAnnotationError(f"{label}.camera.interpolation is invalid")
    roll = _number(camera_value.get("roll_degrees"), f"{label}.camera.roll_degrees")
    if not -180.0 <= roll <= 180.0:
        raise DirectorAnnotationError(f"{label}.camera.roll_degrees must be in [-180, 180]")
    return {
        "id": schedule["id"], "t": time, "actors": normalized_actors,
        "camera": {
            "position": list(position), "look_at": list(look_at),
            "focal_length_mm": focal, "shot_size": shot_size,
            "interpolation": interpolation, "roll_degrees": roll,
        },
    }


def _contract(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "story_id", "shot_id", "duration_seconds", "sample_count", "actors", "keyframes",
        "inheritance_sha256", "inherited_keyframes", "story_prompt",
        "appearance_instruction",
        "restyle_profile",
    }
    if set(value) != required:
        raise DirectorAnnotationError("director contract fields are invalid")
    story_id = _identifier(value.get("story_id"), "contract.story_id")
    shot_id = _identifier(value.get("shot_id"), "contract.shot_id")
    duration = _number(value.get("duration_seconds"), "contract.duration_seconds")
    if duration <= 0:
        raise DirectorAnnotationError("contract.duration_seconds must be positive")
    sample_count = value.get("sample_count")
    if type(sample_count) is not int or sample_count < 2:
        raise DirectorAnnotationError("contract.sample_count must be an integer >= 2")
    actors_value = value.get("actors")
    if not isinstance(actors_value, list) or not actors_value:
        raise DirectorAnnotationError("contract.actors must be a non-empty list")
    actors = [_identifier(actor, "contract actor") for actor in actors_value]
    if len(set(actors)) != len(actors):
        raise DirectorAnnotationError("contract actor IDs must be unique")
    frames_value = value.get("keyframes")
    if not isinstance(frames_value, list) or len(frames_value) != 5:
        raise DirectorAnnotationError("contract must contain exactly K0--K4")
    frames = []
    for index, item in enumerate(frames_value):
        if not isinstance(item, Mapping) or set(item) != {"id", "t"}:
            raise DirectorAnnotationError("contract keyframe fields are invalid")
        key = item.get("id")
        time = _unit(item.get("t"), f"contract K{index}.t")
        if key != f"K{index}":
            raise DirectorAnnotationError("contract keyframe schedule is invalid")
        frames.append({"id": key, "t": time})
    times = [frame["t"] for frame in frames]
    if times != sorted(times) or len(set(times)) != len(times) or times[0] != 0.0 or times[-1] != 1.0:
        raise DirectorAnnotationError("contract keyframe schedule is invalid")
    inheritance_sha256 = value.get("inheritance_sha256")
    if not isinstance(inheritance_sha256, str) or not _SHA256.fullmatch(inheritance_sha256):
        raise DirectorAnnotationError("contract inheritance_sha256 is invalid")
    inherited_value = value.get("inherited_keyframes")
    if not isinstance(inherited_value, list) or len(inherited_value) != 5:
        raise DirectorAnnotationError("contract inherited_keyframes must contain K0--K4")
    inherited = [
        _normalize_frame(raw, schedule, actors, f"contract inherited K{index}")
        for index, (raw, schedule) in enumerate(zip(inherited_value, frames))
    ]
    story_prompt = value.get("story_prompt")
    appearance = value.get("appearance_instruction")
    if not isinstance(story_prompt, str) or not story_prompt.strip():
        raise DirectorAnnotationError("contract.story_prompt must be non-empty")
    if not isinstance(appearance, str) or not appearance.strip():
        raise DirectorAnnotationError("contract.appearance_instruction must be non-empty")
    profile_value = value.get("restyle_profile")
    if not isinstance(profile_value, Mapping):
        raise DirectorAnnotationError("contract.restyle_profile must be an object")
    try:
        profile = load_restyle_profile(profile_value)
    except RestylePromptError as exc:
        raise DirectorAnnotationError(f"contract.restyle_profile is invalid: {exc}") from exc
    if profile.scene_id != story_id:
        raise DirectorAnnotationError("contract.restyle_profile scene_id differs from story_id")
    if {subject.actor_id for subject in profile.subjects} != set(actors):
        raise DirectorAnnotationError("contract.restyle_profile subjects differ from actors")
    return {
        "story_id": story_id, "shot_id": shot_id, "duration_seconds": duration,
        "sample_count": sample_count, "actors": actors, "keyframes": frames,
        "inheritance_sha256": inheritance_sha256, "inherited_keyframes": inherited,
        "story_prompt": story_prompt.strip(), "appearance_instruction": appearance.strip(),
        "restyle_profile": profile,
    }


def compile_director_annotation(
    payload: Mapping[str, Any], contract: Mapping[str, Any]
) -> CompiledDirectorAnnotation:
    if not isinstance(payload, Mapping):
        raise DirectorAnnotationError("annotation must be one object")
    payload_fields = set(payload)
    compiler_fields = payload_fields - set(_PAYLOAD_FIELDS)
    if compiler_fields:
        if compiler_fields not in (
            {"prompt_compiler_version"},
            {"prompt_compiler_version", "restyle_compiler_version"},
        ):
            _exact(payload, _PAYLOAD_FIELDS, "annotation")
        if payload.get("prompt_compiler_version") != PROMPT_COMPILER_VERSION:
            raise DirectorAnnotationError("prompt_compiler_version is invalid")
        if (
            "restyle_compiler_version" in compiler_fields
            and payload.get("restyle_compiler_version") != RESTYLE_COMPILER_VERSION
        ):
            raise DirectorAnnotationError("restyle_compiler_version is invalid")
    else:
        _exact(payload, _PAYLOAD_FIELDS, "annotation")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DirectorAnnotationError(f"schema_version must be {SCHEMA_VERSION}")
    author = payload.get("author_id")
    if not isinstance(author, str) or not author.strip():
        raise DirectorAnnotationError("author_id is required")
    iteration = payload.get("iteration_id")
    parent = payload.get("parent_iteration_id")
    if not isinstance(iteration, str) or not _ITERATION.fullmatch(iteration):
        raise DirectorAnnotationError("iteration_id is invalid")
    if not isinstance(parent, str) or not _ITERATION.fullmatch(parent):
        raise DirectorAnnotationError("parent_iteration_id is invalid")
    if int(iteration[1:]) != int(parent[1:]) + 1:
        raise DirectorAnnotationError("iteration IDs are not consecutive")
    if payload.get("auto_filled_values") != 0:
        raise DirectorAnnotationError("auto_filled_values must be exactly 0")
    visual_style = payload.get("visual_style")
    mood = payload.get("mood")
    if visual_style not in {"source_default", "cinematic_realism", "documentary"}:
        raise DirectorAnnotationError("visual_style is invalid")
    if mood not in {"source_default", "warm", "neutral", "tense"}:
        raise DirectorAnnotationError("mood is invalid")

    expected = _contract(contract)
    boundary = payload.get("frozen_through_keyframe")
    if boundary not in {"K0", "K1", "K2", "K3"}:
        raise DirectorAnnotationError("frozen boundary must leave an editable suffix (K0--K3)")
    boundary_index = int(str(boundary)[1:])
    if payload.get("inheritance_sha256") != expected["inheritance_sha256"]:
        raise DirectorAnnotationError("inheritance_sha256 differs from director contract")
    inherited_value = payload.get("inherited_locked_values")
    if not isinstance(inherited_value, list) or len(inherited_value) != boundary_index + 1:
        raise DirectorAnnotationError("inherited_locked_values must end at the frozen boundary")
    submitted_inherited = [
        _normalize_frame(raw, expected["keyframes"][index], expected["actors"], f"inherited K{index}")
        for index, raw in enumerate(inherited_value)
    ]
    if submitted_inherited != expected["inherited_keyframes"][:boundary_index + 1]:
        raise DirectorAnnotationError("locked inherited values differ from their source")
    frames_value = payload.get("keyframes")
    if not isinstance(frames_value, list) or len(frames_value) != 5:
        raise DirectorAnnotationError("annotation must contain exactly K0--K4")

    actor_points: dict[str, list[TrajectoryPoint]] = {actor: [] for actor in expected["actors"]}
    cameras: list[CameraKeyframe] = []
    normalized_frames = []
    for index, (raw, schedule) in enumerate(zip(frames_value, expected["keyframes"])):
        normalized = _normalize_frame(
            raw, schedule, expected["actors"], f"K{index}", require_camera_source=True
        )
        if index <= boundary_index and normalized != expected["inherited_keyframes"][index]:
            raise DirectorAnnotationError("locked inherited keyframes were modified")
        source = raw.get("camera_source") if isinstance(raw, Mapping) else None
        is_inherited = normalized["camera"] == expected["inherited_keyframes"][index]["camera"]
        required_source = "inherited" if is_inherited else "human_modified"
        if source != required_source:
            raise DirectorAnnotationError(
                f"K{index}.camera_source must be {required_source} for the submitted camera"
            )
        normalized["camera_source"] = source
        time = normalized["t"]
        for actor in expected["actors"]:
            point = normalized["actors"][actor]
            actor_points[actor].append(TrajectoryPoint(
                t=time, x=point["x"], y=point["y"], visible=True
            ))
        camera_value = normalized["camera"]
        position = tuple(camera_value["position"])
        look_at = tuple(camera_value["look_at"])
        focal = camera_value["focal_length_mm"]
        shot_size = camera_value["shot_size"]
        interpolation = camera_value["interpolation"]
        roll = camera_value["roll_degrees"]
        camera = CameraKeyframe(
            keyframe_id=schedule["id"], t=time, position=position, look_at=look_at,
            focal_length_mm=focal, shot_size=str(shot_size),
            interpolation=str(interpolation), roll_degrees=roll,
        )
        cameras.append(camera)

        normalized_frames.append(normalized)

    locked_frames = [
        {key: value for key, value in frame.items() if key != "camera_source"}
        for frame in normalized_frames[:boundary_index + 1]
    ]
    if locked_frames != expected["inherited_keyframes"][:boundary_index + 1]:
        raise DirectorAnnotationError("locked inherited keyframes were modified")

    tracks = tuple(
        TrajectoryTrack(
            track_id=f"human_{actor}", target=TrajectoryTarget("actor", actor),
            primitive="polyline", semantic="move", points=tuple(actor_points[actor]),
        )
        for actor in expected["actors"]
    )
    actor_trajectory = TrajectoryInstruction(
        scene_id=expected["story_id"], shot_id=expected["shot_id"],
        duration_seconds=expected["duration_seconds"],
        sample_count=expected["sample_count"], tracks=tracks,
    )
    normalized = {
        "schema_version": SCHEMA_VERSION, "author_id": author.strip(),
        "iteration_id": iteration, "parent_iteration_id": parent,
        "auto_filled_values": 0, "frozen_through_keyframe": boundary,
        "inheritance_sha256": expected["inheritance_sha256"],
        "inherited_locked_values": submitted_inherited, "keyframes": normalized_frames,
        "visual_style": visual_style, "mood": mood,
        "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "restyle_compiler_version": RESTYLE_COMPILER_VERSION,
    }
    camera_doc = {
        "schema_version": SCHEMA_VERSION, "scene_id": expected["story_id"],
        "shot_id": expected["shot_id"],
        "duration_seconds": expected["duration_seconds"],
        "states": [camera.to_dict() for camera in cameras],
    }
    try:
        trajectory_prompt = compile_trajectory_prompt(
            story_prompt=expected["story_prompt"],
            appearance_instruction=expected["appearance_instruction"],
            duration_seconds=expected["duration_seconds"],
            keyframes=normalized_frames,
            visual_style=visual_style,
            mood=mood,
        )
    except TrajectoryPromptError as exc:
        raise DirectorAnnotationError(f"cannot compile trajectory prompt: {exc}") from exc
    try:
        restyle_prompt = compile_restyle_prompt(
            profile=expected["restyle_profile"],
            trajectory_prompt=trajectory_prompt,
            duration_seconds=expected["duration_seconds"],
        )
    except RestylePromptError as exc:
        raise DirectorAnnotationError(f"cannot compile restyle prompt: {exc}") from exc
    return CompiledDirectorAnnotation(
        actor_trajectory=actor_trajectory,
        camera_trajectory=tuple(cameras),
        trajectory_prompt=trajectory_prompt,
        restyle_prompt=restyle_prompt,
        canonical_annotation=_canonical(normalized),
        camera_document=_canonical(camera_doc),
    )


def camera_trajectory_from_path(
    path: Path | str, *, scene_id: str | None = None, shot_id: str | None = None,
    duration_seconds: float | None = None,
) -> tuple[CameraKeyframe, ...]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DirectorAnnotationError(f"cannot read camera trajectory: {exc}") from exc
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "scene_id", "shot_id", "duration_seconds", "states"
    }:
        raise DirectorAnnotationError("camera trajectory fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise DirectorAnnotationError("camera trajectory schema_version is invalid")
    document_scene = _identifier(value.get("scene_id"), "camera trajectory scene_id")
    document_shot = _identifier(value.get("shot_id"), "camera trajectory shot_id")
    document_duration = _number(
        value.get("duration_seconds"), "camera trajectory duration_seconds"
    )
    if document_duration <= 0:
        raise DirectorAnnotationError("camera trajectory duration_seconds must be positive")
    if scene_id is not None and document_scene != scene_id:
        raise DirectorAnnotationError("camera trajectory scene_id differs from render input")
    if shot_id is not None and document_shot != shot_id:
        raise DirectorAnnotationError("camera trajectory shot_id differs from render input")
    if duration_seconds is not None and not math.isclose(
        document_duration, duration_seconds, rel_tol=0.0, abs_tol=1e-9
    ):
        raise DirectorAnnotationError("camera trajectory duration differs from render input")
    raw_states = value.get("states")
    if not isinstance(raw_states, list) or len(raw_states) != 5:
        raise DirectorAnnotationError("camera trajectory must contain exactly K0--K4")
    states = []
    for index, raw in enumerate(raw_states):
        if not isinstance(raw, Mapping) or set(raw) != _CAMERA_FIELDS | {"keyframe_id", "t"}:
            raise DirectorAnnotationError(f"camera K{index} fields are invalid")
        key = raw.get("keyframe_id")
        time = _unit(raw.get("t"), f"camera K{index}.t")
        if key != f"K{index}":
            raise DirectorAnnotationError("camera keyframe schedule is invalid")
        position = _vector3(raw.get("position"), f"camera K{index}.position")
        look_at = _vector3(raw.get("look_at"), f"camera K{index}.look_at")
        if position == look_at:
            raise DirectorAnnotationError(f"camera K{index}.look_at must differ from position")
        focal = _number(raw.get("focal_length_mm"), f"camera K{index}.focal_length_mm")
        if not 1.0 <= focal <= 300.0:
            raise DirectorAnnotationError(f"camera K{index}.focal_length_mm is invalid")
        shot_size = raw.get("shot_size")
        interpolation = raw.get("interpolation")
        if shot_size not in _SHOT_SIZES or interpolation not in _INTERPOLATIONS:
            raise DirectorAnnotationError(f"camera K{index} style/interpolation is invalid")
        roll = _number(raw.get("roll_degrees"), f"camera K{index}.roll_degrees")
        if not -180.0 <= roll <= 180.0:
            raise DirectorAnnotationError(f"camera K{index}.roll_degrees is invalid")
        states.append(CameraKeyframe(
            keyframe_id=str(key), t=time, position=position, look_at=look_at,
            focal_length_mm=focal, shot_size=str(shot_size),
            interpolation=str(interpolation), roll_degrees=roll,
        ))
    times = [state.t for state in states]
    if times != sorted(times) or len(set(times)) != len(times) or times[0] != 0.0 or times[-1] != 1.0:
        raise DirectorAnnotationError("camera keyframe schedule is invalid")
    return tuple(states)
