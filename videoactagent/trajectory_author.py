"""Human K0--K4 actor-trajectory authoring on a top-down world plane.

The diagnostic frames are references only.  Clicks are interpreted on a
separate top-down plane using the ShotScript world bounds; camera animation
remains exactly the ShotScript camera animation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from videoactagent.trajectory import (
    TrajectoryInstruction,
    TrajectoryPoint,
    TrajectoryTarget,
    TrajectoryTrack,
    canonical_bytes,
)


SCHEMA_VERSION = "1.0"
PROJECTION_POLICY = "top_down_world_bounds_linear_y_up_z0"
CAMERA_POLICY = "shotscript_locked"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _read(path: Path, label: str) -> dict[str, Any]:
    try:
        result = json.loads(
            path.read_bytes().decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must be one JSON object")
    return result


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def _record(path: Path, root: Path) -> dict[str, object]:
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha(path),
            "bytes": path.stat().st_size}


def _safe_path(root: Path, relative: object, label: str, *, strict: bool = True) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} path is missing")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != relative:
        raise ValueError(f"unsafe {label} path: {relative!r}")
    resolved = (root / candidate).resolve(strict=strict)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"{label} path escapes its root") from exc
    if strict and not resolved.is_file():
        raise ValueError(f"{label} is not a file")
    return resolved


def _verify_record(root: Path, record: object, label: str) -> Path:
    if not isinstance(record, Mapping) or set(record) != {"path", "sha256", "bytes"}:
        raise ValueError(f"{label} record is invalid")
    path = _safe_path(root, record.get("path"), label)
    if record.get("bytes") != path.stat().st_size or record.get("sha256") != _sha(path):
        raise ValueError(f"{label} SHA-256/size mismatch")
    return path


def _verify_coded_bundle(bundle_path: Path) -> dict[str, Any]:
    bundle = bundle_path.resolve(strict=True)
    if bundle.name != "bundle.json":
        raise ValueError("--bundle must name bundle.json")
    root = bundle.parent
    manifest_path = root / "manifest.json"
    manifest = _read(manifest_path, "coded-draft manifest")
    document = _read(bundle, "coded-draft bundle")
    if document.get("schema_version") != "1.0" or manifest.get("schema_version") != "1.0":
        raise ValueError("coded-draft schema must be 1.0")
    story = document.get("story_id")
    if not isinstance(story, str) or not story or manifest.get("story_id") != story:
        raise ValueError("coded-draft story mismatch")
    if document.get("backend_consumed") is not False or manifest.get("backend_consumed") is not False:
        raise ValueError("coded draft was already backend-consumed")
    inventory_value = manifest.get("artifact_inventory")
    if not isinstance(inventory_value, list) or not inventory_value:
        raise ValueError("coded-draft inventory is missing")
    inventory: dict[str, Mapping[str, object]] = {}
    for index, item in enumerate(inventory_value):
        path = _verify_record(root, item, f"inventory[{index}]")
        relative = path.relative_to(root).as_posix()
        if relative in inventory:
            raise ValueError(f"duplicate inventory path: {relative}")
        inventory[relative] = item
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*") if path.is_file() and path != manifest_path
    }
    if actual != set(inventory):
        raise ValueError("coded-draft inventory is not closed")
    output_bundle = manifest.get("outputs", {}).get("bundle")
    _verify_record(root, output_bundle, "manifest bundle")
    if output_bundle != inventory.get("bundle.json"):
        raise ValueError("manifest bundle binding mismatch")

    sources = document.get("source_bindings")
    if not isinstance(sources, Mapping):
        raise ValueError("source bindings are missing")
    source_paths: dict[str, Path] = {}
    for name in ("shotscript", "semantic_plan"):
        binding = sources.get(name)
        if not isinstance(binding, Mapping):
            raise ValueError(f"{name} binding is missing")
        relative = binding.get("snapshot_path")
        path = _safe_path(root, relative, name)
        if (binding.get("snapshot_sha256") != _sha(path)
                or binding.get("bytes") != path.stat().st_size
                or binding.get("verified_equal") is not True
                or binding.get("original_sha256") != binding.get("snapshot_sha256")):
            raise ValueError(f"{name} source binding mismatch")
        record = inventory.get(str(relative))
        if not isinstance(record, Mapping) or record.get("sha256") != _sha(path):
            raise ValueError(f"{name} is absent from inventory")
        source_paths[name] = path

    shotscript = _read(source_paths["shotscript"], "ShotScript")
    semantic = _read(source_paths["semantic_plan"], "semantic plan")
    if shotscript.get("scene_id") != story or semantic.get("story_id") != story:
        raise ValueError("source story mismatch")
    shots = shotscript.get("shots")
    if not isinstance(shots, list) or len(shots) != 1 or not isinstance(shots[0], Mapping):
        raise ValueError("trajectory authoring requires one whole-story shot")
    actors_value = shots[0].get("actors")
    if not isinstance(actors_value, list) or not actors_value:
        raise ValueError("ShotScript actors are missing")
    actors: list[str] = []
    for item in actors_value:
        actor = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(actor, str) or not actor or actor in actors:
            raise ValueError("actor IDs must be unique non-empty strings")
        actors.append(actor)
    bounds = shotscript.get("world_bounds")
    if (not isinstance(bounds, list) or len(bounds) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(float(v)) for v in bounds)
            or not bounds[0] < bounds[1] or not bounds[2] < bounds[3]):
        raise ValueError("world_bounds are invalid")
    keyframes = document.get("motion_semantics", {}).get("keyframes")
    if not isinstance(keyframes, list) or len(keyframes) != 5:
        raise ValueError("exactly K0--K4 are required")
    expected_times = [item.get("t") for item in semantic.get("semantic_keyframes", [])]
    if len(expected_times) != 5:
        raise ValueError("semantic plan must declare exactly five keyframes")
    verified_frames = []
    for index, item in enumerate(keyframes):
        if not isinstance(item, Mapping):
            raise ValueError("keyframe binding is invalid")
        key = f"K{index}"
        t = item.get("t")
        frame_index = item.get("frame_index")
        if item.get("semantic_id") != key or t != expected_times[index]:
            raise ValueError("keyframe IDs/times differ from semantic plan")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 1:
            raise ValueError("keyframe time is invalid")
        if type(frame_index) is not int:
            raise ValueError("keyframe frame index is invalid")
        diag = item.get("diagnostic")
        path = _verify_record(root, diag, f"{key} diagnostic frame")
        if diag != inventory.get(path.relative_to(root).as_posix()):
            raise ValueError(f"{key} diagnostic frame inventory mismatch")
        verified_frames.append({"id": key, "t": float(t), "frame_index": frame_index,
                                "path": path, "record": dict(diag)})
    expected = manifest.get("expected_media")
    if not isinstance(expected, Mapping):
        raise ValueError("expected media is missing")
    timeline = {name: expected.get(name) for name in ("frame_count", "fps", "duration_seconds")}
    if timeline != {"frame_count": 120, "fps": 24, "duration_seconds": 5.0}:
        raise ValueError("authoring currently requires the native 120-frame/24fps/5s timeline")
    return {"root": root, "bundle": bundle, "manifest": manifest_path,
            "story_id": story, "shot_id": shots[0].get("shot_id"), "actors": actors,
            "world_bounds": [float(v) for v in bounds], "timeline": timeline,
            "shotscript": source_paths["shotscript"], "semantic": source_paths["semantic_plan"],
            "keyframes": verified_frames}


def normalized_to_world(x: float, y: float, bounds: tuple[float, float, float, float]) -> tuple[float, float, float]:
    if any(not math.isfinite(float(value)) for value in (x, y, *bounds)) or not 0 <= x <= 1 or not 0 <= y <= 1:
        raise ValueError("normalized point/bounds are invalid")
    min_x, max_x, min_y, max_y = bounds
    return min_x + x * (max_x - min_x), max_y - y * (max_y - min_y), 0.0


def world_to_normalized(x: float, y: float, bounds: tuple[float, float, float, float]) -> tuple[float, float]:
    min_x, max_x, min_y, max_y = bounds
    if max_x <= min_x or max_y <= min_y:
        raise ValueError("world bounds are invalid")
    normalized = ((x - min_x) / (max_x - min_x), (max_y - y) / (max_y - min_y))
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in normalized):
        raise ValueError("world point is outside world bounds")
    return normalized


def _copy_verified(source: Path, target: Path, root: Path) -> dict[str, object]:
    before = _sha(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if _sha(source) != before or _sha(target) != before:
        raise ValueError("source changed while copying")
    return _record(target, root)


def prepare_workspace(bundle_path: Path | str, output_dir: Path | str) -> Path:
    verified = _verify_coded_bundle(Path(bundle_path))
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid4().hex}.staging"
    staging.mkdir()
    try:
        source_records = {
            "coded_bundle": _copy_verified(verified["bundle"], staging / "source" / "bundle.json", staging),
            "coded_manifest": _copy_verified(verified["manifest"], staging / "source" / "manifest.json", staging),
            "shotscript": _copy_verified(verified["shotscript"], staging / "source" / "shotscript.json", staging),
            "semantic_plan": _copy_verified(verified["semantic"], staging / "source" / "semantic_plan.json", staging),
        }
        keyframes = []
        for item in verified["keyframes"]:
            target = staging / "reference" / f"{item['id']}.png"
            record = _copy_verified(item["path"], target, staging)
            keyframes.append({"id": item["id"], "t": item["t"],
                              "frame_index": item["frame_index"], "diagnostic_frame": record,
                              "source_diagnostic_sha256": item["record"]["sha256"]})
        inventory = [*source_records.values(), *(item["diagnostic_frame"] for item in keyframes)]
        document = {
            "schema_version": SCHEMA_VERSION,
            "story_id": verified["story_id"], "shot_id": verified["shot_id"],
            "actors": verified["actors"], "keyframes": keyframes,
            "world_bounds": verified["world_bounds"], "timeline": verified["timeline"],
            "projection_policy": PROJECTION_POLICY, "camera_policy": CAMERA_POLICY,
            "source": source_records, "artifact_inventory": inventory,
            "human_points_present": False,
        }
        manifest = staging / "trajectory_author_manifest.json"
        manifest.write_bytes(_bytes(document))
        verify_workspace(manifest)
        os.replace(staging, output)
        return output / manifest.name
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_workspace(manifest_path: Path | str) -> dict[str, Any]:
    manifest = Path(manifest_path).resolve(strict=True)
    if manifest.name != "trajectory_author_manifest.json":
        raise ValueError("workspace manifest name is invalid")
    root = manifest.parent
    doc = _read(manifest, "trajectory author manifest")
    required = {"schema_version", "story_id", "shot_id", "actors", "keyframes",
                "world_bounds", "timeline", "projection_policy", "camera_policy",
                "source", "artifact_inventory", "human_points_present"}
    if set(doc) != required or doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("trajectory author manifest schema is invalid")
    if doc.get("projection_policy") != PROJECTION_POLICY or doc.get("camera_policy") != CAMERA_POLICY:
        raise ValueError("projection/camera policy mismatch")
    if doc.get("human_points_present") is not False:
        raise ValueError("prepared workspace must not claim human points")
    inventory_value = doc.get("artifact_inventory")
    if not isinstance(inventory_value, list):
        raise ValueError("workspace inventory is missing")
    seen: dict[str, Mapping[str, object]] = {}
    for index, record in enumerate(inventory_value):
        path = _verify_record(root, record, f"workspace inventory[{index}]")
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            raise ValueError("duplicate workspace inventory path")
        seen[relative] = record
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file() and path != manifest}
    if actual != set(seen):
        raise ValueError("workspace inventory is not closed")
    frames = doc.get("keyframes")
    if not isinstance(frames, list) or len(frames) != 5:
        raise ValueError("workspace must contain K0--K4")
    for index, item in enumerate(frames):
        if not isinstance(item, Mapping) or item.get("id") != f"K{index}":
            raise ValueError("workspace keyframe IDs are invalid")
        path = _verify_record(root, item.get("diagnostic_frame"), f"K{index} frame")
        if item.get("source_diagnostic_sha256") != _sha(path):
            raise ValueError(f"K{index} source frame binding mismatch")
    return doc


def _unit(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be in [0, 1]")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{label} must be in [0, 1]")
    return result


def save_authoring(manifest_path: Path | str, payload: Mapping[str, Any]) -> Path:
    manifest_path = Path(manifest_path).resolve(strict=True)
    doc = verify_workspace(manifest_path)
    root = manifest_path.parent
    if set(payload) != {"author_id", "points"}:
        raise ValueError("save payload has unknown or missing fields")
    author = payload.get("author_id")
    if not isinstance(author, str) or not author.strip():
        raise ValueError("author_id is required")
    points = payload.get("points")
    if not isinstance(points, list):
        raise ValueError("points must be a list")
    actors = doc["actors"]
    keyframes = doc["keyframes"]
    expected = {(actor, item["id"]) for actor in actors for item in keyframes}
    captured: dict[tuple[str, str], tuple[float, float]] = {}
    for item in points:
        if not isinstance(item, Mapping) or set(item) != {"actor_id", "keyframe_id", "t", "x", "y"}:
            raise ValueError("point fields are invalid")
        key = (item.get("actor_id"), item.get("keyframe_id"))
        if key not in expected:
            raise ValueError("point actor/keyframe is unknown")
        if key in captured:
            raise ValueError("duplicate actor/keyframe point")
        frame = keyframes[int(str(key[1])[1:])]
        if item.get("t") != frame["t"]:
            raise ValueError("point t differs from keyframe schedule")
        captured[key] = (_unit(item.get("x"), "point.x"), _unit(item.get("y"), "point.y"))
    if set(captured) != expected:
        missing = sorted(expected - set(captured))
        raise ValueError(f"all actor/K points must be explicitly authored; missing={missing}")
    tracks = []
    for actor in actors:
        trajectory_points = tuple(
            TrajectoryPoint(t=frame["t"], x=captured[(actor, frame["id"])][0],
                            y=captured[(actor, frame["id"])][1], visible=True)
            for frame in keyframes
        )
        tracks.append(TrajectoryTrack(track_id=f"human_{actor}",
            target=TrajectoryTarget("actor", actor), primitive="polyline",
            semantic="move", points=trajectory_points))
    instruction = TrajectoryInstruction(scene_id=doc["story_id"], shot_id=doc["shot_id"],
        duration_seconds=doc["timeline"]["duration_seconds"], sample_count=120,
        tracks=tuple(tracks))
    trajectory = root / "trajectory.json"
    authoring = root / "trajectory_authoring.json"
    if trajectory.exists() or authoring.exists():
        raise ValueError("authoring outputs already exist")
    trajectory_data = canonical_bytes(instruction)
    trajectory_tmp = root / f".trajectory.{uuid4().hex}.tmp"
    authoring_tmp = root / f".authoring.{uuid4().hex}.tmp"
    try:
        trajectory_tmp.write_bytes(trajectory_data)
        evidence = {
            "schema_version": SCHEMA_VERSION, "author_id": author.strip(),
            "authored_at": datetime.now(timezone.utc).isoformat(),
            "trajectory_path": "trajectory.json",
            "trajectory_sha256": hashlib.sha256(trajectory_data).hexdigest(),
            "world_bounds": doc["world_bounds"], "projection_policy": PROJECTION_POLICY,
            "camera_policy": CAMERA_POLICY, "source_bundle": doc["source"]["coded_bundle"],
            "source_manifest": doc["source"]["coded_manifest"],
            "source": {
                "shotscript": doc["source"]["shotscript"],
                "semantic_plan": doc["source"]["semantic_plan"],
            },
            "authoring_manifest": _record(manifest_path, root),
            "source_frames": [
                {
                    "id": item["id"], "t": item["t"],
                    "frame_index": item["frame_index"],
                    "diagnostic_frame": item["diagnostic_frame"],
                }
                for item in keyframes
            ],
            "point_count": len(captured), "auto_filled_points": 0,
        }
        authoring_tmp.write_bytes(_bytes(evidence))
        os.replace(trajectory_tmp, trajectory)
        os.replace(authoring_tmp, authoring)
    finally:
        trajectory_tmp.unlink(missing_ok=True)
        authoring_tmp.unlink(missing_ok=True)
    return trajectory


class _Handler(BaseHTTPRequestHandler):
    root: Path
    manifest: Path
    html: bytes

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send(self.html, "text/html; charset=utf-8")
        elif path == "/api/session":
            self._send(_bytes(verify_workspace(self.manifest)), "application/json")
        elif path.startswith("/files/"):
            relative = unquote(path[len("/files/"):])
            try:
                target = _safe_path(self.root, relative, "served file")
            except (ValueError, OSError):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send(target.read_bytes(), "image/png")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/save":
            self.send_error(HTTPStatus.NOT_FOUND); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = save_authoring(self.manifest, payload)
            self._send(_bytes({"trajectory": result.name, "sha256": _sha(result)}), "application/json")
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            self._send(_bytes({"error": str(exc)}), "application/json", HTTPStatus.BAD_REQUEST)

    def _send(self, data: bytes, media: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status); self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        return


def serve_workspace(manifest: Path | str, port: int = 8768) -> None:
    path = Path(manifest).resolve(strict=True)
    verify_workspace(path)
    _Handler.root = path.parent; _Handler.manifest = path
    _Handler.html = (Path(__file__).parents[1] / "static" / "trajectory_author.html").read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"TRAJECTORY_AUTHOR_SERVING http://127.0.0.1:{port}")
    server.serve_forever()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare"); prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    serve = sub.add_parser("serve"); serve.add_argument("--manifest", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8768)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_workspace(args.bundle, args.output_dir)
            print(f"TRAJECTORY_AUTHOR_PREPARED {result}")
        else:
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be 1..65535")
            serve_workspace(args.manifest, args.port)
    except (ValueError, OSError) as exc:
        print(f"TRAJECTORY_AUTHOR_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
