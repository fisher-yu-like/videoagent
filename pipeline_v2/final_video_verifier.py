"""Deterministic checks for a generated final video.

This verifier deliberately does not infer realism from file metadata. It checks
the actual downloaded media and leaves identity, action order, camera coverage,
and visual quality to the final VLM or human review stage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
from pathlib import Path
from typing import Any


FINAL_VIDEO_VERIFIER_SCHEMA_VERSION = "final-video-verifier-1.0"
MULTI_CAMERA_FINAL_VIDEO_VERIFIER_SCHEMA_VERSION = "multi-camera-final-video-verifier-1.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_final_video(
    video_path: Path | str,
    *,
    expected_frame_count: int,
    expected_fps: int,
    expected_duration: float,
    expected_resolution: tuple[int, int] | None = None,
    probe_video: Callable[[Path], Mapping[str, Any]] | None = None,
    blackdetect_events: int | None = None,
    source_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed report for one real downloaded video."""
    path = Path(video_path).resolve()
    checks: list[dict[str, Any]] = []
    if not path.is_file() or path.stat().st_size <= 0:
        checks.append({"check_id": "media.file", "status": "failed", "message": "final video is missing or empty"})
        return {"schema_version": FINAL_VIDEO_VERIFIER_SCHEMA_VERSION, "verdict": "failed", "checks": checks, "source_hashes": dict(source_hashes or {})}
    checks.append({"check_id": "media.file", "status": "passed", "message": "final video exists and is non-empty", "bytes": path.stat().st_size})
    probe: dict[str, Any] | None = None
    if probe_video is None:
        checks.append({"check_id": "media.ffprobe", "status": "unknown", "message": "no real ffprobe function was supplied"})
    else:
        try:
            probe = dict(probe_video(path))
            duration_ok = abs(float(probe.get("duration", 0.0)) - expected_duration) <= 0.20
            frame_ok = int(probe.get("nb_frames", -1)) >= max(1, expected_frame_count - 5)
            fps_ok = str(probe.get("r_frame_rate")) in {f"{expected_fps}/1", str(expected_fps)}
            resolution_ok = expected_resolution is None or [int(probe.get("width", -1)), int(probe.get("height", -1))] == list(expected_resolution)
            passed = duration_ok and frame_ok and fps_ok and resolution_ok
            checks.append({"check_id": "media.ffprobe", "status": "passed" if passed else "failed", "message": "downloaded media metadata matches the expected contract" if passed else "downloaded media metadata does not match the expected contract", "probe": probe, "expected": {"frame_count": expected_frame_count, "fps": expected_fps, "duration": expected_duration, "resolution": list(expected_resolution) if expected_resolution else None}})
        except Exception as exc:
            checks.append({"check_id": "media.ffprobe", "status": "failed", "message": f"ffprobe failed: {type(exc).__name__}: {exc}"})
    if blackdetect_events is None:
        checks.append({"check_id": "media.black_frames", "status": "unknown", "message": "black-frame detector was not supplied"})
    else:
        checks.append({"check_id": "media.black_frames", "status": "passed" if blackdetect_events == 0 else "failed", "message": "no black-frame events detected" if blackdetect_events == 0 else "black-frame events detected", "events": int(blackdetect_events)})
    checks.append({"check_id": "visual.vlm_or_human", "status": "unknown", "message": "identity, action order, camera coverage, and realism require final VLM or human review"})
    statuses = {item["status"] for item in checks}
    verdict = "failed" if "failed" in statuses else "pending_review" if "unknown" in statuses else "passed"
    return {"schema_version": FINAL_VIDEO_VERIFIER_SCHEMA_VERSION, "verdict": verdict, "video": {"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size}, "probe": probe, "checks": checks, "source_hashes": dict(source_hashes or {})}


def write_final_video_report(report: Mapping[str, Any], output_path: Path | str) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(dict(report), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return destination


def aggregate_final_video_reports(reports: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate independent camera reports without hiding a failed camera."""
    verdicts = {str(report.get("verdict", "failed")) for report in reports.values()}
    if "failed" in verdicts:
        verdict = "failed"
    elif "pending_review" in verdicts:
        verdict = "pending_review"
    else:
        verdict = "passed"
    return {
        "schema_version": MULTI_CAMERA_FINAL_VIDEO_VERIFIER_SCHEMA_VERSION,
        "verdict": verdict,
        "camera_count": len(reports),
        "cameras": {str(camera_id): dict(report) for camera_id, report in reports.items()},
    }
