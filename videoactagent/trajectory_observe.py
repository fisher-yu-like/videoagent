"""Extract real video frames and collect hash-bound manual trajectory clicks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import imageio_ffmpeg
from PIL import Image

from videoactagent.trajectory import TrajectoryInstruction, canonical_bytes


SCHEMA_VERSION = "0.1"
EVIDENCE_TYPE = "manual_visual_annotation"
COORDINATE_SPACE = "normalized_0_1_top_left"
_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "scene_id",
        "shot_id",
        "track_id",
        "video_sha256",
        "trajectory_file_sha256",
        "points",
    }
)
_CLICK_KEYS = frozenset({"frame", "x", "y", "visible"})
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "coordinate_space",
        "scene_id",
        "shot_id",
        "track_id",
        "target",
        "video_sha256",
        "video_bytes",
        "video_frame_count",
        "video_size",
        "video_fps",
        "video_duration_seconds",
        "trajectory_file_sha256",
        "trajectory_canonical_sha256",
        "frames",
    }
)
_FRAME_KEYS = frozenset(
    {"frame", "t", "time_seconds", "path", "sha256", "width", "height"}
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_strict_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_bytes().decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _safe_output(path: Path, workspace: Path | None) -> Path:
    if workspace is not None:
        root = workspace.resolve()
        resolved = (
            path.resolve(strict=False)
            if path.is_absolute()
            else (root / path).resolve(strict=False)
        )
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("output must stay below workspace") from exc
    else:
        resolved = path.resolve(strict=False)
    return resolved


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            pass
    return False


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _close_reader(reader: object) -> None:
    generator_frame = getattr(reader, "gi_frame", None)
    process = generator_frame.f_locals.get("process") if generator_frame else None
    try:
        reader.close()  # type: ignore[attr-defined]
    finally:
        if process is not None:
            for pipe_name in ("stdin", "stdout"):
                pipe = getattr(process, pipe_name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _decode_selected_frames(
    video_path: Path, frame_indices: tuple[int, ...], frame_dir: Path
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    counted_frames, counted_seconds = imageio_ffmpeg.count_frames_and_secs(
        str(video_path)
    )
    if type(counted_frames) is not int or counted_frames <= 0:
        raise ValueError("video has no decodable frames")
    if not frame_indices:
        raise ValueError("at least one frame index is required")
    if any(type(index) is not int or index < 0 or index >= counted_frames for index in frame_indices):
        raise ValueError(f"frame index must be in [0, {counted_frames - 1}]")
    if list(frame_indices) != sorted(set(frame_indices)):
        raise ValueError("frame indices must be sorted and unique")

    reader = imageio_ffmpeg.read_frames(str(video_path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        if not isinstance(metadata, Mapping):
            raise ValueError("ffmpeg metadata is unavailable")
        size = metadata.get("size")
        fps = metadata.get("fps")
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or type(size[0]) is not int
            or type(size[1]) is not int
            or size[0] <= 0
            or size[1] <= 0
        ):
            raise ValueError("video size is unavailable")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(float(fps)) or fps <= 0:
            raise ValueError("video fps is unavailable")
        width, height = int(size[0]), int(size[1])
        selected = set(frame_indices)
        records: list[dict[str, object]] = []
        for index, raw_frame in enumerate(reader):
            if index not in selected:
                if index > frame_indices[-1]:
                    break
                continue
            if len(raw_frame) != width * height * 3:
                raise ValueError(f"decoded frame {index} has an invalid byte count")
            relative = Path("frames") / f"frame_{index:06d}.png"
            frame_path = frame_dir.parent / relative
            image = Image.frombytes("RGB", (width, height), raw_frame)
            image.save(frame_path, format="PNG", optimize=False)
            records.append(
                {
                    "frame": index,
                    "t": 0.0 if counted_frames == 1 else index / (counted_frames - 1),
                    "time_seconds": index / float(fps),
                    "path": relative.as_posix(),
                    "sha256": sha256_file(frame_path),
                    "width": width,
                    "height": height,
                }
            )
            # Stop while the generator is still suspended so ``_close_reader``
            # can reach and close its subprocess pipes.  Iterating once more
            # would exhaust the generator and discard that process reference.
            if index == frame_indices[-1]:
                break
        if [record["frame"] for record in records] != list(frame_indices):
            raise ValueError("ffmpeg ended before all selected frames were decoded")
        return (
            {
                "frame_count": counted_frames,
                "duration_seconds": float(counted_seconds),
                "size": [width, height],
                "fps": float(fps),
            },
            records,
        )
    finally:
        _close_reader(reader)


def prepare_session(
    video_path: Path,
    trajectory_path: Path,
    track_id: str,
    output_dir: Path,
    frame_indices: Sequence[int],
    *,
    workspace: Path | None = None,
) -> dict[str, object]:
    """Decode selected real frames and atomically publish one observer session."""

    video = Path(video_path).resolve()
    trajectory_source = Path(trajectory_path).resolve()
    output = _safe_output(Path(output_dir), workspace)
    if not video.is_file():
        raise ValueError(f"video is not a file: {video}")
    if not trajectory_source.is_file():
        raise ValueError(f"trajectory is not a file: {trajectory_source}")
    if _same_file(output, video) or _same_file(output, trajectory_source):
        raise ValueError("output collides with an input file")
    if output.exists():
        raise FileExistsError(f"observer output already exists: {output}")

    indices = tuple(frame_indices)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid4().hex}.tmp"
    try:
        staging.mkdir()
        (staging / "frames").mkdir()
        video_snapshot = staging / f".source-video{video.suffix}"
        trajectory_snapshot = staging / ".source-trajectory.json"
        shutil.copyfile(video, video_snapshot)
        shutil.copyfile(trajectory_source, trajectory_snapshot)
        trajectory_bytes = trajectory_snapshot.read_bytes()
        instruction = TrajectoryInstruction.from_json_bytes(trajectory_bytes)
        tracks = [track for track in instruction.tracks if track.track_id == track_id]
        if len(tracks) != 1:
            raise ValueError(f"trajectory must contain exactly one track {track_id!r}")
        track = tracks[0]
        video_sha256 = sha256_file(video_snapshot)
        video_bytes = video_snapshot.stat().st_size
        trajectory_file_sha256 = hashlib.sha256(trajectory_bytes).hexdigest()
        video_metadata, frames = _decode_selected_frames(
            video_snapshot, indices, staging / "frames"
        )
        if (
            not video.is_file()
            or video.stat().st_size != video_bytes
            or sha256_file(video) != video_sha256
            or not trajectory_source.is_file()
            or sha256_file(trajectory_source) != trajectory_file_sha256
        ):
            raise ValueError("input changed during observation preparation")
        video_snapshot.unlink()
        trajectory_snapshot.unlink()
        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "evidence_type": "manual_visual_annotation_session",
            "coordinate_space": COORDINATE_SPACE,
            "scene_id": instruction.scene_id,
            "shot_id": instruction.shot_id,
            "track_id": track.track_id,
            "target": track.target.to_dict(),
            "video_sha256": video_sha256,
            "video_bytes": video_bytes,
            "video_frame_count": video_metadata["frame_count"],
            "video_size": video_metadata["size"],
            "video_fps": video_metadata["fps"],
            "video_duration_seconds": video_metadata["duration_seconds"],
            "trajectory_file_sha256": trajectory_file_sha256,
            "trajectory_canonical_sha256": hashlib.sha256(
                canonical_bytes(instruction)
            ).hexdigest(),
            "frames": frames,
        }
        _atomic_write(staging / "session_manifest.json", _json_bytes(manifest))
        os.replace(staging, output)
        if os.name == "posix":
            directory_fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _unit_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number in [0, 1]") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be a finite number in [0, 1]")
    return 0.0 if result == 0.0 else result


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def validate_session_manifest(value: Mapping[str, object]) -> None:
    _exact_keys(value, _MANIFEST_KEYS, "session manifest")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("session schema_version mismatch")
    if value["evidence_type"] != "manual_visual_annotation_session":
        raise ValueError("session evidence_type mismatch")
    if value["coordinate_space"] != COORDINATE_SPACE:
        raise ValueError("session coordinate_space mismatch")
    for field in ("scene_id", "shot_id", "track_id"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"session {field} must be a non-empty string")
    target = value["target"]
    if not isinstance(target, Mapping):
        raise ValueError("session target must be an object")
    _exact_keys(target, frozenset({"type", "id"}), "session target")
    if not all(isinstance(target[field], str) and target[field] for field in ("type", "id")):
        raise ValueError("session target values must be non-empty strings")
    for field in (
        "video_sha256",
        "trajectory_file_sha256",
        "trajectory_canonical_sha256",
    ):
        _require_sha256(value[field], f"session {field}")
    for field in ("video_bytes", "video_frame_count"):
        if type(value[field]) is not int or value[field] <= 0:  # type: ignore[operator]
            raise ValueError(f"session {field} must be a positive integer")
    size = value["video_size"]
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(dimension) is not int or dimension <= 0 for dimension in size)
    ):
        raise ValueError("session video_size must contain two positive integers")
    for field in ("video_fps", "video_duration_seconds"):
        number = value[field]
        try:
            converted = float(number) if not isinstance(number, bool) else math.nan
        except (OverflowError, TypeError, ValueError):
            converted = math.nan
        if not math.isfinite(converted) or converted <= 0:
            raise ValueError(f"session {field} must be finite and positive")
    frames = value["frames"]
    if not isinstance(frames, list) or not frames:
        raise ValueError("session frames must be a non-empty list")
    previous_index = -1
    previous_t = -1.0
    for frame in frames:
        if not isinstance(frame, Mapping):
            raise ValueError("session frame must be an object")
        _exact_keys(frame, _FRAME_KEYS, "session frame")
        index = frame["frame"]
        if type(index) is not int or index <= previous_index:
            raise ValueError("session frame indices must be strictly increasing")
        previous_index = index
        t = _unit_number(frame["t"], "session frame t")
        if t <= previous_t:
            raise ValueError("session frame times must be strictly increasing")
        previous_t = t
        for label in ("time_seconds",):
            number = frame[label]
            try:
                converted = float(number) if not isinstance(number, bool) else math.nan
            except (OverflowError, TypeError, ValueError):
                converted = math.nan
            if not math.isfinite(converted) or converted < 0:
                raise ValueError(f"session frame {label} must be finite and non-negative")
        path = frame["path"]
        if not isinstance(path, str) or not path:
            raise ValueError("session frame path must be a non-empty string")
        _require_sha256(frame["sha256"], "session frame sha256")
        if frame["width"] != size[0] or frame["height"] != size[1]:
            raise ValueError("session frame dimensions must match video_size")


def save_annotation(session_dir: Path, payload: Mapping[str, object]) -> dict[str, object]:
    """Validate one-click-per-frame input and atomically save the annotation."""

    session = Path(session_dir).resolve()
    manifest_path = session / "session_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("session manifest is missing")
    manifest = load_strict_object(manifest_path, "session manifest")
    validate_session_manifest(manifest)
    if not isinstance(payload, Mapping):
        raise ValueError("annotation payload must be an object")
    _exact_keys(payload, _PAYLOAD_KEYS, "annotation payload")
    for field in (
        "schema_version",
        "scene_id",
        "shot_id",
        "track_id",
        "video_sha256",
        "trajectory_file_sha256",
    ):
        expected = manifest[field] if field != "schema_version" else SCHEMA_VERSION
        if payload[field] != expected:
            raise ValueError(f"annotation {field} provenance mismatch")
    if payload["evidence_type"] != EVIDENCE_TYPE:
        raise ValueError("annotation evidence_type must be manual_visual_annotation")
    raw_points = payload["points"]
    if not isinstance(raw_points, list):
        raise ValueError("annotation points must be a list")
    frames = manifest["frames"]
    assert isinstance(frames, list)
    by_frame = {frame["frame"]: frame for frame in frames}  # type: ignore[index]
    if len(raw_points) != len(by_frame):
        raise ValueError("annotation requires exactly one point per selected frame")
    points: list[dict[str, object]] = []
    seen: set[int] = set()
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            raise ValueError("annotation point must be an object")
        _exact_keys(raw, _CLICK_KEYS, "annotation point")
        frame_index = raw["frame"]
        if type(frame_index) is not int or frame_index not in by_frame or frame_index in seen:
            raise ValueError("annotation requires exactly one point per selected frame")
        seen.add(frame_index)
        visible = raw["visible"]
        if type(visible) is not bool:
            raise ValueError("annotation point visible must be a JSON boolean")
        if visible:
            x = _unit_number(raw["x"], "annotation point x")
            y = _unit_number(raw["y"], "annotation point y")
        else:
            if raw["x"] is not None or raw["y"] is not None:
                raise ValueError("occluded annotation points require null x and y")
            x = y = None
        frame = by_frame[frame_index]
        points.append(
            {
                "frame": frame_index,
                "t": frame["t"],
                "time_seconds": frame["time_seconds"],
                "frame_sha256": frame["sha256"],
                "x": x,
                "y": y,
                "visible": visible,
            }
        )
    points.sort(key=lambda item: item["frame"])  # type: ignore[arg-type]
    annotation: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_type": EVIDENCE_TYPE,
        "coordinate_space": COORDINATE_SPACE,
        "scene_id": manifest["scene_id"],
        "shot_id": manifest["shot_id"],
        "track_id": manifest["track_id"],
        "target": manifest["target"],
        "video_sha256": manifest["video_sha256"],
        "trajectory_file_sha256": manifest["trajectory_file_sha256"],
        "trajectory_canonical_sha256": manifest["trajectory_canonical_sha256"],
        "session_manifest_sha256": sha256_file(manifest_path),
        "points": points,
    }
    _atomic_write(session / "manual_annotation.json", _json_bytes(annotation))
    return annotation


def _handler(session_dir: Path) -> type[BaseHTTPRequestHandler]:
    html_path = Path(__file__).resolve().parents[1] / "static" / "trajectory_observer.html"

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(
            self, code: str, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST
        ) -> None:
            body = _json_bytes({"error": {"code": code, "message": message}})
            self._send(status, body, "application/json; charset=utf-8")

        def _guard_request(self, *, check_origin: bool) -> bool:
            host, port = self.server.server_address[:2]  # type: ignore[index]
            expected_host = f"{host}:{port}"
            if self.headers.get("Host") != expected_host:
                self._error("invalid_host", "Host must match the localhost observer")
                return False
            if check_origin:
                origin = self.headers.get("Origin")
                if origin is not None and origin != f"http://{expected_host}":
                    self._error(
                        "invalid_origin", "Origin must match the localhost observer"
                    )
                    return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard_request(check_origin=False):
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send(HTTPStatus.OK, html_path.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/session":
                self._send(
                    HTTPStatus.OK,
                    (session_dir / "session_manifest.json").read_bytes(),
                    "application/json",
                )
                return
            if path.startswith("/frames/"):
                name = path.removeprefix("/frames/")
                if not name or Path(name).name != name:
                    self._error("invalid_frame_path", "Invalid frame path")
                    return
                frame = (session_dir / "frames" / name).resolve(strict=False)
                try:
                    frame.relative_to((session_dir / "frames").resolve())
                except ValueError:
                    self._error("invalid_frame_path", "Invalid frame path")
                    return
                if not frame.is_file():
                    self._error("not_found", "Frame not found", HTTPStatus.NOT_FOUND)
                    return
                self._send(HTTPStatus.OK, frame.read_bytes(), "image/png")
                return
            self._error("not_found", "Route not found", HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard_request(check_origin=True):
                return
            if urlsplit(self.path).path != "/annotation":
                self._error("not_found", "Route not found", HTTPStatus.NOT_FOUND)
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                length_text = self.headers.get("Content-Length")
                if length_text is None or not length_text.isdecimal():
                    raise ValueError("Content-Length is required")
                length = int(length_text)
                if length <= 0 or length > 1024 * 1024:
                    raise ValueError("annotation body size is invalid")
                payload = json.loads(
                    self.rfile.read(length).decode("utf-8", errors="strict"),
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON number: {token}")
                    ),
                )
                annotation = save_annotation(session_dir, payload)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                self._error("invalid_annotation", str(exc))
                return
            self._send(HTTPStatus.OK, _json_bytes(annotation), "application/json")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _parse_frames(value: str) -> tuple[int, ...]:
    try:
        frames = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("frames must be comma-separated integers") from exc
    if not frames:
        raise argparse.ArgumentTypeError("at least one frame is required")
    return frames


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "serve"):
        child = subparsers.add_parser(command)
        child.add_argument("--video", type=Path, required=True)
        child.add_argument("--trajectory", type=Path, required=True)
        child.add_argument("--track", required=True)
        child.add_argument("--output-dir", type=Path, required=True)
        child.add_argument("--frames", type=_parse_frames, required=True)
        child.add_argument("--workspace", type=Path, default=Path("."))
        if command == "serve":
            child.add_argument("--port", type=int, default=8766)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    server: ThreadingHTTPServer | None = None
    try:
        output = _safe_output(args.output_dir, args.workspace)
        if args.command == "serve":
            if type(args.port) is not int or not 1 <= args.port <= 65535:
                raise ValueError("port must be in [1, 65535]")
            # Reserve the port before producing a session so a bind failure
            # cannot leave an output behind from a failed serve command.
            server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(output))
        manifest = prepare_session(
            args.video,
            args.trajectory,
            args.track,
            output,
            args.frames,
            workspace=args.workspace,
        )
        if args.command == "serve":
            assert server is not None
            print(
                f"TRAJECTORY_OBSERVER_SERVING http://127.0.0.1:{args.port} "
                f"frames={len(manifest['frames'])}"
            )
            try:
                server.serve_forever()
            finally:
                server.server_close()
                server = None
        else:
            print(
                f"TRAJECTORY_OBSERVER_PREPARED output={output} "
                f"video_sha256={manifest['video_sha256']}"
            )
    except (FileExistsError, OSError, TypeError, ValueError) as exc:
        print(f"TRAJECTORY_OBSERVER_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
