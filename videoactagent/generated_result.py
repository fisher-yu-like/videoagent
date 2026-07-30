"""Fail-closed inspection and observation preparation for generated videos.

This module deliberately treats an existing MP4 as untrusted input.  A result
is usable only after ffmpeg has decoded its declared key frames and its real
media profile has met the requested duration/resolution contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import imageio_ffmpeg

from videoactagent.camera_eval import evaluate_camera_motion
from videoactagent.module_io import inspect_video, sha256_file
from videoactagent.trajectory import TrajectoryInstruction
from videoactagent.trajectory_observe import prepare_session
from videoactagent.whole_story import evaluate_output_completeness


@dataclass(frozen=True)
class ExpectedProfile:
    """The requested media contract, including permitted duration coverage."""

    duration_seconds: float
    resolution: tuple[int, int]
    minimum_coverage: float = 0.95
    maximum_coverage: float = 1.05

    def __post_init__(self) -> None:
        duration = _positive_float(self.duration_seconds, "duration_seconds")
        minimum = _positive_float(self.minimum_coverage, "minimum_coverage")
        maximum = _positive_float(self.maximum_coverage, "maximum_coverage")
        if minimum > maximum:
            raise ValueError("minimum_coverage must not exceed maximum_coverage")
        if (
            not isinstance(self.resolution, tuple)
            or len(self.resolution) != 2
            or any(type(value) is not int or value <= 0 for value in self.resolution)
        ):
            raise ValueError("resolution must contain two positive integers")
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "minimum_coverage", minimum)
        object.__setattr__(self, "maximum_coverage", maximum)


def _positive_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return result


def _five_frame_indices(frame_count: int) -> list[int]:
    if type(frame_count) is not int or frame_count <= 0:
        raise ValueError("video decoded zero frames")
    last = frame_count - 1
    return [0, round(0.25 * last), round(0.5 * last), round(0.75 * last), last]


def _close_reader(reader: object) -> None:
    generator_frame = getattr(reader, "gi_frame", None)
    process = generator_frame.f_locals.get("process") if generator_frame else None
    try:
        reader.close()  # type: ignore[attr-defined]
    finally:
        if process is not None:
            for pipe_name in ("stdin", "stdout", "stderr"):
                pipe = getattr(process, pipe_name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _decode_key_frame_indices(video: Path, indices: list[int]) -> dict[str, object]:
    """Decode every requested key frame, rather than trusting container metadata."""

    selected = set(indices)
    decoded: list[int] = []
    reader = imageio_ffmpeg.read_frames(str(video), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        size = metadata.get("size") if isinstance(metadata, dict) else None
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise ValueError("key-frame decode has no frame size")
        width, height = int(size[0]), int(size[1])
        expected_bytes = width * height * 3
        for index, frame in enumerate(reader):
            if index not in selected:
                continue
            if len(frame) != expected_bytes:
                raise ValueError(f"decoded key frame {index} has an invalid byte count")
            decoded.append(index)
            if index == indices[-1]:
                break
    finally:
        _close_reader(reader)
    missing = [index for index in indices if index not in decoded]
    return {
        "indices": indices,
        "decoded_indices": decoded,
        "decodable": not missing,
        "missing_indices": missing,
    }


def inspect_generated_video(
    video: Path | str,
    requested_duration: float,
    expected_resolution: tuple[int, int],
) -> dict[str, object]:
    """Decode and gate one generated MP4 without treating file existence as proof."""

    requested = _positive_float(requested_duration, "requested_duration")
    if (
        not isinstance(expected_resolution, tuple)
        or len(expected_resolution) != 2
        or any(type(value) is not int or value <= 0 for value in expected_resolution)
    ):
        raise ValueError("expected_resolution must contain two positive integers")
    source = Path(video).resolve()
    if not source.is_file():
        raise ValueError(f"video is not a file: {source}")

    before_sha256 = sha256_file(source)
    media = inspect_video(source)
    frame_count = media["frame_count"]
    if type(frame_count) is not int or frame_count <= 0:
        raise ValueError("video decoded zero frames")
    key_frames = _decode_key_frame_indices(source, _five_frame_indices(frame_count))
    after_sha256 = sha256_file(source)
    if before_sha256 != after_sha256:
        raise ValueError("MP4 hash changed during generated-result inspection")

    counted_duration = float(media["duration_seconds"])
    size = media["size"]
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("decoded video size is unavailable")
    completeness = evaluate_output_completeness(
        requested_duration=requested,
        decoded_duration=counted_duration,
        decoded_resolution=(int(size[0]), int(size[1])),
        expected_resolution=expected_resolution,
        key_times_decodable=bool(key_frames["decodable"]),
        # This is a media gate.  Publishing re-evaluates with only genuine
        # manual annotations, so interpolation can never enable scoring.
        annotated_frame_count=1,
        interpolation_sample_count=0,
    )
    media_complete = completeness["status"] != "incomplete"
    return {
        "schema_version": "0.1",
        "status": "media_complete" if media_complete else "incomplete",
        "movement_scoring_allowed": bool(media_complete),
        "requested_duration_seconds": requested,
        "frame_count": frame_count,
        "counted_duration_seconds": counted_duration,
        "stream_duration_seconds": float(media["stream_duration_seconds"]),
        "fps": float(media["fps"]),
        "codec": media["codec"],
        "size": [int(size[0]), int(size[1])],
        "mp4_sha256": before_sha256,
        "duration_coverage": completeness["duration_coverage"],
        "resolution_matches": completeness["resolution_matches"],
        "key_times_decodable": bool(key_frames["decodable"]),
        "key_frame_indices": key_frames["indices"],
        "decoded_key_frame_indices": key_frames["decoded_indices"],
        "missing_key_frame_indices": key_frames["missing_indices"],
    }


def _expected_trajectory(job_dir: Path) -> Path:
    candidates = [job_dir / "expected_trajectory.json", job_dir / "trajectory.json"]
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise ValueError("job directory must contain exactly one expected trajectory JSON")
    return found[0]


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _camera_assessment(session_dir: Path, frame_indices: list[int]) -> dict[str, object]:
    manifest = json.loads((session_dir / "session_manifest.json").read_text(encoding="utf-8"))
    frame_map = {item["frame"]: item for item in manifest["frames"]}
    first, middle, last = (frame_map[index] for index in (frame_indices[0], frame_indices[2], frame_indices[-1]))
    try:
        assessment = evaluate_camera_motion(
            session_dir / first["path"],
            session_dir / middle["path"],
            session_dir / last["path"],
            expected_motion="truck_right",
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "evidence_type": "heuristic_phase_correlation",
            "status": "unavailable",
            "manual_review_required": True,
            "detail": f"{type(exc).__name__}: {exc}",
            "limitations": ["camera heuristic must be manually reviewed"],
        }
    assessment["manual_review_required"] = True
    assessment["manual_review_reason"] = (
        "phase correlation is heuristic evidence, not camera-pose ground truth"
    )
    return assessment


def publish_result_report(
    job_dir: Path | str, video: Path | str, profile: ExpectedProfile
) -> dict[str, object]:
    """Atomically publish observation sessions only for a complete hash-stable MP4."""

    if not isinstance(profile, ExpectedProfile):
        raise ValueError("profile must be an ExpectedProfile")
    destination = Path(job_dir).resolve()
    if not destination.is_dir():
        raise ValueError(f"job_dir is not a directory: {destination}")
    source = Path(video).resolve()
    trajectory_source = _expected_trajectory(destination)
    if any((destination / name).exists() for name in ("private_source.mp4", "manual_sessions", "generated_result_report.json")):
        raise FileExistsError("generated-result artifacts already exist")

    source_hash = sha256_file(source) if source.is_file() else None
    inspection = inspect_generated_video(source, profile.duration_seconds, profile.resolution)
    if inspection["status"] != "media_complete":
        raise ValueError("generated video is incomplete; observation preparation is rejected")
    if source_hash != inspection["mp4_sha256"]:
        raise ValueError("MP4 hash changed before observation preparation")

    instruction = TrajectoryInstruction.from_json_bytes(trajectory_source.read_bytes())
    actor_tracks = [track for track in instruction.tracks if track.target_type == "actor"]
    if not actor_tracks:
        raise ValueError("expected trajectory contains no actor tracks")
    actor_ids = [track.target_id for track in actor_tracks]
    if len(set(actor_ids)) != len(actor_ids):
        raise ValueError("expected trajectory must contain one track per actor ID")
    interpolation_samples = int(instruction.sample_count)
    frame_indices = list(inspection["key_frame_indices"])
    if len(set(frame_indices)) != len(frame_indices):
        raise ValueError("five observation frames require at least five decoded frames")

    staging = destination / f".generated-result.{uuid4().hex}.tmp"
    try:
        staging.mkdir()
        snapshot = staging / "private_source.mp4"
        trajectory_snapshot = staging / "expected_trajectory.json"
        shutil.copyfile(source, snapshot)
        shutil.copyfile(trajectory_source, trajectory_snapshot)
        if sha256_file(snapshot) != source_hash or sha256_file(trajectory_snapshot) != sha256_file(trajectory_source):
            raise ValueError("input changed while creating private observation snapshot")

        session_root = staging / "manual_sessions"
        sessions: list[dict[str, object]] = []
        for track in actor_tracks:
            session_path = session_root / track.target_id
            manifest = prepare_session(
                snapshot,
                trajectory_snapshot,
                track.track_id,
                session_path,
                tuple(frame_indices),
                workspace=staging,
            )
            sessions.append(
                {
                    "actor_id": track.target_id,
                    "track_id": track.track_id,
                    "path": (Path("manual_sessions") / track.target_id).as_posix(),
                    "video_sha256": manifest["video_sha256"],
                    "manual_annotation_count": 0,
                }
            )
        assessment = _camera_assessment(session_root / actor_tracks[0].target_id, frame_indices)
        if not source.is_file() or sha256_file(source) != source_hash:
            raise ValueError("MP4 hash changed during observation preparation")
        if sha256_file(snapshot) != source_hash:
            raise ValueError("private MP4 snapshot hash changed during observation preparation")

        completeness = evaluate_output_completeness(
            requested_duration=profile.duration_seconds,
            decoded_duration=float(inspection["counted_duration_seconds"]),
            decoded_resolution=tuple(inspection["size"]),  # type: ignore[arg-type]
            expected_resolution=profile.resolution,
            key_times_decodable=bool(inspection["key_times_decodable"]),
            annotated_frame_count=0,
            interpolation_sample_count=interpolation_samples,
            threshold=profile.minimum_coverage,
            maximum_coverage=profile.maximum_coverage,
        )
        report: dict[str, object] = {
            **inspection,
            **completeness,
            "schema_version": "0.1",
            "profile": {
                "duration_seconds": profile.duration_seconds,
                "resolution": list(profile.resolution),
                "minimum_coverage": profile.minimum_coverage,
                "maximum_coverage": profile.maximum_coverage,
            },
            "source_snapshot": {"path": "private_source.mp4", "sha256": source_hash},
            "expected_trajectory_sha256": sha256_file(trajectory_snapshot),
            "manual_sessions": sessions,
            "camera_assessment": assessment,
        }
        _atomic_write_json(staging / "generated_result_report.json", report)
        for name in ("private_source.mp4", "manual_sessions", "generated_result_report.json"):
            os.replace(staging / name, destination / name)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)
