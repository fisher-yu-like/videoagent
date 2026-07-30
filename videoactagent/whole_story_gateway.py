"""Bounded story-level gateway adapter for approved full-chain jobs.

Preparation is deliberately offline.  The optional transport arguments exist so
tests can exercise the boundary without network access; production defaults to
the single-attempt JD helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from videoactagent.backends.jd import (
    build_kling_t2v,
    build_seedance_t2v,
    download_once,
    extract_status,
    extract_video_urls,
    query_once,
    submit_once,
)
from videoactagent.full_chain import ExperimentJob
from videoactagent.run_record import RunDirectory, redact


class ReleaseError(RuntimeError):
    """Raised before a disallowed or altered network attempt."""


@dataclass(frozen=True)
class PreparedApiJob:
    path: Path
    backend: str
    conditioning_mode: str
    payload: dict[str, Any]
    request_sha256: str


_BUDGETS = {
    "generation_submissions": 16,
    "status_queries": 64,
    "downloads": 16,
    "vace_inferences": 8,
    "automatic_retries": 0,
}
_STATE_FILE = "state.json"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} must be an object")
    return value


def _self_hashed(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["sha256"] = _canonical_sha256(result)
    return result


def _check_self_hash(value: dict[str, Any], label: str) -> None:
    expected = value.get("sha256")
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    if not isinstance(expected, str) or expected != _canonical_sha256(unsigned):
        raise ReleaseError(f"snapshot hash mismatch: {label}")


def _payload_for(job: ExperimentJob) -> dict[str, Any]:
    if job.conditioning_mode != "prompt_only":
        raise ValueError("gateway jobs must use prompt_only conditioning")
    if job.document.get("duration_seconds") != 5.0:
        raise ValueError("gateway jobs must have a five-second root duration")
    if "shot_id" in json.dumps(job.document):
        raise ValueError("gateway jobs must be story-level and omit shot_id")
    if job.backend == "kling":
        return build_kling_t2v(job.prompt, duration=5)
    if job.backend == "seedance":
        return build_seedance_t2v(job.prompt, duration=5)
    raise ValueError(f"unsupported API gateway backend: {job.backend}")


def prepare_api_job(job: ExperimentJob, output_dir: Path | str) -> PreparedApiJob:
    """Create a new, offline prepared API job without reading credentials."""
    destination = Path(output_dir)
    if destination.exists():
        raise ValueError(f"prepared API job already exists: {destination}")
    payload = _payload_for(job)
    destination.mkdir(parents=True, exist_ok=False)
    request = {"payload": payload}
    request_path = destination / "request.json"
    _write_json(request_path, request)
    request_sha256 = _file_sha256(request_path)
    job_id = f"{job.story_id}__{job.backend}"
    source = _self_hashed(
        {
            "schema_version": "1.0",
            "job_id": job_id,
            # The full immutable job input is the matrix-bound item available
            # at this API adapter boundary.
            "matrix_sha256": _canonical_sha256(
                {"job_id": job_id, "release": job.release, "document": job.document}
            ),
            "release": job.release,
            "budgets": _BUDGETS,
            "request_sha256": request_sha256,
            "job_document_sha256": _canonical_sha256(job.document),
        }
    )
    source_path = destination / "source_snapshot.json"
    _write_json(source_path, source)
    metadata = _self_hashed(
        {
            "schema_version": "1.0",
            "backend": job.backend,
            "story_id": job.story_id,
            "conditioning_mode": "prompt_only",
            "submit_retry_limit": 0,
            "query_limit": 4,
            "download_limit": 1,
            "request_sha256": request_sha256,
            "source_snapshot_sha256": _file_sha256(source_path),
        }
    )
    _write_json(destination / "metadata.json", metadata)
    _write_json(
        destination / _STATE_FILE,
        {
            "generation_submission_count": 0,
            "status_query_count": 0,
            "download_count": 0,
            "task_id": None,
            "status": "prepared",
        },
    )
    return PreparedApiJob(destination, job.backend, "prompt_only", payload, request_sha256)


def _load_prepared(job_dir: Path | str) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    path = Path(job_dir)
    request_path = path / "request.json"
    request = _read_json(request_path, "request")
    metadata = _read_json(path / "metadata.json", "metadata")
    source = _read_json(path / "source_snapshot.json", "source snapshot")
    state = _read_json(path / _STATE_FILE, "state")
    _check_self_hash(metadata, "metadata")
    _check_self_hash(source, "source snapshot")
    request_sha256 = _file_sha256(request_path)
    if metadata.get("request_sha256") != request_sha256 or source.get("request_sha256") != request_sha256:
        raise ReleaseError("snapshot hash mismatch: request")
    if metadata.get("source_snapshot_sha256") != _file_sha256(path / "source_snapshot.json"):
        raise ReleaseError("snapshot hash mismatch: source snapshot")
    if metadata.get("backend") not in {"kling", "seedance"} or metadata.get("conditioning_mode") != "prompt_only":
        raise ReleaseError("prepared job metadata is invalid")
    if metadata.get("submit_retry_limit") != 0 or metadata.get("query_limit") != 4 or metadata.get("download_limit") != 1:
        raise ReleaseError("prepared job attempt limits are invalid")
    if source.get("budgets") != _BUDGETS or source.get("release") not in {"canary", "remainder"}:
        raise ReleaseError("prepared job source snapshot is invalid")
    payload = request.get("payload")
    if not isinstance(payload, dict) or "shot_id" in json.dumps(payload):
        raise ReleaseError("prepared job request is invalid")
    return path, request, metadata, source, state


def _approve(path: Path, source: dict[str, Any], release: str) -> None:
    if release != source.get("release"):
        raise ReleaseError("release is not approved for this job")
    token_path = path.parent / f"release_{release}.json"
    token = _read_json(token_path, "release approval")
    if (
        token.get("schema_version") != "1.0"
        or token.get("matrix_sha256") != source.get("matrix_sha256")
        or token.get("release") != release
        or token.get("budgets") != source.get("budgets")
    ):
        raise ReleaseError("release is not approved for this matrix and budget")


def _recheck_active_release(path: Path, source: dict[str, Any], state: dict[str, Any]) -> None:
    """Keep later network actions tied to the approval used for submission."""
    release = state.get("release")
    if release is not None:
        if not isinstance(release, str):
            raise ReleaseError("release is not approved for this job")
        _approve(path, source, release)


def _state_path(path: Path) -> Path:
    return path / _STATE_FILE


def _write_state(path: Path, state: dict[str, Any]) -> None:
    _write_json(_state_path(path), state)


def _attempt_dir(path: Path, operation: str, count: int) -> Path:
    target = path / "attempts" / f"{operation}_{count}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def _safe_failure(exc: Exception) -> dict[str, str]:
    message = str(exc)
    for name in ("JD_KLING_KEY",):
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "[REDACTED]")
    return {"type": type(exc).__name__, "message": message}


def _default_submit(payload: dict[str, Any], attempt: RunDirectory) -> str:
    return submit_once(
        payload,
        os.environ.get("JD_KLING_KEY", ""),
        os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com"),
        attempt,
    )


def submit_prepared_job(
    job_dir: Path | str,
    release: str,
    transport: Callable[[dict[str, Any], str, str, RunDirectory], str] = submit_once,
) -> str:
    """Submit exactly once after validating an explicit matrix-bound approval."""
    path, request, _metadata, source, state = _load_prepared(job_dir)
    _approve(path, source, release)
    if state.get("generation_submission_count") != 0:
        raise ReleaseError("submission already attempted")
    count = 1
    attempt_path = _attempt_dir(path, "submit", count)
    attempt = RunDirectory(attempt_path)
    state["generation_submission_count"] = count
    _write_state(path, state)
    try:
        if transport is submit_once:
            task_id = _default_submit(request["payload"], attempt)
            remote_state = _read_json(attempt_path / "state.json", "submit attempt state")
            state["base_url"] = remote_state["base_url"]
        else:
            task_id = transport(request["payload"], "", "", attempt)
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("submit transport did not return a task ID")
        state.update({"task_id": task_id, "status": "submitted", "release": release})
        _write_json(attempt_path / "result.json", {"task_id": task_id})
        _write_state(path, state)
        return task_id
    except Exception as exc:
        _write_json(attempt_path / "failure.json", _safe_failure(exc))
        _write_state(path, state)
        raise


def query_prepared_job(
    job_dir: Path | str,
    transport: Callable[[str, RunDirectory], dict[str, Any]] = query_once,
) -> dict[str, Any]:
    """Issue at most four recorded status queries, with no retry loop."""
    path, _request, metadata, source, state = _load_prepared(job_dir)
    _recheck_active_release(path, source, state)
    count = state.get("status_query_count")
    if not isinstance(count, int) or count >= metadata["query_limit"]:
        raise RuntimeError("query limit reached")
    count += 1
    attempt_path = _attempt_dir(path, "query", count)
    attempt = RunDirectory(attempt_path)
    state["status_query_count"] = count
    _write_state(path, state)
    try:
        if transport is query_once:
            if not isinstance(state.get("base_url"), str) or not isinstance(state.get("task_id"), str):
                raise RuntimeError("submitted task state is required before querying")
            _write_json(attempt_path / "state.json", {"base_url": state["base_url"], "task_id": state["task_id"], "video_urls": []})
            response = query_once(os.environ.get("JD_KLING_KEY", ""), attempt)
        else:
            response = transport("", attempt)
        if not isinstance(response, dict):
            raise RuntimeError("query transport did not return an object")
        state["status"] = extract_status(response)
        state["video_urls"] = [{"id": ident, "url": url} for ident, url in extract_video_urls(response)]
        _write_json(attempt_path / "response.json", response)
        _write_state(path, state)
        return response
    except Exception as exc:
        _write_json(attempt_path / "failure.json", _safe_failure(exc))
        _write_state(path, state)
        raise


def download_prepared_job(
    job_dir: Path | str,
    transport: Callable[[RunDirectory], Path] = download_once,
) -> Path:
    """Download at most one result and persist its attempt outcome."""
    path, _request, metadata, source, state = _load_prepared(job_dir)
    _recheck_active_release(path, source, state)
    count = state.get("download_count")
    if not isinstance(count, int) or count >= metadata["download_limit"]:
        raise RuntimeError("download limit reached")
    count += 1
    attempt_path = _attempt_dir(path, "download", count)
    attempt = RunDirectory(attempt_path)
    state["download_count"] = count
    _write_state(path, state)
    try:
        if transport is download_once:
            _write_json(attempt_path / "state.json", {"video_urls": state.get("video_urls", [])})
            result = download_once(attempt)
        else:
            result = transport(attempt)
        result_path = Path(result)
        if not result_path.is_file():
            raise RuntimeError("download transport did not create a result file")
        _write_json(attempt_path / "result.json", {"path": result_path.name, "sha256": _file_sha256(result_path)})
        _write_state(path, state)
        return result_path
    except Exception as exc:
        _write_json(attempt_path / "failure.json", _safe_failure(exc))
        _write_state(path, state)
        raise
