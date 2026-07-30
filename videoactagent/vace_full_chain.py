"""Export and validate immutable whole-story VACE inference jobs.

The exporter deliberately consumes only the frozen v4 full-chain matrix.  It
does not submit work or contact a server; the accompanying shell runner is the
only component that can launch an inference process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping
import uuid

import imageio_ffmpeg
from PIL import Image

from videoactagent.full_chain import ExperimentJob, compile_matrix
from videoactagent.vace_inputs import (
    MASK_POLICY,
    MASK_SEMANTICS,
    _verify_full_generation_mask,
    sha256_file,
    write_full_generation_mask,
)


VACE_COMMIT = "48eb44f1c4be87cc65a98bff985a26976841e9f3"
WAN_COMMIT = "9737cba9c1c3c4d04b33fcad41c111989865d315"
_INFERENCE = {
    "model_name": "vace-1.3B",
    "size": "480p",
    "frame_num": 81,
    "fps": 16,
    "seed": 2026,
    "sample_steps": 20,
}
_SERVER_CONTRACT = {"vace_commit": VACE_COMMIT, "wan_commit": WAN_COMMIT}
_JOB_NAME = "vace_job.json"
_CONTROL_PATH = "control/src_video.mp4"
_FRAME_COUNT = 81
_FPS = 16
_CONTROL_DURATION = _FRAME_COUNT / _FPS


class VaceFullChainError(ValueError):
    """Raised when a whole-story VACE artifact violates its frozen contract."""


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
        raise VaceFullChainError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise VaceFullChainError(f"{label} must be a JSON object")
    return value


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contains_shot_id(value: Any) -> bool:
    if isinstance(value, dict):
        return "shot_id" in value or any(_contains_shot_id(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_shot_id(item) for item in value)
    return False


def _record(path: Path, relative: str) -> dict[str, Any]:
    return {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _copy_checked(source: Path, target: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    if not source.is_file():
        raise VaceFullChainError(f"v4 {label} is missing")
    if sha256_file(source) != expected_sha256:
        raise VaceFullChainError(f"v4 {label} hash changed from the frozen contract")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256_file(target) != expected_sha256:
        raise VaceFullChainError(f"snapshot {label} hash mismatch")
    return _record(target, target.relative_to(target.parents[1]).as_posix())


def _canonical_job(job: ExperimentJob | Mapping[str, Any]) -> ExperimentJob:
    """Resolve a caller value to exactly one frozen v4 VACE matrix job."""
    if isinstance(job, ExperimentJob):
        story_id, document, prompt = job.story_id, job.document, job.prompt
        backend = job.backend
    elif isinstance(job, Mapping):
        document_value = job.get("document", job)
        if not isinstance(document_value, Mapping):
            raise VaceFullChainError("job document must be an object")
        document = dict(document_value)
        story_id = job.get("story_id", document.get("story_id"))
        prompt = job.get("prompt", document.get("prompt"))
        backend = job.get("backend", document.get("backend"))
    else:
        raise VaceFullChainError("job must be an ExperimentJob or mapping")
    if not isinstance(story_id, str) or not isinstance(prompt, str) or backend != "vace":
        raise VaceFullChainError("job must identify one VACE whole-story request")
    if _contains_shot_id(document):
        raise VaceFullChainError("whole-story VACE jobs must not contain shot_id")

    matrix = compile_matrix(_project_root() / "configs" / "full_chain_matrix.json")
    matches = [
        candidate
        for candidate in matrix.jobs
        if candidate.backend == "vace" and candidate.story_id == story_id
    ]
    if len(matches) != 1:
        raise VaceFullChainError("job must identify exactly one frozen VACE story")
    candidate = matches[0]
    if document != candidate.document or prompt != candidate.prompt:
        raise VaceFullChainError("job differs from the frozen v4 VACE matrix")
    return candidate


def _canonical_sources(candidate: ExperimentJob) -> dict[str, tuple[Path, str, str]]:
    """Return files and required hashes from the frozen full-chain evidence."""
    root = _project_root() / "runs" / "work" / "whole_story_v4" / candidate.story_id
    manifest_path = root / "manifest.json"
    bundle_path = root / "bundles" / "vace.json"
    prompt_path = root / "sources" / "prompt.txt"
    manifest = _read_object(manifest_path, "v4 manifest")
    if manifest.get("story_id") != candidate.story_id or manifest.get("status") != "media_complete":
        raise VaceFullChainError("v4 manifest identity or media status is invalid")
    media = manifest.get("media")
    frames = manifest.get("inspection_frames")
    source_hashes = manifest.get("source_hashes")
    if not isinstance(media, dict) or not isinstance(frames, dict) or not isinstance(source_hashes, dict):
        raise VaceFullChainError("v4 manifest sources are incomplete")
    proxy_name = media.get("path")
    first = frames.get("first")
    if not isinstance(proxy_name, str) or Path(proxy_name).name != proxy_name:
        raise VaceFullChainError("v4 proxy path is unsafe")
    if not isinstance(first, dict) or not isinstance(first.get("path"), str):
        raise VaceFullChainError("v4 first frame is missing")
    first_relative = Path(first["path"])
    if first_relative.is_absolute() or ".." in first_relative.parts:
        raise VaceFullChainError("v4 first frame path is unsafe")
    for name in ("prompt_file_sha256", "submitted_prompt_sha256", "shotscript_sha256"):
        if not isinstance(source_hashes.get(name), str) or len(source_hashes[name]) != 64:
            raise VaceFullChainError(f"v4 {name} is invalid")
    if source_hashes != candidate.document.get("source_hashes"):
        raise VaceFullChainError("VACE source hashes differ from v4 manifest")
    bundle = _read_object(bundle_path, "v4 VACE bundle")
    if bundle != candidate.document:
        raise VaceFullChainError("v4 VACE bundle differs from frozen matrix job")
    paths = {
        "proxy_video": (root / proxy_name, "source/proxy.mp4", media.get("sha256")),
        "prompt": (prompt_path, "source/prompt.txt", source_hashes["prompt_file_sha256"]),
        "first_frame": (root / first_relative, "source/first.png", first.get("sha256")),
        "v4_manifest": (manifest_path, "source/manifest.json", sha256_file(manifest_path)),
        "vace_bundle": (bundle_path, "source/vace_bundle.json", sha256_file(bundle_path)),
    }
    checked: dict[str, tuple[Path, str, str]] = {}
    for name, (path, relative, digest) in paths.items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise VaceFullChainError(f"v4 {name} hash is invalid")
        checked[name] = (path, relative, digest)
    if sha256_file(prompt_path) != source_hashes["prompt_file_sha256"]:
        raise VaceFullChainError("v4 prompt hash changed")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if prompt != candidate.prompt or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != source_hashes["submitted_prompt_sha256"]:
        raise VaceFullChainError("v4 prompt text differs from frozen matrix job")
    if sha256_file(root / proxy_name) != media["sha256"]:
        raise VaceFullChainError("v4 proxy hash changed")
    if sha256_file(root / first_relative) != first["sha256"]:
        raise VaceFullChainError("v4 first frame hash changed")
    return checked


def _mask_record(path: Path, dimensions: tuple[int, int]) -> dict[str, Any]:
    return {
        "path": "src_mask.mp4",
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "frame_count": _FRAME_COUNT,
        "dimensions": [dimensions[0], dimensions[1]],
        "fps": _FPS,
        "mask_semantics": MASK_SEMANTICS,
        "mask_policy": MASK_POLICY,
        "actor_segmentation_claimed": False,
    }


def _close_media_reader(reader: object) -> None:
    """Release imageio-ffmpeg's generator-owned Windows pipe handles."""
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


