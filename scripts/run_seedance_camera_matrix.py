"""Generate one real Seedance reference-video result per shared-world camera.

This intentionally does not use the gateway's multi-reference request as if it
were a multi-output request.  Each camera owns one task and one result.mp4;
the proxy videos still come from one shared Blender world and timeline.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from videoactagent.backends.jd import (
    build_seedance_reference_video,
    build_seedance_reference_videos,
    download_once,
    extract_status,
    query_once,
    submit_once,
)
from videoactagent.seedance_auto_chain import FAILURE_STATUSES, SUCCESS_STATUSES
from videoactagent.run_record import RunDirectory
from videoactagent.seedance_upload import (
    load_upload_config,
    normalize_proxy,
    upload_proxy,
    write_upload_record,
)


CAMERA_SPECS = [
    {"camera_id": "master_front_tracking", "role": "master front tracking"},
    {"camera_id": "lateral_follow", "role": "lateral follow"},
    {"camera_id": "reverse_continuity", "role": "reverse continuity"},
    {"camera_id": "wide_establishing", "role": "wide establishing"},
    {"camera_id": "low_lateral_follow", "role": "low lateral follow"},
    {"camera_id": "high_three_quarter", "role": "high three-quarter"},
    {"camera_id": "front_left_high_three_quarter", "role": "front-left high three-quarter"},
    {"camera_id": "elevated_front_right_transition", "role": "elevated front-right transition"},
]


def build_camera_prompt(camera_id: str, role: str, *, identity_anchor: bool = False, identity_lock: bool = False) -> str:
    identity_text = ""
    if identity_anchor:
        identity_text = (
            " A canonical identity anchor is supplied first: use it only to lock the same two people and wardrobe "
            "across all camera tasks, not its camera framing or station geometry. The traveler is one adult man in "
            "his mid-thirties with short brown hair, a navy knee-length overcoat, white shirt, dark trousers and "
            "black shoes. The friend is one adult man in his early thirties with short dark hair, a gray jacket, "
            "blue jeans and brown shoes; keep these identities and outfits unchanged in every view."
        )
    elif identity_lock:
        identity_text = (
            " Use this fixed identity specification for every task: the traveler is one adult man in his mid-thirties "
            "with short brown hair, a navy knee-length overcoat, white shirt, dark trousers and black shoes; the "
            "friend is one adult man in his early thirties with short dark hair, a gray jacket, blue jeans and brown "
            "shoes. Keep these identities and outfits unchanged; do not create a second traveler or a second suitcase."
        )
    environment_text = (
        " Every white or gray clay cylinder, cube, faceted cap, post, sign or placeholder prop in the reference is "
        "control geometry only; replace it with coherent real railway-station architecture or omit it, and never "
        "render white low-poly posts or Blender primitives as final objects."
    )
    return (
        "Appearance-only live-action reconstruction from one synchronized reference video of one continuous "
        "five-second railway-platform reunion. "
        f"This task owns camera_id={camera_id}, the {role} camera. Generate only this assigned camera view; "
        "do not output a montage or another camera angle. Use the reference only for blocking, character and "
        "suitcase trajectories, contact timing, occlusion order, station layout, and smooth camera motion. "
        "Do not copy white-clay or low-poly geometry, primitive silhouettes, gray albedo, faceted shading, "
        "or Blender render style. Reconstruct coherent live-action adult humans with natural anatomy, faces, "
        "hair, skin, cloth and a real metal rolling suitcase with visible wheels and handle. "
        "Preserve the story order: the traveler pulls the suitcase toward the waiting friend, slows and turns "
        "toward the friend, the friend turns and raises one hand, and the suitcase remains coupled and grounded. "
        "Preserve the same shared world, identities, spatial relationships, assigned camera responsibility, "
        "smooth motion, full five-second duration, and no cuts. Do not add people, props, text, labels, guide "
        "lines, room changes, black frames, camera jumps, time skips, or background replacement."
        + identity_text
        + environment_text
    )


def select_camera_specs(camera_ids: list[str] | None = None) -> list[dict[str, str]]:
    if not camera_ids:
        return list(CAMERA_SPECS)
    known = {item["camera_id"]: item for item in CAMERA_SPECS}
    unknown = [camera_id for camera_id in camera_ids if camera_id not in known]
    if unknown:
        raise ValueError("unknown camera ids: " + ", ".join(unknown))
    if len(set(camera_ids)) != len(camera_ids):
        raise ValueError("camera ids must be unique")
    return [known[camera_id] for camera_id in camera_ids]


def validate_matrix_inputs(proxy_root: Path, output_root: Path, *, existing_names: set[str], camera_specs: list[dict[str, str]] | None = None) -> None:
    """Validate non-media invariants before any upload or generation call."""
    if output_root.exists():
        raise FileExistsError(f"output root already exists: {output_root}")
    if output_root.name in existing_names:
        raise FileExistsError(f"output root name was already used: {output_root.name}")
    specs = camera_specs or CAMERA_SPECS
    if not specs or len({item["camera_id"] for item in specs}) != len(specs):
        raise ValueError("camera matrix must contain unique camera ids")
    if not proxy_root:
        raise ValueError("proxy root is required")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _canonical_sha(value: Any) -> str:
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _prepare_and_submit(
    *,
    spec: dict[str, str],
    proxy_root: Path,
    output_root: Path,
    api_key: str,
    base_url: str,
    model: str,
    upload_config: Any,
    identity_anchor: Any | None = None,
    identity_lock: bool = False,
) -> dict[str, Any]:
    camera_id = spec["camera_id"]
    source = proxy_root / f"{camera_id}.mp4"
    if not source.is_file():
        raise FileNotFoundError(f"missing proxy for {camera_id}: {source}")
    job_dir = output_root / f"Seedance_{camera_id}"
    job_dir.mkdir(parents=True)
    normalized_dir = job_dir / "normalized"
    uploads_dir = job_dir / "uploads"
    normalized_dir.mkdir()
    uploads_dir.mkdir()
    normalized = normalize_proxy(source, normalized_dir / f"{camera_id}_seedance.mp4")
    uploaded = upload_proxy(normalized, run_id=f"seedance_camera_matrix_{camera_id}", config=upload_config)
    write_upload_record(uploaded, uploads_dir / "reference.json")
    prompt = build_camera_prompt(camera_id, spec["role"], identity_anchor=identity_anchor is not None, identity_lock=identity_lock)
    if identity_anchor is None:
        request = build_seedance_reference_video(prompt, uploaded.url, model=model, duration=5)
    else:
        request = build_seedance_reference_videos(prompt, [identity_anchor.url, uploaded.url], model=model, duration=5)
    prepared = {
        "schema_version": "seedance-single-camera-job-1.0",
        "job_id": f"Seedance_{camera_id}",
        "camera_id": camera_id,
        "camera_role": spec["role"],
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_proxy": str(source.resolve()),
        "source_proxy_sha256": _sha256_file(source),
        "prompt": prompt,
        "prompt_sha256": _canonical_sha(prompt),
        "request": request,
        "request_sha256": _canonical_sha(request),
        "upload": uploaded.to_dict(),
        "identity_anchor": identity_anchor.to_dict() if identity_anchor is not None else None,
        "api_calls": {"submit": 0, "query": 0, "download": 0},
    }
    _write_json(job_dir / "prepared_job.json", prepared)
    run = RunDirectory(job_dir)
    task_id = submit_once(request, api_key, base_url, run)
    return {
        "camera_id": camera_id,
        "camera_role": spec["role"],
        "job_id": f"Seedance_{camera_id}",
        "job_dir": job_dir,
        "run": run,
        "task_id": task_id,
        "calls": {"submit": 1, "query": 0, "download": 0},
        "status": "submitted",
    }


def _write_progress(output_root: Path, results: list[dict[str, Any]], expected_camera_count: int) -> None:
    serializable = []
    for item in results:
        row = {key: value for key, value in item.items() if key not in {"job_dir", "run"}}
        serializable.append(row)
    totals = {key: sum(int(row.get("calls", {}).get(key, 0)) for row in serializable) for key in ("submit", "query", "download")}
    _write_json(
        output_root / "progress.json",
        {
            "schema_version": "seedance-camera-matrix-progress-1.0",
            "expected_camera_count": expected_camera_count,
            "results": serializable,
            "actual_download_count": sum(1 for row in serializable if row.get("status") == "succeeded"),
            "total_api_calls": totals,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="Doubao-Seedance-2.0")
    parser.add_argument("--poll-interval", type=float, default=20.0)
    parser.add_argument("--max-polls", type=int, default=10)
    parser.add_argument("--identity-anchor", type=Path, default=None)
    parser.add_argument("--identity-lock", action="store_true")
    parser.add_argument("--camera-id", dest="camera_ids", action="append", default=None)
    args = parser.parse_args()
    proxy_root = args.proxy_root.resolve()
    output_root = args.output_root.resolve()
    specs = select_camera_specs(args.camera_ids)
    validate_matrix_inputs(proxy_root, output_root, existing_names=set(), camera_specs=specs)
    output_root.mkdir(parents=True)
    api_key = os.environ.get("JD_KLING_KEY", "").strip()
    if not api_key:
        raise SystemExit("JD_KLING_KEY is not configured")
    base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")
    _write_json(output_root / "matrix_manifest.json", {"schema_version": "seedance-camera-matrix-1.0", "model": args.model, "camera_specs": specs, "proxy_root": str(proxy_root), "expected_output_count": len(specs)})
    (output_root / "README.txt").write_text("One Seedance task and one result.mp4 per camera_id. Multiple references are not treated as multiple outputs.\n", encoding="utf-8")
    upload_config = _temporary_upload_config()
    identity_anchor = None
    if args.identity_anchor is not None:
        anchor_source = args.identity_anchor.resolve(strict=True)
        anchor_dir = output_root / "identity_anchor"
        anchor_dir.mkdir()
        anchor_normalized = normalize_proxy(anchor_source, anchor_dir / "canonical_identity_seedance.mp4")
        identity_anchor = upload_proxy(anchor_normalized, run_id="seedance_camera_matrix_identity_anchor", config=upload_config)
        write_upload_record(identity_anchor, anchor_dir / "upload.json")
        _write_json(output_root / "identity_anchor.json", {"source_path": str(anchor_source), "source_sha256": _sha256_file(anchor_source), "upload": identity_anchor.to_dict()})
    results: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    for spec in specs:
        try:
            item = _prepare_and_submit(spec=spec, proxy_root=proxy_root, output_root=output_root, api_key=api_key, base_url=base_url, model=args.model, upload_config=upload_config, identity_anchor=identity_anchor, identity_lock=args.identity_lock)
            active.append(item)
            results.append(item)
        except Exception as exc:
            row = {"camera_id": spec["camera_id"], "camera_role": spec["role"], "job_id": f"Seedance_{spec['camera_id']}", "status": "submit_failed", "error": f"{type(exc).__name__}: {exc}", "calls": {"submit": 0, "query": 0, "download": 0}}
            results.append(row)
        _write_progress(output_root, results, len(specs))

    for poll_round in range(args.max_polls):
        next_active: list[dict[str, Any]] = []
        for item in active:
            try:
                item["calls"]["query"] += 1
                response = query_once(api_key, item["run"])
                status = str(extract_status(response) or "unknown").lower()
                item["last_status"] = status
                if status in SUCCESS_STATUSES:
                    item["calls"]["download"] = 1
                    result_path = download_once(item["run"])
                    item["status"] = "succeeded"
                    item["result_path"] = str(result_path)
                    item["result_sha256"] = _sha256_file(result_path)
                    _write_json(item["job_dir"] / "result_summary.json", {"camera_id": item["camera_id"], "task_id": item["task_id"], "status": item["status"], "result_path": str(result_path), "result_sha256": item["result_sha256"], "api_calls": item["calls"]})
                elif status in FAILURE_STATUSES:
                    item["status"] = "task_failed"
                else:
                    next_active.append(item)
            except Exception as exc:
                item["status"] = "query_or_download_failed"
                item["error"] = f"{type(exc).__name__}: {exc}"
            _write_progress(output_root, results, len(specs))
        active = next_active
        if not active or poll_round + 1 >= args.max_polls:
            break
        time.sleep(args.poll_interval)

    for item in active:
        item["status"] = "unknown_after_bounded_polling"
        _write_json(item["job_dir"] / "failure.json", {"camera_id": item["camera_id"], "task_id": item["task_id"], "status": item["status"], "api_calls": item["calls"]})
    _write_progress(output_root, results, len(specs))
    summary = json.loads((output_root / "progress.json").read_text(encoding="utf-8"))
    summary["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    summary["all_expected_downloaded"] = summary["actual_download_count"] == len(specs)
    _write_json(output_root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_expected_downloaded"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
