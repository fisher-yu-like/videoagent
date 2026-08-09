"""Offline backend adapters for an approved proxy and appearance prompt.

This boundary prepares immutable, hash-bound request bundles only.  It never
submits a request or silently changes a reference-video backend into text-only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .appearance_prompt import APPEARANCE_PROMPT_SCHEMA_VERSION
from .state import WorldState


BACKEND_ADAPTER_SCHEMA_VERSION = "backend-adapter-bundle-1.0"
SUPPORTED_BACKENDS = frozenset({
    "vace", "omniweaving", "seedance_reference", "kling_reference",
    "seedance_t2v", "kling_t2v",
})


class BackendAdapterError(ValueError):
    """Raised when an adapter cannot bind a backend request safely."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise BackendAdapterError(f"{label} path is empty")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BackendAdapterError(f"{label} path must stay inside proxy_root")
    return path


def _validate_appearance(appearance_prompt: Mapping[str, Any], world: WorldState) -> dict[str, Any]:
    if not isinstance(appearance_prompt, Mapping):
        raise BackendAdapterError("appearance_prompt must be an object")
    if appearance_prompt.get("schema_version") != APPEARANCE_PROMPT_SCHEMA_VERSION:
        raise BackendAdapterError("appearance_prompt schema_version is invalid")
    if appearance_prompt.get("source_world_state_hash") != world.world_state_hash():
        raise BackendAdapterError("appearance_prompt WorldState hash differs from WorldState")
    prompt = appearance_prompt.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BackendAdapterError("appearance_prompt text is empty")
    expected = _sha256_bytes(prompt.encode("utf-8"))
    if appearance_prompt.get("prompt_sha256") != expected:
        raise BackendAdapterError("appearance_prompt hash does not match text")
    return copy.deepcopy(dict(appearance_prompt))


def _validate_proxy(
    *, world: WorldState, proxy_manifest: Mapping[str, Any], proxy_root: Path
) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(proxy_manifest, Mapping):
        raise BackendAdapterError("proxy manifest must be an object")
    if proxy_manifest.get("schema_version") != "pipeline-v2-render-manifest-1.0":
        raise BackendAdapterError("proxy manifest schema_version is invalid")
    if proxy_manifest.get("world_state_hash") != world.world_state_hash():
        raise BackendAdapterError("proxy manifest WorldState hash differs from WorldState")
    if proxy_manifest.get("frame_count") != world.frame_count or proxy_manifest.get("fps") != world.fps:
        raise BackendAdapterError("proxy manifest frame metadata differs from WorldState")
    camera_ids = {item["id"] for item in world.to_dict()["camera_trajectory_plan"]["cameras"]}
    videos = proxy_manifest.get("videos")
    if not isinstance(videos, list) or len(videos) != len(camera_ids):
        raise BackendAdapterError("proxy manifest video count differs from camera plan")
    checked: list[dict[str, Any]] = []
    video_hashes: dict[str, str] = {}
    seen: set[str] = set()
    for item in videos:
        if not isinstance(item, Mapping):
            raise BackendAdapterError("proxy manifest video entries must be objects")
        camera_id = item.get("camera_id")
        if camera_id not in camera_ids or camera_id in seen:
            raise BackendAdapterError("proxy manifest has duplicate or unknown camera id")
        seen.add(camera_id)
        relative = _safe_relative(item.get("path"), f"{camera_id} video")
        path = (proxy_root / relative).resolve(strict=False)
        try:
            path.relative_to(proxy_root)
        except ValueError as exc:
            raise BackendAdapterError(f"{camera_id} video escapes proxy_root") from exc
        if not path.is_file() or path.stat().st_size <= 0:
            raise BackendAdapterError(f"{camera_id} proxy video is missing or empty")
        actual_hash = _sha256_file(path)
        if actual_hash != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            raise BackendAdapterError(f"{camera_id} proxy video hash or byte count differs")
        if item.get("frame_count") != world.frame_count or item.get("fps") != world.fps:
            raise BackendAdapterError(f"{camera_id} proxy video frame metadata differs")
        checked.append({
            "camera_id": camera_id,
            "path": relative.as_posix(),
            "sha256": actual_hash,
            "bytes": path.stat().st_size,
            "frame_count": item.get("frame_count"),
            "fps": item.get("fps"),
            "resolution": item.get("resolution"),
        })
        video_hashes[camera_id] = actual_hash
    if seen != camera_ids:
        raise BackendAdapterError("proxy manifest does not cover every camera")
    return {
        "schema_version": proxy_manifest.get("schema_version"),
        "world_state_hash": proxy_manifest.get("world_state_hash"),
        "frame_count": proxy_manifest.get("frame_count"),
        "fps": proxy_manifest.get("fps"),
        "resolution": proxy_manifest.get("resolution"),
        "videos": sorted(checked, key=lambda item: item["camera_id"]),
    }, video_hashes


def _reference_payload(prompt: str, url: str, model: str, duration: int) -> dict[str, Any]:
    try:
        if model.startswith("Kling-"):
            # Candidate shape only: the gateway capability remains explicitly
            # unverified until a captured real response proves this contract.
            return {
                "model": model,
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "video_url",
                        "video_url": {"url": url},
                        "role": "reference_video",
                    },
                ],
                "parameters": {
                    "duration": duration,
                    "mode": "pro",
                    "aspect_ratio": "16:9",
                },
            }
        from videoactagent.backends.jd import build_seedance_reference_video
        return build_seedance_reference_video(prompt, url, model=model, duration=duration)
    except Exception as exc:
        raise BackendAdapterError(str(exc)) from exc