def _decode_video_contract(
    path: Path,
    dimensions: tuple[int, int],
    fps: int,
    frame_count: int,
    label: str,
) -> None:
    try:
        counted, _ = imageio_ffmpeg.count_frames_and_secs(str(path))
        reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
        try:
            metadata = next(reader)
            if tuple(metadata.get("size", ())) != dimensions:
                raise VaceFullChainError(f"{label} dimensions are invalid")
            decoded_fps = metadata.get("fps")
            if not isinstance(decoded_fps, (int, float)) or abs(decoded_fps - fps) > 1e-6:
                raise VaceFullChainError(f"{label} fps is invalid")
            expected_bytes = dimensions[0] * dimensions[1] * 3
            decoded = 0
            for frame in reader:
                if len(frame) != expected_bytes:
                    raise VaceFullChainError(f"{label} frame payload is invalid")
                decoded += 1
        finally:
            _close_media_reader(reader)
    except (OSError, RuntimeError, StopIteration) as exc:
        raise VaceFullChainError(f"{label} cannot be decoded: {exc}") from exc
    if counted != frame_count or decoded != frame_count:
        raise VaceFullChainError(
            f"{label} must contain exactly {frame_count} frames, got {counted}/{decoded}"
        )


def _control_record(path: Path, dimensions: tuple[int, int], source_proxy_sha256: str) -> dict[str, Any]:
    return {
        **_record(path, _CONTROL_PATH),
        "frame_count": _FRAME_COUNT,
        "fps": _FPS,
        "dimensions": [dimensions[0], dimensions[1]],
        "timeline_duration_seconds": _CONTROL_DURATION,
        "source_proxy_sha256": source_proxy_sha256,
        "resampling": "ffmpeg_fps_duplicate_with_final_frame_clone",
        "ai_interpolation": False,
    }


