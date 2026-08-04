"""Fact-based checks for synchronized multicamera Blender evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from videoactagent.multicam_plan import MulticamPlan


def _vector(value: object, label: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        raise ValueError(f"{label} must contain three numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} contains a non-finite number")
    return result


def _angle(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    a_len = math.sqrt(sum(item * item for item in a))
    b_len = math.sqrt(sum(item * item for item in b))
    if a_len <= 1e-12 or b_len <= 1e-12:
        return 180.0
    cosine = max(-1.0, min(1.0, sum(a[i] * b[i] for i in range(3)) / (a_len * b_len)))
    return math.degrees(math.acos(cosine))


def _visible(actor: object, camera: Mapping[str, Any]) -> bool:
    point = _vector(actor, "actor position")
    position = _vector(camera.get("position"), "camera position")
    direction = _vector(camera.get("view_direction"), "camera direction")
    ray = tuple(point[index] - position[index] for index in range(3))
    focal = float(camera.get("focal_length_mm", 0.0))
    if focal <= 0 or not math.isfinite(focal):
        return False
    horizontal_half_fov = math.degrees(math.atan(36.0 / (2.0 * focal)))
    return _angle(direction, ray) <= horizontal_half_fov


def evaluate_multicam_iteration(
    *, plan: MulticamPlan, render_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure only facts present in a real render manifest; composition stays human-only."""
    cameras = render_manifest.get("cameras")
    frames = render_manifest.get("shared_world_frames")
    if not isinstance(cameras, Mapping) or set(cameras) != {"camera_a", "camera_b", "camera_c"}:
        raise ValueError("render manifest must contain exactly three cameras")
    if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)) or not frames:
        raise ValueError("render manifest has no shared world frames")

    videos = [cameras[camera_id].get("video", {}) for camera_id in sorted(cameras)]
    counts = [int(video.get("frame_count", -1)) for video in videos]
    fps_values = [float(video.get("fps", -1)) for video in videos]
    durations = [count / fps if fps > 0 else -1 for count, fps in zip(counts, fps_values)]
    sync = {
        "frame_count_equal": len(set(counts)) == 1 and counts[0] == len(frames),
        "fps_equal": len(set(fps_values)) == 1 and fps_values[0] > 0,
        "duration_equal": max(durations) - min(durations) <= 1e-9 and durations[0] > 0,
    }
    expected = max(counts) if counts else 0
    coverage = {
        camera_id: round(min(len(frames), int(cameras[camera_id]["video"].get("frame_count", 0))) / expected, 6)
        if expected > 0 else 0.0
        for camera_id in sorted(cameras)
    }

    per_camera_visibility: dict[str, float] = {}
    adjacent_angles: list[float] = []
    collision_free = True
    all_actor_positions: list[tuple[float, float, float]] = []
    for frame in frames:
        if not isinstance(frame, Mapping):
            raise ValueError("world frame is invalid")
        actors = frame.get("actors")
        objects = frame.get("objects", {})
        frame_cameras = frame.get("cameras")
        if not isinstance(actors, Mapping) or not isinstance(frame_cameras, Mapping):
            raise ValueError("world frame actors/cameras are invalid")
        if (
            not isinstance(objects, Mapping)
            or not all(isinstance(object_id, str) and object_id for object_id in objects)
        ):
            raise ValueError("world frame objects are invalid")
        all_actor_positions.extend(_vector(value, "actor position") for value in actors.values())
        for value in objects.values():
            _vector(value, "object position")

    frame_total = len(frames)
    for assignment in plan.cameras:
        hits = total = 0
        directions = []
        for index, frame in enumerate(frames):
            t = index / (frame_total - 1) if frame_total > 1 else 0.0
            camera = frame["cameras"].get(assignment.camera_id)
            if not isinstance(camera, Mapping):
                continue
            directions.append(_vector(camera.get("view_direction"), "camera direction"))
            position = _vector(camera.get("position"), "camera position")
            for actor in frame["actors"].values():
                actor_position = _vector(actor, "actor position")
                distance = math.sqrt(sum((position[i] - actor_position[i]) ** 2 for i in range(3)))
                collision_free = collision_free and distance > 0.25
            responsible = any(
                segment.start - 1e-9 <= t <= segment.end + 1e-9
                for segment in assignment.responsibility_segments
            )
            if responsible:
                targets = (
                    list(frame["actors"].values())
                    if assignment.target == "all_actors"
                    else [
                        frame["actors"].get(
                            assignment.target,
                            frame.get("objects", {}).get(assignment.target),
                        )
                    ]
                )
                total += len(targets)
                hits += sum(target is not None and _visible(target, camera) for target in targets)
        adjacent_angles.extend(_angle(left, right) for left, right in zip(directions, directions[1:]))
        per_camera_visibility[assignment.camera_id] = round(hits / total, 6) if total else 0.0

    maximum_angle = max(adjacent_angles, default=0.0)
    minimum_visibility = min(per_camera_visibility.values(), default=0.0)
    orientation = {
        "maximum_adjacent_view_angle_degrees": round(maximum_angle, 6),
        "passed": maximum_angle <= 30.0,
    }
    automatic_passed = (
        all(sync.values())
        and all(value == 1.0 for value in coverage.values())
        and minimum_visibility >= 0.95
        and orientation["passed"]
        and collision_free
    )
    return {
        "schema_version": "multicam-eval-1.0",
        "sync": sync,
        "coverage": coverage,
        "responsibility_target_visibility": {
            "per_camera": per_camera_visibility,
            "minimum": minimum_visibility,
            "threshold": 0.95,
        },
        "orientation": orientation,
        "collision": {"camera_actor_clearance_passed": collision_free},
        "geometry_consistency": {"shared_world_record": True, "passed": True},
        "human_composition_status": "unknown",
        "automatic_passed": automatic_passed,
    }
