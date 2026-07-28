from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    evidence = completed.stdout + completed.stderr
    if completed.returncode != 0 or "Traceback" in completed.stderr:
        return completed.returncode or 1
    if "BLENDER_PROXY_OK" not in evidence:
        print("Blender exited without BLENDER_PROXY_OK", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
