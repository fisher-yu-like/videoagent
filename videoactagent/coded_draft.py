"""Build a hash-bound diagnostic/clay Blender coded-draft bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping
from uuid import uuid4

import imageio_ffmpeg
from PIL import Image

from videoactagent.semantic_plan import SemanticPlanError, SemanticStoryPlan


class CodedDraftError(ValueError):
    """Raised when a coded draft cannot be built without weakening evidence."""


def _positive_int(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def _resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if match is None:
        raise argparse.ArgumentTypeError("must be WIDTHxHEIGHT with positive integers")
    return int(match.group(1)), int(match.group(2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--semantic-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fps", type=_positive_int, default=24)
    parser.add_argument("--resolution", type=_resolution, default=(960, 540))
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _existing_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise CodedDraftError(f"{label} is not an existing file: {path}") from exc
    if not resolved.is_file():
        raise CodedDraftError(f"{label} is not a file: {resolved}")
    return resolved


def _output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    if resolved.exists():
        raise CodedDraftError(f"output already exists: {resolved}")
    if resolved.name in {"", ".", ".."}:
        raise CodedDraftError("output must name a new directory")
    return resolved


def _snapshot(source: Path, target: Path, label: str) -> dict[str, object]:
    before = _sha256(source)
    shutil.copyfile(source, target)
    after = _sha256(source)
    snapshot_hash = _sha256(target)
    if before != after or snapshot_hash != before:
        raise CodedDraftError(f"{label} changed while it was being snapshotted")
    try:
        target.chmod(stat.S_IREAD)
    except OSError as exc:
        raise CodedDraftError(f"could not make {label} snapshot read-only") from exc
    return {
        "original_path": str(source),
        "original_sha256": before,
        "snapshot_path": target.relative_to(target.parents[1]).as_posix(),
        "snapshot_sha256": snapshot_hash,
        "bytes": target.stat().st_size,
        "verified_equal": True,
    }


def _remove_staging(path: Path) -> None:
    """Remove a failed staging tree, including read-only source snapshots."""

    def make_writable_and_retry(function, failed_path, _error_info) -> None:
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def _run_profile(
    *,
    blender: Path,
    shotscript: Path,
    output_dir: Path,
    style: str,
    fps: int,
    resolution: tuple[int, int],
    log_path: Path,
) -> None:
    command = [
        sys.executable,
        "-m",
        "videoactagent.blender_runner",
        "--blender",
        str(blender),
        "--shotscript",
        str(shotscript),
        "--output-dir",
        str(output_dir),
        "--render-style",
        style,
        "--fps",
        str(fps),
        "--resolution",
        f"{resolution[0]}x{resolution[1]}",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"COMMAND={json.dumps(command, ensure_ascii=False)}\n"
        f"RETURN_CODE={completed.returncode}\n"
        "--- STDOUT ---\n"
        f"{completed.stdout or ''}"
        "\n--- STDERR ---\n"
        f"{completed.stderr or ''}",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise CodedDraftError(f"{style} Blender render failed with exit code {completed.returncode}")


def _close_reader(reader: object) -> None:
    try:
        reader.close()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def _decode_video(
    path: Path,
    *,
    expected_frames: int,
    expected_fps: int,
    expected_duration: float,
    expected_resolution: tuple[int, int],
    selected_indices: set[int],
) -> tuple[dict[str, object], dict[int, Image.Image]]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise CodedDraftError(f"rendered video is missing or empty: {path}")
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    selected: dict[int, Image.Image] = {}
    frame_count = 0
    try:
        try:
            metadata = next(reader)
        except StopIteration as exc:
            raise CodedDraftError(f"video has no decodable metadata: {path}") from exc
        if not isinstance(metadata, Mapping):
            raise CodedDraftError(f"video metadata is invalid: {path}")
        size = metadata.get("size")
        fps = metadata.get("fps")
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps))
        ):
            raise CodedDraftError(f"video stream metadata is incomplete: {path}")
        actual_resolution = (int(size[0]), int(size[1]))
        actual_fps = float(fps)
        if actual_resolution != expected_resolution:
            raise CodedDraftError(
                f"video resolution {actual_resolution} does not match {expected_resolution}: {path}"
            )
        if abs(actual_fps - expected_fps) > 0.01:
            raise CodedDraftError(
                f"video fps {actual_fps} does not match {expected_fps}: {path}"
            )

        width, height = actual_resolution
        expected_bytes = width * height * 3
        for index, frame in enumerate(reader):
            if len(frame) != expected_bytes:
                raise CodedDraftError(f"decoded frame {index} has an invalid byte count: {path}")
            if index in selected_indices:
                selected[index] = Image.frombytes("RGB", actual_resolution, frame).copy()
            frame_count += 1
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, CodedDraftError):
            raise
        raise CodedDraftError(f"could not decode video {path}: {exc}") from exc
    finally:
        _close_reader(reader)

    if frame_count != expected_frames:
        raise CodedDraftError(
            f"video decoded {frame_count} frames, expected exactly {expected_frames}: {path}"
        )
    missing = selected_indices - set(selected)
    if missing:
        raise CodedDraftError(f"semantic frames are missing after decode: {sorted(missing)}")
    decoded_duration = frame_count / actual_fps
    duration_tolerance = max(0.01, 0.5 / expected_fps)
    if abs(decoded_duration - expected_duration) > duration_tolerance:
        raise CodedDraftError(
            f"decoded duration {decoded_duration} does not match {expected_duration}: {path}"
        )
    stream_duration = metadata.get("duration")
    if isinstance(stream_duration, bool) or not isinstance(stream_duration, (int, float)):
        raise CodedDraftError(f"video stream duration is unavailable: {path}")
    if abs(float(stream_duration) - expected_duration) > duration_tolerance:
        raise CodedDraftError(
            f"stream duration {stream_duration} does not match {expected_duration}: {path}"
        )
    return (
        {
            "frame_count": frame_count,
            "fps": actual_fps,
            "duration_seconds": decoded_duration,
            "stream_duration_seconds": float(stream_duration),
            "resolution": [actual_resolution[0], actual_resolution[1]],
            "codec": metadata.get("codec"),
            "bytes": path.stat().st_size,
        },
        selected,
    )


def _save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.stem}.{uuid4().hex}.png"
    try:
        image.save(temporary, format="PNG")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _relative_record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _artifact_inventory(root: Path, below: Path) -> list[dict[str, object]]:
    return [
        _relative_record(path, root)
        for path in sorted(below.rglob("*"))
        if path.is_file()
    ]


def build_coded_draft(args: argparse.Namespace) -> Path:
    blender = _existing_file(args.blender, "Blender executable")
    shotscript = _existing_file(args.shotscript, "ShotScript")
    prompt = _existing_file(args.prompt, "prompt")
    semantic_path = _existing_file(args.semantic_plan, "semantic plan")
    output = _output_path(args.output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid4().hex}.staging"
    staging.mkdir()

    try:
        plan = SemanticStoryPlan.from_path(semantic_path)
        expected_frames = round(plan.duration_seconds * args.fps)
        if expected_frames <= 0:
            raise CodedDraftError("semantic duration and fps produce zero frames")

        sources_dir = staging / "sources"
        sources_dir.mkdir()
        source_records = {
            "shotscript": _snapshot(shotscript, sources_dir / "shotscript.json", "ShotScript"),
            "prompt": _snapshot(prompt, sources_dir / "prompt.txt", "prompt"),
            "semantic_plan": _snapshot(
                semantic_path, sources_dir / "semantic_plan.json", "semantic plan"
            ),
        }
        semantic_snapshot = sources_dir / "semantic_plan.json"
        snapshot_plan = SemanticStoryPlan.from_path(semantic_snapshot)
        if snapshot_plan.to_dict() != plan.to_dict():
            raise CodedDraftError("semantic plan snapshot changed meaning")

        for style in ("diagnostic", "clay"):
            _run_profile(
                blender=blender,
                shotscript=sources_dir / "shotscript.json",
                output_dir=staging / "renders" / style,
                style=style,
                fps=args.fps,
                resolution=args.resolution,
                log_path=staging / "logs" / f"{style}.log",
            )

        indices = [
            round(keyframe.t * (expected_frames - 1))
            for keyframe in plan.semantic_keyframes
        ]
        selected_indices = set(indices)
        videos: dict[str, dict[str, object]] = {}
        decoded: dict[str, dict[int, Image.Image]] = {}
        for style in ("diagnostic", "clay"):
            video = staging / "renders" / style / "station_proxy.mp4"
            media, selected = _decode_video(
                video,
                expected_frames=expected_frames,
                expected_fps=args.fps,
                expected_duration=plan.duration_seconds,
                expected_resolution=args.resolution,
                selected_indices=selected_indices,
            )
            videos[style] = {**_relative_record(video, staging), "media": media}
            decoded[style] = selected

        if videos["diagnostic"]["sha256"] == videos["clay"]["sha256"]:
            raise CodedDraftError("diagnostic and clay videos must not be identical")

        semantic_frames: list[dict[str, object]] = []
        sheet = Image.new(
            "RGB",
            (args.resolution[0] * 2, args.resolution[1] * len(indices)),
        )
        for row, (keyframe, index) in enumerate(zip(plan.semantic_keyframes, indices)):
            record: dict[str, object] = {
                "semantic_id": keyframe.id,
                "t": keyframe.t,
                "frame_index": index,
            }
            for column, style in enumerate(("diagnostic", "clay")):
                image = decoded[style][index]
                path = staging / "semantic_frames" / f"{keyframe.id}_{style}.png"
                _save_png(image, path)
                record[style] = _relative_record(path, staging)
                sheet.paste(image, (column * args.resolution[0], row * args.resolution[1]))
            semantic_frames.append(record)
        sheet_path = staging / "semantic_contact_sheet.png"
        _save_png(sheet, sheet_path)

        render_artifacts = _artifact_inventory(staging, staging / "renders")
        render_logs = _artifact_inventory(staging, staging / "logs")
        contact_sheet = _relative_record(sheet_path, staging)
        bundle = {
            "schema_version": "1.0",
            "story_id": plan.story_id,
            "conditioning_mode": "source_video_edit",
            "conditioning_video": videos["clay"],
            "appearance_instruction": plan.appearance_instruction,
            "story_prompt": (sources_dir / "prompt.txt").read_text(encoding="utf-8"),
            "motion_semantics": {
                "semantic_plan": source_records["semantic_plan"],
                "keyframes": semantic_frames,
            },
            "diagnostic_video": {
                **videos["diagnostic"],
                "role": "evidence_only",
                "backend_consumed": False,
            },
            "contact_sheet": contact_sheet,
            "source_bindings": source_records,
            "render_artifacts": render_artifacts,
            "render_logs": render_logs,
            "backend_consumed": False,
        }
        bundle_path = staging / "bundle.json"
        _atomic_json(bundle_path, bundle)

        for key, source in (
            ("shotscript", shotscript),
            ("prompt", prompt),
            ("semantic_plan", semantic_path),
        ):
            record = source_records[key]
            snapshot = staging / str(record["snapshot_path"])
            if _sha256(source) != record["original_sha256"] or _sha256(snapshot) != record["snapshot_sha256"]:
                raise CodedDraftError(f"{key} source binding changed during render")

        manifest = {
            "schema_version": "1.0",
            "story_id": plan.story_id,
            "expected_media": {
                "frame_count": expected_frames,
                "fps": args.fps,
                "duration_seconds": plan.duration_seconds,
                "resolution": list(args.resolution),
            },
            "sources": source_records,
            "videos": videos,
            "semantic_frames": semantic_frames,
            "outputs": {
                "contact_sheet": contact_sheet,
                "bundle": _relative_record(bundle_path, staging),
                "render_artifacts": render_artifacts,
                "render_logs": render_logs,
            },
            "backend_consumed": False,
        }
        _atomic_json(staging / "manifest.json", manifest)
        os.replace(staging, output)
        return output
    except Exception:
        try:
            _remove_staging(staging)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = build_coded_draft(args)
    except (
        CodedDraftError,
        SemanticPlanError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"CODED_DRAFT_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"CODED_DRAFT_OK {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
