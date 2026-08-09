"""Run the bounded shared-world Seedance experiment.

Each camera is submitted as its own reference-video task. The script never
packs multiple camera URLs into one request and never retries a submission.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from videoactagent.multiview_prompts import station_multiview_prompts
from videoactagent.seedance_auto_chain import run_uploaded_camera_jobs
from videoactagent.seedance_upload import UploadedAsset, load_upload_config, upload_proxy, NormalizedMedia, normalize_proxy, write_upload_record


def _load_asset(path: Path) -> UploadedAsset:
    value = json.loads(path.read_text(encoding="utf-8"))
    return UploadedAsset(
        source_path=value["source_path"],
        normalized_path=value["normalized_path"],
        source_sha256=value["source_sha256"],
        normalized_sha256=value["normalized_sha256"],
        metadata=value["metadata"],
        url=value["url"],
        provider=value["provider"],
        object_key=value["object_key"],
        expires_at=value.get("expires_at"),
    )


def _temporary_upload_config():
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


def _prepare_proxy_uploads(root: Path, proxy_root: Path) -> None:
    """Prepare the same three-camera upload records used by the old runner."""
    config = _temporary_upload_config()
    normalized_root = root / "normalized"
    uploads_root = root / "uploads"
    normalized_root.mkdir(parents=True, exist_ok=True)
    uploads_root.mkdir(parents=True, exist_ok=True)
    for index in range(1, 4):
        source = proxy_root / f"camera_{index}.mp4"
        if not source.is_file():
            raise FileNotFoundError(f"legacy Seedance Proxy is missing: {source}")
        normalized = normalize_proxy(source, normalized_root / f"camera_{index}_seedance.mp4")
        uploaded = upload_proxy(normalized, run_id=f"legacy_proxy_camera_{index}", config=config)
        write_upload_record(uploaded, uploads_root / f"camera_{index}.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--proxy-root", type=Path, help="directory containing camera_1.mp4, camera_2.mp4, camera_3.mp4; uses the legacy upload path")
    parser.add_argument("--appearance-prompt", type=Path, required=True)
    parser.add_argument("--model", default="Doubao-Seedance-2.5")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument(
        "--refresh-temp-uploads",
        action="store_true",
        help="re-upload each normalized Proxy immediately before each submission",
    )
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=30)
    args = parser.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.proxy_root:
        _prepare_proxy_uploads(root, args.proxy_root.resolve())
    appearance = json.loads(args.appearance_prompt.read_text(encoding="utf-8"))
    all_prompts = station_multiview_prompts(appearance["prompt"])
    if args.start_index < 1 or args.start_index > len(all_prompts):
        raise SystemExit(f"--start-index must be between 1 and {len(all_prompts)}")
    prompts = all_prompts[args.start_index - 1 : args.limit]
    assets = [_load_asset(root / "uploads" / f"camera_{i}.json") for i in range(1, 4)]
    api_key = os.environ.get("JD_KLING_KEY", "").strip()
    if not api_key:
        raise SystemExit("JD_KLING_KEY is not configured")
    base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")

    progress_path = root / "e2e_progress.json"
    results = []
    if progress_path.is_file():
        try:
            existing = json.loads(progress_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                results.extend(existing)
        except (OSError, json.JSONDecodeError):
            pass
    existing_ids = {item.get("job_id") for item in results if isinstance(item, dict)}
    for index, prompt in enumerate(prompts, start=args.start_index):
        job_id = f"G{index}_multiview"
        if job_id in existing_ids:
            continue
        job_assets = assets
        if args.refresh_temp_uploads and all(asset.provider == "tmpfiles" for asset in assets):
            refreshed = []
            config = load_upload_config()
            if not config.temp_upload_enabled:
                config = config.__class__(
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
            for camera_index, asset in enumerate(assets, start=1):
                media = NormalizedMedia(
                    source_path=asset.source_path,
                    normalized_path=asset.normalized_path,
                    source_sha256=asset.source_sha256,
                    normalized_sha256=asset.normalized_sha256,
                    metadata=asset.metadata,
                )
                refreshed_asset = upload_proxy(media, run_id=f"{job_id}_camera_{camera_index}", config=config)
                refreshed.append(refreshed_asset)
            job_assets = refreshed
        result = run_uploaded_camera_jobs(
            job_id=job_id,
            prompt=prompt,
            assets=job_assets,
            camera_ids=[f"camera_{camera_index}" for camera_index in range(1, len(job_assets) + 1)],
            output_root=root,
            api_key=api_key,
            base_url=base_url,
            model=args.model,
            poll_interval_seconds=args.poll_interval,
            max_polls=args.max_polls,
        )
        results.append(result)
        progress_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if result.get("status") in {"submit_failed", "task_failed", "download_failed", "unknown", "partial_or_failed"}:
            break

    summary = {
        "model": args.model,
        "prompt_count": len(prompts),
        "submitted": sum(int(item.get("api_calls", {}).get("submit", 0)) for item in results),
        "queried": sum(int(item.get("api_calls", {}).get("query", 0)) for item in results),
        "downloaded": sum(int(item.get("api_calls", {}).get("download", 0)) for item in results),
        "results": results,
    }
    (root / "e2e_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(item.get("status") == "succeeded" for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