def prepare_backend_adapter(
    *,
    backend: str,
    world: WorldState,
    proxy_manifest: Mapping[str, Any],
    proxy_root: Path | str,
    appearance_prompt: Mapping[str, Any],
    proxy_urls: Mapping[str, str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Prepare a backend request bundle without network activity or retries."""
    if backend not in SUPPORTED_BACKENDS:
        raise BackendAdapterError(f"unsupported backend: {backend}")
    if not isinstance(world, WorldState):
        raise BackendAdapterError("world must be a WorldState")
    root = Path(proxy_root).resolve(strict=True)
    appearance = _validate_appearance(appearance_prompt, world)
    normalized_manifest, video_hashes = _validate_proxy(world=world, proxy_manifest=proxy_manifest, proxy_root=root)
    manifest_digest = _sha256_bytes(_canonical(normalized_manifest))
    if appearance.get("source_proxy_manifest_hash") != manifest_digest:
        raise BackendAdapterError("appearance_prompt Proxy manifest hash differs from Proxy manifest")
    duration = int(round(float(world.to_dict()["scene_plan"]["duration_seconds"])))
    source_hashes = {
        "world_state": world.world_state_hash(),
        "proxy_manifest": manifest_digest,
        "appearance_prompt": appearance["prompt_sha256"],
        "videos": video_hashes,
    }
    base: dict[str, Any] = {
        "schema_version": BACKEND_ADAPTER_SCHEMA_VERSION,
        "backend": backend,
        "model": model,
        "status": "ready",
        "conditioning_mode": "source_video_edit",
        "network_called": False,
        "generation_submit_limit": 1,
        "automatic_retry_limit": 0,
        "fallbacks": {"prompt_only": False},
        "source_hashes": source_hashes,
        "source_world_state_hash": world.world_state_hash(),
        "appearance_prompt": {
            "text": appearance["prompt"],
            "sha256": appearance["prompt_sha256"],
        },
        "appearance_prompt_sha256": appearance["prompt_sha256"],
        "proxy": normalized_manifest,
        "jobs": [],
        "blockers": [],
    }
    if backend in {"seedance_reference", "kling_reference"}:
        base["gateway_capability"] = "unverified"
    else:
        base["gateway_capability"] = "not_applicable"
    if backend in {"vace", "omniweaving"}:
        base["status"] = "blocked"
        base["blockers"].append(
            "backend does not currently accept Proxy in this pipeline; no source-video adapter is enabled"
        )
        base["bundle_sha256"] = _sha256_bytes(_canonical(base))
        return base
    if backend == "seedance_reference":
        base["conditioning_mode"] = "reference_video"
    if backend == "kling_reference":
        base["conditioning_mode"] = "reference_video"

    if backend in {"seedance_reference", "kling_reference"}:
        if duration != 5:
            base["blockers"].append("reference-video backend requires exactly five seconds")
        if not proxy_urls:
            base["blockers"].append("public HTTPS proxy URL is required for reference-video backend")
        if base["blockers"]:
            base["status"] = "blocked"
            base["bundle_sha256"] = _sha256_bytes(_canonical(base))
            return base

    for item in normalized_manifest["videos"]:
        camera_id = item["camera_id"]
        request: dict[str, Any]
        if backend in {"vace", "omniweaving"}:
            request = {
                "task_type": "editing",
                "prompt": appearance["prompt"],
                "condition_video": item["path"],
                "camera_id": camera_id,
            }
        elif backend == "kling_t2v":
            from videoactagent.backends.jd import build_kling_t2v
            request = build_kling_t2v(appearance["prompt"], duration)
            base["conditioning_mode"] = "prompt_only"
        elif backend == "seedance_t2v":
            from videoactagent.backends.jd import build_seedance_t2v
            request = build_seedance_t2v(appearance["prompt"], duration)
            base["conditioning_mode"] = "prompt_only"
        else:
            urls = proxy_urls or {}
            url = urls.get(camera_id)
            if not isinstance(url, str) or not url.strip():
                base["status"] = "blocked"
                base["blockers"].append("public HTTPS proxy URL is required for reference-video backend")
                break
            request = _reference_payload(
                appearance["prompt"], url,
                model or ("Kling-V3-omni" if backend == "kling_reference" else "Doubao-Seedance-2.5"),
                duration,
            )
            request["camera_id"] = camera_id
            base["conditioning_mode"] = "reference_video"
            base["status"] = "ready_for_single_probe"
        base["jobs"].append({
            "job_id": f"{backend}__{camera_id}",
            "camera_id": camera_id,
            "proxy_video": item["path"],
            "proxy_video_sha256": item["sha256"],
            "prompt_sha256": appearance["prompt_sha256"],
            "request": request,
        })
    if base["status"] == "blocked":
        base["jobs"] = []
    base["bundle_sha256"] = _sha256_bytes(_canonical(base))
    return base


def write_backend_adapter_bundle(bundle: Mapping[str, Any], output_dir: Path | str) -> Path:
    """Write one adapter bundle and prompt into a new directory, never overwrite."""
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise BackendAdapterError(f"adapter output already exists: {destination}")
    destination.mkdir(parents=True)
    (destination / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    prompt_record = bundle.get("appearance_prompt")
    if isinstance(prompt_record, Mapping) and isinstance(prompt_record.get("text"), str):
        (destination / "appearance_prompt.txt").write_text(prompt_record["text"], encoding="utf-8")
    return destination
