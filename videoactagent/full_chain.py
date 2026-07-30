"""Compile and snapshot the immutable, offline full-chain story matrix."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ExperimentConfigError(ValueError):
    """Raised when a full-chain matrix is not the approved offline contract."""


_STORY_ID = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_BACKENDS = ("kling", "seedance", "vace")
_FROZEN_STORY_IDS = (
    "station_reunion",
    "station_departure",
    "crosswalk_opposite",
    "crosswalk_diagonal",
    "forest_follow",
    "forest_converge",
    "studio_exchange",
    "studio_formation",
)
_FROZEN_CANARIES = ("station_reunion", "studio_formation")
_CANONICAL_SUMMARY = Path("runs/work/whole_story_v4/summary.json")
_CANONICAL_SUMMARY_SHA256 = "bc0f58d6ccdf331bb728c250882cece05dcdba020a2f3674f4c179a0f32aca02"
_CANONICAL_FROZEN_INPUTS_SHA256 = "a5a019b5f2777ff9c5cf194beb002711ea2334544f7c89e864f52f1b020cf32d"
_EXPECTED_BUDGETS = {
    "generation_submissions": 16,
    "status_queries": 64,
    "downloads": 16,
    "vace_inferences": 8,
    "automatic_retries": 0,
}


@dataclass(frozen=True)
class ExperimentBudget:
    generation_submissions: int
    status_queries: int
    downloads: int
    vace_inferences: int
    automatic_retries: int

    def as_dict(self) -> dict[str, int]:
        return {
            "generation_submissions": self.generation_submissions,
            "status_queries": self.status_queries,
            "downloads": self.downloads,
            "vace_inferences": self.vace_inferences,
            "automatic_retries": self.automatic_retries,
        }

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ExperimentBudget):
            return self.as_dict() == other.as_dict()
        if isinstance(other, dict):
            return self.as_dict() == other
        return NotImplemented


@dataclass(frozen=True)
class ExperimentConfig:
    path: Path
    workspace: Path
    source_summary: Path
    story_ids: tuple[str, ...]
    canary_story_ids: tuple[str, ...]
    backends: tuple[str, ...]
    duration_seconds: float
    query_limit: int
    download_limit: int
    vace_frame_count: int
    vace_fps: int
    vace_seed: int
    budgets: ExperimentBudget
    frozen_summary_sha256: str
    frozen_stories: dict[str, dict[str, Any]]
    document: dict[str, Any]
    config_sha256: str


@dataclass(frozen=True)
class ExperimentJob:
    story_id: str
    backend: str
    release: str
    conditioning_mode: str
    adapter_status: str
    prompt: str
    document: dict[str, Any]


@dataclass(frozen=True)
class ExperimentMatrix:
    config: ExperimentConfig
    jobs: tuple[ExperimentJob, ...]
    canary_story_ids: tuple[str, ...]
    budgets: ExperimentBudget
    source_files: tuple[tuple[Path, str, str], ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentConfigError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExperimentConfigError(f"{label} must be an object")
    return value


def _safe_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ExperimentConfigError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ExperimentConfigError(f"{label} must stay inside the workspace")
    candidate = root / relative
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ExperimentConfigError(f"{label} must stay inside the workspace")
    return candidate


def _trusted_file(path: Path, root: Path, label: str) -> Path:
    """Require a regular file below root without link or junction traversal."""
    try:
        trusted_root = root.resolve(strict=True)
        relative = path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ExperimentConfigError(f"{label} is outside its expected root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        is_junction = getattr(current, "is_junction", lambda: False)
        try:
            attributes = getattr(os.lstat(current), "st_file_attributes", 0)
        except OSError as exc:
            raise ExperimentConfigError(f"{label} is missing") from exc
        is_windows_reparse_point = bool(attributes & 0x0400)
        if current.is_symlink() or is_junction() or is_windows_reparse_point:
            raise ExperimentConfigError(f"{label} must not traverse a symlink or junction")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ExperimentConfigError(f"{label} is missing") from exc
    if resolved != trusted_root and trusted_root not in resolved.parents:
        raise ExperimentConfigError(f"{label} escapes its expected root")
    if not resolved.is_file():
        raise ExperimentConfigError(f"{label} must be a file")
    return resolved


def _contains_token(value: object) -> bool:
    if isinstance(value, dict):
        return any("token" in str(key).lower() or _contains_token(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_token(item) for item in value)
    return False


def _safe_slug(value: object, label: str) -> str:
    if not isinstance(value, str) or _STORY_ID.fullmatch(value) is None:
        raise ExperimentConfigError(f"{label} must be a safe story ID")
    return value


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExperimentConfigError(f"{label} must be an object")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentConfigError(f"{label} must be an integer")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ExperimentConfigError(f"{label} must be a SHA-256")
    return value


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _contains_shot_id(value: object) -> bool:
    if isinstance(value, dict):
        return "shot_id" in value or any(_contains_shot_id(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_shot_id(item) for item in value)
    return False


def _same_hashes(left: object, right: object, label: str) -> None:
    left_data = _object(left, f"{label} left hashes")
    right_data = _object(right, f"{label} right hashes")
    expected = {"prompt_file_sha256", "submitted_prompt_sha256", "shotscript_sha256"}
    if set(left_data) != expected or set(right_data) != expected:
        raise ExperimentConfigError(f"{label} source hashes must be complete")
    for name in expected:
        if _hash(left_data[name], f"{label}.{name}") != _hash(right_data[name], f"{label}.{name}"):
            raise ExperimentConfigError(f"{label} source hash mismatch")


def _parse_config(path: Path) -> ExperimentConfig:
    document = _read_json(path, "matrix config")
    allowed_fields = {
        "schema_version",
        "source_summary",
        "frozen_inputs",
        "story_ids",
        "canary_story_ids",
        "backends",
        "request",
        "vace",
        "budgets",
        "submit",
        "release_policy",
        "release_gates",
    }
    if set(document) != allowed_fields:
        raise ExperimentConfigError("matrix config contains unknown, missing, or token fields")
    if _contains_token(document):
        raise ExperimentConfigError("matrix config must not contain a release token")
    if document.get("schema_version") != "1.0":
        raise ExperimentConfigError("schema_version must be 1.0")
    workspace = path.parent.parent.resolve()
    if document.get("source_summary") != _CANONICAL_SUMMARY.as_posix():
        raise ExperimentConfigError("source_summary must bind the canonical whole_story_v4 summary")
    source_summary = _safe_path(workspace, document.get("source_summary"), "source_summary")
    frozen_inputs = _object(document.get("frozen_inputs"), "frozen_inputs")
    if set(frozen_inputs) != {"summary_sha256", "stories"}:
        raise ExperimentConfigError("frozen_inputs must contain summary_sha256 and stories")
    if _canonical_digest(frozen_inputs) != _CANONICAL_FROZEN_INPUTS_SHA256:
        raise ExperimentConfigError(
            "frozen_inputs differs from the independently anchored canonical v4 digest set"
        )
    frozen_summary_sha256 = _hash(frozen_inputs.get("summary_sha256"), "frozen summary hash")
    if frozen_summary_sha256 != _CANONICAL_SUMMARY_SHA256:
        raise ExperimentConfigError("frozen summary hash differs from the independently anchored v4 summary")
    frozen_stories_value = _object(frozen_inputs.get("stories"), "frozen stories")
    if tuple(frozen_stories_value) != _FROZEN_STORY_IDS:
        raise ExperimentConfigError("frozen stories must be the exact approved v4 set and order")
    frozen_stories: dict[str, dict[str, Any]] = {}
    for story_id in _FROZEN_STORY_IDS:
        frozen = _object(frozen_stories_value.get(story_id), f"frozen {story_id}")
        if set(frozen) != {"manifest_sha256", "proxy_sha256", "source_hashes", "backend_input_sha256"}:
            raise ExperimentConfigError(f"frozen {story_id} digest fields are incomplete")
        _hash(frozen.get("manifest_sha256"), f"frozen {story_id} manifest hash")
        _hash(frozen.get("proxy_sha256"), f"frozen {story_id} proxy hash")
        _same_hashes(frozen.get("source_hashes"), frozen.get("source_hashes"), f"frozen {story_id}")
        backend_hashes = _object(frozen.get("backend_input_sha256"), f"frozen {story_id} backend hashes")
        if tuple(backend_hashes) != _BACKENDS:
            raise ExperimentConfigError(f"frozen {story_id} backend hashes are incomplete")
        for backend in _BACKENDS:
            _hash(backend_hashes[backend], f"frozen {story_id} {backend} hash")
        frozen_stories[story_id] = frozen
    story_values = document.get("story_ids")
    if not isinstance(story_values, list) or len(story_values) != 8:
        raise ExperimentConfigError("story_ids must contain exactly eight stories")
    story_ids = tuple(_safe_slug(value, "story_ids entry") for value in story_values)
    if story_ids != _FROZEN_STORY_IDS:
        raise ExperimentConfigError("story_ids must be the exact approved v4 set and order")
    canary_values = document.get("canary_story_ids")
    if not isinstance(canary_values, list) or len(canary_values) != 2:
        raise ExperimentConfigError("canary_story_ids must contain exactly two stories")
    canaries = tuple(_safe_slug(value, "canary story") for value in canary_values)
    if canaries != _FROZEN_CANARIES:
        raise ExperimentConfigError("canary stories must be station_reunion and studio_formation")
    backend_values = document.get("backends")
    if not isinstance(backend_values, list) or tuple(backend_values) != _BACKENDS:
        raise ExperimentConfigError("backends must be the approved kling, seedance, vace order")

    request = _object(document.get("request"), "request")
    if set(request) != {"duration_seconds", "query_limit", "download_limit"}:
        raise ExperimentConfigError("request contains unknown or missing fields")
    duration = request.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or float(duration) != 5.0:
        raise ExperimentConfigError("request.duration_seconds must be 5.0")
    query_limit = _integer(request.get("query_limit"), "request.query_limit")
    download_limit = _integer(request.get("download_limit"), "request.download_limit")
    if query_limit != 4 or download_limit != 1:
        raise ExperimentConfigError("request limits must be query=4 and download=1")
    vace = _object(document.get("vace"), "vace")
    if set(vace) != {"frame_count", "fps", "seed"}:
        raise ExperimentConfigError("vace contains unknown or missing fields")
    frames = _integer(vace.get("frame_count"), "vace.frame_count")
    fps = _integer(vace.get("fps"), "vace.fps")
    seed = _integer(vace.get("seed"), "vace.seed")
    if (frames, fps, seed) != (81, 16, 2026):
        raise ExperimentConfigError("vace must use 81 frames, fps 16, seed 2026")
    budget_data = _object(document.get("budgets"), "budgets")
    if set(budget_data) != set(_EXPECTED_BUDGETS):
        raise ExperimentConfigError("budgets must declare the exact approved fields")
    budgets = ExperimentBudget(**{name: _integer(budget_data[name], f"budgets.{name}") for name in _EXPECTED_BUDGETS})
    if budgets.as_dict() != _EXPECTED_BUDGETS:
        raise ExperimentConfigError("budgets must equal the approved 24-job limits")
    if document.get("submit") is not False:
        raise ExperimentConfigError("prepare is offline: submit=false is required")
    if document.get("release_policy") != "explicit_matrix_bound_token":
        raise ExperimentConfigError("release_policy must be explicit_matrix_bound_token")
    gates = _object(document.get("release_gates"), "release_gates")
    if set(gates) != {"canary_enabled", "remainder_enabled"}:
        raise ExperimentConfigError("release_gates contains unknown or missing fields")
    canary_enabled = gates.get("canary_enabled")
    remainder_enabled = gates.get("remainder_enabled")
    if not isinstance(canary_enabled, bool) or not isinstance(remainder_enabled, bool):
        raise ExperimentConfigError("release gates must be booleans")
    if canary_enabled or remainder_enabled:
        raise ExperimentConfigError("prepare config must leave canary and remainder disabled")
    return ExperimentConfig(
        path=path,
        workspace=workspace,
        source_summary=source_summary,
        story_ids=story_ids,
        canary_story_ids=canaries,
        backends=_BACKENDS,
        duration_seconds=5.0,
        query_limit=query_limit,
        download_limit=download_limit,
        vace_frame_count=frames,
        vace_fps=fps,
        vace_seed=seed,
        budgets=budgets,
        frozen_summary_sha256=frozen_summary_sha256,
        frozen_stories=frozen_stories,
        document=document,
        config_sha256=_sha256(path),
    )


def load_experiment_config(path: Path | str) -> ExperimentConfig:
    """Load the approved config without reading or submitting any backend request."""
    config_path = Path(path).resolve()
    return _parse_config(config_path)


def _case_path(summary_root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ExperimentConfigError(f"{label} must be a relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ExperimentConfigError(f"{label} must be a safe relative path")
    result = summary_root / relative
    resolved = result.resolve(strict=False)
    if resolved != summary_root and summary_root not in resolved.parents:
        raise ExperimentConfigError(f"{label} escapes source root")
    return result


def _validate_case(
    config: ExperimentConfig,
    summary_root: Path,
    case: dict[str, Any],
) -> tuple[list[ExperimentJob], list[tuple[Path, str, str]]]:
    story_id = _safe_slug(case.get("story_id"), "summary story_id")
    manifest_path = _case_path(summary_root, case.get("manifest"), f"{story_id} manifest")
    manifest_path = _trusted_file(manifest_path, summary_root, f"{story_id} manifest")
    frozen = config.frozen_stories[story_id]
    if _sha256(manifest_path) != frozen["manifest_sha256"]:
        raise ExperimentConfigError(f"{story_id} manifest differs from the frozen v4 digest")
    manifest = _read_json(manifest_path, f"{story_id} manifest")
    if manifest.get("schema_version") != "1.0" or manifest.get("story_id") != story_id:
        raise ExperimentConfigError(f"{story_id} manifest identity mismatch")
    if manifest.get("status") != "media_complete":
        raise ExperimentConfigError(f"{story_id} manifest is not media_complete")
    proxy_hash = _hash(case.get("proxy_sha256"), f"{story_id} summary proxy hash")
    media = _object(manifest.get("media"), f"{story_id} media")
    if (
        proxy_hash != frozen["proxy_sha256"]
        or _hash(media.get("sha256"), f"{story_id} media hash") != proxy_hash
    ):
        raise ExperimentConfigError(f"{story_id} proxy hash differs from summary")
    source_hashes = _object(manifest.get("source_hashes"), f"{story_id} source_hashes")
    _same_hashes(source_hashes, source_hashes, story_id)
    _same_hashes(source_hashes, frozen["source_hashes"], f"{story_id} frozen")
    case_dir = manifest_path.parent
    prompt_path = case_dir / "sources" / "prompt.txt"
    shotscript_path = case_dir / "sources" / "shotscript.json"
    prompt_path = _trusted_file(prompt_path, case_dir, f"{story_id} prompt")
    shotscript_path = _trusted_file(shotscript_path, case_dir, f"{story_id} ShotScript")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ExperimentConfigError(f"{story_id} prompt is empty")
    if _sha256(prompt_path) != source_hashes["prompt_file_sha256"]:
        raise ExperimentConfigError(f"{story_id} prompt file hash mismatch")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != source_hashes["submitted_prompt_sha256"]:
        raise ExperimentConfigError(f"{story_id} submitted prompt hash mismatch")
    if _sha256(shotscript_path) != source_hashes["shotscript_sha256"]:
        raise ExperimentConfigError(f"{story_id} ShotScript hash mismatch")
    bundle_files = _object(manifest.get("bundle_files"), f"{story_id} bundle_files")
    bundles = _object(manifest.get("bundles"), f"{story_id} bundles")
    if set(bundle_files) != set(_BACKENDS) or set(bundles) != set(_BACKENDS):
        raise ExperimentConfigError(f"{story_id} must include all backend manifests")

    source_files = [
        (manifest_path, f"cases/{story_id}/manifest.json", _sha256(manifest_path)),
        (prompt_path, f"cases/{story_id}/sources/prompt.txt", _sha256(prompt_path)),
        (shotscript_path, f"cases/{story_id}/sources/shotscript.json", _sha256(shotscript_path)),
    ]
    jobs: list[ExperimentJob] = []
    for backend in _BACKENDS:
        entry = _object(bundle_files.get(backend), f"{story_id} {backend} bundle file")
        bundle_path = _case_path(case_dir, entry.get("path"), f"{story_id} {backend} bundle path")
        bundle_path = _trusted_file(bundle_path, case_dir, f"{story_id} {backend} backend manifest")
        if _sha256(bundle_path) != frozen["backend_input_sha256"][backend]:
            raise ExperimentConfigError(f"{story_id} {backend} input differs from the frozen v4 digest")
        if _sha256(bundle_path) != _hash(entry.get("sha256"), f"{story_id} {backend} bundle hash"):
            raise ExperimentConfigError(f"{story_id} {backend} backend manifest hash mismatch")
        bundle = _read_json(bundle_path, f"{story_id} {backend} backend manifest")
        if bundle != bundles[backend]:
            raise ExperimentConfigError(f"{story_id} {backend} embedded bundle differs from bundle file")
        if bundle.get("schema_version") != "1.0" or bundle.get("story_id") != story_id or bundle.get("backend") != backend:
            raise ExperimentConfigError(f"{story_id} {backend} backend identity mismatch")
        if bundle.get("duration_seconds") != config.duration_seconds or bundle.get("prompt") != prompt:
            raise ExperimentConfigError(f"{story_id} {backend} backend request differs from frozen source")
        _same_hashes(source_hashes, bundle.get("source_hashes"), f"{story_id} {backend}")
        expected_mode = "source_video" if backend == "vace" else "prompt_only"
        expected_adapter = (
            "offline_input_manifest_not_preprocess_job"
            if backend == "vace"
            else "offline_input_manifest_not_submission_payload"
        )
        if bundle.get("conditioning_mode") != expected_mode or bundle.get("adapter_status") != expected_adapter:
            raise ExperimentConfigError(f"{story_id} {backend} conditioning or adapter status is invalid")
        if backend == "vace":
            source_video = _object(bundle.get("source_video"), f"{story_id} vace source_video")
            if _hash(source_video.get("sha256"), f"{story_id} vace source hash") != proxy_hash:
                raise ExperimentConfigError(f"{story_id} VACE source video differs from proxy")
        if _contains_shot_id(bundle):
            raise ExperimentConfigError(f"{story_id} {backend} must be story-level and omit shot_id")
        source_files.append((bundle_path, f"cases/{story_id}/bundles/{backend}.json", _sha256(bundle_path)))
        jobs.append(
            ExperimentJob(
                story_id=story_id,
                backend=backend,
                release="canary" if story_id in config.canary_story_ids else "remainder",
                conditioning_mode=expected_mode,
                adapter_status=expected_adapter,
                prompt=prompt,
                document=bundle,
            )
        )
    return jobs, source_files


def compile_matrix(path: Path | str) -> ExperimentMatrix:
    """Validate v4 evidence and compile its strict 8 x 3 story-level product."""
    config = load_experiment_config(path)
    source_summary = _trusted_file(config.source_summary, config.workspace, "whole-story summary")
    if _sha256(source_summary) != config.frozen_summary_sha256:
        raise ExperimentConfigError("whole-story summary differs from the frozen v4 digest")
    summary = _read_json(source_summary, "whole-story summary")
    if summary.get("schema_version") != "1.0" or summary.get("status") != "media_complete":
        raise ExperimentConfigError("whole-story summary must be v1 media_complete")
    if summary.get("submit") is not False or summary.get("api_calls") != 0 or summary.get("server_inference_jobs") != 0:
        raise ExperimentConfigError("whole-story evidence must remain offline")
    cases = summary.get("cases")
    if not isinstance(cases, list) or len(cases) != 8 or summary.get("case_count") != 8:
        raise ExperimentConfigError("whole-story summary must contain exactly eight cases")
    summary_ids = tuple(_safe_slug(case.get("story_id"), "summary story") for case in cases if isinstance(case, dict))
    if len(summary_ids) != 8 or summary_ids != config.story_ids or len(set(summary_ids)) != 8:
        raise ExperimentConfigError("matrix story IDs must exactly match the frozen v4 summary")
    summary_root = source_summary.parent
    jobs: list[ExperimentJob] = []
    source_files: list[tuple[Path, str, str]] = [
        (config.path, "sources/full_chain_matrix.json", config.config_sha256),
        (source_summary, "sources/whole_story_v4_summary.json", _sha256(source_summary)),
    ]
    for case in cases:
        if not isinstance(case, dict):
            raise ExperimentConfigError("summary cases must be objects")
        case_jobs, case_sources = _validate_case(config, summary_root, case)
        jobs.extend(case_jobs)
        source_files.extend(case_sources)
    pairs = [(job.story_id, job.backend) for job in jobs]
    if len(jobs) != 24 or len(set(pairs)) != 24:
        raise ExperimentConfigError("matrix must have one unique job for every story/backend pair")
    if sum(job.release == "canary" for job in jobs) != 6:
        raise ExperimentConfigError("matrix must have exactly six canary jobs")
    return ExperimentMatrix(
        config=config,
        jobs=tuple(jobs),
        canary_story_ids=config.canary_story_ids,
        budgets=config.budgets,
        source_files=tuple(source_files),
    )


def _matrix_document(matrix: ExperimentMatrix, snapshots: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "prepared",
        "release_policy": "explicit_matrix_bound_token",
        "source_config_sha256": matrix.config.config_sha256,
        "canary_story_ids": list(matrix.canary_story_ids),
        "budgets": matrix.budgets.as_dict(),
        "jobs": [
            {
                "job_id": f"{job.story_id}__{job.backend}",
                "story_id": job.story_id,
                "backend": job.backend,
                "release": job.release,
                "conditioning_mode": job.conditioning_mode,
                "adapter_status": job.adapter_status,
                "prompt": job.prompt,
                "document": job.document,
                "request": {
                    "duration_seconds": matrix.config.duration_seconds,
                    "query_limit": matrix.config.query_limit,
                    "download_limit": matrix.config.download_limit,
                },
                "vace": (
                    {
                        "frame_count": matrix.config.vace_frame_count,
                        "fps": matrix.config.vace_fps,
                        "seed": matrix.config.vace_seed,
                    }
                    if job.backend == "vace"
                    else None
                ),
            }
            for job in matrix.jobs
        ],
        "snapshot_sha256": snapshots,
    }


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_prepared_matrix(matrix: ExperimentMatrix, output_dir: Path | str) -> dict[str, Any]:
    """Write an immutable offline snapshot into a brand-new directory only."""
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise ValueError(f"prepared matrix output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    snapshots: dict[str, str] = {}
    try:
        for source, relative, expected_sha in matrix.source_files:
            if not source.is_file() or _sha256(source) != expected_sha:
                raise ExperimentConfigError(f"frozen source changed before snapshot: {source}")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            actual_sha = _sha256(target)
            if actual_sha != expected_sha:
                raise ExperimentConfigError(f"snapshot hash mismatch: {relative}")
            if target.suffix == ".json" and _contains_token(_read_json(target, relative)):
                raise ExperimentConfigError(f"snapshot must not contain a release token: {relative}")
            _fsync_file(target)
            snapshots[relative] = actual_sha
        document = _matrix_document(matrix, snapshots)
        if _contains_token(document):
            raise ExperimentConfigError("prepared matrix must not contain a release token")
        matrix_path = staging / "matrix.json"
        matrix_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _fsync_file(matrix_path)
        snapshots["matrix.json"] = _sha256(matrix_path)
        _fsync_directory(staging)
        os.replace(staging, destination)
        staging = None
        return {
            "status": "prepared",
            "matrix_path": str(destination / "matrix.json"),
            "job_count": len(matrix.jobs),
            "canary_job_count": sum(job.release == "canary" for job in matrix.jobs),
            "snapshot_sha256": snapshots,
        }
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