def _materialize_control(source: Path, target: Path, dimensions: tuple[int, int]) -> dict[str, Any]:
    """Make an explicit 16fps control track without inventing motion frames."""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-i", str(source),
        "-vf", "fps=16:round=near,tpad=stop_mode=clone:stop_duration=0.125,trim=end_frame=81,setpts=N/(16*TB)",
        "-frames:v", str(_FRAME_COUNT), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(_FPS), "-movflags", "+faststart", "-metadata", "creation_time=1970-01-01T00:00:00Z",
        str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VaceFullChainError(f"cannot materialize full-length VACE control video: {exc}") from exc
    _decode_video_contract(target, dimensions, _FPS, _FRAME_COUNT, "control video")
    return _control_record(target, dimensions, sha256_file(source))


def export_vace_job(job: ExperimentJob | Mapping[str, Any], output_dir: Path | str) -> dict[str, Any]:
    """Snapshot one approved v4 source-video job into a new local directory."""
    candidate = _canonical_job(job)
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise VaceFullChainError(f"VACE job output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        sources = _canonical_sources(candidate)
        snapshots = {
            name: _copy_checked(source, staging / relative, digest, name)
            for name, (source, relative, digest) in sources.items()
        }
        first_path = staging / snapshots["first_frame"]["path"]
        try:
            with Image.open(first_path) as image:
                dimensions = image.size
                image.verify()
        except (OSError, ValueError) as exc:
            raise VaceFullChainError(f"snapshot first frame cannot be decoded: {exc}") from exc
        if dimensions[0] <= 0 or dimensions[1] <= 0:
            raise VaceFullChainError("snapshot first frame has invalid dimensions")
        control = _materialize_control(
            staging / snapshots["proxy_video"]["path"], staging / _CONTROL_PATH, dimensions
        )
        mask_path = staging / "src_mask.mp4"
        write_full_generation_mask(mask_path, dimensions, fps=_FPS, frame_count=_FRAME_COUNT)
        document = {
            "schema_version": "1.0",
            "story_id": candidate.story_id,
            "backend": "vace",
            "conditioning_mode": "source_video",
            "source_hashes": candidate.document["source_hashes"],
            "snapshots": snapshots,
            "control": control,
            "prompt": {
                "text": candidate.prompt,
                "sha256": hashlib.sha256(candidate.prompt.encode("utf-8")).hexdigest(),
            },
            "mapping": {
                "src_video": _CONTROL_PATH,
                "src_mask": "src_mask.mp4",
                "src_ref_images": ["source/first.png"],
                "prompt": candidate.prompt,
            },
            "mask": _mask_record(mask_path, dimensions),
            "inference": dict(_INFERENCE),
            "server_contract": dict(_SERVER_CONTRACT),
            "evidence": {"source_validation_passed": True, "inference_success": False},
        }
        if _contains_shot_id(document):
            raise AssertionError("exporter introduced forbidden shot_id")
        (staging / _JOB_NAME).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, destination)
        staging = None
        return validate_vace_job(destination)
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _checked_snapshot(job_dir: Path, snapshots: Any, name: str, path: str, expected_sha: str) -> None:
    if not isinstance(snapshots, dict) or snapshots.get(name) is None:
        raise VaceFullChainError(f"{name} snapshot is missing")
    record = snapshots[name]
    if not isinstance(record, dict) or record.get("path") != path:
        raise VaceFullChainError(f"{name} snapshot path is invalid")
    candidate = (job_dir / path).resolve()
    if job_dir not in candidate.parents or not candidate.is_file():
        raise VaceFullChainError(f"{name} snapshot is missing")
    actual = _record(candidate, path)
    if record != actual or actual["sha256"] != expected_sha:
        raise VaceFullChainError(f"{name} snapshot hash mismatch")


