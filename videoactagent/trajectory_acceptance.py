"""Validate persisted evidence from a real trajectory-editor browser session.

This module deliberately has no manifest-generation command: the manifest must
be written only after a person actually performs the declared browser actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from PIL import Image

from .trajectory import TrajectoryInstruction


_ROOT_KEYS = frozenset(
    {
        "schema_version", "evidence_type", "timestamp", "scene_id", "shot_id", "track_id",
        "operations", "input_preview", "trajectory", "overlay", "final_times",
    }
)
_FILE_KEYS = frozenset({"path", "sha256"})
_OVERLAY_KEYS = frozenset({"path", "sha256", "width", "height", "mode"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS = [
    {"action": "set_slider", "value": 0.8},
    {"action": "drag_point", "original_t": 0.2},
    {"action": "Finish"},
    {"action": "Save"},
    {"action": "Load"},
]


def _exact(value: Mapping[str, object], keys: frozenset[str], label: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{label} fields must be exactly {sorted(keys)}")


def _safe_file(workspace: Path, value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label}.path must be a string")
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{label}.path must stay below workspace")
    root = workspace.resolve(strict=True)
    path = root.joinpath(*relative.parts)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}.path resolves outside workspace") from exc
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"{label}.path must be a single-link regular file")
    return path


def _validate_file_record(value: object, workspace: Path, label: str) -> Path:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    _exact(value, _FILE_KEYS, label)
    path = _safe_file(workspace, value["path"], label)
    digest = value["sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"{label}.sha256 must be lowercase SHA-256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        raise ValueError(f"{label}.sha256 mismatch: expected {digest}, actual {actual}")
    return path


def validate_acceptance_manifest(value: object, workspace: Path) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("acceptance manifest must be an object")
    _exact(value, _ROOT_KEYS, "acceptance manifest")
    if value["schema_version"] != "0.1":
        raise ValueError("schema_version must be '0.1'")
    if value["evidence_type"] != "manual_browser_interaction":
        raise ValueError("evidence_type must be manual_browser_interaction")
    timestamp = value["timestamp"]
    if not isinstance(timestamp, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be valid ISO-8601") from exc
    if parsed_timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    if value["operations"] != _OPERATIONS:
        raise ValueError("operations must record slider0.8, drag original t0.2, Finish, Save, Load")
    if not all(isinstance(value[key], str) and value[key] for key in ("scene_id", "shot_id", "track_id")):
        raise ValueError("scene_id, shot_id, and track_id must be non-empty strings")

    preview_path = _validate_file_record(value["input_preview"], workspace, "input_preview")
    trajectory_path = _validate_file_record(value["trajectory"], workspace, "trajectory")
    overlay = value["overlay"]
    if not isinstance(overlay, Mapping):
        raise ValueError("overlay must be an object")
    _exact(overlay, _OVERLAY_KEYS, "overlay")
    overlay_path = _validate_file_record(
        {"path": overlay["path"], "sha256": overlay["sha256"]}, workspace, "overlay"
    )

    instruction = TrajectoryInstruction.from_path(trajectory_path)
    instruction.validate_identity(value["scene_id"], value["shot_id"])  # type: ignore[arg-type]
    matching = [track for track in instruction.tracks if track.track_id == value["track_id"]]
    if len(matching) != 1:
        raise ValueError("track_id must identify exactly one trajectory track")
    claimed_times = value["final_times"]
    actual_times = [point.t for point in matching[0].points]
    if claimed_times != actual_times or claimed_times != [0.2, 0.8]:
        raise ValueError(f"final_times mismatch: claimed {claimed_times}, actual {actual_times}")

    with Image.open(overlay_path) as image:
        dimensions = [image.width, image.height]
        mode = image.mode
    if overlay["width"] != dimensions[0] or overlay["height"] != dimensions[1] or overlay["mode"] != mode:
        raise ValueError("overlay dimensions/mode mismatch")

    return {
        "status": "ok",
        "evidence_type": value["evidence_type"],
        "timestamp": timestamp,
        "input_preview_sha256": hashlib.sha256(preview_path.read_bytes()).hexdigest(),
        "trajectory_sha256": hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
        "overlay_sha256": hashlib.sha256(overlay_path.read_bytes()).hexdigest(),
        "overlay_dimensions": dimensions,
        "overlay_mode": mode,
        "final_times": actual_times,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest_path = _safe_file(args.workspace, str(args.manifest), "manifest")
        value = json.loads(manifest_path.read_bytes())
        result = validate_acceptance_manifest(value, args.workspace)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"TRAJECTORY_ACCEPTANCE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("TRAJECTORY_ACCEPTANCE_OK " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
