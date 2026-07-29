from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import imageio_ffmpeg
from PIL import Image


VACE_COMMIT = "48eb44f1c4be87cc65a98bff985a26976841e9f3"
SCHEMA_VERSION = "0.1"
EVIDENCE_SOURCE = "real_stage2_blender"
VACE_MODEL_NAME = "vace-1.3B"
VACE_SIZE = "480p"
VACE_SEED = 2025
MASK_SEMANTICS = "white_generate_black_retain"
MASK_POLICY = "full_frame_generate_from_real_stage2_dimensions"
REQUIRED_CONTROLS = ("first_frame", "last_frame", "proxy_video")


class ProvenanceError(ValueError):
    """Raised when a Stage 2 bundle cannot prove its media provenance."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"cannot read bundle: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceError("bundle must be a JSON object")
    return value


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProvenanceError(f"{label} must be positive")
    return float(value)


def _verified_control(
    bundle_dir: Path,
    shot_id: str,
    control_name: str,
    record: Any,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProvenanceError(f"{control_name} record is missing for shot {shot_id}")
    relative_text = record.get("path")
    if not isinstance(relative_text, str) or not relative_text:
        raise ProvenanceError(f"{control_name} path is missing for shot {shot_id}")
    relative = Path(relative_text)
    if relative.is_absolute():
        raise ProvenanceError(f"{control_name} path is outside bundle directory")
    bundle_root = bundle_dir.resolve()
    candidate = (bundle_root / relative).resolve()
    if not candidate.is_relative_to(bundle_root):
        raise ProvenanceError(f"{control_name} path is outside bundle directory")
    if not candidate.is_file():
        raise ProvenanceError(f"{control_name} is missing: {relative_text}")

    expected_bytes = record.get("bytes")
    expected_sha256 = record.get("sha256")
    actual_bytes = candidate.stat().st_size
    actual_sha256 = sha256_file(candidate)
    if expected_bytes != actual_bytes:
        raise ProvenanceError(
            f"{control_name} byte mismatch: expected {expected_bytes}, got {actual_bytes}"
        )
    if expected_sha256 != actual_sha256:
        raise ProvenanceError(f"{control_name} sha256 mismatch")
    return {
        "path": "source/" + relative.as_posix(),
        "bytes": actual_bytes,
        "sha256": actual_sha256,
        "resolved_path": candidate,
    }


def load_verified_shot(bundle_path: Path, shot_id: str) -> dict[str, Any]:
    bundle_path = Path(bundle_path).resolve()
    bundle = _load_json_object(bundle_path)
    fps_value = _positive_number(bundle.get("fps"), "fps")
    fps: int | float = int(fps_value) if fps_value.is_integer() else fps_value

    shots = bundle.get("shots")
    if not isinstance(shots, list):
        raise ProvenanceError("shots must be a list")
    seen_shot_ids: set[Any] = set()
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        candidate_id = shot.get("shot_id")
        if candidate_id in seen_shot_ids:
            raise ProvenanceError(f"duplicate shot id: {candidate_id}")
        seen_shot_ids.add(candidate_id)
    matches = [
        shot
        for shot in shots
        if isinstance(shot, dict) and shot.get("shot_id") == shot_id
    ]
    if not matches:
        raise ProvenanceError(f"missing shot id: {shot_id}")
    shot = matches[0]

    frame_range = shot.get("frame_range")
    if (
        not isinstance(frame_range, list)
        or len(frame_range) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in frame_range)
    ):
        raise ProvenanceError("frame_range must contain two integers")
    frame_start, frame_end = frame_range
    source_frame_count = frame_end - frame_start + 1
    if source_frame_count <= 0:
        raise ProvenanceError("frame_range inclusive length must be positive")
    duration = _positive_number(shot.get("duration"), "duration")
    expected_source_frames = round(duration * fps_value)
    if source_frame_count != expected_source_frames:
        raise ProvenanceError(
            "duration-derived frame count does not match frame_range frame count"
        )

    controls = shot.get("controls")
    if not isinstance(controls, dict):
        raise ProvenanceError(f"controls are missing for shot {shot_id}")
    verified_controls = {
        name: _verified_control(bundle_path.parent, shot_id, name, controls.get(name))
        for name in REQUIRED_CONTROLS
    }

    first_path = verified_controls["first_frame"]["resolved_path"]
    try:
        with Image.open(first_path) as image:
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise ProvenanceError(f"first_frame is not a valid image: {exc}") from exc
    if width <= 0 or height <= 0:
        raise ProvenanceError("first_frame dimensions must be positive")

    return {
        "bundle_path": bundle_path,
        "bundle_bytes": bundle_path.stat().st_size,
        "bundle_sha256": sha256_file(bundle_path),
        "shot": shot,
        "shot_id": shot_id,
        "fps": fps,
        "duration": duration,
        "frame_range": [frame_start, frame_end],
        "source_frame_count": source_frame_count,
        "expected_preprocessed_frames": ((source_frame_count - 1) // 4) * 4 + 1,
        "dimensions": [width, height],
        "controls": verified_controls,
    }


def _verify_full_generation_mask(
    path: Path,
    dimensions: tuple[int, int],
    fps: int | float,
    frame_count: int,
) -> None:
    counted_frames, _ = imageio_ffmpeg.count_frames_and_secs(str(path))
    if counted_frames != frame_count:
        raise RuntimeError(
            f"mask frame count mismatch: {counted_frames} != {frame_count}"
        )
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    decoded_count = 0
    try:
        metadata = next(reader)
        if tuple(metadata.get("size", ())) != dimensions:
            raise RuntimeError(
                f"mask dimensions mismatch: {metadata.get('size')} != {dimensions}"
            )
        decoded_fps = metadata.get("fps")
        if not isinstance(decoded_fps, (int, float)) or abs(decoded_fps - fps) > 1e-6:
            raise RuntimeError(f"mask fps mismatch: {decoded_fps} != {fps}")
        expected_frame_bytes = dimensions[0] * dimensions[1] * 3
        for _ in range(counted_frames):
            frame = next(reader)
            decoded_count += 1
            if len(frame) != expected_frame_bytes:
                raise RuntimeError(
                    f"mask frame {decoded_count} has {len(frame)} bytes, "
                    f"expected {expected_frame_bytes}"
                )
            if min(frame) < 250:
                raise RuntimeError(f"mask frame {decoded_count} is not full white")
    finally:
        # imageio-ffmpeg 0.6.0 leaves stdin/stdout open when ffmpeg has
        # already exited before generator.close() runs. Retain the process
        # while closing the generator, then close any surviving pipe handles.
        generator_frame = getattr(reader, "gi_frame", None)
        process = (
            generator_frame.f_locals.get("process")
            if generator_frame is not None
            else None
        )
        try:
            reader.close()
        finally:
            if process is not None:
                for pipe_name in ("stdin", "stdout"):
                    pipe = getattr(process, pipe_name, None)
                    if pipe is not None and not pipe.closed:
                        pipe.close()
    if decoded_count != frame_count:
        raise RuntimeError(
            f"mask frame count mismatch: {decoded_count} != {frame_count}"
        )


def write_full_generation_mask(
    target: Path,
    dimensions: tuple[int, int],
    fps: int | float,
    frame_count: int,
) -> dict[str, Any]:
    if dimensions[0] <= 0 or dimensions[1] <= 0:
        raise ValueError("mask dimensions must be positive")
    if fps <= 0:
        raise ValueError("mask fps must be positive")
    if frame_count <= 0:
        raise ValueError("mask frame_count must be positive")

    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.stem}.{uuid.uuid4().hex}.tmp{target.suffix}"
    )
    white_frame = bytes([255]) * dimensions[0] * dimensions[1] * 3
    try:
        writer = imageio_ffmpeg.write_frames(
            str(temporary),
            dimensions,
            pix_fmt_in="rgb24",
            pix_fmt_out="yuv420p",
            fps=fps,
            codec="libx264",
            macro_block_size=1,
            ffmpeg_log_level="error",
            output_params=[
                "-movflags",
                "+faststart",
                "-metadata",
                "creation_time=1970-01-01T00:00:00Z",
            ],
        )
        try:
            writer.send(None)
            for _ in range(frame_count):
                writer.send(white_frame)
        finally:
            writer.close()
        _verify_full_generation_mask(temporary, dimensions, fps, frame_count)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "path": "src_mask.mp4",
        "bytes": target.stat().st_size,
        "sha256": sha256_file(target),
        "frame_count": frame_count,
        "dimensions": [dimensions[0], dimensions[1]],
        "fps": fps,
        "mask_semantics": MASK_SEMANTICS,
        "mask_policy": MASK_POLICY,
        "actor_segmentation_claimed": False,
    }


def build_vace_inputs(bundle_path: Path, shot_id: str, output_dir: Path) -> Path:
    verified = load_verified_shot(bundle_path, shot_id)
    prompt_fields = verified["shot"].get("prompts")
    if not isinstance(prompt_fields, dict):
        raise ProvenanceError(f"prompts are missing for shot {shot_id}")
    cinematic = prompt_fields.get("cinematic")
    timed = prompt_fields.get("timed")
    if not isinstance(cinematic, str) or not isinstance(timed, str):
        raise ProvenanceError("cinematic and timed prompts must be strings")
    prompt = cinematic + " " + timed

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    mask_record = write_full_generation_mask(
        output_dir / "src_mask.mp4",
        tuple(verified["dimensions"]),
        verified["fps"],
        verified["source_frame_count"],
    )
    controls_for_manifest = {
        name: {key: record[key] for key in ("path", "bytes", "sha256")}
        for name, record in verified["controls"].items()
    }
    job = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evidence_source": EVIDENCE_SOURCE,
        "selected_shot_id": shot_id,
        "source_bundle": {
            "path": "source/control_bundle.json",
            "bytes": verified["bundle_bytes"],
            "sha256": verified["bundle_sha256"],
        },
        "source": {
            "shot_id": verified["shot_id"],
            "fps": verified["fps"],
            "duration_seconds": verified["duration"],
            "frame_range": verified["frame_range"],
            "source_frame_count": verified["source_frame_count"],
            "expected_preprocessed_frames": verified["expected_preprocessed_frames"],
            "dimensions": verified["dimensions"],
            "controls": controls_for_manifest,
        },
        "mapping_path_bases": {"source": "bundle_parent"},
        "mapping": {
            "src_video": verified["controls"]["proxy_video"]["path"],
            "src_mask": "src_mask.mp4",
            "src_ref_images": [verified["controls"]["first_frame"]["path"]],
            "prompt": prompt,
        },
        "prompt": {
            "text": prompt,
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
        "mask": mask_record,
        "vace": {
            "commit": VACE_COMMIT,
            "model_name": VACE_MODEL_NAME,
            "size": VACE_SIZE,
            "seed": VACE_SEED,
        },
        "evidence": {
            "source_validation_passed": False,
            "inference_success": False,
        },
    }
    job_path = output_dir / "vace_job.json"
    temporary = output_dir / ".vace_job.json.tmp"
    temporary.write_text(
        json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(job_path)
    return job_path


def verify_prepared_job(job_path: Path, bundle_path: Path) -> dict[str, Any]:
    job_path = Path(job_path).resolve()
    job = _load_json_object(job_path)
    if job.get("schema_version") != SCHEMA_VERSION:
        raise ProvenanceError("prepared job schema_version mismatch")
    if job.get("evidence_source") != EVIDENCE_SOURCE:
        raise ProvenanceError("prepared job evidence_source mismatch")
    created_at = job.get("created_at")
    if (
        not isinstance(created_at, str)
        or "T" not in created_at
        or not created_at.endswith("Z")
    ):
        raise ProvenanceError("prepared job created_at must be an ISO-8601 UTC Z timestamp")
    try:
        parsed_created_at = datetime.fromisoformat(created_at[:-1] + "+00:00")
    except ValueError as exc:
        raise ProvenanceError(
            "prepared job created_at must be an ISO-8601 UTC Z timestamp"
        ) from exc
    if parsed_created_at.utcoffset() != timezone.utc.utcoffset(parsed_created_at):
        raise ProvenanceError("prepared job created_at must be UTC")
    shot_id = job.get("selected_shot_id")
    if not isinstance(shot_id, str):
        raise ProvenanceError("prepared job is missing selected_shot_id")
    verified = load_verified_shot(bundle_path, shot_id)
    if job.get("source_bundle", {}).get("sha256") != verified["bundle_sha256"]:
        raise ProvenanceError("prepared job source bundle hash mismatch")
    if job.get("source_bundle", {}).get("bytes") != verified["bundle_bytes"]:
        raise ProvenanceError("prepared job source bundle byte mismatch")
    if job.get("source_bundle", {}).get("path") != "source/control_bundle.json":
        raise ProvenanceError("prepared job source bundle path mismatch")

    source = job.get("source", {})
    expected_source_metadata = {
        "shot_id": verified["shot_id"],
        "fps": verified["fps"],
        "duration_seconds": verified["duration"],
        "frame_range": verified["frame_range"],
        "source_frame_count": verified["source_frame_count"],
        "expected_preprocessed_frames": verified["expected_preprocessed_frames"],
        "dimensions": verified["dimensions"],
    }
    for field, expected in expected_source_metadata.items():
        if source.get(field) != expected:
            raise ProvenanceError(f"prepared job source {field} mismatch")

    recorded_controls = source.get("controls", {})
    for name in REQUIRED_CONTROLS:
        actual = verified["controls"][name]
        recorded = recorded_controls.get(name, {})
        for field in ("path", "bytes", "sha256"):
            if recorded.get(field) != actual[field]:
                raise ProvenanceError(
                    f"prepared job {name} {field} does not match verified source"
                )

    prompt_fields = verified["shot"].get("prompts", {})
    expected_prompt = prompt_fields.get("cinematic", "") + " " + prompt_fields.get(
        "timed", ""
    )
    expected_prompt_record = {
        "text": expected_prompt,
        "sha256": hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest(),
    }
    if job.get("prompt") != expected_prompt_record:
        raise ProvenanceError("prepared job prompt contract mismatch")

    if job.get("mapping_path_bases") != {"source": "bundle_parent"}:
        raise ProvenanceError("prepared job mapping path bases mismatch")
    expected_mapping = {
        "src_video": verified["controls"]["proxy_video"]["path"],
        "src_mask": "src_mask.mp4",
        "src_ref_images": [verified["controls"]["first_frame"]["path"]],
        "prompt": expected_prompt,
    }
    if job.get("mapping") != expected_mapping:
        raise ProvenanceError("prepared job four-input mapping mismatch")

    expected_vace = {
        "commit": VACE_COMMIT,
        "model_name": VACE_MODEL_NAME,
        "size": VACE_SIZE,
        "seed": VACE_SEED,
    }
    if job.get("vace") != expected_vace:
        raise ProvenanceError("prepared job VACE settings mismatch")

    mask = job.get("mask", {})
    mask_relative_text = mask.get("path")
    if not isinstance(mask_relative_text, str) or not mask_relative_text:
        raise ProvenanceError("prepared mask path is missing")
    mask_relative = Path(mask_relative_text)
    job_root = job_path.parent.resolve()
    if mask_relative.is_absolute():
        raise ProvenanceError("prepared mask path is outside job directory")
    mask_path = (job_root / mask_relative).resolve()
    if not mask_path.is_relative_to(job_root):
        raise ProvenanceError("prepared mask path is outside job directory")
    if job.get("mapping", {}).get("src_mask") != mask_relative_text:
        raise ProvenanceError("prepared mask mapping does not match mask path")
    if not mask_path.is_file():
        raise ProvenanceError("prepared mask is missing")
    expected_mask = {
        "path": "src_mask.mp4",
        "bytes": mask_path.stat().st_size,
        "sha256": sha256_file(mask_path),
        "frame_count": verified["source_frame_count"],
        "dimensions": verified["dimensions"],
        "fps": verified["fps"],
        "mask_semantics": MASK_SEMANTICS,
        "mask_policy": MASK_POLICY,
        "actor_segmentation_claimed": False,
    }
    if mask != expected_mask:
        raise ProvenanceError("prepared mask record mismatch")
    _verify_full_generation_mask(
        mask_path,
        tuple(verified["dimensions"]),
        verified["fps"],
        verified["source_frame_count"],
    )
    if job.get("evidence") != {
        "source_validation_passed": False,
        "inference_success": False,
    }:
        raise ProvenanceError("prepared job evidence flags must remain false")
    return job


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare verified Stage 2 media for VACE")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--shot", required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "prepare":
        job_path = build_vace_inputs(args.bundle, args.shot, args.output_dir)
        verify_prepared_job(job_path, args.bundle)
        print(
            "VACE_INPUTS_PREPARED",
            json.dumps(
                {"job": str(job_path.resolve()), "sha256": sha256_file(job_path)},
                ensure_ascii=False,
            ),
        )


if __name__ == "__main__":
    main()
