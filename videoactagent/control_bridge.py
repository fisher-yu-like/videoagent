from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.shotscript import ShotScript


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def run_blender(command: list[str], label: str) -> None:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    evidence = completed.stdout + completed.stderr
    if completed.returncode != 0 or "Traceback" in evidence:
        raise RuntimeError(
            f"{label} failed with exit {completed.returncode}\n{evidence}"
        )


def export_frame(
    blender: Path,
    blend: Path,
    frame: int,
    target: Path,
) -> None:
    stem = target.with_name(f"_{target.stem}")
    run_blender(
        [
            str(blender),
            "--background",
            str(blend),
            "-F",
            "PNG",
            "-o",
            str(stem),
            "-f",
            str(frame),
        ],
        f"Blender frame export {frame}",
    )
    rendered = Path(f"{stem}{frame:04d}.png")
    if not rendered.is_file() or rendered.stat().st_size == 0:
        raise RuntimeError(f"Blender did not create frame: {rendered}")
    rendered.replace(target)


def export_video(
    blender: Path,
    blend: Path,
    frame_start: int,
    frame_end: int,
    target: Path,
) -> None:
    stem = target.with_name("_proxy")
    run_blender(
        [
            str(blender),
            "--background",
            str(blend),
            "-s",
            str(frame_start),
            "-e",
            str(frame_end),
            "-F",
            "FFMPEG",
            "-o",
            str(stem),
            "-a",
        ],
        f"Blender video export {frame_start}-{frame_end}",
    )
    rendered = Path(f"{stem}{frame_start:04d}-{frame_end:04d}.mp4")
    if not rendered.is_file() or rendered.stat().st_size == 0:
        raise RuntimeError(f"Blender did not create video: {rendered}")
    rendered.replace(target)


def export_controls(
    blender: Path,
    shotscript_path: Path,
    blend: Path,
    output_dir: Path,
) -> list[dict]:
    blender = blender.resolve()
    shotscript_path = shotscript_path.resolve()
    blend = blend.resolve()
    output_dir = output_dir.resolve()
    for path, label in (
        (blender, "Blender executable"),
        (shotscript_path, "ShotScript"),
        (blend, "Blender scene"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")

    script = ShotScript.from_path(shotscript_path)
    records = []
    frame_cursor = 1
    for shot in script.shots:
        shot_frames = int(round(shot.duration * script.fps))
        frame_start = frame_cursor
        frame_end = frame_start + shot_frames - 1
        shot_dir = output_dir / "shots" / shot.shot_id
        shot_dir.mkdir(parents=True, exist_ok=True)
        first = shot_dir / "first.png"
        last = shot_dir / "last.png"
        video = shot_dir / "proxy.mp4"

        export_frame(blender, blend, frame_start, first)
        export_frame(blender, blend, frame_end, last)
        export_video(blender, blend, frame_start, frame_end, video)
        records.append(
            {
                "shot_id": shot.shot_id,
                "frame_range": [frame_start, frame_end],
                "first_frame": str(first),
                "last_frame": str(last),
                "proxy_video": str(video),
            }
        )
        frame_cursor = frame_end + 1
    return records


def main() -> None:
    args = parse_args()
    records = export_controls(
        args.blender,
        args.shotscript,
        args.blend,
        args.output_dir,
    )
    print("CONTROL_BRIDGE_OK", json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    main()
