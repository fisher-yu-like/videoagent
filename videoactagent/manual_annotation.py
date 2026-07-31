"""Prepare and serve a provenance-bound human comparison annotation workspace.

This module decodes real media and records only explicit human decisions.  It
does not track actors automatically and does not infer three-dimensional camera
poses from generated videos.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import imageio_ffmpeg
from PIL import Image


SCHEMA_VERSION = "1.0"
NORMALIZED_TIMEPOINTS = (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0)
VISIBILITY_VALUES = frozenset({"visible", "occluded", "out_of_frame", "ambiguous"})
IDENTITY_VALUES = frozenset({"confirmed", "probable", "ambiguous", "unknown"})
ANNOTATION_STATES = frozenset({"draft", "reviewed"})
CAMERA_CATEGORIES = frozenset(
    {
        "static",
        "pan_left",
        "pan_right",
        "tilt_up",
        "tilt_down",
        "dolly_in",
        "dolly_out",
        "truck_left",
        "truck_right",
        "pedestal_up",
        "pedestal_down",
        "zoom_in",
        "zoom_out",
        "handheld_or_complex",
        "ambiguous",
    }
)
CAMERA_INTENSITIES = frozenset({"none", "low", "medium", "high", "ambiguous"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", flags=re.ASCII)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _parse_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _load_object(path: Path, label: str) -> dict[str, Any]:
    return _parse_object(path.read_bytes(), label)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _below(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must stay below {root}") from exc
    return resolved


def _output_path(path: Path, workspace: Path | None) -> Path:
    if workspace is None:
        return path.resolve(strict=False)
    root = workspace.resolve()
    candidate = path if path.is_absolute() else root / path
    return _below(candidate, root, "output")


def _safe_token(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or _SAFE_TOKEN.fullmatch(value) is None
    ):
        raise ValueError(
            f"{label} must be a safe token matching "
            "[A-Za-z0-9][A-Za-z0-9_.-]* and cannot be . or .."
        )
    return value


def _close_reader(reader: object) -> None:
    frame = getattr(reader, "gi_frame", None)
    process = frame.f_locals.get("process") if frame else None
    try:
        reader.close()  # type: ignore[attr-defined]
    finally:
        if process is not None:
            for name in ("stdin", "stdout"):
                pipe = getattr(process, name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def frame_indices_for_count(frame_count: int) -> tuple[int, ...]:
    """Map the fixed normalized protocol to real decoded frame indices."""

    if type(frame_count) is not int or frame_count < 9:
        raise ValueError("video must contain at least nine decodable frames")
    indices = tuple(round(t * (frame_count - 1)) for t in NORMALIZED_TIMEPOINTS)
    if len(set(indices)) != len(indices):
        raise ValueError("video is too short for nine unique normalized samples")
    return indices


def _decode_video(
    video_path: Path, destination: Path, relative_root: Path
) -> dict[str, object]:
    """Decode exactly the protocol frames while returning actual media metadata."""

    counted_frames, counted_seconds = imageio_ffmpeg.count_frames_and_secs(str(video_path))
    indices = frame_indices_for_count(counted_frames)
    reader = imageio_ffmpeg.read_frames(str(video_path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        if not isinstance(metadata, Mapping):
            raise ValueError("ffmpeg video metadata is unavailable")
        size = metadata.get("size")
        fps = metadata.get("fps")
        if (
            not isinstance(size, (tuple, list))
            or len(size) != 2
            or any(type(value) is not int or value <= 0 for value in size)
        ):
            raise ValueError("video dimensions are unavailable")
        if (
            isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps))
            or fps <= 0
        ):
            raise ValueError("video fps is unavailable")
        width, height = int(size[0]), int(size[1])
        wanted = dict(zip(indices, NORMALIZED_TIMEPOINTS))
        frames: list[dict[str, object]] = []
        destination.mkdir(parents=True, exist_ok=False)
        for index, raw_frame in enumerate(reader):
            if index not in wanted:
                if index > indices[-1]:
                    break
                continue
            if len(raw_frame) != width * height * 3:
                raise ValueError(f"decoded frame {index} has an invalid byte count")
            name = f"frame_{index:06d}.png"
            path = destination / name
            Image.frombytes("RGB", (width, height), raw_frame).save(
                path, format="PNG", optimize=False
            )
            frames.append(
                {
                    "frame": index,
                    "t": wanted[index],
                    "time_seconds": index / float(fps),
                    "path": (relative_root / name).as_posix(),
                    "sha256": sha256_file(path),
                    "width": width,
                    "height": height,
                }
            )
            if index == indices[-1]:
                break
        if [frame["frame"] for frame in frames] != list(indices):
            raise ValueError("ffmpeg ended before all protocol frames were decoded")
        return {
            "sha256": sha256_file(video_path),
            "bytes": video_path.stat().st_size,
            "frame_count": counted_frames,
            "fps": float(fps),
            "duration_seconds": float(counted_seconds),
            "size": [width, height],
            "frames": frames,
        }
    finally:
        _close_reader(reader)


def _actor_ids(shotscript: Path) -> list[str]:
    value = _load_object(shotscript, "ShotScript")
    shots = value.get("shots")
    if not isinstance(shots, list) or len(shots) != 1 or not isinstance(shots[0], Mapping):
        raise ValueError("ShotScript must contain one whole-story shot")
    actors = shots[0].get("actors")
    if not isinstance(actors, list) or not actors:
        raise ValueError("ShotScript must declare at least one actor")
    result: list[str] = []
    for actor in actors:
        actor_id = actor.get("id") if isinstance(actor, Mapping) else None
        if not isinstance(actor_id, str) or not actor_id or actor_id in result:
            raise ValueError("ShotScript actor ids must be unique non-empty strings")
        result.append(actor_id)
    return result


def _decode_bound_video(
    source: Path,
    claimed_sha: object,
    staging: Path,
    relative_root: Path,
    mismatch_label: str,
) -> dict[str, object]:
    if not source.is_file():
        raise ValueError(f"video is not a file: {source}")
    before_size = source.stat().st_size
    before_sha = sha256_file(source)
    if claimed_sha is not None and claimed_sha != before_sha:
        raise ValueError(f"{mismatch_label} SHA-256 mismatch")
    media = _decode_video(source, staging / relative_root, relative_root)
    if (
        not source.is_file()
        or source.stat().st_size != before_size
        or sha256_file(source) != before_sha
    ):
        raise ValueError("source video changed during workspace preparation")
    return media


def prepare_workspace(
    index_path: Path,
    output_dir: Path,
    *,
    workspace: Path | None = None,
    proxy_root: Path | None = None,
) -> dict[str, object]:
    """Build a comparison worklist and extract real frames without annotations."""

    index = Path(index_path).resolve()
    output = _output_path(Path(output_dir), workspace)
    if not index.is_file():
        raise ValueError(f"full result index is not a file: {index}")
    if output.exists():
        raise FileExistsError(f"annotation workspace already exists: {output}")
    index_bytes = index.read_bytes()
    index_sha256 = hashlib.sha256(index_bytes).hexdigest()
    value = _parse_object(index_bytes, "full result index")
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("full result index rows must be a non-empty list")
    run_root = index.parent.parent.resolve()
    proxies = (
        Path(proxy_root).resolve()
        if proxy_root is not None
        else run_root / "source_evidence" / "whole_story_v4"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid4().hex}.tmp"
    staging.mkdir()
    try:
        references: dict[str, object] = {}
        items: list[dict[str, object]] = []
        seen_jobs: set[str] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ValueError("full result index row must be an object")
            story_id = raw.get("story_id")
            job_id = raw.get("job_id")
            backend = raw.get("backend")
            status = raw.get("status")
            if not all(
                isinstance(item, str) and item
                for item in (story_id, job_id, backend, status)
            ):
                raise ValueError("result rows require story_id, job_id, backend, and status")
            story_id = _safe_token(story_id, "story_id")
            job_id = _safe_token(job_id, "job_id")
            backend = _safe_token(backend, "backend")
            if job_id in seen_jobs:
                raise ValueError(f"duplicate job_id: {job_id}")
            seen_jobs.add(job_id)
            if story_id not in references:
                case_root = _below(proxies / story_id, proxies, "proxy case")
                proxy = _below(case_root / "proxy.mp4", case_root, "proxy video")
                shotscript = _below(
                    case_root / "sources" / "shotscript.json", case_root, "ShotScript"
                )
                actors = _actor_ids(shotscript)
                relative = Path("frames") / "reference" / story_id
                media = _decode_bound_video(
                    proxy, None, staging, relative, "proxy video"
                )
                references[story_id] = {
                    "reference_id": story_id,
                    "story_id": story_id,
                    "actor_ids": actors,
                    "video": media,
                    "annotation_path": f"annotations/reference__{story_id}.json",
                }
            reference = references[story_id]
            assert isinstance(reference, Mapping)
            item: dict[str, object] = {
                "job_id": job_id,
                "story_id": story_id,
                "backend": backend,
                "status": status,
                "reference_id": story_id,
                "actor_ids": reference["actor_ids"],
                "annotation_path": f"annotations/result__{job_id}.json",
            }
            if status != "success":
                item.update(
                    {
                        "disabled": True,
                        "failure_reason": raw.get("error", "generation did not succeed"),
                    }
                )
            else:
                declared = raw.get("video_path")
                if not isinstance(declared, str) or not declared:
                    raise ValueError(f"successful result {job_id} has no video_path")
                relative_source = Path(declared)
                if relative_source.is_absolute() or ".." in relative_source.parts:
                    raise ValueError("result video_path must be relative and cannot escape")
                source = _below(run_root / relative_source, run_root, "result video")
                relative = Path("frames") / "result" / job_id
                item["video"] = _decode_bound_video(
                    source,
                    raw.get("sha256"),
                    staging,
                    relative,
                    "result video",
                )
                item["disabled"] = False
            items.append(item)
        session: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "evidence_type": "human_comparison_annotation_session",
            "coordinate_space": "normalized_0_1_top_left",
            "camera_observation_space": "observable_background_motion_only",
            "normalized_timepoints": list(NORMALIZED_TIMEPOINTS),
            "source_index_sha256": index_sha256,
            "references": references,
            "items": items,
        }
        if not index.is_file() or index.read_bytes() != index_bytes:
            raise ValueError("full result index changed during workspace preparation")
        _atomic_write(staging / "session_manifest.json", _json_bytes(session))
        os.replace(staging, output)
        return session
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _unit(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    converted = float(value)
    if not math.isfinite(converted) or not 0.0 <= converted <= 1.0:
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    return converted


def _target(
    session: Mapping[str, object], target_type: str, target_id: str
) -> Mapping[str, object]:
    if target_type == "reference":
        references = session.get("references")
        target = references.get(target_id) if isinstance(references, Mapping) else None
    elif target_type == "result":
        items = session.get("items")
        target = next(
            (
                item
                for item in items
                if isinstance(item, Mapping) and item.get("job_id") == target_id
            ),
            None,
        ) if isinstance(items, list) else None
    else:
        raise ValueError("target_type must be reference or result")
    if not isinstance(target, Mapping):
        raise ValueError("annotation target is not in the worklist")
    if target.get("disabled") is True:
        raise ValueError("failed/disabled results cannot be annotated")
    return target


_FRAME_KEYS = frozenset(
    {"frame", "t", "time_seconds", "path", "sha256", "width", "height"}
)
_ANNOTATION_KEYS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "coordinate_space",
        "camera_observation_space",
        "target_type",
        "target_id",
        "story_id",
        "video_sha256",
        "session_manifest_sha256",
        "reference_annotation_sha256",
        "annotation_status",
        "eligible_for_paper",
        "review",
        "actors",
        "camera",
    }
)
_SAVED_SAMPLE_KEYS = frozenset(
    {
        "frame",
        "t",
        "frame_sha256",
        "footpoint",
        "visibility",
        "identity",
        "geometry_eligible",
    }
)


def _sha256_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _session(session_root: Path) -> tuple[dict[str, Any], Path, str]:
    manifest_path = session_root / "session_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("session manifest is missing")
    data = manifest_path.read_bytes()
    session = _parse_object(data, "annotation session")
    if (
        session.get("schema_version") != SCHEMA_VERSION
        or session.get("evidence_type") != "human_comparison_annotation_session"
        or session.get("coordinate_space") != "normalized_0_1_top_left"
        or session.get("camera_observation_space")
        != "observable_background_motion_only"
    ):
        raise ValueError("annotation session schema is invalid")
    return session, manifest_path, hashlib.sha256(data).hexdigest()


def _frame_path(session_root: Path, frame: Mapping[str, object]) -> Path:
    if set(frame) != _FRAME_KEYS:
        raise ValueError("session frame fields do not match the schema")
    declared = frame.get("path")
    if not isinstance(declared, str) or not declared.startswith("frames/"):
        raise ValueError("session frame path is invalid")
    relative = Path(declared)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("session frame path is invalid")
    return _below(session_root / relative, session_root / "frames", "session frame")


def _verify_frame_file(
    session_root: Path, frame: Mapping[str, object]
) -> tuple[Path, bytes]:
    path = _frame_path(session_root, frame)
    if not path.is_file():
        raise ValueError("session frame is missing")
    data = path.read_bytes()
    expected_sha = _sha256_text(frame.get("sha256"), "session frame SHA-256")
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValueError("session frame SHA-256 mismatch")
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.format != "PNG":
                raise ValueError("session frame is not a PNG")
            actual_size = [image.width, image.height]
    except OSError as exc:
        raise ValueError("session frame PNG decode failed") from exc
    if actual_size != [frame.get("width"), frame.get("height")]:
        raise ValueError("session frame PNG dimensions mismatch")
    return path, data


def _verify_target_video(session_root: Path, target: Mapping[str, object]) -> Mapping[str, object]:
    video = target.get("video")
    if not isinstance(video, Mapping):
        raise ValueError("target video metadata is unavailable")
    expected_video_keys = {
        "sha256",
        "bytes",
        "frame_count",
        "fps",
        "duration_seconds",
        "size",
        "frames",
    }
    if set(video) != expected_video_keys:
        raise ValueError("target video metadata fields do not match the schema")
    _sha256_text(video.get("sha256"), "target video SHA-256")
    frame_count = video.get("frame_count")
    if type(frame_count) is not int or frame_count < 9:
        raise ValueError("target video frame_count is invalid")
    size = video.get("size")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(item) is not int or item <= 0 for item in size)
    ):
        raise ValueError("target video size is invalid")
    frames = video.get("frames")
    if not isinstance(frames, list) or len(frames) != len(NORMALIZED_TIMEPOINTS):
        raise ValueError("target video protocol frames are invalid")
    expected_indices = frame_indices_for_count(frame_count)
    for expected_index, expected_t, frame in zip(
        expected_indices, NORMALIZED_TIMEPOINTS, frames
    ):
        if not isinstance(frame, Mapping):
            raise ValueError("session frame must be an object")
        if frame.get("frame") != expected_index or frame.get("t") != expected_t:
            raise ValueError("session frame/time protocol mismatch")
        if [frame.get("width"), frame.get("height")] != size:
            raise ValueError("session frame dimensions differ from target video")
        _verify_frame_file(session_root, frame)
    return video


def _annotation_path(
    session_root: Path, target_type: str, target_id: str
) -> Path:
    annotation_root = session_root / "annotations"
    return _below(
        annotation_root / f"{target_type}__{target_id}.json",
        annotation_root,
        "annotation",
    )


def _review_is_valid(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"reviewer_id", "reviewed_at_utc"}:
        return False
    reviewer = value.get("reviewer_id")
    timestamp = value.get("reviewed_at_utc")
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(timestamp, str):
        return False
    if not timestamp.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _validate_reference_snapshot(
    session_root: Path,
    reference_sha: object,
    reference_id: str,
    story_id: object,
) -> dict[str, Any]:
    digest = _sha256_text(reference_sha, "reference annotation SHA-256")
    annotation_root = session_root / "annotations"
    snapshot = _below(
        annotation_root / "by_sha256" / f"{digest}.json",
        annotation_root,
        "reference annotation snapshot",
    )
    if not snapshot.is_file() or sha256_file(snapshot) != digest:
        raise ValueError("reference annotation SHA-256 mismatch")
    value = _load_object(snapshot, "reference annotation snapshot")
    validated = _validate_annotation_value(
        session_root, snapshot, value, snapshot_sha=digest
    )
    if (
        validated["target_type"] != "reference"
        or validated["target_id"] != reference_id
        or validated["story_id"] != story_id
        or validated["annotation_status"] != "reviewed"
    ):
        raise ValueError("reference annotation provenance mismatch")
    return validated


def _validate_annotation_value(
    session_root: Path,
    path: Path,
    value: Mapping[str, object],
    *,
    snapshot_sha: str | None = None,
) -> dict[str, Any]:
    if set(value) != _ANNOTATION_KEYS:
        raise ValueError("stored annotation fields do not match the schema")
    session, _manifest_path, manifest_sha = _session(session_root)
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("evidence_type") != "human_comparison_annotation"
        or value.get("coordinate_space") != session["coordinate_space"]
        or value.get("camera_observation_space")
        != "observable_background_motion_only"
    ):
        raise ValueError("stored annotation schema is invalid")
    target_type = value.get("target_type")
    target_id = _safe_token(value.get("target_id"), "target_id")
    if target_type not in {"reference", "result"}:
        raise ValueError("stored annotation target_type is invalid")
    target = _target(session, target_type, target_id)
    annotation_root = session_root / "annotations"
    resolved = _below(path, annotation_root, "annotation")
    if snapshot_sha is None:
        if resolved != _annotation_path(session_root, target_type, target_id):
            raise ValueError("annotation filename does not match its target")
    else:
        expected_snapshot = _below(
            annotation_root / "by_sha256" / f"{snapshot_sha}.json",
            annotation_root,
            "reference annotation snapshot",
        )
        if (
            resolved != expected_snapshot
            or hashlib.sha256(_json_bytes(value)).hexdigest() != snapshot_sha
        ):
            raise ValueError("reference snapshot filename/content mismatch")
    video = _verify_target_video(session_root, target)
    if value.get("story_id") != target.get("story_id"):
        raise ValueError("annotation story provenance mismatch")
    if value.get("video_sha256") != video.get("sha256"):
        raise ValueError("annotation video SHA-256 mismatch")
    if value.get("session_manifest_sha256") != manifest_sha:
        raise ValueError("annotation session manifest SHA-256 mismatch")
    frames = video["frames"]
    actor_ids = target.get("actor_ids")
    actors = value.get("actors")
    if (
        not isinstance(actor_ids, list)
        or not isinstance(actors, Mapping)
        or set(actors) != set(actor_ids)
    ):
        raise ValueError("stored actors do not match the target")
    for actor_id in actor_ids:
        samples = actors.get(actor_id)
        if not isinstance(samples, list) or len(samples) != len(frames):
            raise ValueError("stored actor sample count is invalid")
        for frame, sample in zip(frames, samples):
            if not isinstance(frame, Mapping) or not isinstance(sample, Mapping):
                raise ValueError("stored actor sample is invalid")
            if set(sample) != _SAVED_SAMPLE_KEYS:
                raise ValueError("stored actor sample fields do not match the schema")
            if (
                sample.get("frame") != frame.get("frame")
                or sample.get("t") != frame.get("t")
                or sample.get("frame_sha256") != frame.get("sha256")
            ):
                raise ValueError("stored actor frame provenance mismatch")
            visibility = sample.get("visibility")
            identity = sample.get("identity")
            if visibility not in VISIBILITY_VALUES or identity not in IDENTITY_VALUES:
                raise ValueError("stored visibility or identity is invalid")
            footpoint = sample.get("footpoint")
            if visibility == "visible":
                if not isinstance(footpoint, Mapping) or set(footpoint) != {"x", "y"}:
                    raise ValueError("stored visible sample has no footpoint")
                _unit(footpoint["x"], "stored footpoint x")
                _unit(footpoint["y"], "stored footpoint y")
            elif footpoint is not None:
                raise ValueError("stored non-visible sample has a footpoint")
            expected_eligibility = visibility == "visible" and identity in {
                "confirmed",
                "probable",
            }
            if sample.get("geometry_eligible") is not expected_eligibility:
                raise ValueError("stored geometry eligibility is inconsistent")
    camera = value.get("camera")
    if not isinstance(camera, Mapping) or set(camera) != {
        "category",
        "intensity",
        "confidence",
    }:
        raise ValueError("stored camera fields do not match the schema")
    if camera.get("category") not in CAMERA_CATEGORIES:
        raise ValueError("stored camera category is invalid")
    if camera.get("intensity") not in CAMERA_INTENSITIES:
        raise ValueError("stored camera intensity is invalid")
    _unit(camera.get("confidence"), "stored camera confidence")
    state = value.get("annotation_status")
    if state == "draft":
        if value.get("review") is not None or value.get("eligible_for_paper") is not False:
            raise ValueError("draft annotation review state is invalid")
    elif state == "reviewed":
        if not _review_is_valid(value.get("review")):
            raise ValueError("reviewed annotation metadata is invalid")
        if value.get("eligible_for_paper") is not True:
            raise ValueError("reviewed annotation eligibility field is inconsistent")
    else:
        raise ValueError("stored annotation status is invalid")
    if target_type == "reference":
        if value.get("reference_annotation_sha256") is not None:
            raise ValueError("reference annotation cannot bind a reference")
    else:
        reference_id = _safe_token(target.get("reference_id"), "reference_id")
        _validate_reference_snapshot(
            session_root,
            value.get("reference_annotation_sha256"),
            reference_id,
            target.get("story_id"),
        )
    return dict(value)


def save_annotation(session_dir: Path, payload: Mapping[str, object]) -> dict[str, object]:
    """Validate and atomically save one explicit human annotation payload."""

    session_root = Path(session_dir).resolve()
    session, _manifest_path, manifest_sha = _session(session_root)
    if not isinstance(payload, Mapping):
        raise ValueError("annotation payload must be an object")
    expected_keys = {
        "schema_version",
        "target_type",
        "target_id",
        "annotation_status",
        "reference_annotation_sha256",
        "actors",
        "camera",
    }
    if set(payload) != expected_keys:
        raise ValueError("annotation payload fields do not match the schema")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("annotation schema_version mismatch")
    target_type = payload["target_type"]
    target_id = payload["target_id"]
    state = payload["annotation_status"]
    if not isinstance(target_type, str) or not isinstance(target_id, str):
        raise ValueError("annotation target identifiers must be strings")
    target_id = _safe_token(target_id, "target_id")
    if state != "draft":
        raise ValueError("first annotation save must be draft")
    target = _target(session, target_type, target_id)
    reference_sha: str | None = None
    if target_type == "reference":
        if payload["reference_annotation_sha256"] is not None:
            raise ValueError("reference annotation cannot bind another reference")
    else:
        reference_id = _safe_token(target["reference_id"], "reference_id")
        reference_sha = _safe_token(
            payload["reference_annotation_sha256"],
            "reference_annotation_sha256",
        )
        if len(reference_sha) != 64 or any(
            character not in "0123456789abcdef" for character in reference_sha
        ):
            raise ValueError("reference annotation SHA-256 mismatch")
        _validate_reference_snapshot(
            session_root, reference_sha, reference_id, target.get("story_id")
        )
    video = _verify_target_video(session_root, target)
    frames = video["frames"]
    actors = payload["actors"]
    actor_ids = target.get("actor_ids")
    if not isinstance(actors, Mapping) or set(actors) != set(actor_ids):  # type: ignore[arg-type]
        raise ValueError("actors must match all declared target actor ids")
    saved_actors: dict[str, object] = {}
    for actor_id in actor_ids:  # type: ignore[union-attr]
        samples = actors[actor_id]
        if not isinstance(samples, list) or len(samples) != len(frames):
            raise ValueError(f"actor {actor_id} requires one sample per protocol frame")
        saved_samples: list[dict[str, object]] = []
        for expected, sample in zip(frames, samples):
            if not isinstance(expected, Mapping) or not isinstance(sample, Mapping):
                raise ValueError("actor samples must be objects")
            if set(sample) != {"frame", "t", "footpoint", "visibility", "identity"}:
                raise ValueError("actor sample fields do not match the schema")
            if sample["frame"] != expected["frame"] or sample["t"] != expected["t"]:
                raise ValueError("actor sample frame/time provenance mismatch")
            visibility = sample["visibility"]
            identity = sample["identity"]
            if visibility not in VISIBILITY_VALUES or identity not in IDENTITY_VALUES:
                raise ValueError("invalid visibility or identity label")
            footpoint = sample["footpoint"]
            normalized_point: dict[str, float] | None
            if visibility == "visible":
                if not isinstance(footpoint, Mapping) or set(footpoint) != {"x", "y"}:
                    raise ValueError("visible actor sample requires a footpoint")
                normalized_point = {
                    "x": _unit(footpoint["x"], "footpoint x"),
                    "y": _unit(footpoint["y"], "footpoint y"),
                }
            else:
                if footpoint is not None:
                    raise ValueError("non-visible actor sample requires a null footpoint")
                normalized_point = None
            eligible = visibility == "visible" and identity in {"confirmed", "probable"}
            saved_samples.append(
                {
                    "frame": sample["frame"],
                    "t": sample["t"],
                    "frame_sha256": expected["sha256"],
                    "footpoint": normalized_point,
                    "visibility": visibility,
                    "identity": identity,
                    "geometry_eligible": eligible,
                }
            )
        saved_actors[actor_id] = saved_samples
    camera = payload["camera"]
    if not isinstance(camera, Mapping) or set(camera) != {"category", "intensity", "confidence"}:
        raise ValueError("camera must contain category, intensity, and confidence only")
    if camera["category"] not in CAMERA_CATEGORIES:
        raise ValueError("invalid observable background motion category")
    if camera["intensity"] not in CAMERA_INTENSITIES:
        raise ValueError("invalid observable background motion intensity")
    saved: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_type": "human_comparison_annotation",
        "coordinate_space": session["coordinate_space"],
        "camera_observation_space": "observable_background_motion_only",
        "target_type": target_type,
        "target_id": target_id,
        "story_id": target["story_id"],
        "video_sha256": video["sha256"],
        "session_manifest_sha256": manifest_sha,
        "reference_annotation_sha256": reference_sha,
        "annotation_status": state,
        "eligible_for_paper": False,
        "review": None,
        "actors": saved_actors,
        "camera": {
            "category": camera["category"],
            "intensity": camera["intensity"],
            "confidence": _unit(camera["confidence"], "camera confidence"),
        },
    }
    output = _annotation_path(session_root, target_type, target_id)
    data = _json_bytes(saved)
    _validate_annotation_value(session_root, output, saved)
    _atomic_write(output, data)
    return saved


def review_annotation(
    session_dir: Path,
    target_type: str,
    target_id: str,
    *,
    reviewer_id: str,
) -> dict[str, object]:
    """Independently promote an existing validated draft to reviewed."""

    session_root = Path(session_dir).resolve()
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValueError("reviewer_id must be non-empty")
    reviewer = reviewer_id.strip()
    if any(ord(character) < 32 for character in reviewer) or len(reviewer) > 200:
        raise ValueError("reviewer_id is invalid")
    if target_type not in {"reference", "result"}:
        raise ValueError("target_type must be reference or result")
    target_id = _safe_token(target_id, "target_id")
    output = _annotation_path(session_root, target_type, target_id)
    if not output.is_file():
        raise ValueError("draft annotation must be saved before review")
    draft = _validate_annotation_value(
        session_root, output, _load_object(output, "draft annotation")
    )
    if draft["annotation_status"] != "draft":
        raise ValueError("only an existing draft can be reviewed")
    reviewed = dict(draft)
    reviewed["annotation_status"] = "reviewed"
    reviewed["eligible_for_paper"] = True
    reviewed["review"] = {
        "reviewer_id": reviewer,
        "reviewed_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    data = _json_bytes(reviewed)
    _validate_annotation_value(session_root, output, reviewed)
    if target_type == "reference":
        digest = hashlib.sha256(data).hexdigest()
        annotation_root = session_root / "annotations"
        snapshot = _below(
            annotation_root / "by_sha256" / f"{digest}.json",
            annotation_root,
            "reference annotation snapshot",
        )
        _validate_annotation_value(
            session_root, snapshot, reviewed, snapshot_sha=digest
        )
        if snapshot.exists():
            if not snapshot.is_file() or snapshot.read_bytes() != data:
                raise ValueError("immutable reference annotation snapshot conflict")
        else:
            _atomic_write(snapshot, data)
    _atomic_write(output, data)
    return reviewed


def annotation_is_paper_eligible(
    session_dir: Path, annotation_path: Path
) -> bool:
    """Revalidate paper eligibility against an immutable reference artifact."""

    session_root = Path(session_dir).resolve()
    annotation_root = session_root / "annotations"
    candidate = Path(annotation_path)
    if not candidate.is_absolute():
        candidate = annotation_root / candidate
    annotation = _below(candidate, annotation_root, "annotation")
    if not annotation.is_file():
        raise ValueError("annotation is not a file")
    try:
        validated = _validate_annotation_value(
            session_root, annotation, _load_object(annotation, "annotation")
        )
    except (OSError, TypeError, ValueError):
        return False
    return validated["annotation_status"] == "reviewed" and _review_is_valid(
        validated["review"]
    )


def _frame_allowlist(session_dir: Path) -> tuple[dict[str, Mapping[str, object]], str]:
    session, _manifest, manifest_sha = _session(session_dir)
    targets: list[Mapping[str, object]] = []
    references = session.get("references")
    items = session.get("items")
    if not isinstance(references, Mapping) or not isinstance(items, list):
        raise ValueError("annotation session target collections are invalid")
    targets.extend(
        target for target in references.values() if isinstance(target, Mapping)
    )
    targets.extend(
        target
        for target in items
        if isinstance(target, Mapping) and target.get("disabled") is not True
    )
    allowed: dict[str, Mapping[str, object]] = {}
    for target in targets:
        video = target.get("video")
        frames = video.get("frames") if isinstance(video, Mapping) else None
        if not isinstance(frames, list):
            raise ValueError("annotation session target frames are invalid")
        for frame in frames:
            if not isinstance(frame, Mapping):
                raise ValueError("annotation session frame is invalid")
            _frame_path(session_dir, frame)
            route = "/" + str(frame["path"])
            previous = allowed.get(route)
            if previous is not None and previous != frame:
                raise ValueError("conflicting session frame allowlist entry")
            allowed[route] = frame
    return allowed, manifest_sha


def _handler(session_dir: Path) -> type[BaseHTTPRequestHandler]:
    html = Path(__file__).resolve().parents[1] / "static" / "manual_annotation.html"
    allowed_frames, allowed_manifest_sha = _frame_allowlist(session_dir)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
            self._send(status, _json_bytes({"error": {"message": message}}), "application/json")

        def _guard(self, check_origin: bool) -> bool:
            host, port = self.server.server_address[:2]  # type: ignore[index]
            expected = f"{host}:{port}"
            if self.headers.get("Host") != expected:
                self._error("Host must match this localhost annotator")
                return False
            origin = self.headers.get("Origin")
            if check_origin and origin is not None and origin != f"http://{expected}":
                self._error("Origin must match this localhost annotator")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard(False):
                return
            route = unquote(urlsplit(self.path).path)
            if route == "/":
                self._send(HTTPStatus.OK, html.read_bytes(), "text/html; charset=utf-8")
                return
            if route == "/session":
                self._send(
                    HTTPStatus.OK,
                    (session_dir / "session_manifest.json").read_bytes(),
                    "application/json",
                )
                return
            if route.startswith("/frames/"):
                frame_record = allowed_frames.get(route)
                if frame_record is None:
                    self._error("frame is not in the session allowlist")
                    return
                try:
                    _current, _path, current_manifest_sha = _session(session_dir)
                    if current_manifest_sha != allowed_manifest_sha:
                        raise ValueError("session manifest changed after server start")
                    _frame, frame_bytes = _verify_frame_file(session_dir, frame_record)
                except (OSError, TypeError, ValueError) as exc:
                    self._error(str(exc), HTTPStatus.CONFLICT)
                    return
                self._send(HTTPStatus.OK, frame_bytes, "image/png")
                return
            if route.startswith("/annotations/"):
                name = route.removeprefix("/annotations/")
                if not name or Path(name).name != name or not name.endswith(".json"):
                    self._error("invalid annotation path")
                    return
                annotation = _below(
                    session_dir / "annotations" / name,
                    session_dir / "annotations",
                    "annotation",
                )
                if not annotation.is_file():
                    self._error("annotation not found", HTTPStatus.NOT_FOUND)
                    return
                self._send(HTTPStatus.OK, annotation.read_bytes(), "application/json")
                return
            self._error("route not found", HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard(True):
                return
            route = urlsplit(self.path).path
            if route not in {"/annotation", "/review"}:
                self._error("route not found", HTTPStatus.NOT_FOUND)
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                length_text = self.headers.get("Content-Length")
                if length_text is None or not length_text.isdecimal():
                    raise ValueError("Content-Length is required")
                length = int(length_text)
                if not 0 < length <= 2 * 1024 * 1024:
                    raise ValueError("annotation request size is invalid")
                payload = json.loads(
                    self.rfile.read(length).decode("utf-8", errors="strict"),
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON number: {token}")
                    ),
                )
                if route == "/annotation":
                    saved = save_annotation(session_dir, payload)
                else:
                    if not isinstance(payload, Mapping) or set(payload) != {
                        "target_type",
                        "target_id",
                        "reviewer_id",
                    }:
                        raise ValueError("review payload fields do not match the schema")
                    saved = review_annotation(
                        session_dir,
                        payload["target_type"],
                        payload["target_id"],
                        reviewer_id=payload["reviewer_id"],
                    )
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                self._error(str(exc))
                return
            self._send(HTTPStatus.OK, _json_bytes(saved), "application/json")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="decode media and build a worklist")
    prepare.add_argument("--index", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--workspace", type=Path, default=Path("."))
    prepare.add_argument("--proxy-root", type=Path)
    serve = subparsers.add_parser("serve", help="serve an existing prepared worklist")
    serve.add_argument("--session-dir", type=Path, required=True)
    serve.add_argument("--workspace", type=Path, default=Path("."))
    serve.add_argument("--port", type=int, default=8767)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            output = _output_path(args.output_dir, args.workspace)
            session = prepare_workspace(
                args.index,
                output,
                workspace=args.workspace,
                proxy_root=args.proxy_root,
            )
            print(
                "MANUAL_ANNOTATION_PREPARED "
                f"output={output} references={len(session['references'])} "
                f"items={len(session['items'])}"
            )
            return 0
        if type(args.port) is not int or not 1 <= args.port <= 65535:
            raise ValueError("port must be in [1, 65535]")
        session_dir = _output_path(args.session_dir, args.workspace)
        if not (session_dir / "session_manifest.json").is_file():
            raise ValueError("prepared session_manifest.json is missing")
        server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(session_dir))
        print(f"MANUAL_ANNOTATION_SERVING http://127.0.0.1:{args.port}")
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    except (FileExistsError, OSError, TypeError, ValueError) as exc:
        print(f"MANUAL_ANNOTATION_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
