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
from videoactagent.shotscript import ShotScript, ShotScriptError
from videoactagent.trajectory import TrajectoryInstruction


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
    parser.add_argument("--render-timeout", type=_positive_int, default=300)
    parser.add_argument("--trajectory", type=Path)
    parser.add_argument("--trajectory-authoring", type=Path)
    args = parser.parse_args(argv)
    if (args.trajectory is None) != (args.trajectory_authoring is None):
        parser.error("--trajectory and --trajectory-authoring must be used together")
    return args


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        payload = (
            json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CodedDraftError(f"cannot serialize JSON: {exc}") from exc
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
    failure_dir: Path,
    timeout: int,
    trajectory: Path | None = None,
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
        "--timeout",
        str(timeout),
    ]
    if trajectory is not None:
        command.extend(["--trajectory", str(trajectory)])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired as exc:
        log = _render_log(
            command,
            timeout=timeout,
            outcome="timeout",
            returncode=None,
            stdout=_subprocess_text(exc.stdout),
            stderr=_subprocess_text(exc.stderr),
        )
        _publish_failure_log(failure_dir, style, log)
        raise CodedDraftError(f"{style} Blender runner exceeded {timeout} seconds") from exc

    log = _render_log(
        command,
        timeout=timeout,
        outcome="completed",
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log, encoding="utf-8")
    if completed.returncode != 0:
        _publish_failure_log(failure_dir, style, log)
        raise CodedDraftError(f"{style} Blender render failed with exit code {completed.returncode}")


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _render_log(
    command: list[str],
    *,
    timeout: int,
    outcome: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> str:
    return (
        f"COMMAND={json.dumps(command, ensure_ascii=False)}\n"
        f"TIMEOUT_SECONDS={timeout}\n"
        f"OUTCOME={outcome}\n"
        f"RETURN_CODE={returncode}\n"
        "--- STDOUT ---\n"
        f"{stdout}"
        "\n--- STDERR ---\n"
        f"{stderr}"
    )


def _publish_failure_log(failure_dir: Path, style: str, contents: str) -> None:
    temporary = failure_dir.parent / f".{failure_dir.name}.{uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        log_path = temporary / f"{style}.log"
        with log_path.open("xb") as handle:
            handle.write(contents.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if failure_dir.exists():
            raise CodedDraftError(f"failure evidence already exists: {failure_dir}")
        os.rename(temporary, failure_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


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
    pixel_digest = hashlib.sha256()
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
            pixel_digest.update(frame)
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
    if (
        isinstance(stream_duration, bool)
        or not isinstance(stream_duration, (int, float))
        or not math.isfinite(float(stream_duration))
        or float(stream_duration) <= 0
    ):
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
            "decoded_pixel_sha256": pixel_digest.hexdigest(),
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


def _verify_final_inventory(
    root: Path, records: list[Mapping[str, object]], *, ignored: set[str]
) -> None:
    expected: dict[str, Mapping[str, object]] = {}
    resolved_root = root.resolve(strict=True)
    for record in records:
        path_text = record.get("path")
        if not isinstance(path_text, str) or not path_text:
            raise CodedDraftError("inventory record has no safe path")
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise CodedDraftError(f"unsafe inventory path: {path_text}")
        target = (resolved_root / relative).resolve(strict=False)
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise CodedDraftError(f"inventory path escapes staging: {path_text}") from exc
        canonical = relative.as_posix()
        if canonical in expected:
            raise CodedDraftError(f"duplicate inventory path: {canonical}")
        expected[canonical] = record

    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in ignored
    }
    if set(actual) != set(expected):
        raise CodedDraftError(
            "final staging inventory differs from the declared manifest inventory"
        )
    for relative, target in actual.items():
        record = expected[relative]
        if record.get("bytes") != target.stat().st_size or record.get("sha256") != _sha256(target):
            raise CodedDraftError(f"final hash verification failed: {relative}")


def _canonicalize_video_records(
    videos: dict[str, dict[str, object]],
    render_artifacts: list[dict[str, object]],
) -> None:
    canonical = {str(record["path"]): record for record in render_artifacts}
    for style in ("diagnostic", "clay"):
        current = videos[style]
        record = canonical.get(str(current["path"]))
        if record is None or any(
            current[field] != record[field] for field in ("path", "bytes", "sha256")
        ):
            raise CodedDraftError(
                f"{style} video changed between media decode and artifact inventory"
            )
        videos[style] = {**record, "media": current["media"]}


def _verify_json_artifact_references(
    documents: list[Mapping[str, object]],
    canonical_records: list[Mapping[str, object]],
) -> None:
    canonical = {str(record["path"]): record for record in canonical_records}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if {"path", "bytes", "sha256"}.issubset(value):
                path = value.get("path")
                record = canonical.get(str(path))
                if record is None or any(
                    value.get(field) != record.get(field)
                    for field in ("path", "bytes", "sha256")
                ):
                    raise CodedDraftError(
                        f"JSON artifact reference differs from canonical inventory: {path}"
                    )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for document in documents:
        visit(document)


def _validate_semantic_ids(plan: SemanticStoryPlan) -> None:
    ids = [keyframe.id for keyframe in plan.semantic_keyframes]
    expected = [f"K{index}" for index in range(len(ids))]
    if ids != expected:
        raise CodedDraftError(
            f"semantic keyframe ids must be the continuous sequence {expected}"
        )


def _safe_semantic_frame_path(root: Path, filename: str) -> Path:
    resolved_root = root.resolve(strict=False)
    target = (resolved_root / filename).resolve(strict=False)
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise CodedDraftError("semantic frame path escapes its output directory") from exc
    return target


def _validate_shotscript(
    path: Path, plan: SemanticStoryPlan, effective_fps: int
) -> tuple[ShotScript, int]:
    script = ShotScript.from_path(path)
    if script.scene_id != plan.story_id:
        raise CodedDraftError(
            f"ShotScript scene_id {script.scene_id!r} does not match story_id {plan.story_id!r}"
        )
    if len(script.shots) != 1:
        raise CodedDraftError("coded drafts require exactly one whole-story shot")
    durations = [shot.duration for shot in script.shots]
    if any(not math.isfinite(duration) or duration <= 0 for duration in durations):
        raise CodedDraftError("every ShotScript duration must be finite and positive")
    total_duration = math.fsum(durations)
    if not math.isclose(
        total_duration, plan.duration_seconds, rel_tol=0.0, abs_tol=1e-9
    ):
        raise CodedDraftError(
            f"ShotScript duration {total_duration} does not match semantic duration {plan.duration_seconds}"
        )
    frames = [round(duration * effective_fps) for duration in durations]
    if any(frame_count <= 0 for frame_count in frames):
        raise CodedDraftError("every ShotScript shot must round to at least one frame")
    return script, sum(frames)


def _validate_render_report(
    path: Path,
    *,
    style: str,
    fps: int,
    resolution: tuple[int, int],
) -> None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodedDraftError(f"cannot parse {style} render report: {exc}") from exc
    if not isinstance(report, Mapping) or report.get("render_style") != style:
        raise CodedDraftError(f"{style} render report has the wrong render_style")
    profile = report.get("effective_profile")
    if not isinstance(profile, Mapping):
        raise CodedDraftError(f"{style} render report has no effective_profile")
    if profile.get("fps") != fps or profile.get("resolution") != list(resolution):
        raise CodedDraftError(f"{style} render report profile does not match the request")


def _validate_trajectory_sources(
    trajectory_path: Path,
    authoring_path: Path,
    script: ShotScript,
) -> tuple[TrajectoryInstruction, dict[str, Any]]:
    try:
        instruction = TrajectoryInstruction.from_path(trajectory_path)
        authoring = json.loads(authoring_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CodedDraftError(f"cannot validate explicit trajectory: {exc}") from exc
    if not isinstance(authoring, Mapping):
        raise CodedDraftError("trajectory authoring evidence must be one object")
    if (
        authoring.get("trajectory_sha256") != _sha256(trajectory_path)
        or authoring.get("projection_policy")
        != "top_down_world_bounds_linear_y_up_z0"
        or authoring.get("camera_policy") != "shotscript_locked"
        or authoring.get("auto_filled_points") != 0
        or not isinstance(authoring.get("author_id"), str)
        or not str(authoring["author_id"]).strip()
    ):
        raise CodedDraftError("trajectory authoring evidence binding is invalid")
    relative = authoring.get("trajectory_path")
    if relative != trajectory_path.name or trajectory_path.parent != authoring_path.parent:
        raise CodedDraftError("trajectory authoring path binding is invalid")
    if len(script.shots) != 1:
        raise CodedDraftError("explicit trajectory requires one whole-story shot")
    instruction.validate_identity(script.scene_id, script.shots[0].shot_id)
    if not math.isclose(instruction.duration_seconds, script.shots[0].duration, abs_tol=1e-9):
        raise CodedDraftError("trajectory duration differs from ShotScript")
    expected_actors = {actor.actor_id for actor in script.shots[0].actors}
    actor_tracks = [track for track in instruction.tracks if track.target_type == "actor"]
    if (
        len(actor_tracks) != len(expected_actors)
        or {track.target_id for track in actor_tracks} != expected_actors
        or any(track.primitive != "polyline" or track.semantic != "move" for track in actor_tracks)
    ):
        raise CodedDraftError("trajectory must contain exactly one move track per actor")
    return instruction, dict(authoring)


def _validate_trajectory_render_manifest(
    path: Path,
    *,
    style: str,
    fps: int,
    resolution: tuple[int, int],
    trajectory_sha256: str,
    actor_ids: set[str],
) -> None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodedDraftError(f"cannot parse {style} trajectory render manifest: {exc}") from exc
    profile = report.get("effective_profile") if isinstance(report, Mapping) else None
    applied = report.get("applied_tracks") if isinstance(report, Mapping) else None
    applied_actor_ids = {
        value.get("target_id")
        for value in applied.values()
        if isinstance(value, Mapping) and value.get("target_type") == "actor"
    } if isinstance(applied, Mapping) else set()
    if (
        report.get("render_style") != style
        or not isinstance(profile, Mapping)
        or profile.get("fps") != fps
        or profile.get("resolution") != list(resolution)
        or report.get("trajectory_sha256") != trajectory_sha256
        or applied_actor_ids != actor_ids
    ):
        raise CodedDraftError(f"{style} trajectory render manifest binding is invalid")


def build_coded_draft(args: argparse.Namespace) -> Path:
    blender = _existing_file(args.blender, "Blender executable")
    shotscript = _existing_file(args.shotscript, "ShotScript")
    prompt = _existing_file(args.prompt, "prompt")
    semantic_path = _existing_file(args.semantic_plan, "semantic plan")
    trajectory = (
        _existing_file(args.trajectory, "trajectory")
        if args.trajectory is not None else None
    )
    trajectory_authoring = (
        _existing_file(args.trajectory_authoring, "trajectory authoring")
        if args.trajectory_authoring is not None else None
    )
    output = _output_path(args.output_dir)
    failure_dir = output.with_name(f"{output.name}.failed")
    if failure_dir.exists():
        raise CodedDraftError(f"failure evidence already exists: {failure_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid4().hex}.staging"
    staging.mkdir()

    try:
        plan = SemanticStoryPlan.from_path(semantic_path)
        _validate_semantic_ids(plan)

        sources_dir = staging / "sources"
        sources_dir.mkdir()
        source_records = {
            "shotscript": _snapshot(shotscript, sources_dir / "shotscript.json", "ShotScript"),
            "prompt": _snapshot(prompt, sources_dir / "prompt.txt", "prompt"),
            "semantic_plan": _snapshot(
                semantic_path, sources_dir / "semantic_plan.json", "semantic plan"
            ),
        }
        if trajectory is not None and trajectory_authoring is not None:
            source_records["trajectory"] = _snapshot(
                trajectory, sources_dir / "trajectory.json", "trajectory"
            )
            source_records["trajectory_authoring"] = _snapshot(
                trajectory_authoring,
                sources_dir / "trajectory_authoring.json",
                "trajectory authoring",
            )
        semantic_snapshot = sources_dir / "semantic_plan.json"
        snapshot_plan = SemanticStoryPlan.from_path(semantic_snapshot)
        if snapshot_plan.to_dict() != plan.to_dict():
            raise CodedDraftError("semantic plan snapshot changed meaning")
        script, expected_frames = _validate_shotscript(
            sources_dir / "shotscript.json", snapshot_plan, args.fps
        )
        explicit_instruction = None
        explicit_authoring = None
        if trajectory is not None:
            explicit_instruction, explicit_authoring = _validate_trajectory_sources(
                sources_dir / "trajectory.json",
                sources_dir / "trajectory_authoring.json",
                script,
            )

        for style in ("diagnostic", "clay"):
            _run_profile(
                blender=blender,
                shotscript=sources_dir / "shotscript.json",
                output_dir=staging / "renders" / style,
                style=style,
                fps=args.fps,
                resolution=args.resolution,
                log_path=staging / "logs" / f"{style}.log",
                failure_dir=failure_dir,
                timeout=args.render_timeout,
                trajectory=(sources_dir / "trajectory.json") if trajectory is not None else None,
            )

        indices = [
            round(keyframe.t * (expected_frames - 1))
            for keyframe in plan.semantic_keyframes
        ]
        selected_indices = set(indices)
        videos: dict[str, dict[str, object]] = {}
        decoded: dict[str, dict[int, Image.Image]] = {}
        for style in ("diagnostic", "clay"):
            filename = "trajectory_proxy.mp4" if trajectory is not None else "station_proxy.mp4"
            video = staging / "renders" / style / filename
            if trajectory is not None:
                _validate_trajectory_render_manifest(
                    staging / "renders" / style / "trajectory_proxy_manifest.json",
                    style=style, fps=args.fps, resolution=args.resolution,
                    trajectory_sha256=_sha256(sources_dir / "trajectory.json"),
                    actor_ids={actor.actor_id for actor in script.shots[0].actors},
                )
            else:
                _validate_render_report(
                    staging / "renders" / style / "trajectory_report.json",
                    style=style, fps=args.fps, resolution=args.resolution,
                )
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
        if (
            videos["diagnostic"]["media"]["decoded_pixel_sha256"]
            == videos["clay"]["media"]["decoded_pixel_sha256"]
        ):
            raise CodedDraftError(
                "diagnostic and clay videos decode to identical frame pixels"
            )

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
                path = _safe_semantic_frame_path(
                    staging / "semantic_frames", f"{keyframe.id}_{style}.png"
                )
                _save_png(image, path)
                record[style] = _relative_record(path, staging)
                sheet.paste(image, (column * args.resolution[0], row * args.resolution[1]))
            semantic_frames.append(record)
        sheet_path = staging / "semantic_contact_sheet.png"
        _save_png(sheet, sheet_path)

        render_artifacts = _artifact_inventory(staging, staging / "renders")
        _canonicalize_video_records(videos, render_artifacts)
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
                "explicit_trajectory_binding": (
                    {
                        "available": True,
                        "trajectory": {
                            "path": source_records["trajectory"]["snapshot_path"],
                            "sha256": source_records["trajectory"]["snapshot_sha256"],
                            "bytes": source_records["trajectory"]["bytes"],
                        },
                        "authoring": {
                            "path": source_records["trajectory_authoring"]["snapshot_path"],
                            "sha256": source_records["trajectory_authoring"]["snapshot_sha256"],
                            "bytes": source_records["trajectory_authoring"]["bytes"],
                        },
                        "projection_policy": explicit_authoring["projection_policy"],
                        "camera_policy": explicit_authoring["camera_policy"],
                    }
                    if explicit_instruction is not None and explicit_authoring is not None
                    else {
                        "available": False, "trajectory": None, "authoring": None,
                        "projection_policy": None, "camera_policy": "shotscript_locked",
                    }
                ),
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

        bound_sources = [
            ("shotscript", shotscript),
            ("prompt", prompt),
            ("semantic_plan", semantic_path),
        ]
        if trajectory is not None and trajectory_authoring is not None:
            bound_sources.extend(
                (("trajectory", trajectory), ("trajectory_authoring", trajectory_authoring))
            )
        for key, source in bound_sources:
            record = source_records[key]
            snapshot = staging / str(record["snapshot_path"])
            if _sha256(source) != record["original_sha256"] or _sha256(snapshot) != record["snapshot_sha256"]:
                raise CodedDraftError(f"{key} source binding changed during render")

        inventory_records: list[Mapping[str, object]] = [
            {
                "path": record["snapshot_path"],
                "sha256": record["snapshot_sha256"],
                "bytes": record["bytes"],
            }
            for record in source_records.values()
        ]
        inventory_records.extend(render_artifacts)
        inventory_records.extend(render_logs)
        inventory_records.extend(
            frame[style]
            for frame in semantic_frames
            for style in ("diagnostic", "clay")
        )
        inventory_records.extend((contact_sheet, _relative_record(bundle_path, staging)))

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
            "artifact_inventory": inventory_records,
        }
        manifest_path = staging / "manifest.json"
        _atomic_json(manifest_path, manifest)
        _verify_final_inventory(staging, inventory_records, ignored={"manifest.json"})
        persisted_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(persisted_bundle, Mapping) or not isinstance(
            persisted_manifest, Mapping
        ):
            raise CodedDraftError("persisted bundle and manifest must be JSON objects")
        _verify_json_artifact_references(
            [persisted_bundle, persisted_manifest], inventory_records
        )
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
        ShotScriptError,
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
