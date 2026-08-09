"""Auditable Seedance reference-video orchestration.

The module composes the existing JD gateway lifecycle with local media
normalization/upload. It submits exactly once per job and only polls thereafter.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Callable, Iterable, Mapping, Sequence

from .backends.jd import (
    build_seedance_reference_video,
    build_seedance_multiview_reference_videos,
    extract_status,
    download_once,
    extract_video_urls,
    query_once,
    submit_once,
)
from .run_record import RunDirectory
from .seedance_upload import (
    NormalizedMedia,
    UploadedAsset,
    UploadConfig,
    normalize_proxy,
    upload_proxy,
    write_upload_record,
)


SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "done"})
FAILURE_STATUSES = frozenset({"failed", "failure", "error", "cancelled", "canceled", "expired"})


def _canonical_sha(value: object) -> str:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_multiview_job(
    *,
    job_id: str,
    prompt: str,
    assets: Sequence[UploadedAsset],
    model: str,
) -> dict[str, object]:
    if not 3 <= len(assets) <= 8:
        raise ValueError("multiview job requires between three and eight uploaded assets")
    request = build_seedance_multiview_reference_videos(
        prompt,
        [asset.url for asset in assets],
        model=model,
        duration=5,
    )
    uploads = [asset.to_dict() for asset in assets]
    return {
        "job_id": job_id,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "prompt": prompt,
        "uploads": uploads,
        "request": request,
        "request_sha256": _canonical_sha(request),
        "api_calls": {"submit": 0, "query": 0, "download": 0},
    }


def build_single_camera_job(
    *,
    job_id: str,
    camera_id: str,
    prompt: str,
    asset: UploadedAsset,
    model: str,
) -> dict[str, object]:
    """Build one Seedance request for exactly one camera reference.

    The current gateway is treated conservatively: a camera is an independent
    task, not one multi-video request. Cross-camera identity is audited by the
    shared Blender World and by the result manifest, not assumed from an
    unsupported request shape.
    """
    if not isinstance(camera_id, str) or not camera_id.strip():
        raise ValueError("camera_id must be nonempty")
    request = build_seedance_reference_video(prompt, asset.url, model=model, duration=5)
    return {
        "job_id": job_id,
        "camera_id": camera_id,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "prompt": prompt,
        "upload": asset.to_dict(),
        "request": request,
        "request_sha256": _canonical_sha(request),
        "api_calls": {"submit": 0, "query": 0, "download": 0},
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_multiview_job(
    *,
    job_id: str,
    prompt: str,
    proxy_paths: Sequence[Path | str],
    output_root: Path | str,
    api_key: str,
    base_url: str,
    model: str,
    upload_config: UploadConfig | None = None,
    poll_interval_seconds: float = 10.0,
    max_polls: int = 30,
    normalize_fn: Callable[..., NormalizedMedia] = normalize_proxy,
    upload_fn: Callable[..., UploadedAsset] = upload_proxy,
) -> dict[str, object]:
    if not 3 <= len(proxy_paths) <= 8:
        raise ValueError("run_multiview_job requires between three and eight Proxy paths")
    job_dir = Path(output_root).resolve() / job_id
    if job_dir.exists():
        raise FileExistsError(f"job directory already exists: {job_dir}")
    job_dir.mkdir(parents=True)
    normalized_dir = job_dir / "normalized"
    uploads_dir = job_dir / "uploads"
    normalized_dir.mkdir()
    uploads_dir.mkdir()
    assets: list[UploadedAsset] = []
    try:
        for index, source in enumerate(proxy_paths, start=1):
            source_path = Path(source).resolve(strict=True)
            media = normalize_fn(
                source_path,
                normalized_dir / f"camera_{index}_seedance.mp4",
            )
            asset = upload_fn(media, run_id=job_id, config=upload_config)
            assets.append(asset)
            write_upload_record(asset, uploads_dir / f"camera_{index}.json")
        job = build_multiview_job(job_id=job_id, prompt=prompt, assets=assets, model=model)
        _write_json(job_dir / "prepared_job.json", job)
    except Exception as exc:
        failure = {"status": "blocked_preflight", "stage": "normalize_or_upload", "error": str(exc)}
        _write_json(job_dir / "failure.json", failure)
        return {**failure, "job_id": job_id, "api_calls": {"submit": 0, "query": 0, "download": 0}}

    run = RunDirectory(job_dir)
    try:
        task_id = submit_once(job["request"], api_key, base_url, run)
    except Exception as exc:
        failure = {"status": "submit_failed", "stage": "submit", "error": str(exc), "api_calls": {"submit": 1, "query": 0, "download": 0}}
        _write_json(job_dir / "failure.json", failure)
        return {**failure, "job_id": job_id}

    calls = {"submit": 1, "query": 0, "download": 0}
    final_status = "unknown"
    final_response: Mapping[str, object] | None = None
    for poll_index in range(max_polls):
        calls["query"] += 1
        response = query_once(api_key, run)
        final_response = response
        raw_status = extract_status(response)
        status = str(raw_status or "unknown").lower()
        if status in SUCCESS_STATUSES:
            final_status = "succeeded"
            break
        if status in FAILURE_STATUSES:
            final_status = "task_failed"
            break
        if poll_index + 1 < max_polls:
            time.sleep(poll_interval_seconds)
    else:
        final_status = "unknown"

    if final_status == "succeeded":
        try:
            calls["download"] += 1
            result_path = download_once(run)
            result = {"status": final_status, "stage": "download", "result_path": str(result_path), "task_id": task_id, "api_calls": calls}
            _write_json(job_dir / "result_summary.json", result)
            return {**result, "job_id": job_id}
        except Exception as exc:
            failure = {"status": "download_failed", "stage": "download", "error": str(exc), "task_id": task_id, "api_calls": calls}
            _write_json(job_dir / "failure.json", failure)
            return {**failure, "job_id": job_id}

    failure = {
        "status": final_status,
        "stage": "query",
        "task_id": task_id,
        "last_response": final_response,
        "api_calls": calls,
    }
    _write_json(job_dir / "failure.json", failure)
    return {**failure, "job_id": job_id}


def run_uploaded_multiview_job(
    *,
    job_id: str,
    prompt: str,
    assets: Sequence[UploadedAsset],
    output_root: Path | str,
    api_key: str,
    base_url: str,
    model: str,
    poll_interval_seconds: float = 10.0,
    max_polls: int = 30,
) -> dict[str, object]:
    """Run one generation using already-uploaded shared-world assets."""
    if not 3 <= len(assets) <= 8:
        raise ValueError("run_uploaded_multiview_job requires between three and eight assets")
    job_dir = Path(output_root).resolve() / job_id
    if job_dir.exists():
        raise FileExistsError(f"job directory already exists: {job_dir}")
    job_dir.mkdir(parents=True)
    uploads_dir = job_dir / "uploads"
    uploads_dir.mkdir()
    for index, asset in enumerate(assets, start=1):
        write_upload_record(asset, uploads_dir / f"camera_{index}.json")
    job = build_multiview_job(job_id=job_id, prompt=prompt, assets=assets, model=model)
    _write_json(job_dir / "prepared_job.json", job)
    run = RunDirectory(job_dir)
    calls = {"submit": 0, "query": 0, "download": 0}
    try:
        task_id = submit_once(job["request"], api_key, base_url, run)
        calls["submit"] = 1
    except Exception as exc:
        failure = {"status": "submit_failed", "stage": "submit", "error": str(exc), "api_calls": {"submit": 1, "query": 0, "download": 0}}
        _write_json(job_dir / "failure.json", failure)
        return {**failure, "job_id": job_id}

    final_status = "unknown"
    final_response: Mapping[str, object] | None = None
    for poll_index in range(max_polls):
        calls["query"] += 1
        response = query_once(api_key, run)
        final_response = response
        status = str(extract_status(response) or "unknown").lower()
        if status in SUCCESS_STATUSES:
            final_status = "succeeded"
            break
        if status in FAILURE_STATUSES:
            final_status = "task_failed"
            break
        if poll_index + 1 < max_polls:
            time.sleep(poll_interval_seconds)
    if final_status == "succeeded":
        try:
            calls["download"] = 1
            result_path = download_once(run)
            result = {"status": final_status, "stage": "download", "result_path": str(result_path), "task_id": task_id, "api_calls": calls}
            _write_json(job_dir / "result_summary.json", result)
            return {**result, "job_id": job_id}
        except Exception as exc:
            failure = {"status": "download_failed", "stage": "download", "error": str(exc), "task_id": task_id, "api_calls": calls}
            _write_json(job_dir / "failure.json", failure)
            return {**failure, "job_id": job_id}
    failure = {"status": final_status, "stage": "query", "task_id": task_id, "last_response": final_response, "api_calls": calls}
    _write_json(job_dir / "failure.json", failure)
    return {**failure, "job_id": job_id}


def run_uploaded_single_camera_job(
    *,
    job_id: str,
    camera_id: str,
    prompt: str,
    asset: UploadedAsset,
    output_root: Path | str,
    api_key: str,
    base_url: str,
    model: str,
    poll_interval_seconds: float = 10.0,
    max_polls: int = 30,
) -> dict[str, object]:
    """Submit exactly one reference-video task for one camera asset."""
    job_dir = Path(output_root).resolve() / job_id
    if job_dir.exists():
        raise FileExistsError(f"job directory already exists: {job_dir}")
    job_dir.mkdir(parents=True)
    uploads_dir = job_dir / "uploads"
    uploads_dir.mkdir()
    write_upload_record(asset, uploads_dir / "reference.json")
    job = build_single_camera_job(
        job_id=job_id,
        camera_id=camera_id,
        prompt=prompt,
        asset=asset,
        model=model,
    )
    _write_json(job_dir / "prepared_job.json", job)
    run = RunDirectory(job_dir)
    calls = {"submit": 0, "query": 0, "download": 0}
    try:
        task_id = submit_once(job["request"], api_key, base_url, run)
        calls["submit"] = 1
    except Exception as exc:
        failure = {"status": "submit_failed", "stage": "submit", "error": str(exc), "camera_id": camera_id, "api_calls": {**calls, "submit": 1}}
        _write_json(job_dir / "failure.json", failure)
        return {**failure, "job_id": job_id, "camera_id": camera_id}

    final_status = "unknown"
    final_response: Mapping[str, object] | None = None
    for poll_index in range(max_polls):
        calls["query"] += 1
        response = query_once(api_key, run)
        final_response = response
        status = str(extract_status(response) or "unknown").lower()
        if status in SUCCESS_STATUSES:
            final_status = "succeeded"
            break
        if status in FAILURE_STATUSES:
            final_status = "task_failed"
            break
        if poll_index + 1 < max_polls:
            time.sleep(poll_interval_seconds)

    if final_status == "succeeded":
        try:
            result_path = download_once(run)
            calls["download"] = 1
            result = {"status": final_status, "stage": "download", "result_path": str(result_path), "task_id": task_id, "camera_id": camera_id, "api_calls": calls}
            _write_json(job_dir / "result_summary.json", result)
            return {**result, "job_id": job_id, "camera_id": camera_id}
        except Exception as exc:
            failure = {"status": "download_failed", "stage": "download", "error": str(exc), "task_id": task_id, "camera_id": camera_id, "api_calls": calls}
            _write_json(job_dir / "failure.json", failure)
            return {**failure, "job_id": job_id, "camera_id": camera_id}

    failure = {"status": final_status, "stage": "query", "task_id": task_id, "camera_id": camera_id, "last_response": final_response, "api_calls": calls}
    _write_json(job_dir / "failure.json", failure)
    return {**failure, "job_id": job_id, "camera_id": camera_id}


def run_uploaded_camera_jobs(
    *,
    job_id: str,
    prompt: str,
    assets: Sequence[UploadedAsset],
    camera_ids: Sequence[str],
    output_root: Path | str,
    api_key: str,
    base_url: str,
    model: str,
    poll_interval_seconds: float = 10.0,
    max_polls: int = 30,
) -> dict[str, object]:
    """Run one independent Seedance task per camera, without retries."""
    if len(assets) != len(camera_ids) or not assets:
        raise ValueError("assets and camera_ids must have the same nonzero length")
    results = []
    for camera_id, asset in zip(camera_ids, assets):
        results.append(
            run_uploaded_single_camera_job(
                job_id=f"{job_id}_{camera_id}",
                camera_id=camera_id,
                prompt=prompt,
                asset=asset,
                output_root=output_root,
                api_key=api_key,
                base_url=base_url,
                model=model,
                poll_interval_seconds=poll_interval_seconds,
                max_polls=max_polls,
            )
        )
    calls = {key: sum(int(item.get("api_calls", {}).get(key, 0)) for item in results) for key in ("submit", "query", "download")}
    status = "succeeded" if all(item.get("status") == "succeeded" for item in results) else "partial_or_failed"
    return {"status": status, "camera_results": results, "api_calls": calls}
