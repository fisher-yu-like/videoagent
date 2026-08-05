"""Immutable, hash-bound input contract for the isolated Blender codegen lab."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import os
import shutil
from typing import Any
from uuid import uuid4

from videoactagent.shotscript import ShotScript, ShotScriptError
from videoactagent.trajectory import TrajectoryInstruction


TIMES = (0.0, 0.2, 0.5, 0.8, 1.0)
SCHEMA_VERSION = "blender-codegen-input-1.0"


class CodegenContractError(ValueError):
    """Raised when the codegen input is incomplete or identity-inconsistent."""


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodegenContractError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise CodegenContractError(f"{label} must be finite")
    return result


def _hash_file(path: Path) -> tuple[str, int]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CodegenContractError(f"cannot read source file: {path}") from exc
    return hashlib.sha256(data).hexdigest(), len(data)


def _world_xy(bounds: tuple[float, float, float, float], x: float, y: float) -> tuple[float, float]:
    min_x, max_x, min_y, max_y = bounds
    return min_x + x * (max_x - min_x), max_y - y * (max_y - min_y)


def _json_value(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodegenContractError(f"cannot parse {label}: {path}") from exc


def _canonical_write(path: Path, value: object) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_resolution(resolution: tuple[int, int]) -> tuple[int, int]:
    if not isinstance(resolution, tuple) or len(resolution) != 2:
        raise CodegenContractError("resolution must be a width/height tuple")
    width, height = resolution
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise CodegenContractError("resolution must contain positive integers")
    return width, height


def build_codegen_input(
    *,
    prompt: str,
    shotscript: Mapping[str, Any],
    trajectory: Mapping[str, Any],
    fps: int,
    resolution: tuple[int, int],
    source_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a whole-story station input and return the codegen schema."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise CodegenContractError("prompt must be a non-empty string")
    if len(prompt) > 20000:
        raise CodegenContractError("prompt is too long")
    if type(fps) is not int or fps <= 0:
        raise CodegenContractError("fps must be a positive integer")
    width, height = _validate_resolution(resolution)
    try:
        script = ShotScript.from_dict(dict(shotscript))
        instruction = TrajectoryInstruction.from_dict(dict(trajectory))
    except (ShotScriptError, ValueError, TypeError) as exc:
        raise CodegenContractError(str(exc)) from exc
    if len(script.shots) != 1:
        raise CodegenContractError("codegen requires exactly one whole-story shot")
    shot = script.shots[0]
    if instruction.scene_id != script.scene_id:
        raise CodegenContractError("scene identity mismatch between ShotScript and trajectory")
    if instruction.shot_id != shot.shot_id:
        raise CodegenContractError("shot identity mismatch between ShotScript and trajectory")
    if abs(instruction.duration_seconds - shot.duration) > 1e-9:
        raise CodegenContractError("trajectory duration differs from ShotScript duration")
    if len(instruction.tracks) != len(shot.actors):
        raise CodegenContractError("trajectory must contain exactly one track per actor")

    actor_by_id = {actor.actor_id: actor for actor in shot.actors}
    track_ids = [track.target_id for track in instruction.tracks]
    if set(track_ids) != set(actor_by_id) or len(track_ids) != len(set(track_ids)):
        raise CodegenContractError("trajectory actor tracks do not match ShotScript actors")
    bounds = tuple(script.world_bounds)
    normalized_tracks: list[dict[str, Any]] = []
    for track in instruction.tracks:
        if track.target_type != "actor" or track.primitive != "polyline" or track.semantic != "move":
            raise CodegenContractError("codegen actor tracks must be polyline move tracks")
        if len(track.points) != 5:
            raise CodegenContractError("each actor track must contain exactly K0--K4")
        actual_times = tuple(point.t for point in track.points)
        if actual_times != TIMES:
            raise CodegenContractError("actor track times must be exactly K0--K4 [0, .2, .5, .8, 1]")
        actor = actor_by_id[track.target_id]
        points: list[dict[str, Any]] = []
        for index, point in enumerate(track.points):
            wx, wy = _world_xy(bounds, point.x, point.y)
            points.append({
                "keyframe_id": f"K{index}",
                "t": point.t,
                "x": point.x,
                "y": point.y,
                "world": [wx, wy],
                "visible": point.visible,
            })
        expected_start = (actor.start.x, actor.start.y)
        expected_end = (actor.end.x, actor.end.y)
        if any(abs(points[0]["world"][i] - expected_start[i]) > 1e-6 for i in range(2)):
            raise CodegenContractError(f"{track.target_id} K0 does not match ShotScript actor start")
        if any(abs(points[-1]["world"][i] - expected_end[i]) > 1e-6 for i in range(2)):
            raise CodegenContractError(f"{track.target_id} K4 does not match ShotScript actor end")
        normalized_tracks.append({
            "track_id": track.track_id,
            "target": track.target.to_dict(),
            "primitive": track.primitive,
            "semantic": track.semantic,
            "points": points,
        })

    frame_end = int(round(shot.duration * fps))
    if frame_end < 1:
        raise CodegenContractError("render contract must contain at least one frame")
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt": prompt.strip(),
        "shotscript": dict(shotscript),
        "trajectory": {
            "schema_version": instruction.schema_version,
            "scene_id": instruction.scene_id,
            "shot_id": instruction.shot_id,
            "coordinate_space": instruction.coordinate_space,
            "duration_seconds": instruction.duration_seconds,
            "sample_count": instruction.sample_count,
            "tracks": normalized_tracks,
        },
        "render_contract": {
            "fps": fps,
            "resolution": [width, height],
            "frame_start": 1,
            "frame_end": frame_end,
            "duration_seconds": shot.duration,
            "world_bounds": list(bounds),
        },
        "source_bindings": dict(source_bindings),
    }


def snapshot_codegen_inputs(
    *,
    prompt_path: Path,
    shotscript_path: Path,
    trajectory_path: Path,
    destination: Path,
    fps: int,
    resolution: tuple[int, int],
) -> dict[str, Any]:
    """Create a new immutable source snapshot and write ``input.json``."""

    destination = Path(destination).resolve(strict=False)
    if destination.exists():
        raise CodegenContractError(f"destination already exists: {destination}")
    prompt_path = Path(prompt_path).resolve()
    shotscript_path = Path(shotscript_path).resolve()
    trajectory_path = Path(trajectory_path).resolve()
    for path in (prompt_path, shotscript_path, trajectory_path):
        if not path.is_file():
            raise CodegenContractError(f"source file does not exist: {path}")
    prompt = prompt_path.read_text(encoding="utf-8")
    shotscript = _json_value(shotscript_path, "ShotScript")
    trajectory = _json_value(trajectory_path, "trajectory")
    source_dir = destination / "sources"
    source_dir.mkdir(parents=True, exist_ok=False)
    source_specs = {
        "prompt": (prompt_path, source_dir / "prompt.txt"),
        "shotscript": (shotscript_path, source_dir / "shotscript.json"),
        "trajectory": (trajectory_path, source_dir / "trajectory.json"),
    }
    bindings: dict[str, dict[str, Any]] = {}
    try:
        for key, (source, copied) in source_specs.items():
            shutil.copyfile(source, copied)
            source_hash, byte_count = _hash_file(copied)
            original_hash, original_bytes = _hash_file(source)
            if source_hash != original_hash or byte_count != original_bytes:
                raise CodegenContractError(f"source changed while snapshotting: {source}")
            bindings[key] = {"path": str(copied.relative_to(destination)), "sha256": source_hash, "bytes": byte_count}
        result = build_codegen_input(
            prompt=prompt,
            shotscript=shotscript,
            trajectory=trajectory,
            fps=fps,
            resolution=resolution,
            source_bindings=bindings,
        )
        _canonical_write(destination / "input.json", result)
        return result
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
