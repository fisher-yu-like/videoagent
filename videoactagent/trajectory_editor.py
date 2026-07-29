"""Localhost-only trajectory authoring server.

The editor accepts workspace-relative paths only, snapshots the selected image
and ShotScript at startup, and never sends a request to a video backend.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from .trajectory import TrajectoryInstruction, canonical_bytes


MAX_REQUEST_BYTES = 1024 * 1024
OUTPUT_LOCK_TIMEOUT_SECONDS = 5.0
OUTPUT_LOCK_RETRY_SECONDS = 0.02
LOCK_DIAGNOSTIC_MAX_BYTES = 16 * 1024
LOCK_BYTE_OFFSET = LOCK_DIAGNOSTIC_MAX_BYTES
STATIC_HTML = Path(__file__).with_name("static") / "trajectory_editor.html"
STATIC_LOGIC = Path(__file__).with_name("static") / "trajectory_editor_logic.js"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_unsafe_relative(path: Path, label: str) -> None:
    if path.is_absolute():
        raise ValueError(f"{label} must be workspace-relative")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must not escape the workspace")


def _reject_symlink_components(root: Path, relative: Path, label: str) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"{label} contains a symlink component: {current}")


def _workspace_path(root: Path, relative: Path, label: str) -> Path:
    _reject_unsafe_relative(relative, label)
    _reject_symlink_components(root, relative, label)
    candidate = root.joinpath(*relative.parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside workspace") from exc
    return candidate


def _read_single_link_input(root: Path, relative: Path, label: str) -> tuple[Path, bytes]:
    path = _workspace_path(root, relative, label)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if info.st_nlink != 1:
        raise ValueError(f"{label} must not be a hardlink")
    with path.open("rb") as handle:
        data = handle.read()
        opened = os.fstat(handle.fileno())
    if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
        raise ValueError(f"{label} changed while it was opened")
    return path, data


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _safe_existing_output(path: Path, inputs: tuple[Path, Path]) -> bool:
    if not path.exists():
        return False
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"output contains a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"output must be a regular file: {path}")
    if info.st_nlink != 1:
        raise ValueError(f"output must not be a hardlink: {path}")
    if any(_same_file(path, source) for source in inputs):
        raise ValueError(f"output collision with input: {path}")
    return True


def _write_temporary(
    temporary: Path,
    data: bytes,
    verify_directory: Callable[[], None],
) -> Path:
    verify_directory()
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        verify_directory()
    except BaseException:
        try:
            verify_directory()
            temporary.unlink(missing_ok=True)
            verify_directory()
        except BaseException:
            pass
        raise
    return temporary


def _fsync_directory(directory: Path) -> None:
    # CPython on Windows cannot fsync a directory handle.  The transaction
    # journal is therefore fsynced as a file and kept in the output directory's
    # parent, but stdlib alone cannot fully close a hostile directory-rename
    # race on Windows.  Identity checks plus the durable journal are the
    # explicit recovery-oriented degradation.
    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _OutputLockOwnership:
    device: int
    inode: int
    token: str


def _lock_open_flags(*, writable: bool) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("lock metadata write made no progress")
        offset += written


def _write_persistent_lock_metadata(descriptor: int, data: bytes) -> None:
    if len(data) > LOCK_DIAGNOSTIC_MAX_BYTES:
        raise ValueError("output lock metadata exceeds diagnostic region")
    os.ftruncate(descriptor, LOCK_BYTE_OFFSET + 1)
    os.lseek(descriptor, 0, os.SEEK_SET)
    _write_all(descriptor, data)
    _write_all(descriptor, b" " * (LOCK_DIAGNOSTIC_MAX_BYTES - len(data)))
    os.fsync(descriptor)


def _read_lock_diagnostic(path: Path) -> str:
    """Read one bounded persistent-lock record without following links."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return "lock disappeared before diagnostics were read"
    except OSError as exc:
        return f"lock metadata unavailable: {type(exc).__name__}: {exc}"
    if not stat.S_ISREG(before.st_mode):
        return f"unsafe lock type (mode={oct(before.st_mode)})"
    if before.st_nlink != 1:
        return f"unsafe lock link count ({before.st_nlink})"
    try:
        descriptor = os.open(path, _lock_open_flags(writable=False))
    except OSError as exc:
        return f"lock metadata unavailable: {type(exc).__name__}: {exc}"
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            return "unsafe opened lock metadata"
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return "lock changed while diagnostics were opened"
        data = os.read(descriptor, LOCK_DIAGNOSTIC_MAX_BYTES)
    except OSError as exc:
        return f"lock metadata unavailable: {type(exc).__name__}: {exc}"
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError:
        return "lock changed while diagnostics were read"
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        return "lock changed while diagnostics were read"
    return data.decode("utf-8", errors="replace").strip() or "empty lock metadata"


