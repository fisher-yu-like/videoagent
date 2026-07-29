"""Evaluate requested trajectories against hash-bound manual observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from videoactagent.trajectory import TrajectoryInstruction, canonical_bytes
from videoactagent.trajectory_observe import (
    COORDINATE_SPACE,
    EVIDENCE_TYPE,
    SCHEMA_VERSION,
    _decode_selected_frames,
    load_strict_object,
    sha256_file,
    validate_session_manifest,
)


_TRACK_POINT_KEYS = frozenset({"t", "x", "y", "visible"})
_ANNOTATION_KEYS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "coordinate_space",
        "scene_id",
        "shot_id",
        "track_id",
        "target",
        "video_sha256",
        "trajectory_file_sha256",
        "trajectory_canonical_sha256",
        "session_manifest_sha256",
        "points",
    }
)
_ANNOTATION_POINT_KEYS = frozenset(
    {"frame", "t", "time_seconds", "frame_sha256", "x", "y", "visible"}
)
_TARGET_KEYS = frozenset({"type", "id"})
MAX_SAMPLE_COUNT = 1024


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _unit_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number in [0, 1]") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    return 0.0 if result == 0.0 else result


def _validate_points(
    raw_points: Sequence[Mapping[str, object]], label: str
) -> list[dict[str, object]]:
    if not raw_points:
        raise ValueError(f"{label} must contain at least one point")
    points: list[dict[str, object]] = []
    previous = -1.0
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} point must be an object")
        _exact_keys(raw, _TRACK_POINT_KEYS, f"{label} point")
        t = _unit_number(raw["t"], f"{label} point t")
        if t <= previous:
            raise ValueError(f"{label} point times must be strictly increasing")
        previous = t
        visible = raw["visible"]
        if type(visible) is not bool:
            raise ValueError(f"{label} point visible must be a JSON boolean")
        if visible:
            x = _unit_number(raw["x"], f"{label} point x")
            y = _unit_number(raw["y"], f"{label} point y")
        else:
            if raw["x"] is not None or raw["y"] is not None:
                raise ValueError(f"{label} occluded point requires null x and y")
            x = y = None
        points.append({"t": t, "x": x, "y": y, "visible": visible})
    return points


def _sample_point(points: list[dict[str, object]], t: float) -> tuple[str, dict[str, float] | None]:
    tolerance = 1e-12
    for point in points:
        point_t = float(point["t"])
        if abs(point_t - t) <= tolerance:
            if point["visible"]:
                return "visible", {
                    "x": float(point["x"]),
                    "y": float(point["y"]),
                }
            return "occluded", None
    if t < float(points[0]["t"]) or t > float(points[-1]["t"]):
        return "outside_track", None
    for left, right in zip(points, points[1:]):
        left_t, right_t = float(left["t"]), float(right["t"])
        if left_t < t < right_t:
            if not left["visible"] or not right["visible"]:
                return "occluded_gap", None
            fraction = (t - left_t) / (right_t - left_t)
            return "visible", {
                "x": float(left["x"]) + fraction * (float(right["x"]) - float(left["x"])),
                "y": float(left["y"]) + fraction * (float(right["y"]) - float(left["y"])),
            }
    raise ValueError("unable to resample trajectory point")


def _distance(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    return math.hypot(left["x"] - right["x"], left["y"] - right["y"])


def _dtw(
    requested: list[dict[str, float]], observed: list[dict[str, float]]
) -> dict[str, object]:
    rows, columns = len(requested), len(observed)
    distances = [
        [_distance(requested[row], observed[column]) for column in range(columns)]
        for row in range(rows)
    ]
    costs = [[math.inf] * columns for _ in range(rows)]
    predecessors: list[list[tuple[int, int] | None]] = [
        [None] * columns for _ in range(rows)
    ]
    for row in range(rows):
        for column in range(columns):
            choices: list[tuple[float, tuple[int, int] | None]] = []
            if row == 0 and column == 0:
                choices.append((0.0, None))
            if row > 0:
                choices.append((costs[row - 1][column], (row - 1, column)))
            if column > 0:
                choices.append((costs[row][column - 1], (row, column - 1)))
            if row > 0 and column > 0:
                choices.append((costs[row - 1][column - 1], (row - 1, column - 1)))
            prior_cost, predecessor = min(choices, key=lambda item: item[0])
            costs[row][column] = distances[row][column] + prior_cost
            predecessors[row][column] = predecessor

    row, column = rows - 1, columns - 1
    path: list[dict[str, object]] = []
    while True:
        path.append(
            {
                "requested_index": row,
                "observed_index": column,
                "distance": distances[row][column],
                "normalized_distance": distances[row][column] / math.sqrt(2.0),
            }
        )
        predecessor = predecessors[row][column]
        if predecessor is None:
            break
        row, column = predecessor
    path.reverse()
    cost = costs[-1][-1]
    return {
        "cost": cost,
        "normalized_cost": cost / len(path) / math.sqrt(2.0),
        "path_length": len(path),
        "path": path,
        "distance_matrix": distances,
        "accumulated_cost_matrix": costs,
    }


def _visible_segments(samples: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    segments: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for sample in samples:
        if sample["status"] == "compared":
            current.append(sample)
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def evaluate_tracks(
    requested_points: Sequence[Mapping[str, object]],
    observed_points: Sequence[Mapping[str, object]],
    *,
    sample_count: int,
    arrival_tolerance: float = 0.05,
) -> dict[str, object]:
    """Resample and compare two normalized tracks without bridging occlusion."""

    requested = _validate_points(requested_points, "requested track")
    observed = _validate_points(observed_points, "observed track")
    if (
        type(sample_count) is not int
        or sample_count < 1
        or sample_count > MAX_SAMPLE_COUNT
    ):
        raise ValueError(
            f"sample_count must be an integer in [1, {MAX_SAMPLE_COUNT}]"
        )
    tolerance = _unit_number(arrival_tolerance, "arrival_tolerance")
    requested_endpoint_point = next(
        (point for point in reversed(requested) if point["visible"]), None
    )
    if sample_count == 1:
        times = [
            float(requested_endpoint_point["t"])
            if requested_endpoint_point is not None
            else float(requested[0]["t"])
        ]
    else:
        times = [index / (sample_count - 1) for index in range(sample_count)]
    samples: list[dict[str, object]] = []
    for t in times:
        requested_status, requested_value = _sample_point(requested, t)
        observed_status, observed_value = _sample_point(observed, t)
        if requested_status == "visible" and observed_status == "visible":
            assert requested_value is not None and observed_value is not None
            distance = _distance(requested_value, observed_value)
            status = "compared"
        else:
            distance = None
            if observed_status in {"occluded", "occluded_gap"}:
                status = observed_status
            elif requested_status != "visible":
                status = f"requested_{requested_status}"
            else:
                status = observed_status
        samples.append(
            {
                "sample_index": len(samples),
                "t": t,
                "requested": requested_value,
                "observed": observed_value,
                "status": status,
                "distance": distance,
                "normalized_distance": None if distance is None else distance / math.sqrt(2.0),
            }
        )

    compared = [sample for sample in samples if sample["status"] == "compared"]
    distances = [float(sample["distance"]) for sample in compared]
    if requested_endpoint_point is None:
        endpoint_t = None
        endpoint_status = "unavailable"
        endpoint_reason = "requested_track_has_no_visible_endpoint"
        endpoint_error = None
    else:
        endpoint_t = float(requested_endpoint_point["t"])
        requested_endpoint_value = {
            "x": float(requested_endpoint_point["x"]),
            "y": float(requested_endpoint_point["y"]),
        }
        observed_endpoint_status, observed_endpoint_value = _sample_point(
            observed, endpoint_t
        )
        if observed_endpoint_status == "visible":
            assert observed_endpoint_value is not None
            endpoint_status = "compared"
            endpoint_reason = None
            endpoint_error = _distance(
                requested_endpoint_value, observed_endpoint_value
            )
        else:
            endpoint_status = observed_endpoint_status
            endpoint_reason = {
                "occluded": "observed_endpoint_occluded",
                "occluded_gap": "observed_endpoint_occluded_gap",
                "outside_track": "observed_endpoint_outside_track",
            }.get(
                observed_endpoint_status,
                f"observed_endpoint_{observed_endpoint_status}",
            )
            endpoint_error = None

    direction_dot = 0.0
    requested_norm_sq = 0.0
    observed_norm_sq = 0.0
    for segment in _visible_segments(samples):
        for left, right in zip(segment, segment[1:]):
            requested_left = left["requested"]
            requested_right = right["requested"]
            observed_left = left["observed"]
            observed_right = right["observed"]
            assert isinstance(requested_left, Mapping) and isinstance(requested_right, Mapping)
            assert isinstance(observed_left, Mapping) and isinstance(observed_right, Mapping)
            requested_delta = (
                float(requested_right["x"]) - float(requested_left["x"]),
                float(requested_right["y"]) - float(requested_left["y"]),
            )
            observed_delta = (
                float(observed_right["x"]) - float(observed_left["x"]),
                float(observed_right["y"]) - float(observed_left["y"]),
            )
            direction_dot += requested_delta[0] * observed_delta[0] + requested_delta[1] * observed_delta[1]
            requested_norm_sq += requested_delta[0] ** 2 + requested_delta[1] ** 2
            observed_norm_sq += observed_delta[0] ** 2 + observed_delta[1] ** 2
    if requested_norm_sq <= 1e-15 or observed_norm_sq <= 1e-15:
        direction_cosine = None
        direction_match = None
    else:
        direction_cosine = max(
            -1.0,
            min(1.0, direction_dot / math.sqrt(requested_norm_sq * observed_norm_sq)),
        )
        direction_match = direction_cosine > 0.0

    requested_endpoint = (
        {
            "x": float(requested_endpoint_point["x"]),
            "y": float(requested_endpoint_point["y"]),
        }
        if requested_endpoint_point is not None
        else None
    )
    requested_arrival = None
    observed_arrival = None
    if requested_endpoint is not None:
        for sample in samples:
            value = sample["requested"]
            if isinstance(value, Mapping) and _distance(value, requested_endpoint) <= tolerance:
                requested_arrival = float(sample["t"])
                break
        for sample in samples:
            value = sample["observed"]
            if sample["status"] == "compared" and isinstance(value, Mapping) and _distance(value, requested_endpoint) <= tolerance:
                observed_arrival = float(sample["t"])
                break
    arrival_error = (
        abs(observed_arrival - requested_arrival)
        if observed_arrival is not None and requested_arrival is not None
        else None
    )

    dtw_segments: list[dict[str, object]] = []
    for segment_index, segment in enumerate(_visible_segments(samples)):
        requested_values = [dict(sample["requested"]) for sample in segment]  # type: ignore[arg-type]
        observed_values = [dict(sample["observed"]) for sample in segment]  # type: ignore[arg-type]
        segment_result = _dtw(requested_values, observed_values)
        segment_result["segment_index"] = segment_index
        segment_result["sample_indices"] = [sample["sample_index"] for sample in segment]
        dtw_segments.append(segment_result)
    if len(dtw_segments) == 1:
        dtw_report = dict(dtw_segments[0])
        dtw_report["segments"] = dtw_segments
    elif dtw_segments:
        total_cost = sum(float(segment["cost"]) for segment in dtw_segments)
        total_path = sum(int(segment["path_length"]) for segment in dtw_segments)
        dtw_report = {
            "cost": total_cost,
            "normalized_cost": total_cost / total_path / math.sqrt(2.0),
            "path_length": total_path,
            "path": [],
            "distance_matrix": [],
            "accumulated_cost_matrix": [],
            "segments": dtw_segments,
        }
    else:
        dtw_report = {
            "cost": None,
            "normalized_cost": None,
            "path_length": 0,
            "path": [],
            "distance_matrix": [],
            "accumulated_cost_matrix": [],
            "segments": [],
        }

    aggregate = {
        "requested_sample_count": sample_count,
        "compared_sample_count": len(compared),
        "occluded_sample_count": sum(
            sample["status"] in {"occluded", "occluded_gap"} for sample in samples
        ),
        "mean_distance": sum(distances) / len(distances) if distances else None,
        "mean_normalized_distance": (
            sum(distances) / len(distances) / math.sqrt(2.0) if distances else None
        ),
        "max_distance": max(distances) if distances else None,
        "endpoint_t": endpoint_t,
        "endpoint_status": endpoint_status,
        "endpoint_reason": endpoint_reason,
        "endpoint_error": endpoint_error,
        "normalized_endpoint_error": (
            endpoint_error / math.sqrt(2.0) if endpoint_error is not None else None
        ),
        "direction_cosine": direction_cosine,
        "direction_match": direction_match,
        "requested_arrival_t": requested_arrival,
        "observed_arrival_t": observed_arrival,
        "arrival_error": arrival_error,
        "arrival_tolerance": tolerance,
        "dtw_normalized_cost": dtw_report["normalized_cost"],
    }
    return {
        "time_base": {
            "type": (
                "requested_endpoint_single"
                if sample_count == 1
                else "normalized_uniform"
            ),
            "sample_count": sample_count,
        },
        "occlusion_policy": "visible endpoints only; never interpolate across an occluded point",
        "samples": samples,
        "dtw": dtw_report,
        "aggregate": aggregate,
    }


def _validate_annotation(value: Mapping[str, object]) -> None:
    _exact_keys(value, _ANNOTATION_KEYS, "annotation")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("annotation schema_version mismatch")
    if value["evidence_type"] != EVIDENCE_TYPE:
        raise ValueError("annotation is not manual_visual_annotation evidence")
    if value["coordinate_space"] != COORDINATE_SPACE:
        raise ValueError("annotation coordinate_space mismatch")
    target = value["target"]
    if not isinstance(target, Mapping):
        raise ValueError("annotation target must be an object")
    _exact_keys(target, _TARGET_KEYS, "annotation target")
    raw_points = value["points"]
    if not isinstance(raw_points, list) or not raw_points:
        raise ValueError("annotation points must be a non-empty list")
    previous_frame = -1
    previous_t = -1.0
    for point in raw_points:
        if not isinstance(point, Mapping):
            raise ValueError("annotation point must be an object")
        _exact_keys(point, _ANNOTATION_POINT_KEYS, "annotation point")
        frame = point["frame"]
        if type(frame) is not int or frame <= previous_frame:
            raise ValueError("annotation frames must be strictly increasing integers")
        previous_frame = frame
        t = _unit_number(point["t"], "annotation point t")
        if t <= previous_t:
            raise ValueError("annotation times must be strictly increasing")
        previous_t = t
        seconds = point["time_seconds"]
        try:
            converted_seconds = (
                float(seconds) if not isinstance(seconds, bool) else math.nan
            )
        except (OverflowError, TypeError, ValueError):
            converted_seconds = math.nan
        if not math.isfinite(converted_seconds) or converted_seconds < 0:
            raise ValueError("annotation point time_seconds must be finite and non-negative")
        if type(point["visible"]) is not bool:
            raise ValueError("annotation point visible must be a JSON boolean")
        if point["visible"]:
            _unit_number(point["x"], "annotation point x")
            _unit_number(point["y"], "annotation point y")
        elif point["x"] is not None or point["y"] is not None:
            raise ValueError("occluded annotation point requires null x and y")


def _safe_frame_path(session_root: Path, declared: object) -> Path:
    if not isinstance(declared, str) or not declared:
        raise ValueError("session frame path must be a non-empty string")
    relative = Path(declared)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("session frame path escapes session directory")
    resolved = (session_root / relative).resolve(strict=False)
    try:
        resolved.relative_to(session_root.resolve())
    except ValueError as exc:
        raise ValueError("session frame path escapes session directory") from exc
    return resolved


def _snapshot_source(
    source: Path,
    destination: Path,
    label: str,
    guards: list[tuple[Path, str, int, str]],
) -> tuple[str, int]:
    """Copy one source and bind subsequent work to the copied bytes."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    digest = sha256_file(destination)
    byte_count = destination.stat().st_size
    if (
        not source.is_file()
        or source.stat().st_size != byte_count
        or sha256_file(source) != digest
    ):
        raise ValueError(f"{label} changed during evaluation snapshot")
    guards.append((source, digest, byte_count, label))
    return digest, byte_count


