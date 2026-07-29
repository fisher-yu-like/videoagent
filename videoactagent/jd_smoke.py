from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.backends.jd import (
    build_kling_t2v,
    download_once,
    query_once,
    submit_once,
)
from videoactagent.run_record import RunDirectory


def require_key() -> str:
    api_key = os.environ.get("JD_KLING_KEY", "").strip()
    if not api_key:
        raise RuntimeError("JD_KLING_KEY is required")
    return api_key


def find_shot(bundle: dict, shot_id: str) -> dict:
    for shot in bundle["shots"]:
        if shot["shot_id"] == shot_id:
            return shot
    raise ValueError(f"shot not found in bundle: {shot_id}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit-kling")
    submit.add_argument("--bundle", type=Path, required=True)
    submit.add_argument("--shot", required=True)
    submit.add_argument("--prompt", choices=("plain", "cinematic"), required=True)
    submit.add_argument("--run-root", type=Path, required=True)

    query = subparsers.add_parser("query")
    query.add_argument("--run-dir", type=Path, required=True)

    download = subparsers.add_parser("download")
    download.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "submit-kling":
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        shot = find_shot(bundle, args.shot)
        prompt = shot["prompts"][args.prompt]
        payload = build_kling_t2v(prompt, int(round(shot["duration"])))
        run = RunDirectory.create(args.run_root, "kling")
        run.write_json(
            "metadata.json",
            {
                "backend": "kling",
                "shot_id": args.shot,
                "prompt_condition": args.prompt,
                "submit_retry_limit": 0,
            },
        )
        print(f"RUN_DIR={run.path.resolve()}", flush=True)
        submit_once(
            payload,
            require_key(),
            os.environ.get(
                "JD_KLING_BASE", "https://modelservice.jdcloud.com"
            ),
            run,
        )
        return

    run = RunDirectory(args.run_dir.resolve())
    if args.command == "query":
        query_once(require_key(), run)
        return
    if args.command == "download":
        download_once(run)
        return
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