def validate_vace_job(job_dir: Path | str) -> dict[str, Any]:
    """Fail closed unless a local whole-story VACE job and every hash is valid."""
    root = Path(job_dir).resolve()
    document = _read_object(root / _JOB_NAME, "VACE job")
    required = {
        "schema_version", "story_id", "backend", "conditioning_mode", "source_hashes",
        "snapshots", "control", "prompt", "mapping", "mask", "inference", "server_contract", "evidence",
    }
    if set(document) != required:
        raise VaceFullChainError("VACE job has unknown or missing fields")
    if _contains_shot_id(document):
        raise VaceFullChainError("whole-story VACE job must not contain shot_id")
    story_id = document.get("story_id")
    if not isinstance(story_id, str):
        raise VaceFullChainError("VACE job story_id is invalid")
    # Resolve the canonical record independently.  The exported job deliberately
    # contains only source hashes, not the original backend document.
    matrix = compile_matrix(_project_root() / "configs" / "full_chain_matrix.json")
    matches = [item for item in matrix.jobs if item.backend == "vace" and item.story_id == story_id]
    if len(matches) != 1:
        raise VaceFullChainError("VACE job story is not in the frozen v4 matrix")
    canonical = matches[0]
    if document.get("schema_version") != "1.0" or document.get("backend") != "vace" or document.get("conditioning_mode") != "source_video":
        raise VaceFullChainError("VACE whole-story identity contract is invalid")
    if document.get("source_hashes") != canonical.document["source_hashes"]:
        raise VaceFullChainError("VACE job source hashes differ from frozen v4 matrix")
    prompt = document.get("prompt")
    expected_prompt = {"text": canonical.prompt, "sha256": hashlib.sha256(canonical.prompt.encode("utf-8")).hexdigest()}
    if prompt != expected_prompt:
        raise VaceFullChainError("VACE job prompt contract is invalid")
    if document.get("inference") != _INFERENCE:
        raise VaceFullChainError("VACE inference contract is invalid")
    if document.get("server_contract") != _SERVER_CONTRACT:
        raise VaceFullChainError("VACE server commit contract is invalid")
    if document.get("evidence") != {"source_validation_passed": True, "inference_success": False}:
        raise VaceFullChainError("VACE evidence flags are invalid")
    expected_sources = _canonical_sources(canonical)
    snapshots = document.get("snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(expected_sources):
        raise VaceFullChainError("VACE snapshots are incomplete")
    for name, (_source, relative, digest) in expected_sources.items():
        _checked_snapshot(root, snapshots, name, relative, digest)
    control_path = root / _CONTROL_PATH
    mapping = {
        "src_video": _CONTROL_PATH, "src_mask": "src_mask.mp4",
        "src_ref_images": ["source/first.png"], "prompt": canonical.prompt,
    }
    if document.get("mapping") != mapping:
        raise VaceFullChainError("VACE four-input mapping is invalid")
    first_path = root / "source" / "first.png"
    with Image.open(first_path) as image:
        dimensions = image.size
        image.verify()
    if not control_path.is_file() or document.get("control") != _control_record(
        control_path, dimensions, snapshots["proxy_video"]["sha256"]
    ):
        raise VaceFullChainError("VACE control dimensions differ from first frame")
    _decode_video_contract(control_path, dimensions, _FPS, _FRAME_COUNT, "control video")
    mask_path = root / "src_mask.mp4"
    expected_mask = _mask_record(mask_path, dimensions) if mask_path.is_file() else None
    if document.get("mask") != expected_mask:
        raise VaceFullChainError("VACE mask hash or metadata is invalid")
    try:
        _verify_full_generation_mask(mask_path, dimensions, _FPS, _FRAME_COUNT)
    except (OSError, RuntimeError) as exc:
        raise VaceFullChainError(f"VACE mask decode failed: {exc}") from exc
    return document


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("job_dir", type=Path)
    args = parser.parse_args()
    validated = validate_vace_job(args.job_dir)
    print("VACE_FULL_CHAIN_VALID", json.dumps(validated, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
