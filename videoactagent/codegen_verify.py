"""Offline verification for hash-bound Blender codegen render evidence."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
from typing import Any



class CodegenVerifyError(ValueError):
    """Raised when decoded render evidence does not satisfy the contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, path: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise CodegenVerifyError(f"unsafe {label} path") from exc
    return resolved


def _finite_vector(value: object, label: str, length: int = 3) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise CodegenVerifyError(f"{label} must be a vector")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise CodegenVerifyError(f"{label} contains non-finite values")
    return result


def _decode(path: Path, *, expected_frames: int, expected_fps: int, expected_resolution: tuple[int, int], expected_duration: float) -> dict[str, Any]:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise CodegenVerifyError("imageio_ffmpeg is required for video verification") from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise CodegenVerifyError(f"video is missing or empty: {path}")
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        try:
            metadata = next(reader)
        except StopIteration as exc:
            raise CodegenVerifyError("video has no decodable metadata") from exc
        if not isinstance(metadata, Mapping):
            raise CodegenVerifyError("video metadata is invalid")
        size = metadata.get("size")
        fps = metadata.get("fps")
        if not isinstance(size, (list, tuple)) or len(size) != 2 or not isinstance(fps, (int, float)) or not math.isfinite(float(fps)):
            raise CodegenVerifyError("video metadata is incomplete")
        resolution = (int(size[0]), int(size[1]))
        actual_fps = float(fps)
        if resolution != expected_resolution:
            raise CodegenVerifyError(f"video resolution {resolution} does not match {expected_resolution}")
        if abs(actual_fps - expected_fps) > 0.01:
            raise CodegenVerifyError(f"video fps {actual_fps} does not match {expected_fps}")
        expected_bytes = resolution[0] * resolution[1] * 3
        selected_indices = {0, expected_frames // 2, expected_frames - 1}
        selected: dict[int, str] = {}
        frame_count = 0
        digest = hashlib.sha256()
        for frame in reader:
            if len(frame) != expected_bytes:
                raise CodegenVerifyError("decoded frame byte count is invalid")
            digest.update(frame)
            if frame_count in selected_indices:
                selected[frame_count] = hashlib.sha256(frame).hexdigest()
            frame_count += 1
        if frame_count != expected_frames:
            raise CodegenVerifyError(f"video decoded {frame_count} frames, expected {expected_frames}")
        tolerance = max(0.01, 0.5 / expected_fps)
        decoded_duration = frame_count / actual_fps
        stream_duration = metadata.get("duration")
        if not isinstance(stream_duration, (int, float)) or not math.isfinite(float(stream_duration)) or float(stream_duration) <= 0:
            raise CodegenVerifyError("video stream duration is unavailable")
        if abs(decoded_duration - expected_duration) > tolerance or abs(float(stream_duration) - expected_duration) > tolerance:
            raise CodegenVerifyError("video duration does not match render contract")
        if len(set(selected.values())) < 2:
            raise CodegenVerifyError("sampled video frames are pixel-identical")
        return {"frame_count": frame_count, "fps": actual_fps, "resolution": [resolution[0], resolution[1]], "duration_seconds": decoded_duration, "stream_duration_seconds": float(stream_duration), "decoded_pixel_sha256": digest.hexdigest(), "sampled_pixel_sha256": [selected[index] for index in sorted(selected)]}
    except CodegenVerifyError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodegenVerifyError(f"could not decode video: {exc}") from exc
    finally:
        try:
            reader.close()
        except (AttributeError, OSError):
            pass


def _artifact_record(root: Path, record: object, label: str) -> Path:
    if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
        raise CodegenVerifyError(f"manifest {label} record is invalid")
    target = _inside(root, root / record["path"], label)
    if not target.is_file() or target.stat().st_size <= 0:
        raise CodegenVerifyError(f"manifest {label} is missing or empty")
    if record.get("sha256") != _sha256(target):
        raise CodegenVerifyError(f"manifest {label} hash mismatch")
    return target


def verify_codegen_render(*, job_root: Path, input_path: Path, render_dir: Path, position_tolerance: float = 0.15) -> dict[str, object]:
    """Decode the MP4 and compare its trajectory and artifact evidence."""

    root = Path(job_root).resolve()
    input_file = _inside(root, Path(input_path), "input")
    render = _inside(root, Path(render_dir), "render")
    if not input_file.is_file() or not render.is_dir():
        raise CodegenVerifyError("input or render directory is missing")
    if not isinstance(position_tolerance, (int, float)) or not math.isfinite(float(position_tolerance)) or float(position_tolerance) < 0:
        raise CodegenVerifyError("position_tolerance must be finite and non-negative")
    try:
        document = json.loads(input_file.read_text(encoding="utf-8"))
        manifest = json.loads((render / "codegen_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodegenVerifyError("input or manifest is not valid JSON") from exc
    if not isinstance(document, Mapping) or not isinstance(manifest, Mapping):
        raise CodegenVerifyError("input or manifest must be an object")
    if manifest.get("input_sha256") != _sha256(input_file):
        raise CodegenVerifyError("manifest input hash mismatch")
    contract = document.get("render_contract")
    if not isinstance(contract, Mapping):
        raise CodegenVerifyError("render contract is missing")
    fps = int(contract["fps"]); resolution = (int(contract["resolution"][0]), int(contract["resolution"][1]))
    expected_frames = int(contract["frame_end"]) - int(contract["frame_start"]) + 1
    expected_duration = float(contract["duration_seconds"])
    video_path = _artifact_record(render, manifest.get("video"), "video")
    blend_path = _artifact_record(render, manifest.get("blend"), "blend")
    for name in ("first", "middle", "last"):
        _artifact_record(render, {"path": f"frames/{name}.png", "sha256": _sha256(render / "frames" / f"{name}.png")}, f"{name} frame")
    video = _decode(video_path, expected_frames=expected_frames, expected_fps=fps, expected_resolution=resolution, expected_duration=expected_duration)

    actors = document.get("shotscript", {}).get("shots", [{}])[0].get("actors", [])
    actor_transforms = manifest.get("actor_transforms")
    if not isinstance(actor_transforms, Mapping):
        raise CodegenVerifyError("manifest actor transforms are missing")
    max_error = 0.0
    errors: dict[str, float] = {}
    expected_keys = {"K0", "K2", "K4"}
    tracks = {track["target"]["id"]: track for track in document["trajectory"]["tracks"]}
    for actor in actors:
        actor_id = actor.get("id")
        if not isinstance(actor_id, str) or actor_id not in actor_transforms or actor_id not in tracks:
            raise CodegenVerifyError(f"trajectory actor evidence is missing: {actor_id}")
        values = actor_transforms[actor_id]
        if not isinstance(values, Mapping) or set(values) != expected_keys:
            raise CodegenVerifyError(f"trajectory evidence for {actor_id} must contain K0/K2/K4")
        points = {point["keyframe_id"]: point for point in tracks[actor_id]["points"]}
        for key in sorted(expected_keys):
            observed = _finite_vector(values[key].get("observed"), f"{actor_id} {key} observed")
            expected = _finite_vector(points[key].get("world"), f"{actor_id} {key} expected", length=2)
            error = math.hypot(observed[0] - expected[0], observed[1] - expected[1])
            errors[f"{actor_id}.{key}"] = error
            max_error = max(max_error, error)
    if max_error > float(position_tolerance):
        raise CodegenVerifyError(f"trajectory position error {max_error:.6f} exceeds tolerance {position_tolerance}")
    inventory = []
    for path in sorted(render.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            inventory.append({"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size})
    return {"status": "succeeded", "manifest_sha256": _sha256(render / "codegen_manifest.json"), "video": video, "trajectory": {"max_error": max_error, "errors": errors}, "artifacts": inventory, "blend": {"path": str(blend_path.relative_to(root)).replace("\\", "/")}}
