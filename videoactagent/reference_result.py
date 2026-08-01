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


def _expected_proxy(
    approved_proxy: Mapping[str, object] | Path | None,
    expected_duration_seconds: float | None,
) -> tuple[float, dict[str, object] | None]:
    duration = expected_duration_seconds
    provenance: dict[str, object] | None = None
    if isinstance(approved_proxy, Path):
        proxy = approved_proxy.resolve(strict=True)
        if not proxy.is_file():
            raise ValueError("approved proxy is not a file")
        _count, seconds = imageio_ffmpeg.count_frames_and_secs(str(proxy))
        provenance = {
            "path": str(proxy),
            "sha256": _sha256_file(proxy),
            "bytes": proxy.stat().st_size,
            "duration_seconds": float(seconds),
        }
        if duration is None:
            duration = float(seconds)
    elif isinstance(approved_proxy, Mapping):
        raw_duration = approved_proxy.get("duration_seconds")
        if raw_duration is None and isinstance(approved_proxy.get("media"), Mapping):
            raw_duration = approved_proxy["media"].get("duration_seconds")  # type: ignore[index]
        if raw_duration is not None:
            if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
                raise ValueError("approved proxy duration must be numeric")
            if duration is not None and float(duration) != float(raw_duration):
                raise ValueError("expected duration disagrees with approved proxy")
            duration = float(raw_duration)
        provenance = dict(approved_proxy)
    elif approved_proxy is not None:
        raise ValueError("approved_proxy must be a media record or local path")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("expected duration is required")
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("expected duration must be positive and finite")
    return duration, provenance


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
    expected_duration, proxy_provenance = _expected_proxy(
        approved_proxy, expected_duration_seconds
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
            width, height = decoded["dimensions"]
            fps = decoded["fps"]
            duration = decoded["duration_seconds"]
            if width < 1280 or height < 720 or width * 9 != height * 16:
                raise ValueError("result dimensions must be positive 720-class 16:9")
            tolerance = 1.0 / float(fps)
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