def _verify_source_guards(guards: Sequence[tuple[Path, str, int, str]]) -> None:
    for source, expected_sha, expected_bytes, label in guards:
        if (
            not source.is_file()
            or source.stat().st_size != expected_bytes
            or sha256_file(source) != expected_sha
        ):
            raise ValueError(f"{label} changed during evaluation")


def _evaluate_files_with_sources(
    trajectory_path: Path,
    video_path: Path,
    session_manifest_path: Path,
    annotation_path: Path,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    """Verify all source bytes and calculate one manual trajectory report."""

    trajectory_source = Path(trajectory_path).resolve()
    video = Path(video_path).resolve()
    manifest_path = Path(session_manifest_path).resolve()
    annotation_source = Path(annotation_path).resolve()
    for path, label in (
        (trajectory_source, "trajectory"),
        (video, "video"),
        (manifest_path, "session manifest"),
        (annotation_source, "annotation"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} is not a file: {path}")

    guards: list[tuple[Path, str, int, str]] = []
    with tempfile.TemporaryDirectory(prefix="videoactagent-eval-snapshot-") as directory:
        snapshot_root = Path(directory)
        trajectory_snapshot = snapshot_root / "trajectory.json"
        video_snapshot = snapshot_root / f"video{video.suffix}"
        manifest_snapshot = snapshot_root / "session_manifest.json"
        annotation_snapshot = snapshot_root / "manual_annotation.json"
        trajectory_file_sha, _ = _snapshot_source(
            trajectory_source, trajectory_snapshot, "trajectory", guards
        )
        actual_video_sha, actual_video_bytes = _snapshot_source(
            video, video_snapshot, "video", guards
        )
        manifest_sha, _ = _snapshot_source(
            manifest_path, manifest_snapshot, "session manifest", guards
        )
        annotation_sha, _ = _snapshot_source(
            annotation_source, annotation_snapshot, "annotation", guards
        )

        trajectory_bytes = trajectory_snapshot.read_bytes()
        instruction = TrajectoryInstruction.from_json_bytes(trajectory_bytes)
        manifest = load_strict_object(manifest_snapshot, "session manifest")
        annotation = load_strict_object(annotation_snapshot, "annotation")
        validate_session_manifest(manifest)
        _validate_annotation(annotation)
        trajectory_canonical_sha = hashlib.sha256(
            canonical_bytes(instruction)
        ).hexdigest()

        if (
            manifest["video_sha256"] != actual_video_sha
            or annotation["video_sha256"] != actual_video_sha
        ):
            raise ValueError("video SHA provenance mismatch")
        if manifest["video_bytes"] != actual_video_bytes:
            raise ValueError("video byte-count provenance mismatch")
        if (
            manifest["trajectory_file_sha256"] != trajectory_file_sha
            or annotation["trajectory_file_sha256"] != trajectory_file_sha
        ):
            raise ValueError("trajectory file SHA provenance mismatch")
        if (
            manifest["trajectory_canonical_sha256"] != trajectory_canonical_sha
            or annotation["trajectory_canonical_sha256"]
            != trajectory_canonical_sha
        ):
            raise ValueError("trajectory canonical SHA provenance mismatch")
        if annotation["session_manifest_sha256"] != manifest_sha:
            raise ValueError("session manifest SHA provenance mismatch")
        if (
            manifest["scene_id"] != instruction.scene_id
            or annotation["scene_id"] != instruction.scene_id
        ):
            raise ValueError("scene provenance mismatch")
        if (
            manifest["shot_id"] != instruction.shot_id
            or annotation["shot_id"] != instruction.shot_id
        ):
            raise ValueError("shot provenance mismatch")
        if manifest["track_id"] != annotation["track_id"]:
            raise ValueError("track provenance mismatch")
        matching_tracks = [
            track
            for track in instruction.tracks
            if track.track_id == annotation["track_id"]
        ]
        if len(matching_tracks) != 1:
            raise ValueError("track provenance mismatch")
        track = matching_tracks[0]
        if (
            manifest["target"] != track.target.to_dict()
            or annotation["target"] != track.target.to_dict()
        ):
            raise ValueError("target provenance mismatch")

        frames = manifest["frames"]
        points = annotation["points"]
        assert isinstance(frames, list) and isinstance(points, list)
        if len(frames) != len(points):
            raise ValueError("annotation/frame count provenance mismatch")
        session_root = manifest_path.parent
        frame_snapshots: list[Path] = []
        for index, frame in enumerate(frames):
            assert isinstance(frame, Mapping)
            frame_source = _safe_frame_path(session_root, frame["path"])
            if not frame_source.is_file():
                raise ValueError("frame source is missing")
            frame_snapshot = snapshot_root / "source_frames" / f"{index:06d}.png"
            frame_sha, _ = _snapshot_source(
                frame_source, frame_snapshot, f"frame {frame['frame']}", guards
            )
            if frame_sha != frame["sha256"]:
                raise ValueError("frame SHA provenance mismatch")
            frame_snapshots.append(frame_snapshot)

        decoded_root = snapshot_root / "decoded"
        (decoded_root / "frames").mkdir(parents=True)
        video_metadata, decoded_frames = _decode_selected_frames(
            video_snapshot,
            tuple(int(frame["frame"]) for frame in frames),  # type: ignore[index]
            decoded_root / "frames",
        )
        if len(decoded_frames) != len(frames):
            raise ValueError("decoded frame count provenance mismatch")
        for recorded, decoded in zip(frames, decoded_frames):
            assert isinstance(recorded, Mapping)
            if recorded["frame"] != decoded["frame"]:
                raise ValueError("frame index provenance mismatch")
            if not math.isclose(
                float(recorded["t"]),
                float(decoded["t"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("frame t provenance mismatch")
            if not math.isclose(
                float(recorded["time_seconds"]),
                float(decoded["time_seconds"]),
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError("frame time_seconds provenance mismatch")
            if recorded["sha256"] != decoded["sha256"]:
                raise ValueError("decoded frame provenance mismatch")
            if (
                recorded["width"] != decoded["width"]
                or recorded["height"] != decoded["height"]
            ):
                raise ValueError("frame dimensions provenance mismatch")
        if manifest["video_frame_count"] != video_metadata["frame_count"]:
            raise ValueError("video frame-count provenance mismatch")
        if manifest["video_size"] != video_metadata["size"]:
            raise ValueError("video dimensions provenance mismatch")
        if not math.isclose(
            float(manifest["video_fps"]),
            float(video_metadata["fps"]),
            rel_tol=1e-9,
        ):
            raise ValueError("video fps provenance mismatch")
        if not math.isclose(
            float(manifest["video_duration_seconds"]),
            float(video_metadata["duration_seconds"]),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("video duration provenance mismatch")

        for frame, point, frame_snapshot in zip(frames, points, frame_snapshots):
            assert isinstance(frame, Mapping) and isinstance(point, Mapping)
            try:
                with Image.open(frame_snapshot) as image:
                    image.load()
                    actual_size = [image.width, image.height]
            except OSError as exc:
                raise ValueError("frame PNG decode failed") from exc
            if (
                actual_size != [frame["width"], frame["height"]]
                or actual_size != manifest["video_size"]
            ):
                raise ValueError("frame dimensions provenance mismatch")
            for field in ("frame", "t", "time_seconds"):
                if point[field] != frame[field]:
                    raise ValueError(f"frame {field} provenance mismatch")
            if point["frame_sha256"] != frame["sha256"]:
                raise ValueError("frame SHA provenance mismatch")

        requested_points = [point.to_dict() for point in track.points]
        observed_points = [
            {
                "t": point["t"],
                "x": point["x"],
                "y": point["y"],
                "visible": point["visible"],
            }
            for point in points
        ]
        metrics = evaluate_tracks(
            requested_points,
            observed_points,
            sample_count=instruction.sample_count,
        )
        report = {
            "schema_version": SCHEMA_VERSION,
            "evidence_type": "manual_visual_trajectory_evaluation",
            "coordinate_space": COORDINATE_SPACE,
            "scene_id": instruction.scene_id,
            "shot_id": instruction.shot_id,
            "track_id": track.track_id,
            "target": track.target.to_dict(),
            "provenance": {
                "video_sha256": actual_video_sha,
                "trajectory_file_sha256": trajectory_file_sha,
                "trajectory_canonical_sha256": trajectory_canonical_sha,
                "session_manifest_sha256": manifest_sha,
                "annotation_sha256": annotation_sha,
                "frame_sha256": [frame["sha256"] for frame in frames],
            },
            "metrics": metrics,
        }
        _verify_source_guards(guards)
        source_paths = tuple(dict.fromkeys(source for source, _, _, _ in guards))
        return report, source_paths


def evaluate_files(
    trajectory_path: Path,
    video_path: Path,
    session_manifest_path: Path,
    annotation_path: Path,
) -> dict[str, object]:
    """Return the evaluation report while keeping source paths internal."""

    report, _ = _evaluate_files_with_sources(
        trajectory_path,
        video_path,
        session_manifest_path,
        annotation_path,
    )
    return report


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _safe_output(path: Path, workspace: Path) -> Path:
    root = workspace.resolve()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output must stay below workspace") from exc
    return resolved


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("evaluate",))
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--session-manifest", type=Path, required=True)
    parser.add_argument("--annotation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = _safe_output(args.output, args.workspace)
        report, protected = _evaluate_files_with_sources(
            args.trajectory,
            args.video,
            args.session_manifest,
            args.annotation,
        )
        if output.is_dir():
            raise ValueError("output must be a file path, not a directory")
        if any(_same_file(output, source) for source in protected):
            raise ValueError("output collides with an input")
        data = _json_bytes(report)
        _atomic_write(output, data)
    except (OSError, TypeError, ValueError) as exc:
        print(f"TRAJECTORY_EVAL_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"TRAJECTORY_EVAL_OK output={output} "
        f"sha256={hashlib.sha256(data).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
