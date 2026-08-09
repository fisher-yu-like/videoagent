"""Submit one controlled real Seedance job from the verified complex proxy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from videoactagent.seedance_auto_chain import run_multiview_job
from videoactagent.seedance_upload import load_upload_config


PROMPT = (
    "Appearance-only live-action reconstruction from three synchronized reference videos of one continuous "
    "five-second railway-platform reunion. [Video1] is the master front tracking view, [Video2] is the lateral "
    "follow view, and [Video3] is the high three-quarter view. Use the references only as guides for blocking, "
    "character/object trajectories, contact timing, occlusion order, shared station layout, and smooth camera motion. "
    "Do not copy the white-clay or low-poly geometry, primitive silhouettes, gray albedo, faceted shading, or Blender "
    "render style. Reconstruct coherent live-action adult humans: recognizable natural anatomy, realistic faces, hair, "
    "skin and cloth; a real metal rolling suitcase with visible wheels and handle; natural railway-platform materials, "
    "lighting, shadows, and depth. Preserve the exact story order: the traveler pulls the suitcase toward the waiting "
    "friend, slows and turns toward the friend, the friend turns and raises one hand, and the suitcase remains coupled "
    "and grounded. Preserve one shared world, identity, spatial relationships, camera responsibilities, smooth motion, "
    "full 5-second duration, and no cuts. Do not add people, props, text, labels, guide lines, room changes, black frames, "
    "camera jumps, time skips, or background replacement."
)


def _temp_config():
    config = load_upload_config()
    if config.has_tos_credentials or config.temp_upload_enabled:
        return config
    return config.__class__(
        access_key=config.access_key,
        secret_key=config.secret_key,
        bucket=config.bucket,
        endpoint=config.endpoint,
        region=config.region,
        expires_seconds=config.expires_seconds,
        temp_upload_enabled=True,
        temp_upload_endpoint=config.temp_upload_endpoint,
        temp_upload_provider=config.temp_upload_provider,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="Doubao-Seedance-2.0")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=25)
    args = parser.parse_args()
    proxy_root = args.proxy_root.resolve()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise SystemExit("output root already exists: " + str(output_root))
    output_root.mkdir(parents=True)
    (output_root / "appearance_prompt.txt").write_text(PROMPT + "\n", encoding="utf-8")
    proxies = [
        proxy_root / "master_front_tracking.mp4",
        proxy_root / "lateral_follow.mp4",
        proxy_root / "high_three_quarter.mp4",
    ]
    for path in proxies:
        if not path.is_file():
            raise SystemExit("missing proxy: " + str(path))
    api_key = os.environ.get("JD_KLING_KEY", "").strip()
    if not api_key:
        raise SystemExit("JD_KLING_KEY is not configured")
    result = run_multiview_job(
        job_id="Seedance_complex_appearance_3view",
        prompt=PROMPT,
        proxy_paths=proxies,
        output_root=output_root,
        api_key=api_key,
        base_url=os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com"),
        model=args.model,
        upload_config=_temp_config(),
        poll_interval_seconds=args.poll_interval,
        max_polls=args.max_polls,
    )
    summary = {
        "prompt_path": str(output_root / "appearance_prompt.txt"),
        "reference_proxies": [str(path) for path in proxies],
        "model": args.model,
        "result": result,
    }
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
