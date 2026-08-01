"""Launch one Blender process for a synchronized multicamera proxy bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


def _positive(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def _resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if match is None:
        raise argparse.ArgumentTypeError("must be WIDTHxHEIGHT")
    return int(match.group(1)), int(match.group(2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--camera-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--render-style", choices=("diagnostic", "clay"), default="diagnostic")
    parser.add_argument("--fps", type=_positive, default=3)
    parser.add_argument("--resolution", type=_resolution, default=(960, 540))
    parser.add_argument("--timeout", type=_positive, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    script = Path(__file__).with_name("multicam_blender_proxy.py").resolve()
    command = [
        str(args.blender.resolve()), "--background", "--factory-startup", "-F", "FFMPEG",
        "--python", str(script), "--",
        "--shotscript", str(args.shotscript.resolve()),
        "--trajectory", str(args.trajectory.resolve()),
        "--camera-bundle", str(args.camera_bundle.resolve()),
        "--output-dir", str(args.output_dir.resolve()),
        "--render-style", args.render_style,
        "--fps", str(args.fps),
        "--resolution", f"{args.resolution[0]}x{args.resolution[1]}",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        print((exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        print("MULTICAM_BLENDER_TIMEOUT", file=sys.stderr)
        return 124
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    evidence = completed.stdout + completed.stderr
    if completed.returncode != 0 or "MULTICAM_PROXY_OK" not in evidence:
        return completed.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
