"""Compile normalized trajectory intent into deterministic prompt controls."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

from videoactagent.prompts import compile_shot_prompts
from videoactagent.shotscript import ActorPlan, CameraPlan, Shot, ShotScript, Vec3
from videoactagent.trajectory import TrajectoryInstruction, canonical_bytes


SCHEMA_VERSION = "0.1"
BACKEND_SAMPLE_FPS = 24
# Geometry decisions are fixed and backend-independent.  Three points are the
# smallest window that can estimate a circle; normalized shifts/radius changes
# below these thresholds are treated as drawing noise.
GEOMETRY_WINDOW_POINTS = 3
CENTER_SHIFT_THRESHOLD = 0.05
RADIUS_CHANGE_THRESHOLD = 0.04
SIGNED_TURN_THRESHOLD = 0.001
CIRCLE_DEGENERACY_RATIO = 1e-4
MAX_RADIUS_CHORD_RATIO = 20.0

_CAMERA_MAPPING: dict[str, tuple[str, str]] = {
    "pan_left": ("pan_left", "pan left"),
    "pan_right": ("pan_right", "pan right"),
    "truck_left": ("truck_left", "truck left"),
    "truck_right": ("truck_right", "truck right"),
    "dolly_in": ("dolly_in", "dolly in"),
    "dolly_out": ("dolly_out", "dolly out"),
    "orbit_clockwise": ("arc_clockwise", "orbit clockwise"),
    "orbit_counterclockwise": ("arc_counterclockwise", "orbit counterclockwise"),
    "zoom_in": ("zoom_in", "zoom in"),
    "zoom_out": ("zoom_out", "zoom out"),
}


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _point_dict(point: object) -> dict[str, object]:
    return {
        "t": float(getattr(point, "t")),
        "x": float(getattr(point, "x")),
        "y": float(getattr(point, "y")),
        "visible": bool(getattr(point, "visible")),
    }


def _sorted_points(track: object) -> list[object]:
    return sorted(getattr(track, "points"), key=lambda point: float(getattr(point, "t")))


def _horizontal_region(value: float) -> str:
    if value < 1.0 / 3.0:
        return "left"
    if value < 2.0 / 3.0:
        return "centre"
    return "right"


def _vertical_region(value: float) -> str:
    if value < 1.0 / 3.0:
        return "top"
    if value < 2.0 / 3.0:
        return "middle"
    return "bottom"


def _region(point: object) -> str:
    return f"{_horizontal_region(float(getattr(point, 'x')))}-{_vertical_region(float(getattr(point, 'y')))}"


def _actor_name(actor_id: str) -> str:
    suffix = actor_id[len("actor_") :] if actor_id.startswith("actor_") else actor_id
    return f"actor {suffix.upper()}"


def _vec3(value: Vec3) -> list[float]:
    return value.as_list()


def _camera_dict(camera: CameraPlan) -> dict[str, object]:
    return {
        "shot_size": camera.shot_size,
        "focal_length_mm": camera.focal_length_mm,
        "motion": camera.motion,
        "start": _vec3(camera.start),
        "end": _vec3(camera.end),
        "look_at": camera.look_at,
    }


def _actor_dict(actor: ActorPlan) -> dict[str, object]:
    return {
        "id": actor.actor_id,
        "color": actor.color,
        "start": _vec3(actor.start),
        "end": _vec3(actor.end),
        "action": actor.action,
        "facing": actor.facing,
    }


def _shot_dict(shot: Shot) -> dict[str, object]:
    return {
        "shot_id": shot.shot_id,
        "duration": shot.duration,
        "prompt": shot.prompt,
        "camera": _camera_dict(shot.camera),
        "actors": [_actor_dict(actor) for actor in shot.actors],
        "continuity": {
            "previous_shot": shot.continuity.previous_shot,
            "screen_direction": shot.continuity.screen_direction,
            "axis_side": shot.continuity.axis_side,
        },
    }


def _shotscript_dict(shotscript: ShotScript) -> dict[str, object]:
    document = {
        "scene_id": shotscript.scene_id,
        "fps": shotscript.fps,
        "world_bounds": list(shotscript.world_bounds),
        "shots": [_shot_dict(shot) for shot in shotscript.shots],
    }
    if not (
        shotscript.scene_id == "station_platform"
        and shotscript.environment_preset == "station"
    ):
        document["environment_preset"] = shotscript.environment_preset
    return document


def _instruction_digest(instruction: object) -> str:
    if isinstance(instruction, TrajectoryInstruction):
        data = canonical_bytes(instruction)
    else:
        to_dict = getattr(instruction, "to_dict", None)
        if not callable(to_dict):
            raise ValueError("instruction must expose canonical to_dict()")
        data = _json_bytes(to_dict())
    return hashlib.sha256(data).hexdigest()


def _circle_window_features(
    points: list[object],
) -> tuple[dict[str, object], dict[str, float]]:
    def coordinate(point: object) -> tuple[float, float]:
        return float(getattr(point, "x")), float(getattr(point, "y"))

    def fit(window: list[object]) -> tuple[float, float, float]:
        (ax, ay), (bx, by), (cx, cy) = [coordinate(point) for point in window]
        denominator = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        scale_squared = max(
            (ax - bx) ** 2 + (ay - by) ** 2,
            (bx - cx) ** 2 + (by - cy) ** 2,
            (cx - ax) ** 2 + (cy - ay) ** 2,
        )
        normalized_determinant = (
            abs(denominator) / scale_squared if scale_squared > 0.0 else 0.0
        )
        if normalized_determinant <= CIRCLE_DEGENERACY_RATIO:
            raise ValueError(
                "degenerate circle window: points are identical or near-collinear"
            )
        a_squared = ax * ax + ay * ay
        b_squared = bx * bx + by * by
        c_squared = cx * cx + cy * cy
        centre_x = (
            a_squared * (by - cy)
            + b_squared * (cy - ay)
            + c_squared * (ay - by)
        ) / denominator
        centre_y = (
            a_squared * (cx - bx)
            + b_squared * (ax - cx)
            + c_squared * (bx - ax)
        ) / denominator
        radius = math.hypot(ax - centre_x, ay - centre_y)
        radius_chord_ratio = radius / math.sqrt(scale_squared)
        if radius_chord_ratio > MAX_RADIUS_CHORD_RATIO:
            raise ValueError(
                "degenerate circle window: fitted radius/chord ratio exceeds "
                f"{MAX_RADIUS_CHORD_RATIO:g}"
            )
        return centre_x, centre_y, radius

    if len(points) < GEOMETRY_WINDOW_POINTS:
        raise ValueError("circle geometry requires at least three points")
    start_x, start_y, start_radius = fit(points[:GEOMETRY_WINDOW_POINTS])
    end_x, end_y, end_radius = fit(points[-GEOMETRY_WINDOW_POINTS:])
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    radius_delta = end_radius - start_radius
    raw = {
        "delta_x": delta_x,
        "delta_y": delta_y,
        "center_displacement": math.hypot(delta_x, delta_y),
        "start_radius": start_radius,
        "end_radius": end_radius,
        "radius_delta": radius_delta,
    }
    display = {
        "window_points": GEOMETRY_WINDOW_POINTS,
        "start_center": [round(start_x, 6), round(start_y, 6)],
        "end_center": [round(end_x, 6), round(end_y, 6)],
        "center_delta": [round(delta_x, 6), round(delta_y, 6)],
        "center_displacement": round(raw["center_displacement"], 6),
        "start_radius": round(start_radius, 6),
        "end_radius": round(end_radius, 6),
        "radius_delta": round(radius_delta, 6),
        "center_shift_threshold": CENTER_SHIFT_THRESHOLD,
        "radius_change_threshold": RADIUS_CHANGE_THRESHOLD,
        "circle_degeneracy_ratio": CIRCLE_DEGENERACY_RATIO,
        "max_radius_chord_ratio": MAX_RADIUS_CHORD_RATIO,
    }
    return display, raw


def _component_wording(semantic: str) -> str:
    return {
        "truck_left": "truck left",
        "truck_right": "truck right",
        "pan_up": "pan up",
        "pan_down": "pan down",
        "dolly_in": "dolly in",
        "dolly_out": "dolly out",
    }[semantic]


def _compile_camera(track: object) -> tuple[dict[str, object], str]:
    semantic = str(getattr(track, "semantic"))
    try:
        motion, wording = _CAMERA_MAPPING[semantic]
    except KeyError as exc:
        raise ValueError(f"unsupported camera semantic: {semantic}") from exc
    points = _sorted_points(track)
    components: list[dict[str, object]] = [
        {"type": "declared", "semantic": semantic}
    ]
    geometry_features: dict[str, object] | None = None
    if str(getattr(track, "primitive")) == "circle":
        geometry_features, raw_geometry = _circle_window_features(points)
        delta_x = raw_geometry["delta_x"]
        delta_y = raw_geometry["delta_y"]
        center_exceeds = raw_geometry["center_displacement"] > CENTER_SHIFT_THRESHOLD and not math.isclose(
            raw_geometry["center_displacement"], CENTER_SHIFT_THRESHOLD, abs_tol=1e-12
        )
        if center_exceeds:
            if abs(delta_x) >= abs(delta_y):
                translated = "truck_right" if delta_x > 0 else "truck_left"
            else:
                translated = "pan_down" if delta_y > 0 else "pan_up"
            components.append(
                {"type": "translated_center", "semantic": translated}
            )
        radius_delta = raw_geometry["radius_delta"]
        radius_exceeds = abs(radius_delta) > RADIUS_CHANGE_THRESHOLD and not math.isclose(
            abs(radius_delta), RADIUS_CHANGE_THRESHOLD, abs_tol=1e-12
        )
        if radius_exceeds:
            components.append(
                {
                    "type": "radius_change",
                    "semantic": "dolly_out" if radius_delta > 0 else "dolly_in",
                }
            )
    patch = {
        "track_id": str(getattr(track, "track_id")),
        "camera_id": str(getattr(track, "target_id")),
        "primitive": str(getattr(track, "primitive")),
        "semantic": semantic,
        "motion": motion,
        "components": components,
        "geometry_features": geometry_features,
        "keyframes": [_point_dict(point) for point in points],
    }
    if semantic.startswith("orbit_"):
        clause = f"Camera must {wording} around the meeting actors"
    else:
        clause = f"Camera must {wording}"
    secondary = [
        _component_wording(str(component["semantic"])) for component in components[1:]
    ]
    if secondary:
        clause += " while also " + " and ".join(secondary)
    return patch, clause + " while preserving the action axis."


def _curve_direction(points: list[object]) -> tuple[str, float]:
    signed_turn = 0.0
    for first, middle, last in zip(points, points[1:], points[2:]):
        first_dx = float(getattr(middle, "x")) - float(getattr(first, "x"))
        first_dy = float(getattr(middle, "y")) - float(getattr(first, "y"))
        second_dx = float(getattr(last, "x")) - float(getattr(middle, "x"))
        second_dy = float(getattr(last, "y")) - float(getattr(middle, "y"))
        signed_turn += first_dx * second_dy - first_dy * second_dx
    if signed_turn > SIGNED_TURN_THRESHOLD:
        return "clockwise_curve", signed_turn
    if signed_turn < -SIGNED_TURN_THRESHOLD:
        return "counterclockwise_curve", signed_turn
    delta_x = float(getattr(points[-1], "x")) - float(getattr(points[0], "x"))
    delta_y = float(getattr(points[-1], "y")) - float(getattr(points[0], "y"))
    if abs(delta_x) >= abs(delta_y):
        return ("left_to_right" if delta_x > 0 else "right_to_left" if delta_x < 0 else "static"), signed_turn
    return ("top_to_bottom" if delta_y > 0 else "bottom_to_top"), signed_turn


def _curve_wording(direction: str) -> str:
    return {
        "clockwise_curve": "a clockwise curve",
        "counterclockwise_curve": "a counterclockwise curve",
        "left_to_right": "a left-to-right path",
        "right_to_left": "a right-to-left path",
        "top_to_bottom": "a top-to-bottom path",
        "bottom_to_top": "a bottom-to-top path",
        "static": "a near-static path",
    }[direction]


def _compile_actor(
    track: object, actors: dict[str, ActorPlan], duration: float
) -> tuple[dict[str, object], str]:
    actor_id = str(getattr(track, "target_id"))
    if actor_id not in actors:
        raise ValueError(f"trajectory actor target does not exist in shot: {actor_id}")
    if str(getattr(track, "primitive")) != "polyline" or str(
        getattr(track, "semantic")
    ) != "move":
        raise ValueError(f"unsupported actor trajectory: {getattr(track, 'track_id')}")
    points = _sorted_points(track)
    if len(points) < 2:
        raise ValueError("actor trajectory requires at least two points")
    start = points[0]
    end = points[-1]
    previous = points[-2]
    start_time = float(getattr(start, "t")) * duration
    end_time = float(getattr(end, "t")) * duration
    arrival_start = float(getattr(previous, "t")) * duration
    start_region = _region(start)
    end_region = _region(end)
    x_delta = float(getattr(end, "x")) - float(getattr(start, "x"))
    screen_direction = (
        "left_to_right" if x_delta > 0.0 else "right_to_left" if x_delta < 0.0 else "static"
    )
    curve_direction, signed_turn = _curve_direction(points)
    facing = actors[actor_id].facing
    patch = {
        "track_id": str(getattr(track, "track_id")),
        "actor_id": actor_id,
        "primitive": "polyline",
        "semantic": "move",
        "start_region": start_region,
        "end_region": end_region,
        "screen_direction": screen_direction,
        "facing": facing,
        "curve_direction": curve_direction,
        "signed_turn": round(signed_turn, 6),
        "arrival_interval_seconds": [round(arrival_start, 6), round(end_time, 6)],
        "keyframes": [_point_dict(point) for point in points],
    }
    clause = (
        f"{start_time:.1f}-{end_time:.1f}s: {_actor_name(actor_id)} moves from the "
        f"{start_region} region to the {end_region} region along "
        f"{_curve_wording(curve_direction)} while facing {_actor_name(facing)}, arriving during "
        f"{arrival_start:.1f}-{end_time:.1f}s."
    )
    return patch, clause


def _compile_anchor(track: object) -> tuple[dict[str, object], str]:
    points = _sorted_points(track)
    if len(points) != 1:
        raise ValueError("composition anchor requires exactly one point")
    region = _region(points[0])
    patch = {
        "track_id": str(getattr(track, "track_id")),
        "anchor_id": str(getattr(track, "target_id")),
        "region": region,
        "keyframe": _point_dict(points[0]),
    }
    return patch, f"Keep composition anchor {getattr(track, 'target_id')} fixed in the {region} region."


def compile_trajectory(
    instruction: TrajectoryInstruction,
    shotscript: ShotScript,
    shot_id: str,
) -> dict[str, object]:
    """Compile one trajectory instruction without reading or writing files."""

    if shotscript.scene_id != instruction.scene_id:
        raise ValueError(
            f"scene identity mismatch: trajectory={instruction.scene_id!r}, shotscript={shotscript.scene_id!r}"
        )
    if instruction.shot_id != shot_id:
        raise ValueError(
            f"shot identity mismatch: requested={shot_id!r}, trajectory={instruction.shot_id!r}"
        )
    matches = [shot for shot in shotscript.shots if shot.shot_id == shot_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one matching shot {shot_id!r}, found {len(matches)}")
    shot = matches[0]
    if not math.isclose(float(instruction.duration_seconds), shot.duration, abs_tol=1e-9):
        raise ValueError(
            f"trajectory duration {instruction.duration_seconds} does not match shot duration {shot.duration}"
        )
    expected_samples = round(shot.duration * BACKEND_SAMPLE_FPS) + 1
    if instruction.sample_count != expected_samples:
        raise ValueError(
            f"trajectory sample_count must be {expected_samples} for {shot.duration:g}s at {BACKEND_SAMPLE_FPS}fps"
        )

    actors = {actor.actor_id: actor for actor in shot.actors}
    patched_camera: dict[str, object] = _camera_dict(shot.camera)
    patched_actors: list[dict[str, object]] = []
    patched_anchors: list[dict[str, object]] = []
    cinematic_clauses: list[str] = []
    timed_clauses: list[str] = []
    unsupported: list[dict[str, str]] = []
    ordered_tracks = sorted(instruction.tracks, key=lambda track: str(track.track_id))

    camera_seen = False
    actor_seen = False
    for track in ordered_tracks:
        target_type = str(getattr(track, "target_type"))
        if target_type == "camera":
            if camera_seen:
                raise ValueError("version 0.1 supports only one camera trajectory per shot")
            camera_seen = True
            camera_patch, clause = _compile_camera(track)
            patched_camera.update(camera_patch)
            cinematic_clauses.append(clause)
        elif target_type == "actor":
            if actor_seen:
                raise ValueError("version 0.1 supports only one actor trajectory per shot")
            actor_seen = True
            actor_patch, clause = _compile_actor(track, actors, shot.duration)
            patched_actors.append(actor_patch)
            timed_clauses.append(clause)
        elif target_type == "anchor":
            anchor_patch, clause = _compile_anchor(track)
            patched_anchors.append(anchor_patch)
            cinematic_clauses.append(clause)
        else:
            unsupported.append(
                {
                    "track_id": str(getattr(track, "track_id")),
                    "target_type": target_type,
                    "reason": "unsupported_by_t2v_backend",
                }
            )

    prompt_shot = replace(
        shot,
        camera=replace(
            shot.camera,
            motion=str(patched_camera.get("semantic", shot.camera.motion)),
        ),
    )
    base_prompt = compile_shot_prompts(prompt_shot).cinematic
    cinematic = " ".join([base_prompt, *cinematic_clauses]).strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "scene_id": instruction.scene_id,
        "shot_id": shot_id,
        "trajectory_sha256": _instruction_digest(instruction),
        "canonical_shotscript_sha256": hashlib.sha256(
            _json_bytes(_shotscript_dict(shotscript))
        ).hexdigest(),
        "sample_fps": BACKEND_SAMPLE_FPS,
        "sample_count": instruction.sample_count,
        "track_order": [str(track.track_id) for track in ordered_tracks],
        "patched_camera": patched_camera,
        "patched_actors": patched_actors,
        "patched_anchors": patched_anchors,
        "prompt": {"cinematic": cinematic, "timed": timed_clauses},
        "backend_capability": {"t2v": "prompt_approximation"},
        "unsupported": unsupported,
        "submission_ready": not unsupported,
    }


def _patched_shotscript(shotscript: ShotScript, compiled: dict[str, object]) -> dict[str, object]:
    document = _shotscript_dict(shotscript)
    for shot in document["shots"]:  # type: ignore[union-attr]
        if shot["shot_id"] != compiled["shot_id"]:
            continue
        camera = shot["camera"]
        camera["motion"] = compiled["patched_camera"]["motion"]  # type: ignore[index]
        actor_patches = {
            patch["actor_id"]: patch for patch in compiled["patched_actors"]  # type: ignore[union-attr]
        }
        for actor in shot["actors"]:
            patch = actor_patches.get(actor["id"])
            if patch is not None:
                actor["action"] = (
                    f"follow_trajectory_{patch['start_region']}_to_{patch['end_region']}"
                )
                shot["continuity"]["screen_direction"] = patch["screen_direction"]
        shot["trajectory_control"] = {
            "camera": compiled["patched_camera"],
            "actors": compiled["patched_actors"],
            "anchors": compiled["patched_anchors"],
        }
    document["trajectory_control"] = {
        "schema_version": SCHEMA_VERSION,
        "trajectory_sha256": compiled["trajectory_sha256"],
        "canonical_shotscript_sha256": compiled["canonical_shotscript_sha256"],
        "submission_ready": compiled["submission_ready"],
    }
    return document


def _safe_output_dir(path: Path, workspace: Path) -> Path:
    root = workspace.resolve()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output directory must stay below workspace") from exc
    return resolved


def _capture_output_parent(parent: Path) -> tuple[str, int, int]:
    resolved = parent.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("output parent must be a directory")
    metadata = resolved.stat()
    return str(resolved), metadata.st_dev, metadata.st_ino


def _verify_output_parent(parent: Path, expected: tuple[str, int, int]) -> None:
    actual = _capture_output_parent(parent)
    if actual != expected:
        raise ValueError("output parent identity changed before atomic publish")


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _write_group(files: dict[Path, bytes]) -> None:
    if not files:
        return
    parent = next(iter(files)).parent
    if any(target.parent != parent for target in files):
        raise ValueError("all compiled outputs must share one directory")
    if parent.exists():
        raise ValueError(f"output directory already exists: {parent}")
    parent.parent.mkdir(parents=True, exist_ok=True)
    output_parent_identity = _capture_output_parent(parent.parent)
    staging = parent.parent / f".{parent.name}.{uuid4().hex}.tmp"
    staging.mkdir()
    try:
        for target, data in files.items():
            staged_target = staging / target.name
            with staged_target.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        if os.name == "posix":
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _verify_output_parent(parent.parent, output_parent_identity)
        for attempt in range(5):
            try:
                os.replace(staging, parent)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if os.name == "posix":
            directory_fd = os.open(parent.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--shot", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _snapshot_source_bytes(source: Path, snapshot_root: Path, label: str) -> bytes:
    snapshot = snapshot_root / f"{label}-{uuid4().hex}.json"
    shutil.copyfile(source, snapshot)
    return snapshot.read_bytes()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        trajectory_path = args.trajectory.resolve()
        shotscript_path = args.shotscript.resolve()
        if not trajectory_path.is_file() or not shotscript_path.is_file():
            raise ValueError("trajectory and shotscript inputs must be files")
        output_dir = _safe_output_dir(args.output_dir, Path.cwd())
        targets = {
            output_dir / "compiled_control.json",
            output_dir / "trajectory_prompt.txt",
            output_dir / "patched_shotscript.json",
        }
        for source in (trajectory_path, shotscript_path):
            if any(_same_file(source, target) for target in targets):
                raise ValueError("output collision with input file")

        with tempfile.TemporaryDirectory(prefix="videoactagent-trajectory-compile-") as directory:
            snapshot_root = Path(directory)
            trajectory_bytes = _snapshot_source_bytes(
                trajectory_path, snapshot_root, "trajectory"
            )
            shotscript_bytes = _snapshot_source_bytes(
                shotscript_path, snapshot_root, "shotscript"
            )
            instruction = TrajectoryInstruction.from_json_bytes(trajectory_bytes)
            shotscript_document = json.loads(
                shotscript_bytes.decode("utf-8", errors="strict")
            )
            shotscript = ShotScript.from_dict(shotscript_document)
            compiled = compile_trajectory(instruction, shotscript, args.shot)
            compiled["source_artifact_sha256"] = {
                "trajectory": hashlib.sha256(trajectory_bytes).hexdigest(),
                "shotscript": hashlib.sha256(shotscript_bytes).hexdigest(),
            }
            compiled["source_artifact_bytes"] = {
                "trajectory": len(trajectory_bytes),
                "shotscript": len(shotscript_bytes),
            }
            patched_bytes = _json_bytes(_patched_shotscript(shotscript, compiled))
            prompt_lines = [
                compiled["prompt"]["cinematic"],
                *compiled["prompt"]["timed"],
            ]  # type: ignore[index]
            prompt_bytes = ("\n".join(prompt_lines) + "\n").encode("utf-8")
            compiled["artifact_sha256"] = {
                "trajectory_prompt.txt": hashlib.sha256(prompt_bytes).hexdigest(),
                "patched_shotscript.json": hashlib.sha256(patched_bytes).hexdigest(),
            }
            compiled_bytes = _json_bytes(compiled)
            _write_group(
                {
                    output_dir / "patched_shotscript.json": patched_bytes,
                    output_dir / "trajectory_prompt.txt": prompt_bytes,
                    output_dir / "compiled_control.json": compiled_bytes,
                }
            )
    except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"TRAJECTORY_COMPILE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        "TRAJECTORY_COMPILE_OK "
        f"{output_dir} trajectory_sha256={compiled['trajectory_sha256']} "
        f"compiled_sha256={hashlib.sha256(compiled_bytes).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
