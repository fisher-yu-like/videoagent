from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


def positive_int(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None or int(value) <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def resolution_value(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if match is None:
        raise argparse.ArgumentTypeError("must be WIDTHxHEIGHT with positive integers")
    return int(match.group(1)), int(match.group(2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path)
    parser.add_argument("--camera-trajectory", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--render-style",
        choices=("diagnostic", "clay"),
        default="diagnostic",
    )
    parser.add_argument("--fps", type=positive_int, default=3)
    parser.add_argument("--resolution", type=resolution_value, default=(960, 540))
    parser.add_argument("--timeout", type=positive_int, default=180)
    args = parser.parse_args(argv)
    if args.camera_trajectory is not None and args.trajectory is None:
        parser.error("--camera-trajectory requires --trajectory")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    compiler = Path(__file__).with_name("blender_proxy.py").resolve()
    command = [
        str(args.blender.resolve()),
        "--background",
        "--factory-startup",
        "-F",
        "FFMPEG",
        "--python",
        str(compiler),
        "--",
        "--shotscript",
        str(args.shotscript.resolve()),
        "--output-dir",
        str(args.output_dir.resolve()),
        "--render-style",
        args.render_style,
        "--fps",
        str(args.fps),
        "--resolution",
        f"{args.resolution[0]}x{args.resolution[1]}",
    ]
    if args.trajectory is not None:
        command.extend(["--trajectory", str(args.trajectory.resolve())])
    if args.camera_trajectory is not None:
        command.extend([
            "--camera-trajectory", str(args.camera_trajectory.resolve())
        ])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _output_text(exc.stdout)
        stderr = _output_text(exc.stderr)
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
        print(
            f"BLENDER_RUNNER_TIMEOUT timeout_seconds={args.timeout}",
            file=sys.stderr,
        )
        return 124
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    evidence = completed.stdout + completed.stderr
    if completed.returncode != 0 or "Traceback" in completed.stderr:
        return completed.returncode or 1
    marker = "TRAJECTORY_PROXY_OK" if args.trajectory is not None else "BLENDER_PROXY_OK"
    if marker not in evidence:
        print(f"Blender exited without {marker}", file=sys.stderr)
        return 1
    return 0


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
