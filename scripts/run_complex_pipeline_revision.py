"""Re-run the failed complex story at the CodeAgent/Sandbox boundary.

The Director response is reused as an immutable input; this revision fixes only
the target-string normalization and the CodeAgent instruction, then renders a
new shared-world proxy without overwriting the failed revision.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_v2.blender_sandbox import run_blender_sandbox
from pipeline_v2.code_agent import request_code_agent
from pipeline_v2.director import compile_director_plan, normalize_director_plan
from pipeline_v2.proxy_verifier import verify_proxy
from pipeline_v2.render_contract import build_physical_render_contract


REVISION_INSTRUCTION = (
    "This is a structural revision after a real ProxyVerifier failure. Preserve the supplied WorldState exactly. "
    "Do not invent, rescale, or substitute entity or camera positions. For every camera, copy every authored "
    "position and semantic rotation row from CameraTrajectoryPlan into camera_log.authored; set target_id exactly "
    "to the camera target object_id (master/lateral target traveler, reverse/front-left target friend, low target "
    "suitcase, wide/elevated target station_sign where supplied). Only a target with an explicit point has null "
    "target_id. Resolve applied orientation from the same target with to_track_quat, but never change authored rows. "
    "Keep all eight cameras in one shared world and render all 120 frames at 24fps. "
    "Hard prohibition: do not use bpy Action.fcurves, animation_data.action.fcurves, or any fcurves API; "
    "keyframe_insert is allowed, but leave its default interpolation unchanged."
)


def _probe(path: Path) -> dict[str, object]:
    ffprobe = r"D:\ACLOS\Cross\recorder-release\ffprobe.exe"
    document = json.loads(
        subprocess.check_output(
            [
                ffprobe,
                "-v", "error",
                "-show_entries",
                "stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration",
                "-of", "json", str(path),
            ],
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--director-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("openai", "deepseek"), default="openai")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--blender", default=r"D:\blender\blender.exe")
    args = parser.parse_args()

    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = args.director_json.resolve()
    raw_director = json.loads(source.read_text(encoding="utf-8"))
    normalized = normalize_director_plan(
        raw_director,
        fallback_scene_id="station_001",
        fallback_environment_preset="station",
        fallback_fps=24,
    )
    world = compile_director_plan(normalized)
    director_dir = root / "director_reused"
    director_dir.mkdir()
    (director_dir / "source_director_plan.json").write_text(
        json.dumps(raw_director, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (director_dir / "director_plan_normalized.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    world_path = director_dir / "world_state.json"
    world_path.write_text(json.dumps(world.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    story = (
        "A continuous five-second railway-platform reunion in one shared world: a traveler pulls a small rolling "
        "suitcase toward a waiting friend near a station sign; the traveler slows and turns, the friend turns and "
        "raises one hand. One timeline, no cuts, fixed platform edge/back wall/columns/sign, eight coordinated "
        "camera responsibilities, and no new identities or background replacement.\n\n" + REVISION_INSTRUCTION
    )
    (root / "revision_instruction.txt").write_text(REVISION_INSTRUCTION + "\n", encoding="utf-8")
    physical_contract = build_physical_render_contract(world, normalized)
    (root / "physical_render_contract.json").write_text(
        json.dumps(physical_contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    code_dir = root / "code_agent_revision_001"
    request_code_agent(
        story_prompt=story,
        world=world.to_dict(),
        physical_contract=physical_contract,
        provider=args.provider,
        model=args.model,
        output_dir=code_dir,
    )
    sandbox_dir = root / "sandbox_revision_001"
    manifest = run_blender_sandbox(
        blender_executable=args.blender,
        generated_script=code_dir / "generated_blender.py",
        world_state_path=world_path,
        output_dir=sandbox_dir,
        render_style="clay",
        resolution=(640, 360),
        timeout_seconds=1200,
        expected_world_state_hash=world.world_state_hash(),
    )
    verifier = verify_proxy(
        world_state_path=world_path,
        render_output_dir=sandbox_dir,
        director_plan_path=director_dir / "director_plan_normalized.json",
        code_agent_evidence_path=code_dir / "evidence.json",
        generated_script_path=code_dir / "generated_blender.py",
        probe_video=_probe,
    )
    (root / "proxy_verifier_report.json").write_text(json.dumps(verifier, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "revision": "revision_001",
        "director_source": str(source),
        "director_api_calls": 0,
        "code_agent_api_calls": 1,
        "world_state_hash": world.world_state_hash(),
        "camera_count": world.camera_count,
        "render_manifest": manifest,
        "proxy_verifier_verdict": verifier.get("verdict"),
        "required_human_review": verifier.get("required_human_review"),
        "revision_instruction_path": str(root / "revision_instruction.txt"),
    }
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if verifier.get("verdict") in {"pass", "pending_review"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