def _validate_persistent_lock_info(info: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular lock file")
    if info.st_nlink != 1:
        raise ValueError(f"{label} must not be a hardlink (link count {info.st_nlink})")


def _open_persistent_lock(path: Path, token: str) -> tuple[int, _OutputLockOwnership]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None:
        _validate_persistent_lock_info(before, "output lock")

    descriptor = os.open(path, _lock_open_flags(writable=True) | os.O_CREAT, 0o600)
    try:
        opened = os.fstat(descriptor)
        _validate_persistent_lock_info(opened, "opened output lock")
        current = path.lstat()
        _validate_persistent_lock_info(current, "output lock")
        opened_identity = (int(opened.st_dev), int(opened.st_ino))
        if opened_identity != (int(current.st_dev), int(current.st_ino)):
            raise ValueError("output lock changed while it was opened")
        if before is not None and opened_identity != (int(before.st_dev), int(before.st_ino)):
            raise ValueError("output lock changed during acquisition")
        return descriptor, _OutputLockOwnership(*opened_identity, token)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _assert_persistent_lock_identity(
    path: Path,
    descriptor: int,
    ownership: _OutputLockOwnership,
) -> None:
    opened = os.fstat(descriptor)
    _validate_persistent_lock_info(opened, "opened output lock")
    current = path.lstat()
    _validate_persistent_lock_info(current, "output lock")
    expected = (ownership.device, ownership.inode)
    if (int(opened.st_dev), int(opened.st_ino)) != expected:
        raise ValueError("opened output lock identity changed")
    if (int(current.st_dev), int(current.st_ino)) != expected:
        raise ValueError("output lock path identity changed while held")


def _try_advisory_lock(descriptor: int) -> bool:
    opened = os.fstat(descriptor)
    if opened.st_size <= LOCK_BYTE_OFFSET:
        os.lseek(descriptor, LOCK_BYTE_OFFSET, os.SEEK_SET)
        os.write(descriptor, b"\0")
        os.fsync(descriptor)
    os.lseek(descriptor, LOCK_BYTE_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import errno
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                exc, "winerror", None
            ) in {33, 36, 158}:
                return False
            raise
    else:
        import errno
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
    return True


def _unlock_advisory_lock(descriptor: int) -> None:
    os.lseek(descriptor, LOCK_BYTE_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _release_output_lock(descriptor: int, ownership: _OutputLockOwnership) -> None:
    """Release the OS lock and descriptor; never delete the shared lock path."""

    try:
        _unlock_advisory_lock(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _write_transaction_journal(path: Path, record: dict[str, object]) -> None:
    data = (
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_transaction_journal(path: Path) -> None:
    # If unlink fails the journal remains.  Do not add a post-unlink operation
    # that could fail after the only recovery record has already disappeared.
    path.unlink()


def _commit_pair(
    json_path: Path,
    json_data: bytes,
    overlay_path: Path,
    overlay_data: bytes,
    *,
    verify_directory: Callable[[], None],
) -> None:
    """Commit both outputs or restore the prior consistent pair on failure."""

    directory = json_path.parent
    verify_directory()
    old_paths = (json_path.exists(), overlay_path.exists())
    verify_directory()
    if old_paths[0] != old_paths[1]:
        raise ValueError("existing trajectory outputs are an inconsistent partial pair")

    transaction_id = uuid4().hex
    journal_path = directory.parent / f".{directory.name}.trajectory_txn.{transaction_id}.json"
    json_temp = directory / f".{json_path.name}.{transaction_id}.tmp"
    overlay_temp = directory / f".{overlay_path.name}.{transaction_id}.tmp"
    backups = [
        (destination, directory / f".{destination.name}.{transaction_id}.bak")
        for destination in (json_path, overlay_path)
    ] if old_paths[0] else []
    record: dict[str, object] = {
        "schema_version": "0.1",
        "transaction_id": transaction_id,
        "phase": "prepared",
        "original_directory_identity": [],
        "destinations": [str(json_path), str(overlay_path)],
        "temps": [str(json_temp), str(overlay_temp)],
        "backups": [str(backup) for _, backup in backups],
        "potentially_installed": [],
        "installed": [],
        "errors": [],
    }

    owner = getattr(verify_directory, "__self__", None)
    identity = getattr(owner, "output_directory_identity", None)
    if identity is not None:
        record["original_directory_identity"] = list(identity)
    _write_transaction_journal(journal_path, record)

    def checked_replace(source: Path, destination: Path) -> None:
        verify_directory()
        os.replace(source, destination)
        verify_directory()

    def checked_unlink(path: Path) -> None:
        verify_directory()
        path.unlink(missing_ok=True)
        verify_directory()

    def update_journal(phase: str, error_messages: list[str] | None = None) -> None:
        record["phase"] = phase
        if error_messages is not None:
            record["errors"] = error_messages
        _write_transaction_journal(journal_path, record)

    def rollback(primary: BaseException) -> None:
        errors: list[tuple[Path, BaseException]] = []
        try:
            update_journal("rolling_back", [f"{type(primary).__name__}: {primary}"])
        except BaseException as exc:
            errors.append((journal_path, exc))

        if backups:
            for destination, backup in backups:
                if not backup.exists():
                    continue
                try:
                    checked_replace(backup, destination)
                except BaseException as exc:
                    errors.append((backup, exc))
        else:
            for path_text in reversed(record["potentially_installed"]):  # type: ignore[arg-type]
                path = Path(path_text)
                try:
                    checked_unlink(path)
                except BaseException as exc:
                    errors.append((path, exc))

        for temporary in (json_temp, overlay_temp):
            try:
                checked_unlink(temporary)
            except BaseException as exc:
                errors.append((temporary, exc))

        try:
            verify_directory()
            _fsync_directory(directory)
            verify_directory()
        except BaseException as exc:
            errors.append((directory, exc))

        if errors:
            messages = [f"{path}: {type(exc).__name__}: {exc}" for path, exc in errors]
            try:
                update_journal("rollback_failed", messages)
            except BaseException:
                pass
            preserved = [backup for _, backup in backups if backup.exists()]
            if preserved:
                paths = ", ".join(str(path) for path in preserved)
                raise OSError(
                    f"transaction recovery incomplete; backup(s) preserved at: {paths}; "
                    f"journal preserved at: {journal_path}"
                ) from primary
            raise OSError(
                f"transaction recovery incomplete; journal preserved at: {journal_path}"
            ) from primary

        try:
            update_journal("rolled_back")
            _remove_transaction_journal(journal_path)
        except BaseException as exc:
            raise OSError(f"rollback completed but journal preserved at: {journal_path}") from exc
        raise primary

    try:
        update_journal("writing_temps")
        _write_temporary(json_temp, json_data, verify_directory)
        _write_temporary(overlay_temp, overlay_data, verify_directory)
        if backups:
            update_journal("backing_up")
            for destination, backup in backups:
                checked_replace(destination, backup)

        update_journal("installing")
        for temporary, destination in ((json_temp, json_path), (overlay_temp, overlay_path)):
            potentially_installed = record["potentially_installed"]
            assert isinstance(potentially_installed, list)
            potentially_installed.append(str(destination))
            update_journal("installing")
            checked_replace(temporary, destination)
            installed = record["installed"]
            assert isinstance(installed, list)
            installed.append(str(destination))
            update_journal("installing")
        verify_directory()
        _fsync_directory(directory)
        verify_directory()
    except BaseException as primary:
        rollback(primary)

    cleanup_errors: list[tuple[Path, BaseException]] = []
    update_journal("cleaning_up")
    for temporary in (json_temp, overlay_temp):
        try:
            checked_unlink(temporary)
        except BaseException as exc:
            cleanup_errors.append((temporary, exc))
    for _, backup in backups:
        try:
            checked_unlink(backup)
        except BaseException as exc:
            cleanup_errors.append((backup, exc))
    if cleanup_errors:
        messages = [f"{path}: {type(exc).__name__}: {exc}" for path, exc in cleanup_errors]
        try:
            update_journal("cleanup_failed", messages)
        except BaseException:
            pass
        raise OSError(f"transaction committed but cleanup failed; journal preserved at: {journal_path}")

    update_journal("committed")
    try:
        _remove_transaction_journal(journal_path)
    except BaseException as exc:
        raise OSError(f"transaction committed but journal preserved at: {journal_path}") from exc


def _point_xy(point: Any, width: int, height: int) -> tuple[int, int]:
    return (
        max(0, min(width - 1, round(point.x * (width - 1)))),
        max(0, min(height - 1, round(point.y * (height - 1)))),
    )


def _draw_arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], color: str) -> None:
    import math

    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 4:
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    size = max(6.0, min(13.0, length * 0.25))
    tip = end
    left = (round(end[0] - ux * size + px * size * 0.55), round(end[1] - uy * size + py * size * 0.55))
    right = (round(end[0] - ux * size - px * size * 0.55), round(end[1] - uy * size - py * size * 0.55))
    draw.polygon((tip, left, right), fill=color)


def render_overlay(image_bytes: bytes, instruction: TrajectoryInstruction) -> bytes:
    """Render visible paths, direction arrows, and numbered control points."""

    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    palette = ("#00E5FF", "#FF3D71", "#FFD740", "#76FF03", "#D500F9")
    width, height = image.size
    scale = max(1, round(min(width, height) / 270))

    for track_index, track in enumerate(instruction.tracks):
        color = palette[track_index % len(palette)]
        points = [_point_xy(point, width, height) for point in track.points if point.visible]
        if not points:
            continue
        if track.primitive in {"polyline", "circle"} and len(points) > 1:
            connected = points + ([points[0]] if track.primitive == "circle" and points[-1] != points[0] else [])
            draw.line(connected, fill=color, width=3 * scale, joint="curve")
            arrow_index = max(1, len(points) // 2)
            _draw_arrow(draw, points[arrow_index - 1], points[arrow_index], color)
        for number, (x, y) in enumerate(points, start=1):
            radius = 4 * scale
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline="black", width=scale)
            draw.text((x + radius + 2, y - radius - 1), str(number), fill="white", stroke_width=2, stroke_fill="black", font=font)
        label = f"{track.track_id}: {track.semantic}"
        label_y = 8 + track_index * (16 * scale)
        draw.text((8, label_y), label, fill=color, stroke_width=2, stroke_fill="black", font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def load_editor_html() -> bytes:
    return STATIC_HTML.read_bytes()


def load_editor_logic() -> bytes:
    return STATIC_LOGIC.read_bytes()


@dataclass(frozen=True)
class EditorSession:
    workspace: Path
    image_path: Path
    shotscript_path: Path
    output_dir: Path
    shot_id: str
    scene_id: str
    duration_seconds: float
    sample_count: int
    image_bytes: bytes
    shotscript_bytes: bytes
    image_width: int
    image_height: int
    output_directory_identity: tuple[int, int, str]
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    @property
    def trajectory_path(self) -> Path:
        return self.output_dir / "trajectory.json"

    @property
    def overlay_path(self) -> Path:
        return self.output_dir / "trajectory_overlay.png"

    @property
    def output_lock_path(self) -> Path:
        return self.output_dir.parent / f".{self.output_dir.name}.trajectory.lock"

    @classmethod
    def from_paths(
        cls,
        *,
        workspace: Path,
        image: Path,
        shotscript: Path,
        shot_id: str,
        output_dir: Path,
    ) -> "EditorSession":
        root = workspace.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        image_path, image_bytes = _read_single_link_input(root, image, "image")
        shotscript_path, shotscript_bytes = _read_single_link_input(root, shotscript, "shotscript")
        if _same_file(image_path, shotscript_path):
            raise ValueError("image and shotscript must not alias")

        try:
            document = json.loads(shotscript_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("shotscript must be valid UTF-8 JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("scene_id"), str):
            raise ValueError("shotscript must contain scene_id")
        shots = document.get("shots")
        if not isinstance(shots, list):
            raise ValueError("shotscript must contain a shots list")
        matching = [shot for shot in shots if isinstance(shot, dict) and shot.get("shot_id") == shot_id]
        if len(matching) != 1:
            raise ValueError(f"shotscript must contain exactly one shot {shot_id!r}")
        duration = matching[0].get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or float(duration) <= 0:
            raise ValueError("selected shot must have a positive duration")
        fps = document.get("fps", 24)
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or float(fps) <= 0:
            raise ValueError("shotscript fps must be positive")

        try:
            with Image.open(io.BytesIO(image_bytes)) as preview:
                converted = preview.convert("RGB")
                width, height = converted.size
                png_buffer = io.BytesIO()
                converted.save(png_buffer, format="PNG", optimize=False)
                served_image_bytes = png_buffer.getvalue()
        except (OSError, ValueError) as exc:
            raise ValueError("image must be a decodable raster preview") from exc

        output_path = _workspace_path(root, output_dir, "output-dir")
        output_lock_path = output_path.parent / f".{output_path.name}.trajectory.lock"
        if any(_same_file(output_lock_path, source) for source in (image_path, shotscript_path)):
            raise ValueError("output lock collision with input")
        output_path.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(root, output_dir, "output-dir")
        trajectory_path = output_path / "trajectory.json"
        overlay_path = output_path / "trajectory_overlay.png"
        present = (
            _safe_existing_output(trajectory_path, (image_path, shotscript_path)),
            _safe_existing_output(overlay_path, (image_path, shotscript_path)),
        )
        if present[0] != present[1]:
            raise ValueError("existing trajectory outputs are an inconsistent partial pair")
        output_info = output_path.stat()
        output_identity = (
            int(output_info.st_dev),
            int(output_info.st_ino),
            str(output_path.resolve(strict=True)),
        )

        return cls(
            workspace=root,
            image_path=image_path,
            shotscript_path=shotscript_path,
            output_dir=output_path,
            shot_id=shot_id,
            scene_id=document["scene_id"],
            duration_seconds=float(duration),
            sample_count=max(1, round(float(duration) * float(fps)) + 1),
            image_bytes=served_image_bytes,
            shotscript_bytes=shotscript_bytes,
            image_width=width,
            image_height=height,
            output_directory_identity=output_identity,
        )

    def _assert_output_directory_identity(self) -> None:
        relative = self.output_dir.relative_to(self.workspace)
        actual = _workspace_path(self.workspace, relative, "output-dir")
        try:
            info = actual.stat()
            identity = (int(info.st_dev), int(info.st_ino), str(actual.resolve(strict=True)))
        except OSError as exc:
            raise ValueError("output directory identity is unavailable") from exc
        if identity != self.output_directory_identity:
            raise ValueError("output directory identity changed after editor startup")

    def _assert_outputs_safe(self) -> None:
        self._assert_output_directory_identity()

    @contextlib.contextmanager
    def _output_transaction_lock(self):
        """Exclude every other editor process targeting this output directory."""

        self._assert_output_directory_identity()
        path = self.output_lock_path
        token = uuid4().hex
        record = {
            "schema_version": "0.1",
            "pid": os.getpid(),
            "timestamp": time.time(),
            "output_identity": list(self.output_directory_identity),
            "output_directory": str(self.output_dir),
            "token": token,
        }
        data = (
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode("utf-8")
        deadline = time.monotonic() + max(0.0, float(OUTPUT_LOCK_TIMEOUT_SECONDS))
        descriptor: int | None = None
        ownership: _OutputLockOwnership | None = None

        while descriptor is None:
            candidate: int | None = None
            try:
                candidate, candidate_ownership = _open_persistent_lock(path, token)
                acquired = _try_advisory_lock(candidate)
            except BaseException:
                if candidate is not None:
                    try:
                        os.close(candidate)
                    except OSError:
                        pass
                raise

            if not acquired:
                assert candidate is not None
                os.close(candidate)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    diagnostic = _read_lock_diagnostic(path)
                    raise TimeoutError(
                        f"timed out waiting for output transaction lock {path}; "
                        f"existing lock retained for diagnosis: {diagnostic}"
                    )
                time.sleep(min(max(0.001, float(OUTPUT_LOCK_RETRY_SECONDS)), remaining))
                continue

            assert candidate is not None
            descriptor = candidate
            ownership = candidate_ownership
            try:
                _assert_persistent_lock_identity(path, descriptor, ownership)
                _write_persistent_lock_metadata(descriptor, data)
                _assert_persistent_lock_identity(path, descriptor, ownership)
                _fsync_directory(path.parent)
                self._assert_output_directory_identity()
            except BaseException:
                _release_output_lock(descriptor, ownership)
                descriptor = None
                raise

        assert ownership is not None
        try:
            yield
            present = (
                _safe_existing_output(self.trajectory_path, (self.image_path, self.shotscript_path)),
                _safe_existing_output(self.overlay_path, (self.image_path, self.shotscript_path)),
            )
            if present[0] != present[1]:
                raise ValueError("existing trajectory outputs are an inconsistent partial pair")
            self._assert_output_directory_identity()
            _assert_persistent_lock_identity(path, descriptor, ownership)
        finally:
            _release_output_lock(descriptor, ownership)

    def config(self) -> dict[str, object]:
        return {
            "schema_version": "0.1",
            "scene_id": self.scene_id,
            "shot_id": self.shot_id,
            "coordinate_space": "normalized_0_1_top_left",
            "duration_seconds": self.duration_seconds,
            "sample_count": self.sample_count,
            "image": {"url": "/preview.png", "width": self.image_width, "height": self.image_height},
        }

    def save(self, value: object) -> dict[str, object]:
        instruction = TrajectoryInstruction.from_dict(value)
        instruction.validate_identity(self.scene_id, self.shot_id)
        json_data = canonical_bytes(instruction)
        overlay_data = render_overlay(self.image_bytes, instruction)
        with self._lock, self._output_transaction_lock():
            self._assert_outputs_safe()
            _commit_pair(
                self.trajectory_path,
                json_data,
                self.overlay_path,
                overlay_data,
                verify_directory=self._assert_output_directory_identity,
            )
        return {
            "status": "ok",
            "trajectory": str(self.trajectory_path.relative_to(self.workspace)).replace("\\", "/"),
            "overlay": str(self.overlay_path.relative_to(self.workspace)).replace("\\", "/"),
            "trajectory_sha256": _sha256(json_data),
            "overlay_sha256": _sha256(overlay_data),
            "overlay_dimensions": [self.image_width, self.image_height],
        }

    def load_trajectory_bytes(self) -> bytes:
        with self._lock, self._output_transaction_lock():
            self._assert_outputs_safe()
            data = self.trajectory_path.read_bytes()
            instruction = TrajectoryInstruction.from_dict(json.loads(data))
            instruction.validate_identity(self.scene_id, self.shot_id)
            self._assert_output_directory_identity()
            return data


def _handler_type(session: EditorSession) -> type[BaseHTTPRequestHandler]:
    html = load_editor_html()
    logic = load_editor_logic()

    class Handler(BaseHTTPRequestHandler):
        server_version = "VideoActTrajectoryEditor/0.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _authorized(self, *, require_post_origin: bool = False) -> bool:
            expected_host = f"127.0.0.1:{self.server.server_address[1]}"
            if self.headers.get("Host") != expected_host:
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {"status": "error", "error": "forbidden Host"})
                return False
            origin = self.headers.get("Origin")
            if require_post_origin and origin is not None and origin != f"http://{expected_host}":
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {"status": "error", "error": "forbidden Origin"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
            elif path == "/preview.png":
                self._send(HTTPStatus.OK, session.image_bytes, "image/png")
            elif path == "/editor_logic.js":
                self._send(HTTPStatus.OK, logic, "text/javascript; charset=utf-8")
            elif path == "/config.json":
                self._json(HTTPStatus.OK, session.config())
            elif path == "/trajectory.json":
                try:
                    data = session.load_trajectory_bytes()
                except FileNotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"status": "error", "error": "no saved trajectory"})
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"status": "error", "error": str(exc)})
                else:
                    self._send(HTTPStatus.OK, data, "application/json; charset=utf-8")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"status": "error", "error": "not found"})

        def do_POST(self) -> None:
            if not self._authorized(require_post_origin=True):
                return
            if self.path.split("?", 1)[0] != "/save":
                self._json(HTTPStatus.NOT_FOUND, {"status": "error", "error": "not found"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"status": "error", "error": "application/json required"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("invalid Content-Length")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete request body")
                value = json.loads(body)
                result = session.save(value)
            except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(HTTPStatus.OK, result)

    return Handler


def create_server(session: EditorSession, port: int) -> ThreadingHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer in [0, 65535]")
    return ThreadingHTTPServer(("127.0.0.1", port), _handler_type(session))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--shot", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        session = EditorSession.from_paths(
            workspace=Path.cwd(),
            image=args.image,
            shotscript=args.shotscript,
            shot_id=args.shot,
            output_dir=args.output_dir,
        )
        server = create_server(session, args.port)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"TRAJECTORY_EDITOR_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"TRAJECTORY_EDITOR_READY http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
