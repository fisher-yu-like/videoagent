from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.shotscript import ShotScript
from videoactagent.prompts import compile_shot_prompts


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


def file_record(path: Path, output_dir: Path) -> dict:
    contents = path.read_bytes()
    return {
        "path": path.relative_to(output_dir).as_posix(),
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def write_bundle(
    script: ShotScript,
    records: list[dict],
    output_dir: Path,
) -> Path:
    output_dir = output_dir.resolve()
    shots = []
    for shot, record in zip(script.shots, records, strict=True):
        prompts = compile_shot_prompts(shot)
        shots.append(
            {
                "shot_id": shot.shot_id,
                "duration": shot.duration,
                "frame_range": record["frame_range"],
                "prompts": {
                    "plain": prompts.plain,
                    "cinematic": prompts.cinematic,
                    "timed": prompts.timed,
                },
                "controls": {
                    "first_frame": file_record(Path(record["first_frame"]), output_dir),
                    "last_frame": file_record(Path(record["last_frame"]), output_dir),
                    "proxy_video": file_record(Path(record["proxy_video"]), output_dir),
                },
            }
        )
    bundle = {
        "schema_version": "0.1",
        "source_scene_id": script.scene_id,
        "fps": script.fps,
        "submission_ready": False,
        "submission_blocker": "public asset URLs are not configured",
        "shots": shots,
    }
    bundle_path = output_dir / "control_bundle.json"
    temporary = output_dir / ".control_bundle.json.tmp"
    temporary.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(bundle_path)
    validate_bundle(bundle_path, output_dir)
    return bundle_path


def validate_bundle(bundle_path: Path, output_dir: Path) -> None:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if bundle.get("submission_ready") is not False:
        raise RuntimeError("local-only bundle must not be submission ready")
    for shot in bundle.get("shots", []):
        for control in shot.get("controls", {}).values():
            path = output_dir / control["path"]
            actual = file_record(path, output_dir)
            if actual != control:
                raise RuntimeError(f"control hash validation failed: {path}")


def main() -> None:
    args = parse_args()
    script = ShotScript.from_path(args.shotscript)
    records = export_controls(
        args.blender,
        args.shotscript,
        args.blend,
        args.output_dir,
    )
    bundle_path = write_bundle(script, records, args.output_dir.resolve())
    print(
        "CONTROL_BRIDGE_OK",
        json.dumps(
            {"bundle": str(bundle_path), "shots": len(records)},
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    main()
