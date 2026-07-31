"""Prepare an immutable clay-only VACE job from a coded-draft bundle.

This module is deliberately local-only: it validates and snapshots inputs but
does not contact an API, launch VACE, or mutate the source clay video.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping
from uuid import uuid4

import imageio_ffmpeg

from videoactagent.vace_inputs import (
    MASK_POLICY,
    MASK_SEMANTICS,
    VACE_COMMIT,
    VACE_MODEL_NAME,
    VACE_SEED,
    VACE_SIZE,
    _verify_full_generation_mask,
    write_full_generation_mask,
)


_JOB_NAME = "vace_job.json"
_SCHEMA_VERSION = "1.0"
_CONTROL_FRAMES = 81
_CONTROL_FPS = 16
_CONTROL_RESOLUTION = (832, 480)
_CONTROL_PATH = "control/src_video.mp4"
_RESAMPLING = "ffmpeg_scale_fps_final_frame_clone"
_JOB_ARTIFACT_PATHS = (
    "control/src_mask.mp4",
    "control/src_video.mp4",
    "source/clay.mp4",
    "source/coded_draft_bundle.json",
    "source/coded_draft_manifest.json",
    "source/prompt.txt",
    "source/semantic_plan.json",
)


class VaceCodedDraftError(ValueError):
    """Raised when a coded draft cannot prove a clay-only VACE job."""


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateKey) as exc:
        raise VaceCodedDraftError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise VaceCodedDraftError(f"{label} must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record(path: Path, relative: str) -> dict[str, Any]:
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _artifact_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    return {field: record[field] for field in ("path", "bytes", "sha256")}


def _safe_artifact(root: Path, path_text: Any, label: str) -> Path:
    if not isinstance(path_text, str) or not path_text:
        raise VaceCodedDraftError(f"{label} path is missing")
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise VaceCodedDraftError(f"{label} path is unsafe")
    try:
        root_resolved = root.resolve(strict=True)
        target = (root_resolved / relative).resolve(strict=True)
        target.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise VaceCodedDraftError(f"{label} path is outside the coded draft") from exc
    if not target.is_file():
        raise VaceCodedDraftError(f"{label} is not a file")
    return target


def _verify_record(
    root: Path, record: Any, label: str, *, expected_path: str | None = None
) -> Path:
    if not isinstance(record, Mapping):
        raise VaceCodedDraftError(f"{label} record is missing")
    if expected_path is not None and record.get("path") != expected_path:
        raise VaceCodedDraftError(f"{label} path does not match {expected_path}")
    target = _safe_artifact(root, record.get("path"), label)
    if record.get("bytes") != target.stat().st_size:
        raise VaceCodedDraftError(f"{label} byte count mismatch")
    if record.get("sha256") != _sha256(target):
        raise VaceCodedDraftError(f"{label} sha256 mismatch")
    return target


def _close_reader(reader: object) -> None:
    frame = getattr(reader, "gi_frame", None)
    process = frame.f_locals.get("process") if frame is not None else None
    try:
        reader.close()  # type: ignore[attr-defined]
    finally:
        if process is not None:
            for name in ("stdin", "stdout", "stderr"):
                pipe = getattr(process, name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _probe_media(path: Path) -> dict[str, Any]:
    if path.stat().st_size <= 0:
        raise VaceCodedDraftError("clay video is empty")
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    frames = 0
    digest = hashlib.sha256()
    try:
        try:
            metadata = next(reader)
        except StopIteration as exc:
            raise VaceCodedDraftError("clay video has no decodable metadata") from exc
        size = metadata.get("size")
        fps = metadata.get("fps")
        duration = metadata.get("duration")
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in size)
            or isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps))
            or fps <= 0
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration <= 0
        ):
            raise VaceCodedDraftError("clay video metadata is incomplete")
        expected_bytes = int(size[0]) * int(size[1]) * 3
        for frame_bytes in reader:
            if len(frame_bytes) != expected_bytes:
                raise VaceCodedDraftError("clay video frame byte count is invalid")
            digest.update(frame_bytes)
            frames += 1
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, VaceCodedDraftError):
            raise
        raise VaceCodedDraftError(f"cannot decode clay video: {exc}") from exc
    finally:
        _close_reader(reader)
    if frames <= 0:
        raise VaceCodedDraftError("clay video has no decoded frames")
    return {
        "frame_count": frames,
        "fps": float(fps),
        "duration_seconds": frames / float(fps),
        "stream_duration_seconds": float(duration),
        "resolution": [int(size[0]), int(size[1])],
        "codec": metadata.get("codec"),
        "bytes": path.stat().st_size,
        "decoded_pixel_sha256": digest.hexdigest(),
    }


def _same_number(actual: Any, expected: Any, tolerance: float = 1e-6) -> bool:
    return (
        not isinstance(actual, bool)
        and not isinstance(expected, bool)
        and isinstance(actual, (int, float))
        and isinstance(expected, (int, float))
        and math.isfinite(float(actual))
        and math.isfinite(float(expected))
        and abs(float(actual) - float(expected)) <= tolerance
    )


def _verify_media_record(actual: Mapping[str, Any], recorded: Any) -> None:
    if not isinstance(recorded, Mapping) or set(recorded) != set(actual):
        raise VaceCodedDraftError("clay media record is incomplete")
    numeric = {"fps", "duration_seconds", "stream_duration_seconds"}
    for field, value in actual.items():
        if field in numeric:
            if not _same_number(value, recorded.get(field), 0.01):
                raise VaceCodedDraftError(f"clay media {field} mismatch")
        elif recorded.get(field) != value:
            raise VaceCodedDraftError(f"clay media {field} mismatch")


def _verify_inventory(root: Path, records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, list):
        raise VaceCodedDraftError("manifest artifact_inventory must be a list")
    verified: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise VaceCodedDraftError(f"inventory item {index} is not an object")
        path = record.get("path")
        if not isinstance(path, str) or path in verified:
            raise VaceCodedDraftError("manifest inventory path is missing or duplicated")
        _verify_record(root, record, f"inventory item {path}")
        verified[path] = record
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
    }
    if actual != set(verified):
        raise VaceCodedDraftError(
            "coded-draft files differ from the hash-bound artifact inventory"
        )
    return verified


def _verify_job_inventory(root: Path, records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise VaceCodedDraftError("VACE job inventory must be a list")
    expected_paths = list(_JOB_ARTIFACT_PATHS)
    paths = [record.get("path") if isinstance(record, Mapping) else None for record in records]
    if paths != expected_paths or len({str(path).casefold() for path in paths}) != len(paths):
        raise VaceCodedDraftError("VACE job inventory paths are invalid or case-conflicting")
    verified: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"path", "bytes", "sha256"}:
            raise VaceCodedDraftError("VACE job inventory record is not canonical")
        _verify_record(root, record, f"VACE job inventory {record['path']}")
        verified.append(dict(record))
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    declared = set(expected_paths) | {_JOB_NAME}
    if actual != declared or len({path.casefold() for path in actual}) != len(actual):
        raise VaceCodedDraftError("VACE job files differ from the declared closed inventory")
    return verified


def _verified_source_binding(
    root: Path,
    binding: Any,
    label: str,
    inventory: Mapping[str, Mapping[str, Any]],
) -> Path:
    if not isinstance(binding, Mapping) or binding.get("verified_equal") is not True:
        raise VaceCodedDraftError(f"{label} binding is not verified_equal")
    record = {
        "path": binding.get("snapshot_path"),
        "bytes": binding.get("bytes"),
        "sha256": binding.get("snapshot_sha256"),
    }
    target = _verify_record(root, record, f"{label} snapshot")
    declared = inventory.get(str(record["path"]))
    if not isinstance(declared, Mapping) or any(
        declared.get(field) != record[field] for field in ("path", "bytes", "sha256")
    ):
        raise VaceCodedDraftError(f"{label} snapshot inventory record mismatch")
    if binding.get("original_sha256") != binding.get("snapshot_sha256"):
        raise VaceCodedDraftError(f"{label} original and snapshot hashes differ")
    return target


def _derive_source_provenance(
    bundle: Mapping[str, Any],
    semantic: Mapping[str, Any],
    source_frame_count: int,
) -> dict[str, Any]:
    appearance = bundle.get("appearance_instruction")
    story_prompt = bundle.get("story_prompt")
    if not isinstance(appearance, str) or not appearance.strip():
        raise VaceCodedDraftError("appearance_instruction must be non-empty")
    if appearance != semantic.get("appearance_instruction"):
        raise VaceCodedDraftError(
            "appearance_instruction differs from the semantic plan snapshot"
        )
    if not isinstance(story_prompt, str) or not story_prompt.strip():
        raise VaceCodedDraftError("story_prompt must be non-empty")

    motion = bundle.get("motion_semantics")
    keyframes = motion.get("keyframes") if isinstance(motion, Mapping) else None
    semantic_keyframes = semantic.get("semantic_keyframes")
    if (
        not isinstance(keyframes, list)
        or not keyframes
        or not isinstance(semantic_keyframes, list)
        or len(keyframes) != len(semantic_keyframes)
    ):
        raise VaceCodedDraftError("motion/semantic keyframes are missing or unequal")
    schedule: list[dict[str, Any]] = []
    previous_t = -1.0
    previous_frame = -1
    for item, semantic_item in zip(keyframes, semantic_keyframes):
        if not isinstance(item, Mapping) or not isinstance(semantic_item, Mapping):
            raise VaceCodedDraftError("motion semantic keyframe is invalid")
        semantic_id = item.get("semantic_id")
        t = item.get("t")
        frame_index = item.get("frame_index")
        if (
            not isinstance(semantic_id, str)
            or semantic_item.get("id") != semantic_id
            or isinstance(t, bool)
            or not isinstance(t, (int, float))
            or not math.isfinite(float(t))
            or not 0 <= float(t) <= 1
            or not _same_number(t, semantic_item.get("t"), 1e-12)
            or isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
        ):
            raise VaceCodedDraftError("motion semantic keyframe schedule is invalid")
        expected_frame = round(float(t) * (source_frame_count - 1))
        if (
            frame_index != expected_frame
            or float(t) <= previous_t
            or frame_index <= previous_frame
        ):
            raise VaceCodedDraftError(
                "motion keyframe index must equal round(t * (source_frame_count - 1))"
            )
        previous_t = float(t)
        previous_frame = frame_index
        schedule.append(
            {"semantic_id": semantic_id, "t": t, "frame_index": frame_index}
        )
    return {
        "story_prompt": story_prompt.strip(),
        "appearance_instruction": appearance.strip(),
        "keyframes": schedule,
    }


def _verified_coded_draft(
    bundle_path: Path | str, manifest_path: Path | str
) -> dict[str, Any]:
    bundle_file = Path(bundle_path).resolve(strict=True)
    manifest_file = Path(manifest_path).resolve(strict=True)
    if bundle_file.name != "bundle.json" or manifest_file.name != "manifest.json":
        raise VaceCodedDraftError("coded-draft inputs must be bundle.json and manifest.json")
    if bundle_file.parent != manifest_file.parent:
        raise VaceCodedDraftError("bundle and manifest must share one coded-draft directory")
    root = bundle_file.parent
    bundle = _read_object(bundle_file, "coded-draft bundle")
    manifest = _read_object(manifest_file, "coded-draft manifest")
    if bundle.get("schema_version") != _SCHEMA_VERSION or manifest.get("schema_version") != _SCHEMA_VERSION:
        raise VaceCodedDraftError("coded-draft schema_version must be 1.0")
    story_id = bundle.get("story_id")
    if not isinstance(story_id, str) or not story_id or manifest.get("story_id") != story_id:
        raise VaceCodedDraftError("coded-draft story identity mismatch")
    if bundle.get("conditioning_mode") != "source_video_edit":
        raise VaceCodedDraftError("conditioning_mode must be source_video_edit")
    if bundle.get("backend_consumed") is not False or manifest.get("backend_consumed") is not False:
        raise VaceCodedDraftError("coded draft must not already be backend-consumed")

    inventory = _verify_inventory(root, manifest.get("artifact_inventory"))
    bundle_output = manifest.get("outputs", {}).get("bundle")
    _verify_record(root, bundle_output, "manifest bundle", expected_path="bundle.json")
    if inventory.get("bundle.json") != bundle_output:
        raise VaceCodedDraftError("bundle inventory record mismatch")

    clay = bundle.get("conditioning_video")
    manifest_clay = manifest.get("videos", {}).get("clay")
    if not isinstance(clay, Mapping) or clay != manifest_clay:
        raise VaceCodedDraftError("conditioning video must equal the manifest clay video")
    clay_path = _verify_record(root, clay, "clay conditioning video")
    if inventory.get(str(clay.get("path"))) is None:
        raise VaceCodedDraftError("clay conditioning video is absent from inventory")
    media = _probe_media(clay_path)
    _verify_media_record(media, clay.get("media"))
    expected_media = manifest.get("expected_media")
    if not isinstance(expected_media, Mapping):
        raise VaceCodedDraftError("manifest expected_media is missing")
    expected_contract = {
        "frame_count": media["frame_count"],
        "fps": media["fps"],
        "duration_seconds": media["duration_seconds"],
        "resolution": media["resolution"],
    }
    for field, value in expected_contract.items():
        recorded = expected_media.get(field)
        if field in {"fps", "duration_seconds"}:
            if not _same_number(value, recorded, 0.01):
                raise VaceCodedDraftError(f"manifest expected_media {field} mismatch")
        elif recorded != value:
            raise VaceCodedDraftError(f"manifest expected_media {field} mismatch")

    evidence = bundle.get("diagnostic_video")
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("role") != "evidence_only"
        or evidence.get("backend_consumed") is not False
        or evidence.get("sha256") == clay.get("sha256")
    ):
        raise VaceCodedDraftError("non-conditioning evidence video contract is invalid")
    if manifest.get("videos", {}).get("diagnostic") != {
        key: value
        for key, value in evidence.items()
        if key not in {"role", "backend_consumed"}
    }:
        raise VaceCodedDraftError("manifest evidence video mismatch")
    evidence_path = _verify_record(root, evidence, "evidence-only video")
    evidence_inventory = inventory.get(str(evidence.get("path")))
    if not isinstance(evidence_inventory, Mapping) or any(
        evidence_inventory.get(field) != evidence.get(field)
        for field in ("path", "bytes", "sha256")
    ):
        raise VaceCodedDraftError("evidence-only video inventory record mismatch")
    _verify_media_record(_probe_media(evidence_path), evidence.get("media"))

    motion = bundle.get("motion_semantics")
    if not isinstance(motion, Mapping):
        raise VaceCodedDraftError("motion_semantics is missing")
    semantic_binding = motion.get("semantic_plan")
    manifest_binding = manifest.get("sources", {}).get("semantic_plan")
    if not isinstance(semantic_binding, Mapping) or semantic_binding != manifest_binding:
        raise VaceCodedDraftError("semantic plan binding mismatch")
    semantic_path = _verified_source_binding(
        root, semantic_binding, "semantic plan", inventory
    )
    semantic = _read_object(semantic_path, "semantic plan snapshot")
    if semantic.get("story_id") != story_id:
        raise VaceCodedDraftError("semantic plan story identity mismatch")

    source_bindings = bundle.get("source_bindings")
    prompt_binding = (
        source_bindings.get("prompt") if isinstance(source_bindings, Mapping) else None
    )
    manifest_prompt_binding = manifest.get("sources", {}).get("prompt")
    if not isinstance(prompt_binding, Mapping) or prompt_binding != manifest_prompt_binding:
        raise VaceCodedDraftError("prompt source binding mismatch")
    prompt_path = _verified_source_binding(root, prompt_binding, "prompt", inventory)
    try:
        prompt_source_text = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VaceCodedDraftError(f"cannot read prompt snapshot: {exc}") from exc
    if bundle.get("story_prompt") != prompt_source_text:
        raise VaceCodedDraftError(
            "bundle story_prompt differs from the hashed prompt snapshot"
        )
    provenance = _derive_source_provenance(bundle, semantic, media["frame_count"])
    return {
        "root": root,
        "bundle_path": bundle_file,
        "manifest_path": manifest_file,
        "story_id": story_id,
        "clay_path": clay_path,
        "clay_record": dict(clay),
        "media": media,
        "semantic_path": semantic_path,
        "semantic_sha256": _sha256(semantic_path),
        "prompt_path": prompt_path,
        **provenance,
    }


def _copy_snapshot(source: Path, target: Path, relative: str) -> dict[str, Any]:
    before = _sha256(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    after = _sha256(source)
    if before != after or _sha256(target) != before:
        raise VaceCodedDraftError(f"source changed while snapshotting {relative}")
    return _record(target, relative)


def _materialize_control(source: Path, target: Path) -> dict[str, Any]:
    """Create the pinned VACE 480p tensor timeline without AI interpolation."""
    target.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        "scale=832:480:flags=lanczos,"
        "fps=16:round=near,"
        "tpad=stop_mode=clone:stop_duration=0.25,"
        "trim=end_frame=81,"
        "setpts=N/(16*TB)"
    )
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y", "-v", "error", "-i", str(source),
        "-vf", filter_graph,
        "-frames:v", str(_CONTROL_FRAMES),
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(_CONTROL_FPS), "-movflags", "+faststart",
        "-metadata", "creation_time=1970-01-01T00:00:00Z",
        str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VaceCodedDraftError(f"cannot materialize VACE control video: {exc}") from exc
    media = _probe_media(target)
    if (
        media["frame_count"] != _CONTROL_FRAMES
        or not _same_number(media["fps"], _CONTROL_FPS)
        or media["resolution"] != list(_CONTROL_RESOLUTION)
    ):
        raise VaceCodedDraftError(
            "materialized VACE control media contract mismatch: "
            f"frames={media['frame_count']}, fps={media['fps']}, "
            f"resolution={media['resolution']}"
        )
    return {
        **_record(target, _CONTROL_PATH),
        **media,
        "source_clay_sha256": _sha256(source),
        "resampling": _RESAMPLING,
        "ai_interpolation": False,
    }


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    with path.open("xb") as handle:
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode("utf-8")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def build_vace_coded_draft_job(
    bundle_path: Path | str,
    manifest_path: Path | str,
    output_dir: Path | str,
) -> Path:
    """Validate and atomically publish one local clay-only VACE job."""
    verified = _verified_coded_draft(bundle_path, manifest_path)
    destination = Path(output_dir).resolve(strict=False)
    if destination.exists():
        raise VaceCodedDraftError(f"VACE job output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid4().hex}.staging"
    staging.mkdir()
    try:
        bundle_record = _copy_snapshot(
            verified["bundle_path"],
            staging / "source" / "coded_draft_bundle.json",
            "source/coded_draft_bundle.json",
        )
        manifest_record = _copy_snapshot(
            verified["manifest_path"],
            staging / "source" / "coded_draft_manifest.json",
            "source/coded_draft_manifest.json",
        )
        semantic_record = _copy_snapshot(
            verified["semantic_path"],
            staging / "source" / "semantic_plan.json",
            "source/semantic_plan.json",
        )
        prompt_record = _copy_snapshot(
            verified["prompt_path"],
            staging / "source" / "prompt.txt",
            "source/prompt.txt",
        )
        clay_target = staging / "source" / "clay.mp4"
        clay_base = _copy_snapshot(
            verified["clay_path"], clay_target, "source/clay.mp4"
        )
        copied_media = _probe_media(clay_target)
        _verify_media_record(copied_media, verified["media"])
        clay_record = {
            **clay_base,
            "media": copied_media,
            "role": "clay_only",
            "resampling": "none",
            "ai_interpolation": False,
            "source_sha256": verified["clay_record"]["sha256"],
        }

        control = _materialize_control(
            clay_target, staging / "control" / "src_video.mp4"
        )

        mask_path = staging / "control" / "src_mask.mp4"
        mask = write_full_generation_mask(
            mask_path,
            _CONTROL_RESOLUTION,
            _CONTROL_FPS,
            _CONTROL_FRAMES,
        )
        mask["path"] = "control/src_mask.mp4"

        components = {
            "story_prompt": verified["story_prompt"],
            "appearance_instruction": verified["appearance_instruction"],
        }
        prompt_text = f"{components['story_prompt']} {components['appearance_instruction']}"
        job_inventory = sorted(
            (
                _artifact_projection(record)
                for record in (
                    mask,
                    control,
                    clay_record,
                    bundle_record,
                    manifest_record,
                    prompt_record,
                    semantic_record,
                )
            ),
            key=lambda record: record["path"],
        )
        job = {
            "schema_version": _SCHEMA_VERSION,
            "story_id": verified["story_id"],
            "backend": "vace",
            "conditioning_mode": "source_video_edit",
            "control_mode": "source_video_edit",
            "source": {
                "coded_draft_bundle": bundle_record,
                "coded_draft_manifest": manifest_record,
                "prompt_snapshot": prompt_record,
                "semantic_plan": semantic_record,
                "conditioning_video": clay_record,
                "source_bundle_backend_consumed": False,
            },
            "control": control,
            "inventory": job_inventory,
            "prompt": {
                "text": prompt_text,
                "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
                "components": components,
                "normalization": "utf8_snapshot_exact_then_strip_outer_whitespace",
            },
            "motion_semantics": {
                "semantic_plan_sha256": semantic_record["sha256"],
                "explicit_trajectory_binding": {
                    "available": False,
                    "path": None,
                    "sha256": None,
                    "policy": "not_required_for_initial_clay_only_pilot",
                },
                "keyframes": [
                    {
                        "semantic_id": item["semantic_id"],
                        "t": item["t"],
                        "source_frame_index": item["frame_index"],
                        "control_frame_index": round(float(item["t"]) * (_CONTROL_FRAMES - 1)),
                    }
                    for item in verified["keyframes"]
                ],
            },
            "mapping": {
                "src_video": _CONTROL_PATH,
                "src_mask": "control/src_mask.mp4",
                "src_ref_images": None,
                "prompt": prompt_text,
            },
            "mask": mask,
            "vace": {
                "commit": VACE_COMMIT,
                "model_name": VACE_MODEL_NAME,
                "size": VACE_SIZE,
                "seed": VACE_SEED,
                "frame_num": _CONTROL_FRAMES,
                "fps": _CONTROL_FPS,
                "resolution": list(_CONTROL_RESOLUTION),
            },
            "timeline": {
                "source": {
                    "frame_count": copied_media["frame_count"],
                    "fps": copied_media["fps"],
                    "duration_seconds": copied_media["duration_seconds"],
                    "preserved_snapshot": True,
                },
                "control": {
                    "frame_count": _CONTROL_FRAMES,
                    "fps": float(_CONTROL_FPS),
                    "duration_seconds": _CONTROL_FRAMES / _CONTROL_FPS,
                    "resolution": list(_CONTROL_RESOLUTION),
                    "resampling": _RESAMPLING,
                },
                "ai_interpolation": False,
            },
            "server_contract": {
                "reference_images_nullable": True,
                "consume_pinned_control_timeline": True,
            },
            "api_calls": 0,
            "evidence": {
                "source_validation_passed": True,
                "inference_success": False,
            },
        }
        _write_json(staging / _JOB_NAME, job)
        verify_vace_coded_draft_job(staging / _JOB_NAME)
        os.replace(staging, destination)
        return destination / _JOB_NAME
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_vace_coded_draft_job(job_path: Path | str) -> dict[str, Any]:
    """Revalidate every local snapshot, decoded frame, mask, and job mapping."""
    path = Path(job_path).resolve(strict=True)
    if path.name != _JOB_NAME:
        raise VaceCodedDraftError(f"job file must be named {_JOB_NAME}")
    root = path.parent
    job = _read_object(path, "clay-only VACE job")
    required = {
        "schema_version", "story_id", "backend", "conditioning_mode",
        "control_mode", "source", "control", "inventory", "prompt", "motion_semantics", "mapping",
        "mask", "vace", "timeline", "server_contract", "api_calls", "evidence",
    }
    if set(job) != required:
        raise VaceCodedDraftError("VACE job has unknown or missing fields")
    if (
        job.get("schema_version") != _SCHEMA_VERSION
        or job.get("backend") != "vace"
        or job.get("conditioning_mode") != "source_video_edit"
        or job.get("control_mode") != "source_video_edit"
        or job.get("api_calls") != 0
    ):
        raise VaceCodedDraftError("VACE job identity contract is invalid")
    inventory = _verify_job_inventory(root, job.get("inventory"))
    source = job.get("source")
    expected_source_fields = {
        "coded_draft_bundle",
        "coded_draft_manifest",
        "prompt_snapshot",
        "semantic_plan",
        "conditioning_video",
        "source_bundle_backend_consumed",
    }
    if (
        not isinstance(source, Mapping)
        or set(source) != expected_source_fields
        or source.get("source_bundle_backend_consumed") is not False
    ):
        raise VaceCodedDraftError("VACE source contract is invalid")
    snapshot_paths: dict[str, Path] = {}
    for name, expected in (
        ("coded_draft_bundle", "source/coded_draft_bundle.json"),
        ("coded_draft_manifest", "source/coded_draft_manifest.json"),
        ("prompt_snapshot", "source/prompt.txt"),
        ("semantic_plan", "source/semantic_plan.json"),
    ):
        snapshot_paths[name] = _verify_record(
            root, source.get(name), name, expected_path=expected
        )
    clay = source.get("conditioning_video")
    clay_path = _verify_record(
        root, clay, "clay conditioning video", expected_path="source/clay.mp4"
    )
    if (
        not isinstance(clay, Mapping)
        or clay.get("role") != "clay_only"
        or clay.get("resampling") != "none"
        or clay.get("ai_interpolation") is not False
        or clay.get("source_sha256") != clay.get("sha256")
    ):
        raise VaceCodedDraftError("clay-only source policy is invalid")
    media = _probe_media(clay_path)
    _verify_media_record(media, clay.get("media"))

    source_bundle = _read_object(
        snapshot_paths["coded_draft_bundle"], "snapshotted coded-draft bundle"
    )
    source_manifest = _read_object(
        snapshot_paths["coded_draft_manifest"], "snapshotted coded-draft manifest"
    )
    source_semantic = _read_object(
        snapshot_paths["semantic_plan"], "snapshotted semantic plan"
    )
    try:
        source_prompt_text = snapshot_paths["prompt_snapshot"].read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise VaceCodedDraftError(f"cannot read snapshotted prompt: {exc}") from exc
    if (
        source_bundle.get("story_id") != job.get("story_id")
        or source_manifest.get("story_id") != job.get("story_id")
        or source_semantic.get("story_id") != job.get("story_id")
        or source_bundle.get("conditioning_mode") != "source_video_edit"
        or source_bundle.get("backend_consumed") is not False
        or source_manifest.get("backend_consumed") is not False
    ):
        raise VaceCodedDraftError("snapshotted coded-draft identity is invalid")
    manifest_bundle = source_manifest.get("outputs", {}).get("bundle")
    if (
        not isinstance(manifest_bundle, Mapping)
        or manifest_bundle.get("bytes") != source["coded_draft_bundle"]["bytes"]
        or manifest_bundle.get("sha256") != source["coded_draft_bundle"]["sha256"]
    ):
        raise VaceCodedDraftError("snapshotted bundle hash differs from its manifest")
    semantic_binding = source_bundle.get("motion_semantics", {}).get("semantic_plan")
    if (
        not isinstance(semantic_binding, Mapping)
        or semantic_binding != source_manifest.get("sources", {}).get("semantic_plan")
        or semantic_binding.get("bytes") != source["semantic_plan"]["bytes"]
        or semantic_binding.get("snapshot_sha256") != source["semantic_plan"]["sha256"]
    ):
        raise VaceCodedDraftError("snapshotted semantic plan binding is invalid")
    prompt_binding = source_bundle.get("source_bindings", {}).get("prompt")
    if (
        not isinstance(prompt_binding, Mapping)
        or prompt_binding != source_manifest.get("sources", {}).get("prompt")
        or prompt_binding.get("verified_equal") is not True
        or prompt_binding.get("original_sha256") != prompt_binding.get("snapshot_sha256")
        or prompt_binding.get("bytes") != source["prompt_snapshot"]["bytes"]
        or prompt_binding.get("snapshot_sha256") != source["prompt_snapshot"]["sha256"]
        or source_bundle.get("story_prompt") != source_prompt_text
    ):
        raise VaceCodedDraftError("snapshotted prompt binding is invalid")
    original_clay = source_bundle.get("conditioning_video")
    if (
        not isinstance(original_clay, Mapping)
        or original_clay != source_manifest.get("videos", {}).get("clay")
        or original_clay.get("sha256") != clay.get("source_sha256")
        or original_clay.get("bytes") != clay.get("bytes")
        or original_clay.get("media") != clay.get("media")
    ):
        raise VaceCodedDraftError("snapshotted clay binding is invalid")
    source_provenance = _derive_source_provenance(
        source_bundle, source_semantic, media["frame_count"]
    )

    control = job.get("control")
    control_path = _verify_record(
        root, control, "VACE control video", expected_path=_CONTROL_PATH
    )
    control_media = _probe_media(control_path)
    expected_control = {
        **_record(control_path, _CONTROL_PATH),
        **control_media,
        "source_clay_sha256": clay["sha256"],
        "resampling": _RESAMPLING,
        "ai_interpolation": False,
    }
    if (
        control != expected_control
        or control_media["frame_count"] != _CONTROL_FRAMES
        or not _same_number(control_media["fps"], _CONTROL_FPS)
        or control_media["resolution"] != list(_CONTROL_RESOLUTION)
    ):
        raise VaceCodedDraftError("VACE control media contract is invalid")

    prompt = job.get("prompt")
    if not isinstance(prompt, Mapping) or not isinstance(prompt.get("components"), Mapping):
        raise VaceCodedDraftError("VACE prompt contract is missing")
    components = prompt["components"]
    if set(components) != {"story_prompt", "appearance_instruction"} or any(
        not isinstance(components.get(field), str) or not components[field]
        for field in components
    ):
        raise VaceCodedDraftError("VACE prompt components are invalid")
    expected_components = {
        "story_prompt": source_provenance["story_prompt"],
        "appearance_instruction": source_provenance["appearance_instruction"],
    }
    if components != expected_components:
        raise VaceCodedDraftError("VACE prompt components differ from source snapshots")
    prompt_text = f"{components['story_prompt']} {components['appearance_instruction']}"
    expected_prompt = {
        "text": prompt_text,
        "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "components": dict(components),
        "normalization": "utf8_snapshot_exact_then_strip_outer_whitespace",
    }
    if prompt != expected_prompt:
        raise VaceCodedDraftError("VACE prompt hash or text mismatch")
    mapping = {
        "src_video": _CONTROL_PATH,
        "src_mask": "control/src_mask.mp4",
        "src_ref_images": None,
        "prompt": prompt_text,
    }
    if job.get("mapping") != mapping:
        raise VaceCodedDraftError("VACE clay-only mapping is invalid")

    motion = job.get("motion_semantics")
    if (
        not isinstance(motion, Mapping)
        or motion.get("semantic_plan_sha256") != source["semantic_plan"]["sha256"]
        or motion.get("explicit_trajectory_binding") != {
            "available": False,
            "path": None,
            "sha256": None,
            "policy": "not_required_for_initial_clay_only_pilot",
        }
        or not isinstance(motion.get("keyframes"), list)
        or not motion["keyframes"]
    ):
        raise VaceCodedDraftError("VACE motion semantics are invalid")
    expected_keyframes = [
        {
            "semantic_id": item["semantic_id"],
            "t": item["t"],
            "source_frame_index": round(float(item["t"]) * (media["frame_count"] - 1)),
            "control_frame_index": round(float(item["t"]) * (_CONTROL_FRAMES - 1)),
        }
        for item in source_provenance["keyframes"]
    ]
    if motion["keyframes"] != expected_keyframes:
        raise VaceCodedDraftError(
            "VACE motion keyframes differ from the bound semantic/source timeline"
        )

    timeline = {
        "source": {
            "frame_count": media["frame_count"],
            "fps": media["fps"],
            "duration_seconds": media["duration_seconds"],
            "preserved_snapshot": True,
        },
        "control": {
            "frame_count": _CONTROL_FRAMES,
            "fps": float(_CONTROL_FPS),
            "duration_seconds": _CONTROL_FRAMES / _CONTROL_FPS,
            "resolution": list(_CONTROL_RESOLUTION),
            "resampling": _RESAMPLING,
        },
        "ai_interpolation": False,
    }
    if job.get("timeline") != timeline:
        raise VaceCodedDraftError("VACE source timeline was changed")
    expected_vace = {
        "commit": VACE_COMMIT,
        "model_name": VACE_MODEL_NAME,
        "size": VACE_SIZE,
        "seed": VACE_SEED,
        "frame_num": _CONTROL_FRAMES,
        "fps": _CONTROL_FPS,
        "resolution": list(_CONTROL_RESOLUTION),
    }
    if job.get("vace") != expected_vace:
        raise VaceCodedDraftError("VACE model contract is invalid")
    if job.get("server_contract") != {
        "reference_images_nullable": True,
        "consume_pinned_control_timeline": True,
    }:
        raise VaceCodedDraftError("VACE server contract is invalid")
    mask = job.get("mask")
    mask_path = _verify_record(
        root, mask, "full generation mask", expected_path="control/src_mask.mp4"
    )
    expected_mask = {
        "path": "control/src_mask.mp4",
        "bytes": mask_path.stat().st_size,
        "sha256": _sha256(mask_path),
        "frame_count": _CONTROL_FRAMES,
        "dimensions": list(_CONTROL_RESOLUTION),
        "fps": _CONTROL_FPS,
        "mask_semantics": MASK_SEMANTICS,
        "mask_policy": MASK_POLICY,
        "actor_segmentation_claimed": False,
    }
    if mask != expected_mask:
        raise VaceCodedDraftError("VACE mask metadata mismatch")
    expected_inventory = sorted(
        (
            _artifact_projection(record)
            for record in (
                mask,
                control,
                clay,
                source["coded_draft_bundle"],
                source["coded_draft_manifest"],
                source["prompt_snapshot"],
                source["semantic_plan"],
            )
        ),
        key=lambda record: record["path"],
    )
    if inventory != expected_inventory:
        raise VaceCodedDraftError("VACE job inventory differs from bound source records")
    try:
        _verify_full_generation_mask(
            mask_path,
            _CONTROL_RESOLUTION,
            _CONTROL_FPS,
            _CONTROL_FRAMES,
        )
    except (OSError, RuntimeError) as exc:
        raise VaceCodedDraftError(f"VACE mask decode failed: {exc}") from exc
    if job.get("evidence") != {
        "source_validation_passed": True,
        "inference_success": False,
    }:
        raise VaceCodedDraftError("VACE evidence flags are invalid")
    return job


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--job", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            job_path = build_vace_coded_draft_job(
                args.bundle, args.manifest, args.output_dir
            )
            result = {"job": str(job_path), "sha256": _sha256(job_path), "api_calls": 0}
            print("VACE_CODED_DRAFT_PREPARED", json.dumps(result, ensure_ascii=False))
        else:
            job = verify_vace_coded_draft_job(args.job)
            print(
                "VACE_CODED_DRAFT_VALID",
                json.dumps({"story_id": job["story_id"], "api_calls": 0}),
            )
    except (VaceCodedDraftError, OSError, ValueError) as exc:
        print(f"VACE_CODED_DRAFT_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
