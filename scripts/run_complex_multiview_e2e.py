"""Run bounded 3/5/8-view complex-story Seedance jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from videoactagent.seedance_auto_chain import run_uploaded_multiview_job
from videoactagent.seedance_upload import (
    NormalizedMedia,
    load_upload_config,
    normalize_proxy,
    upload_proxy,
)
from videoactagent.syn4d_story_prompts import complex_station_story_prompt


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    parser.add_argument("--max-polls", type=int, default=20)
    args = parser.parse_args()

    proxy_root = args.proxy_root.resolve()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("JD_KLING_KEY", "").strip()
    if not api_key:
        raise SystemExit("JD_KLING_KEY is not configured")
    base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")
    config = _temp_config()

    results = []
    for count in (3, 5, 8):
        job_id = f"M{count}_complex_story"
        prompt = complex_station_story_prompt(count)
        prompt_path = output_root / "prompts" / f"{job_id}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        case_normalized = output_root / "normalized" / job_id
        case_normalized.mkdir(parents=True, exist_ok=True)
        assets = []
        try:
            for index in range(1, count + 1):
                source = proxy_root / f"camera_{index}.mp4"
                media = normalize_proxy(source, case_normalized / f"camera_{index}_seedance.mp4")
                assets.append(upload_proxy(media, run_id=f"{job_id}_camera_{index}", config=config))
        except Exception as exc:
            result = {
                "job_id": job_id,
                "status": "blocked_preflight",
                "stage": "normalize_or_upload",
                "error": str(exc),
                "api_calls": {"submit": 0, "query": 0, "download": 0},
            }
            results.append(result)
            continue
        result = run_uploaded_multiview_job(
            job_id=job_id,
            prompt=prompt,
            assets=assets,
            output_root=output_root,
            api_key=api_key,
            base_url=base_url,
            model=args.model,
            poll_interval_seconds=args.poll_interval,
            max_polls=args.max_polls,
        )
        result["prompt_path"] = str(prompt_path)
        result["prompt_sha256"] = _sha256(prompt_path)
        result["reference_count"] = count
        results.append(result)
        (output_root / "progress.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    summary = {
        "schema_version": "complex-story-multiview-e2e-1.0",
        "model": args.model,
        "reference_counts": [3, 5, 8],
        "results": results,
        "total_api_calls": {
            key: sum(int(item.get("api_calls", {}).get(key, 0)) for item in results)
            for key in ("submit", "query", "download")
        },
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item.get("status") == "succeeded" for item in results if item.get("api_calls", {}).get("submit")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
