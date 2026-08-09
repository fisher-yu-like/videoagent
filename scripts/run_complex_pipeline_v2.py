"""Run one real Director -> Code Agent -> Sandbox -> ProxyVerifier chain."""

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
from pipeline_v2.director import request_director_plan
from pipeline_v2.proxy_verifier import verify_proxy
from pipeline_v2.render_contract import build_physical_render_contract
from pipeline_v2.state import WorldState


STORY_PROMPT = (
    "A continuous five-second railway-platform reunion in one shared world. "
    "A traveler pulls a small rolling suitcase along the platform toward a friend who is waiting near a station sign. "
    "The traveler approaches, slows and turns toward the friend; the friend turns toward the traveler and raises one hand. "
    "The suitcase stays coupled to the traveler, wheels grounded, with no teleportation or unexplained jumps. "
    "The platform edge, back wall, columns and sign remain fixed. Use one timeline and eight coordinated camera responsibilities: "
    "master front tracking, lateral follow, reverse continuity, wide establishing, low lateral follow, high three-quarter, "
    "front-left high three-quarter, and elevated front-right transition. No cuts, no new identities, no background replacement."
)


def _probe(path: Path) -> dict[str, object]:
    ffprobe = r"D:\ACLOS\Cross\recorder-release\ffprobe.exe"
    output = subprocess.check_output(
        [ffprobe, "-v", "error", "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration", "-of", "json", str(path)],
        text=True,
    )
    document = json.loads(output)
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("openai", "deepseek"), default="openai")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--blender", default=r"D:\blender\blender.exe")
    args = parser.parse_args()

    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "story_prompt.txt").write_text(STORY_PROMPT + "\n", encoding="utf-8")

    director_dir = root / "director"
    world = request_director_plan(
        story_prompt=STORY_PROMPT,
        duration_seconds=5.0,
        fps=24,
        camera_count=8,
        provider=args.provider,
        model=args.model,
        output_dir=director_dir,
    )
    world_path = director_dir / "world_state.json"
    normalized_plan = director_dir / "director_plan_normalized.json"
    physical_contract = build_physical_render_contract(
        world,
        json.loads(normalized_plan.read_text(encoding="utf-8")),
    )
    contract_path = root / "physical_render_contract.json"
    contract_path.write_text(json.dumps(physical_contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    code_dir = root / "code_agent"
    request_code_agent(
        story_prompt=STORY_PROMPT,
        world=world.to_dict(),
        physical_contract=physical_contract,
        provider=args.provider,
        model=args.model,
        output_dir=code_dir,
    )

    sandbox_dir = root / "sandbox"
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
        director_plan_path=normalized_plan,
        code_agent_evidence_path=code_dir / "evidence.json",
        generated_script_path=code_dir / "generated_blender.py",
        probe_video=_probe,
    )
    (root / "proxy_verifier_report.json").write_text(
        json.dumps(verifier, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "story_prompt": STORY_PROMPT,
        "world_state_hash": world.world_state_hash(),
        "camera_count": world.camera_count,
        "render_manifest": manifest,
        "proxy_verifier_verdict": verifier.get("verdict"),
        "required_human_review": verifier.get("required_human_review"),
        "api_calls": {"director": 1, "code_agent": 1},
    }
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if verifier.get("verdict") in {"pass", "pending_review"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
