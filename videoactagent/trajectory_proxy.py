"""Launch a real Blender trajectory-controlled proxy render."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from videoactagent.shotscript import ShotScript
from videoactagent.trajectory import TrajectoryInstruction


def camera_world_xy(
    normalized_x: float,
    normalized_y: float,
    centre_x: float,
    centre_y: float,
    scale: float = 18.0,
) -> tuple[float, float]:
    """Project top-left normalized camera points into a Y-up world plane."""

    return (
        centre_x + (float(normalized_x) - 0.5) * scale,
        centre_y + (0.5 - float(normalized_y)) * scale,
    )


def signed_turn_orientation(points: list[tuple[float, float]]) -> float:
    """Return summed signed turns; negative means clockwise in a Y-up plane."""

    total = 0.0
    for first, middle, last in zip(points, points[1:], points[2:]):
        first_dx = middle[0] - first[0]
        first_dy = middle[1] - first[1]
        second_dx = last[0] - middle[0]
        second_dy = last[1] - middle[1]
        total += first_dx * second_dy - first_dy * second_dx
    return total


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    return parser


def _resolve_input(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} is not a file: {resolved}")
    return resolved


def _resolve_output(path: Path) -> Path:
    root = Path.cwd().resolve()
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output directory must stay below the workspace") from exc
    if resolved.exists():
        raise ValueError(f"output directory already exists: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
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


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _captured_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _file_record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _publish_directory(staged: Path, destination: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(staged, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _register_success_evidence(render_dir: Path, exit_code: int) -> dict[str, object]:
    manifest_path = render_dir / "trajectory_proxy_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stdout_log = render_dir / "blender.stdout.log"
    stderr_log = render_dir / "blender.stderr.log"
    manifest["process"] = {"exit_code": exit_code, "timed_out": False}
    manifest["logs"] = {
        "stdout": _file_record(stdout_log, render_dir),
        "stderr": _file_record(stderr_log, render_dir),
    }
    artifact_paths = {
        "blend": render_dir / str(manifest["blend"]["path"]),
        "video": render_dir / str(manifest["video"]["path"]),
        "frame_first": render_dir / str(manifest["frames"]["first"]["path"]),
        "frame_middle": render_dir / str(manifest["frames"]["middle"]["path"]),
        "frame_last": render_dir / str(manifest["frames"]["last"]["path"]),
        "overlay": render_dir / str(manifest["overlay"]["path"]),
        "blender_stdout": stdout_log,
        "blender_stderr": stderr_log,
    }
    missing = [str(path) for path in artifact_paths.values() if not path.is_file()]
    if missing:
        raise ValueError(f"missing successful render artifacts: {missing}")
    manifest["artifacts"] = {
        name: _file_record(path, render_dir) for name, path in artifact_paths.items()
    }
    _atomic_write(manifest_path, _json_bytes(manifest))
    return manifest


def _write_failure_evidence(
    render_dir: Path,
    *,
    exit_code: int | None,
    timed_out: bool,
    error: str,
    shotscript_source: Path,
    trajectory_source: Path,
) -> dict[str, object]:
    stdout_log = render_dir / "blender.stdout.log"
    stderr_log = render_dir / "blender.stderr.log"
    partial = {}
    for path in sorted(render_dir.rglob("*")):
        if path.is_file() and path.name not in {
            "failure_manifest.json",
            "blender.stdout.log",
            "blender.stderr.log",
        }:
            partial[str(path.relative_to(render_dir)).replace("\\", "/")] = _file_record(
                path, render_dir
            )
    failure = {
        "schema_version": "0.1",
        "status": "failed",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "error": error,
        "shotscript_sha256": _sha256_file(shotscript_source),
        "trajectory_sha256": _sha256_file(trajectory_source),
        "logs": {
            "stdout": _file_record(stdout_log, render_dir),
            "stderr": _file_record(stderr_log, render_dir),
        },
        "partial_artifacts": partial,
    }
    _atomic_write(render_dir / "failure_manifest.json", _json_bytes(failure))
    return failure


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        blender = _resolve_input(args.blender, "Blender executable")
        shotscript_source = _resolve_input(args.shotscript, "ShotScript")
        trajectory_source = _resolve_input(args.trajectory, "trajectory")
        output_dir = _resolve_output(args.output_dir)
        if args.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_root = output_dir.parent / f".{output_dir.name}.{uuid4().hex}.tmp"
        staging_root.mkdir()
        render_dir = staging_root / "render"
        with tempfile.TemporaryDirectory(prefix="videoactagent-trajectory-proxy-") as root:
            snapshot_root = Path(root)
            shotscript_snapshot = snapshot_root / "shotscript.json"
            trajectory_snapshot = snapshot_root / "trajectory.json"
            shutil.copyfile(shotscript_source, shotscript_snapshot)
            shutil.copyfile(trajectory_source, trajectory_snapshot)
            script = ShotScript.from_path(shotscript_snapshot)
            instruction = TrajectoryInstruction.from_path(trajectory_snapshot)
            instruction.validate_identity(script.scene_id, instruction.shot_id)
            matches = [shot for shot in script.shots if shot.shot_id == instruction.shot_id]
            if len(matches) != 1:
                raise ValueError(
                    f"expected exactly one controlled shot {instruction.shot_id!r}, found {len(matches)}"
                )
            if matches[0].duration != instruction.duration_seconds:
                raise ValueError("trajectory duration does not match controlled shot")

            compiler = Path(__file__).with_name("blender_proxy.py").resolve()
            command = [
                str(blender),
                "--background",
                "--factory-startup",
                "-F",
                "FFMPEG",
                "--python",
                str(compiler),
                "--",
                "--shotscript",
                str(shotscript_snapshot),
                "--trajectory",
                str(trajectory_snapshot),
                "--output-dir",
                str(render_dir),
            ]
            timed_out = False
            exit_code: int | None
            error = ""
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=args.timeout_seconds,
                )
                stdout = _captured_text(completed.stdout)
                stderr = _captured_text(completed.stderr)
                exit_code = completed.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = _captured_text(exc.stdout)
                stderr = _captured_text(exc.stderr)
                timed_out = True
                exit_code = None
                error = f"Blender timed out after {args.timeout_seconds} seconds"

            render_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(render_dir / "blender.stdout.log", stdout.encode("utf-8"))
            _atomic_write(render_dir / "blender.stderr.log", stderr.encode("utf-8"))
            evidence = stdout + stderr
            succeeded = (
                not timed_out
                and exit_code == 0
                and "Traceback" not in stderr
                and "TRAJECTORY_PROXY_OK" in evidence
            )
            if succeeded:
                try:
                    manifest = _register_success_evidence(render_dir, int(exit_code))
                except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    succeeded = False
                    error = f"success evidence validation failed: {type(exc).__name__}: {exc}"
            if not succeeded:
                if not error:
                    error = (
                        f"Blender exit={exit_code}; marker_present="
                        f"{'TRAJECTORY_PROXY_OK' in evidence}; traceback={'Traceback' in stderr}"
                    )
                _write_failure_evidence(
                    render_dir,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    error=error,
                    shotscript_source=shotscript_snapshot,
                    trajectory_source=trajectory_snapshot,
                )
            _publish_directory(render_dir, output_dir)
        if staging_root.exists():
            staging_root.rmdir()
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
        if not succeeded:
            print(f"TRAJECTORY_PROXY_FAILED: {error}", file=sys.stderr)
            return 124 if timed_out else (exit_code or 1)
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"TRAJECTORY_PROXY_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        "TRAJECTORY_PROXY_OK "
        f"{output_dir} video_sha256={manifest['video']['sha256']} "
        f"trajectory_sha256={manifest['trajectory_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
