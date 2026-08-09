"""Create a new shared-world proxy revision with an unobstructed reverse camera."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_v2.blender_sandbox import run_blender_sandbox
from pipeline_v2.proxy_verifier import verify_proxy


def reverse_camera_positions() -> list[list[float]]:
    # The station wall occupies world z=0..4.4 at y≈3.5.  Raising the reverse
    # camera above the wall preserves the opposite-side view without making
    # the shared scene wall transparent or deleting it for one camera.
    return [[3.8, 4.8, 7.2], [2.2, 4.4, 7.2]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict[str, object]:
    ffprobe = r"D:\ACLOS\Cross\recorder-release\ffprobe.exe"
    document = json.loads(
        subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration", "-of", "json", str(path)],
            text=True,
        )
    )
    stream = document["streams"][0]
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "r_frame_rate": stream["avg_frame_rate"],
        "nb_frames": int(stream["nb_frames"]),
        "codec": stream["codec_name"],
        "duration": float(document["format"]["duration"]),
    }


def _update_reverse_camera(document: dict[str, object]) -> dict[str, object]:
    revised = copy.deepcopy(document)
    positions = reverse_camera_positions()
    cameras = revised["camera_trajectory_plan"]["cameras"]
    reverse = next(item for item in cameras if item["id"] == "reverse_continuity")
    points = reverse["points"]
    for point, position in zip((points[0], points[-1]), positions):
        point["position"] = position
    # Keep all interpolated camera points consistent with the revised endpoints.
    for point in points[1:-1]:
        ratio = (point["frame"] - points[0]["frame"]) / (points[-1]["frame"] - points[0]["frame"])
        point["position"] = [positions[0][axis] * (1.0 - ratio) + positions[1][axis] * ratio for axis in range(3)]
    return revised


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--blender", default=r"D:\blender\blender.exe")
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise SystemExit(f"output root already exists: {output_root}")
    source_world = source_root / "director_reused" / "world_state.json"
    source_plan = source_root / "director_reused" / "director_plan_normalized.json"
    source_script = source_root / "code_agent_revision_001" / "generated_blender.py"
    source_evidence = source_root / "code_agent_revision_001" / "evidence.json"
    for path in (source_world, source_plan, source_script, source_evidence):
        if not path.is_file():
            raise SystemExit(f"missing source artifact: {path}")
    output_root.mkdir(parents=True)
    director_dir = output_root / "director_revised"
    code_dir = output_root / "code_agent_revision_002"
    director_dir.mkdir()
    code_dir.mkdir()
    world = _update_reverse_camera(json.loads(source_world.read_text(encoding="utf-8")))
    plan = _update_reverse_camera(json.loads(source_plan.read_text(encoding="utf-8")))
    _write_json(director_dir / "world_state.json", world)
    _write_json(director_dir / "director_plan_normalized.json", plan)
    shutil.copy2(source_script, code_dir / "generated_blender.py")
    evidence = json.loads(source_evidence.read_text(encoding="utf-8"))
    evidence["postprocess_patch"] = {
        "type": "camera_visibility_revision",
        "source_script_sha256": _sha256_file(source_script),
        "world_state_source_sha256": _sha256_file(source_world),
        "world_state_revised_sha256": _sha256_file(director_dir / "world_state.json"),
        "changes": ["raised reverse_continuity camera above fixed back wall", "preserved shared wall and all other cameras"],
    }
    _write_json(code_dir / "evidence.json", evidence)
    shutil.copy2(source_root / "code_agent_revision_001" / "request.json", code_dir / "request.json")
    shutil.copy2(source_root / "code_agent_revision_001" / "response.json", code_dir / "response.json")
    (output_root / "revision_instruction.txt").write_text(
        "Structural camera revision: reverse_continuity was occluded by the fixed station wall. Raise only that camera above the wall; keep the shared world, wall, entities, trajectories, and other cameras unchanged.\n",
        encoding="utf-8",
    )
    sandbox_dir = output_root / "sandbox_revision_002"
    manifest = run_blender_sandbox(
        blender_executable=args.blender,
        generated_script=code_dir / "generated_blender.py",
        world_state_path=director_dir / "world_state.json",
        output_dir=sandbox_dir,
        render_style="clay",
        resolution=(640, 360),
        timeout_seconds=1200,
        expected_world_state_hash=None,
    )
    report = verify_proxy(
        world_state_path=director_dir / "world_state.json",
        render_output_dir=sandbox_dir,
        director_plan_path=director_dir / "director_plan_normalized.json",
        code_agent_evidence_path=code_dir / "evidence.json",
        generated_script_path=code_dir / "generated_blender.py",
        probe_video=_probe,
    )
    _write_json(output_root / "proxy_verifier_report.json", report)
    summary = {
        "revision": "revision_002",
        "source_root": str(source_root),
        "api_calls": {"director": 0, "code_agent": 0, "seedance": 0},
        "proxy_verifier_verdict": report.get("verdict"),
        "required_human_review": report.get("required_human_review"),
        "render_manifest": manifest,
    }
    _write_json(output_root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report.get("verdict") in {"pass", "pending_review"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
