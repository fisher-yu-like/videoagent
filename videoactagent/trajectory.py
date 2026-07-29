"""Canonical trajectory instructions for local authoring and API compilation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "0.1"
COORDINATE_SPACE = "normalized_0_1_top_left"
TARGET_TYPES = frozenset({"camera", "actor", "anchor", "local_deformation"})
PRIMITIVES = frozenset({"polyline", "circle", "static"})
CAMERA_SEMANTICS = frozenset(
    {
        "pan_left",
        "pan_right",
        "truck_left",
        "truck_right",
        "dolly_in",
        "dolly_out",
        "orbit_clockwise",
        "orbit_counterclockwise",
        "zoom_in",
        "zoom_out",
    }
)
_ORBIT_SEMANTICS = frozenset({"orbit_clockwise", "orbit_counterclockwise"})
_CAMERA_LINEAR_SEMANTICS = CAMERA_SEMANTICS - _ORBIT_SEMANTICS
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_INSTRUCTION_KEYS = frozenset(
    {
        "schema_version",
        "scene_id",
        "shot_id",
        "coordinate_space",
        "duration_seconds",
        "sample_count",
        "tracks",
    }
)
_TRACK_KEYS = frozenset({"track_id", "target", "primitive", "semantic", "points"})
_TARGET_KEYS = frozenset({"type", "id"})
_POINT_KEYS = frozenset({"t", "x", "y", "visible"})


def _require_exact_keys(value: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a non-empty safe identifier")
    return value


def _require_unit_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number in [0, 1]") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    if result == 0.0:
        return 0.0
    return result


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


@dataclass(frozen=True)
class TrajectoryPoint:
    """One normalized preview-space position at normalized shot time ``t``."""

    t: float
    x: float
    y: float
    visible: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", _require_unit_float(self.t, "point.t"))
        object.__setattr__(self, "x", _require_unit_float(self.x, "point.x"))
        object.__setattr__(self, "y", _require_unit_float(self.y, "point.y"))
        if type(self.visible) is not bool:
            raise ValueError("point.visible must be a JSON boolean")

    @classmethod
    def from_dict(cls, value: object) -> TrajectoryPoint:
        if not isinstance(value, Mapping):
            raise ValueError("point must be an object")
        _require_exact_keys(value, _POINT_KEYS, "point")
        return cls(
            t=value["t"],  # type: ignore[arg-type]
            x=value["x"],  # type: ignore[arg-type]
            y=value["y"],  # type: ignore[arg-type]
            visible=value["visible"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {"t": self.t, "x": self.x, "y": self.y, "visible": self.visible}


@dataclass(frozen=True)
class TrajectoryTarget:
    """The controllable entity addressed by a trajectory track."""

    target_type: str
    target_id: str

    def __post_init__(self) -> None:
        if self.target_type not in TARGET_TYPES:
            raise ValueError(f"target_type must be one of {sorted(TARGET_TYPES)}")
        object.__setattr__(self, "target_id", _require_id(self.target_id, "target_id"))

    @classmethod
    def from_dict(cls, value: object) -> TrajectoryTarget:
        if not isinstance(value, Mapping):
            raise ValueError("target must be an object")
        _require_exact_keys(value, _TARGET_KEYS, "target")
        target_type = value["type"]
        target_id = value["id"]
        if not isinstance(target_type, str) or not isinstance(target_id, str):
            raise ValueError("target_type and target_id must be strings")
        return cls(target_type=target_type, target_id=target_id)

    def to_dict(self) -> dict[str, str]:
        return {"type": self.target_type, "id": self.target_id}


@dataclass(frozen=True)
class TrajectoryTrack:
    """A semantically labelled path for one target."""

    track_id: str
    target: TrajectoryTarget
    primitive: str
    semantic: str
    points: tuple[TrajectoryPoint, ...]

    @property
    def target_type(self) -> str:
        """Expose the target kind directly for compilers and validators."""

        return self.target.target_type

    @property
    def target_id(self) -> str:
        """Expose the addressed entity directly for downstream dispatch."""

        return self.target.target_id

    def __post_init__(self) -> None:
        object.__setattr__(self, "track_id", _require_id(self.track_id, "track_id"))
        if not isinstance(self.target, TrajectoryTarget):
            raise ValueError("target must be a TrajectoryTarget")
        if self.primitive not in PRIMITIVES:
            raise ValueError(f"primitive must be one of {sorted(PRIMITIVES)}")
        if not isinstance(self.semantic, str) or not self.semantic:
            raise ValueError("semantic must be a non-empty string")
        if not isinstance(self.points, tuple):
            object.__setattr__(self, "points", tuple(self.points))
        if not self.points or not all(isinstance(point, TrajectoryPoint) for point in self.points):
            raise ValueError("points must be a non-empty sequence of TrajectoryPoint values")

        times = [point.t for point in self.points]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("point times must be sorted and unique")
        self._validate_compatibility()

    def _validate_compatibility(self) -> None:
        target_type = self.target.target_type
        if target_type == "camera":
            allowed = (
                self.primitive == "circle" and self.semantic in _ORBIT_SEMANTICS
            ) or (
                self.primitive == "polyline"
                and self.semantic in _CAMERA_LINEAR_SEMANTICS
            )
        elif target_type == "actor":
            allowed = self.primitive == "polyline" and self.semantic == "move"
        elif target_type == "anchor":
            allowed = self.primitive == "static" and self.semantic == "anchor"
        else:
            # Local deformation is a known, serializable intent so downstream
            # compilers can reject it explicitly instead of losing the request.
            allowed = self.primitive == "polyline" and self.semantic == "move"
        if not allowed:
            raise ValueError(
                "target_type, primitive, and semantic are not compatible: "
                f"{target_type}/{self.primitive}/{self.semantic}"
            )

        if self.primitive == "static" and len(self.points) != 1:
            raise ValueError("static tracks require exactly one point")
        if self.primitive == "polyline" and len(self.points) < 2:
            raise ValueError("polyline tracks require at least two points")
        if self.primitive == "circle" and len(self.points) < 3:
            raise ValueError("circle tracks require at least three points")

    @classmethod
    def from_dict(cls, value: object) -> TrajectoryTrack:
        if not isinstance(value, Mapping):
            raise ValueError("track must be an object")
        _require_exact_keys(value, _TRACK_KEYS, "track")
        points = value["points"]
        if not isinstance(points, list):
            raise ValueError("track.points must be a list")
        track_id = value["track_id"]
        primitive = value["primitive"]
        semantic = value["semantic"]
        if not all(isinstance(item, str) for item in (track_id, primitive, semantic)):
            raise ValueError("track_id, primitive, and semantic must be strings")
        return cls(
            track_id=track_id,  # type: ignore[arg-type]
            target=TrajectoryTarget.from_dict(value["target"]),
            primitive=primitive,  # type: ignore[arg-type]
            semantic=semantic,  # type: ignore[arg-type]
            points=tuple(TrajectoryPoint.from_dict(point) for point in points),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "target": self.target.to_dict(),
            "primitive": self.primitive,
            "semantic": self.semantic,
            "points": [point.to_dict() for point in self.points],
        }


@dataclass(frozen=True)
class TrajectoryInstruction:
    """A complete trajectory instruction for exactly one scene shot."""

    scene_id: str
    shot_id: str
    duration_seconds: float
    sample_count: int
    tracks: tuple[TrajectoryTrack, ...]
    schema_version: str = SCHEMA_VERSION
    coordinate_space: str = COORDINATE_SPACE

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
        if self.coordinate_space != COORDINATE_SPACE:
            raise ValueError(f"coordinate_space must be {COORDINATE_SPACE!r}")
        object.__setattr__(self, "scene_id", _require_id(self.scene_id, "scene_id"))
        object.__setattr__(self, "shot_id", _require_id(self.shot_id, "shot_id"))
        if isinstance(self.duration_seconds, bool) or not isinstance(
            self.duration_seconds, (int, float)
        ):
            raise ValueError("duration_seconds must be a finite positive number")
        try:
            duration = float(self.duration_seconds)
        except (OverflowError, ValueError) as exc:
            raise ValueError("duration_seconds must be a finite positive number") from exc
        if not math.isfinite(duration) or duration <= 0.0:
            raise ValueError("duration_seconds must be a finite positive number")
        object.__setattr__(self, "duration_seconds", duration)
        if type(self.sample_count) is not int or self.sample_count <= 0:
            raise ValueError("sample_count must be a positive integer")
        if not isinstance(self.tracks, tuple):
            object.__setattr__(self, "tracks", tuple(self.tracks))
        if not self.tracks or not all(isinstance(track, TrajectoryTrack) for track in self.tracks):
            raise ValueError("tracks must be a non-empty sequence of TrajectoryTrack values")
        track_ids = [track.track_id for track in self.tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("track IDs must be unique")

    @classmethod
    def from_dict(cls, value: object) -> TrajectoryInstruction:
        if not isinstance(value, Mapping):
            raise ValueError("trajectory document must be an object")
        _require_exact_keys(value, _INSTRUCTION_KEYS, "trajectory document")
        tracks = value["tracks"]
        if not isinstance(tracks, list):
            raise ValueError("tracks must be a list")
        scene_id = value["scene_id"]
        shot_id = value["shot_id"]
        schema_version = value["schema_version"]
        coordinate_space = value["coordinate_space"]
        if not all(
            isinstance(item, str)
            for item in (scene_id, shot_id, schema_version, coordinate_space)
        ):
            raise ValueError("scene_id, shot_id, schema_version, and coordinate_space must be strings")
        return cls(
            scene_id=scene_id,  # type: ignore[arg-type]
            shot_id=shot_id,  # type: ignore[arg-type]
            duration_seconds=value["duration_seconds"],  # type: ignore[arg-type]
            sample_count=value["sample_count"],  # type: ignore[arg-type]
            tracks=tuple(TrajectoryTrack.from_dict(track) for track in tracks),
            schema_version=schema_version,  # type: ignore[arg-type]
            coordinate_space=coordinate_space,  # type: ignore[arg-type]
        )

    @classmethod
    def from_path(cls, path: Path) -> TrajectoryInstruction:
        return cls.from_json_bytes(path.read_bytes())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> TrajectoryInstruction:
        """Parse one strict UTF-8 snapshot, including duplicate-key rejection."""

        if not isinstance(payload, bytes):
            raise ValueError("trajectory JSON snapshot must be bytes")
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        return cls.from_dict(document)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scene_id": self.scene_id,
            "shot_id": self.shot_id,
            "coordinate_space": self.coordinate_space,
            "duration_seconds": self.duration_seconds,
            "sample_count": self.sample_count,
            "tracks": [track.to_dict() for track in self.tracks],
        }

    def validate_identity(self, expected_scene_id: str, expected_shot_id: str) -> None:
        """Fail closed when this instruction is paired with another scene or shot."""

        expected_scene = _require_id(expected_scene_id, "expected_scene_id")
        expected_shot = _require_id(expected_shot_id, "expected_shot_id")
        if self.scene_id != expected_scene:
            raise ValueError(
                f"scene identity mismatch: expected {expected_scene!r}, got {self.scene_id!r}"
            )
        if self.shot_id != expected_shot:
            raise ValueError(
                f"shot identity mismatch: expected {expected_shot!r}, got {self.shot_id!r}"
            )


def canonical_bytes(instruction: TrajectoryInstruction) -> bytes:
    """Return the stable UTF-8 encoding used for persistence and hashing."""

    return (
        json.dumps(
            instruction.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_output(path: Path, workspace: Path) -> Path:
    root = workspace.resolve()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output must stay below workspace") from exc
    return resolved


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-scene")
    parser.add_argument("--expected-shot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        source = args.input.resolve()
        if not source.is_file():
            raise ValueError(f"input is not a file: {source}")
        output = _safe_output(args.output, Path.cwd())
        if _same_file(source, output):
            raise ValueError("output collision with input trajectory")
        instruction = TrajectoryInstruction.from_path(source)
        if (args.expected_scene is None) != (args.expected_shot is None):
            raise ValueError("--expected-scene and --expected-shot must be used together")
        if args.expected_scene is not None:
            instruction.validate_identity(args.expected_scene, args.expected_shot)
        data = canonical_bytes(instruction)
        _atomic_write(output, data)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"TRAJECTORY_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    digest = hashlib.sha256(data).hexdigest()
    print(f"TRAJECTORY_OK {output} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
