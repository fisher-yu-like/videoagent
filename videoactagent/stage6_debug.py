"""User-runnable diagnostics for the Stage 6 VACE input adapter.

Usage::

    python -m videoactagent.stage6_debug doctor --vace-root third_party/VACE
    python -m videoactagent.stage6_debug verify-inputs --job runs/stage6_vace_inputs/s01/vace_job.json --bundle runs/stage2_control_bridge/control_bundle.json
    python -m videoactagent.stage6_debug print-probe-command

The last command only prints the pinned server-side preprocessing probe command.
It never starts preprocessing, downloads weights, or runs inference.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any

import imageio_ffmpeg

from videoactagent.vace_inputs import VACE_COMMIT, sha256_file, verify_prepared_job


DEFAULT_PROBE_COMMAND = (
    "cd /root/videoactagent && "
    "/root/venvs/vace/bin/python -m videoactagent.vace_preprocess_probe "
    "--job runs/stage6_vace_inputs/s01/vace_job.json "
    "--vace-root third_party/VACE "
    "--output runs/stage6_vace_inputs/s01/source_validation.json "
    "--validated-job-output "
    "runs/stage6_vace_inputs/s01/vace_job.validated.json"
)


def build_probe_command() -> str:
    """Return, but do not execute, the Stage 6 pinned server probe command."""

    return DEFAULT_PROBE_COMMAND


def _command_output(command: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr).strip()
    return completed.returncode == 0, output


def doctor(vace_root: Path) -> dict[str, Any]:
    """Inspect the actual local/server environment without changing it."""

    vace_root = Path(vace_root).resolve()
    python_ok = sys.version_info >= (3, 10)
    details: dict[str, Any] = {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "vace_root": str(vace_root),
    }

    ffmpeg_ok = False
    try:
        ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()
        ffmpeg_ok, ffmpeg_output = _command_output([ffmpeg_executable, "-version"])
        details["ffmpeg_executable"] = ffmpeg_executable
        details["ffmpeg_version_line"] = (
            ffmpeg_output.splitlines()[0] if ffmpeg_output else ""
        )
    except (OSError, RuntimeError) as exc:
        details["ffmpeg_error"] = str(exc)

    torch_ok = False
    cuda_ok = False
    try:
        import torch

        torch_ok = True
        details["torch_version"] = torch.__version__
        details["torch_cuda_version"] = torch.version.cuda
        cuda_ok = bool(torch.cuda.is_available())
        if cuda_ok:
            details["cuda_device_count"] = torch.cuda.device_count()
            details["cuda_device_name"] = torch.cuda.get_device_name(0)
    except (ImportError, OSError, RuntimeError) as exc:
        details["torch_error"] = str(exc)

    required_source = vace_root / "vace" / "vace_wan_inference.py"
    required_processor = vace_root / "vace" / "models" / "utils" / "preprocessor.py"
    vace_checkout_ok = (
        vace_root.is_dir()
        and (vace_root / ".git").exists()
        and required_source.is_file()
        and required_processor.is_file()
    )
    head_ok, head_output = _command_output(
        ["git", "rev-parse", "HEAD"], cwd=vace_root if vace_root.is_dir() else None
    )
    actual_commit = head_output.splitlines()[0] if head_ok and head_output else None
    vace_commit_ok = actual_commit == VACE_COMMIT
    details["actual_vace_commit"] = actual_commit
    if not head_ok:
        details["vace_git_error"] = head_output

    checks = {
        "python_ok": python_ok,
        "ffmpeg_ok": ffmpeg_ok,
        "torch_ok": torch_ok,
        "cuda_ok": cuda_ok,
        "vace_checkout_ok": vace_checkout_ok,
        "vace_commit_ok": vace_commit_ok,
    }
    return {
        "command": "doctor",
        "ok": all(checks.values()),
        **checks,
        "expected_vace_commit": VACE_COMMIT,
        "details": details,
    }


def _close_reader(reader: Any) -> None:
    generator_frame = getattr(reader, "gi_frame", None)
    process = (
        generator_frame.f_locals.get("process") if generator_frame is not None else None
    )
    try:
        reader.close()
    finally:
        if process is not None:
            for pipe_name in ("stdin", "stdout"):
                pipe = getattr(process, pipe_name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _probe_video(path: Path) -> dict[str, Any]:
    counted_frames, counted_seconds = imageio_ffmpeg.count_frames_and_secs(str(path))
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
    finally:
        _close_reader(reader)
    size = metadata.get("size")
    if not isinstance(size, tuple) or len(size) != 2:
        raise RuntimeError(f"ffmpeg did not report dimensions for {path}")
    fps = metadata.get("fps")
    if not isinstance(fps, (int, float)):
        raise RuntimeError(f"ffmpeg did not report fps for {path}")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "dimensions": [int(size[0]), int(size[1])],
        "frame_count": int(counted_frames),
        "fps": float(fps),
        "duration_seconds": float(counted_seconds),
    }


def _source_path(bundle_path: Path, recorded_path: str) -> Path:
    parts = PurePosixPath(recorded_path).parts
    if not parts or parts[0] != "source":
        raise ValueError(f"source control path has no source/ prefix: {recorded_path}")
    return bundle_path.resolve().parent.joinpath(*parts[1:]).resolve()


def verify_inputs(job_path: Path, bundle_path: Path) -> dict[str, Any]:
    """Verify a prepared job against real files and report decoded metadata."""

    job_path = Path(job_path).resolve()
    bundle_path = Path(bundle_path).resolve()
    job = verify_prepared_job(job_path, bundle_path)
    controls = job["source"]["controls"]
    proxy_path = _source_path(bundle_path, controls["proxy_video"]["path"])
    mask_path = (job_path.parent / job["mask"]["path"]).resolve()
    proxy_video = _probe_video(proxy_path)
    mask_video = _probe_video(mask_path)

    return {
        "command": "verify-inputs",
        "ok": True,
        "selected_shot_id": job["selected_shot_id"],
        "job": {
            "path": str(job_path),
            "bytes": job_path.stat().st_size,
            "sha256": sha256_file(job_path),
        },
        "bundle": {
            "path": str(bundle_path),
            "bytes": bundle_path.stat().st_size,
            "sha256": sha256_file(bundle_path),
        },
        "source": job["source"],
        "prompt_sha256": job["prompt"]["sha256"],
        "proxy_video": proxy_video,
        "mask_video": mask_video,
        "evidence": job["evidence"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose and verify Stage 6 VACE inputs without inference",
        epilog=(
            "Examples: python -m videoactagent.stage6_debug doctor --vace-root "
            "third_party/VACE | python -m videoactagent.stage6_debug "
            "verify-inputs --job runs/stage6_vace_inputs/s01/vace_job.json "
            "--bundle runs/stage2_control_bridge/control_bundle.json | python -m "
            "videoactagent.stage6_debug print-probe-command"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="inspect required tools")
    doctor_parser.add_argument("--vace-root", type=Path, required=True)

    verify_parser = subparsers.add_parser(
        "verify-inputs", help="verify a prepared job against its source bundle"
    )
    verify_parser.add_argument("--job", type=Path, required=True)
    verify_parser.add_argument("--bundle", type=Path, required=True)

    subparsers.add_parser(
        "print-probe-command", help="print the server probe command without running it"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "print-probe-command":
        print(build_probe_command())
        return 0
    try:
        if args.command == "doctor":
            report = doctor(args.vace_root)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        report = verify_inputs(args.job, args.bundle)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "command": args.command,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
