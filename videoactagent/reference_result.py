"""Strict, local technical validation for one Seedance reference-video result.

Technical decoding is intentionally separate from human restyle assessment.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from uuid import uuid4

import imageio_ffmpeg
from PIL import Image


K_TIMES = (0.0, 0.2, 0.5, 0.8, 1.0)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _atomic_file(path: Path, data: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _codec_name(path: Path) -> str:
    completed = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    diagnostic = completed.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Video:\s*([^,\s]+)", diagnostic)
    if match is None:
        raise ValueError("video codec is unavailable")
    return match.group(1)


def _probe_media(path: Path) -> dict[str, object]:
    frame_count, seconds = imageio_ffmpeg.count_frames_and_secs(str(path))
    if type(frame_count) is not int or frame_count <= 0:
        raise ValueError("approved proxy has no decodable frames")
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        size = metadata.get("size") if isinstance(metadata, Mapping) else None
        fps = metadata.get("fps") if isinstance(metadata, Mapping) else None
        if (
            not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(value) is not int or value <= 0 for value in size)
            or isinstance(fps, bool) or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps)) or float(fps) <= 0
        ):
            raise ValueError("approved proxy media metadata is invalid")
        expected_bytes = int(size[0]) * int(size[1]) * 3
        last = -1
        for index, raw in enumerate(reader):
            last = index
            if len(raw) != expected_bytes:
                raise ValueError("approved proxy decoded frame byte count is invalid")
            if index == frame_count - 1:
                break
        if last != frame_count - 1:
            raise ValueError("approved proxy ended before its declared final frame")
        return {
            "duration_seconds": float(seconds),
            "fps": float(fps),
            "frame_count": frame_count,
            "dimensions": [int(size[0]), int(size[1])],
            "codec": _codec_name(path),
            "decode_pass": True,
        }
    finally:
        _close_reader(reader)


def _approved_proxy_snapshot(
    approved_proxy: Mapping[str, object] | Path | None,
    approved_proxy_root: Path | None,
    expected_duration_seconds: float | None,
) -> tuple[float, dict[str, object]]:
    declared_duration: object = None
    declared_path: str | None = None
    if isinstance(approved_proxy, Path):
        proxy = approved_proxy.resolve(strict=True)
        if not proxy.is_file():
            raise ValueError("approved proxy is not a file")
    elif isinstance(approved_proxy, Mapping):
        if approved_proxy_root is None:
            raise ValueError("approved proxy root is required for a media record")
        root = Path(approved_proxy_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("approved proxy root must be a directory")
        raw_path = approved_proxy.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("approved proxy record path is invalid")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("approved proxy record path is unsafe")
        proxy = (root / relative).resolve(strict=False)
        try:
            proxy.relative_to(root)
        except ValueError as exc:
            raise ValueError("approved proxy record path is unsafe") from exc
        if not proxy.is_file():
            raise ValueError("approved proxy record path is not a file")
        declared_path = raw_path
        claimed_sha = approved_proxy.get("sha256")
        claimed_bytes = approved_proxy.get("bytes")
        if (
            not isinstance(claimed_sha, str) or _SHA256.fullmatch(claimed_sha) is None
            or type(claimed_bytes) is not int or claimed_bytes <= 0
        ):
            raise ValueError("approved proxy record hash/size is invalid")
        actual_sha = _sha256_file(proxy)
        if actual_sha != claimed_sha or proxy.stat().st_size != claimed_bytes:
            raise ValueError("approved proxy record hash/size mismatch")
        declared_duration = approved_proxy.get("duration_seconds")
        if declared_duration is None and isinstance(approved_proxy.get("media"), Mapping):
            declared_duration = approved_proxy["media"].get("duration_seconds")  # type: ignore[index]
    elif approved_proxy is not None:
        raise ValueError("approved_proxy must be a media record or local path")
    else:
        raise ValueError("an actual approved proxy path or record is required")
    actual_sha = _sha256_file(proxy)
    actual_bytes = proxy.stat().st_size
    media = _probe_media(proxy)
    duration = float(media["duration_seconds"])
    tolerance = 1.0 / float(media["fps"])
    if declared_duration is not None:
        if isinstance(declared_duration, bool) or not isinstance(declared_duration, (int, float)):
            raise ValueError("approved proxy declared duration is invalid")
        if abs(float(declared_duration) - duration) > tolerance:
            raise ValueError("approved proxy declared duration differs from decoded media")
    if expected_duration_seconds is not None:
        if (
            isinstance(expected_duration_seconds, bool)
            or not isinstance(expected_duration_seconds, (int, float))
            or abs(float(expected_duration_seconds) - duration) > tolerance
        ):
            raise ValueError("expected duration differs from decoded approved proxy")
    return duration, {
        "record_path": declared_path,
        "resolved_path": str(proxy),
        "sha256": actual_sha,
        "bytes": actual_bytes,
        **media,
    }


def _decode_k_frames(source: Path, destination: Path) -> dict[str, Any]:
    frame_count, counted_seconds = imageio_ffmpeg.count_frames_and_secs(str(source))
    if type(frame_count) is not int or frame_count < 5:
        raise ValueError("video has fewer than five decodable frames")
    indices = tuple(round(t * (frame_count - 1)) for t in K_TIMES)
    if len(set(indices)) != 5:
        raise ValueError("video is too short for distinct K0-K4 frames")
    reader = imageio_ffmpeg.read_frames(str(source), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        size = metadata.get("size") if isinstance(metadata, Mapping) else None
        fps = metadata.get("fps") if isinstance(metadata, Mapping) else None
        if (
            not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(value) is not int or value <= 0 for value in size)
        ):
            raise ValueError("video dimensions are unavailable")
        if (
            isinstance(fps, bool) or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps)) or float(fps) <= 0
        ):
            raise ValueError("video fps is unavailable")
        width, height = int(size[0]), int(size[1])
        wanted = {index: position for position, index in enumerate(indices)}
        decoded: dict[str, dict[str, object]] = {}
        destination.mkdir(parents=True, exist_ok=False)
        last_decoded = -1
        for index, raw in enumerate(reader):
            last_decoded = index
            if index not in wanted:
                continue
            if len(raw) != width * height * 3:
                raise ValueError(f"decoded frame {index} byte count is invalid")
            key = f"K{wanted[index]}"
            filename = f"{key}.png"
            target = destination / filename
            Image.frombytes("RGB", (width, height), raw).save(
                target, format="PNG", optimize=False
            )
            decoded[key] = {
                "normalized_time": K_TIMES[wanted[index]],
                "frame_index": index,
                "time_seconds": index / float(fps),
                "path": f"k_frames/{filename}",
                "sha256": _sha256_file(target),
                "dimensions": [width, height],
            }
            if index == indices[-1]:
                break
        if last_decoded + 1 != frame_count:
            raise ValueError("declared frame count differs from final decodable frame")
        if list(decoded) != ["K0", "K1", "K2", "K3", "K4"]:
            raise ValueError("ffmpeg ended before K0-K4 were decoded")
        if decoded["K4"]["frame_index"] != last_decoded:
            raise ValueError("K4 is not the final decodable frame")
        return {
            "frame_count": frame_count,
            "duration_seconds": float(counted_seconds),
            "fps": float(fps),
            "dimensions": [width, height],
            "frames": decoded,
        }
    finally:
        _close_reader(reader)


def validate_reference_result(
    result_path: Path,
    output_dir: Path,
    *,
    approved_proxy: Mapping[str, object] | Path | None = None,
    approved_proxy_root: Path | None = None,
    expected_duration_seconds: float | None = None,
    expected_result_sha256: str | None = None,
) -> dict[str, object]:
    """Validate and preserve a result without making any quality inference."""

    source = Path(result_path).resolve(strict=True)
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise FileExistsError(f"reference result output already exists: {output}")
    if not source.is_file() or source.suffix.lower() != ".mp4":
        raise ValueError("result must be an MP4 file")
    expected_duration, proxy_provenance = _approved_proxy_snapshot(
        approved_proxy, approved_proxy_root, expected_duration_seconds
    )
    if expected_result_sha256 is not None and (
        not isinstance(expected_result_sha256, str)
        or _SHA256.fullmatch(expected_result_sha256) is None
    ):
        raise ValueError("expected result SHA-256 is invalid")
    source_sha = _sha256_file(source)
    source_size = source.stat().st_size
    base_report: dict[str, object] = {
        "schema_version": "seedance-reference-result/1",
        "source": {
            "path": str(source), "sha256": source_sha, "bytes": source_size,
        },
        "approved_proxy": proxy_provenance,
        "expected_duration_seconds": expected_duration,
        "technical_media_pass": False,
        "human_restyle_pass": None,
        "human_restyle_status": "pending",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid4().hex}.tmp"
    staging.mkdir()
    try:
        try:
            if expected_result_sha256 is not None and source_sha != expected_result_sha256:
                raise ValueError("result SHA-256 mismatch")
            decoded = _decode_k_frames(source, staging / "k_frames")
            proxy_path = Path(str(proxy_provenance["resolved_path"]))
            if (
                not proxy_path.is_file()
                or proxy_path.stat().st_size != proxy_provenance["bytes"]
                or _sha256_file(proxy_path) != proxy_provenance["sha256"]
            ):
                raise ValueError("approved proxy changed during result validation")
            width, height = decoded["dimensions"]
            fps = decoded["fps"]
            duration = decoded["duration_seconds"]
            if width < 1280 or height < 720 or width * 9 != height * 16:
                raise ValueError("result dimensions must be positive 720-class 16:9")
            proxy_fps = float(proxy_provenance["fps"])
            tolerance = max(1.0 / float(fps), 1.0 / proxy_fps)
            if abs(float(duration) - expected_duration) > tolerance:
                raise ValueError(
                    "result duration is materially different from the approved proxy"
                )
            if abs(expected_duration - 5.0) > tolerance:
                raise ValueError("approved reference contract is not approximately five seconds")
            codec = _codec_name(source)
            # Recheck before and after the exact-byte preservation copy.
            if source.stat().st_size != source_size or _sha256_file(source) != source_sha:
                raise ValueError("result changed during validation")
            shutil.copyfile(source, staging / "result.mp4")
            if _sha256_file(staging / "result.mp4") != source_sha:
                raise ValueError("preserved result bytes differ from source")
            report = {
                **base_report,
                "technical_media_pass": True,
                "result": {
                    "path": "result.mp4",
                    "sha256": source_sha,
                    "bytes": source_size,
                    "duration_seconds": duration,
                    "fps": fps,
                    "dimensions": decoded["dimensions"],
                    "frame_count": decoded["frame_count"],
                    "codec": codec,
                    "decode_pass": True,
                    "duration_tolerance_seconds": tolerance,
                },
                "k_frames": decoded["frames"],
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            shutil.rmtree(staging / "k_frames", ignore_errors=True)
            report = {
                **base_report,
                "failure_reason": str(exc),
                "result": {
                    "sha256": source_sha,
                    "bytes": source_size,
                    "decode_pass": False,
                },
                "k_frames": {},
            }
        _atomic_file(staging / "technical_report.json", _json_bytes(report))
        os.replace(staging, output)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)
