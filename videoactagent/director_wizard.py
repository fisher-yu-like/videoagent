"""Prompt-first director wizard layered over the existing multicam director."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import wraps
import hashlib
from http import HTTPStatus
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Lock, RLock, Thread
from typing import Any, Callable
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from videoactagent.director_multicam import (
    DirectorMulticamError,
    _Handler as _MulticamHandler,
    prepare_workspace,
    save_staging,
    session_document as multicam_session_document,
)
from videoactagent.scene_plan import ScenePlanDraft, ScenePlanError


SCHEMA_VERSION = "director-wizard-1.0"
_SCENE_PLAN_ID = re.compile(r"SP[1-9][0-9]*")
_REFERENCE_ID = re.compile(r"R[1-9][0-9]*")
_JOBS: dict[str, dict[str, object]] = {}
_JOBS_LOCK = Lock()
_WORKSPACE_LOCKS: dict[Path, RLock] = {}
_WORKSPACE_LOCKS_LOCK = Lock()


class DirectorWizardError(ValueError):
    """Raised when wizard state, version gates, or evidence are invalid."""


def _workspace_lock(manifest_path: Path | str) -> RLock:
    manifest = Path(manifest_path).resolve(strict=True)
    with _WORKSPACE_LOCKS_LOCK:
        lock = _WORKSPACE_LOCKS.get(manifest)
        if lock is None:
            lock = RLock()
            _WORKSPACE_LOCKS[manifest] = lock
        return lock


def _locked_workspace(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def locked(manifest_path: Path | str, *args: object, **kwargs: object) -> Any:
        with _workspace_lock(manifest_path):
            return function(manifest_path, *args, **kwargs)

    return locked


def request_scene_plan(**kwargs: object) -> dict[str, Any]:
    """Late-bind the planner so this layer stays importable during adapter upgrades."""
    from videoactagent.deepseek_planner import request_scene_plan as planner

    return planner(**kwargs)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_json_bytes(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DirectorWizardError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise DirectorWizardError(f"{label} must be one JSON object")
    return dict(value)


def _record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha(path),
    }


def _verify_record(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "bytes", "sha256"}:
        raise DirectorWizardError(f"{label} binding is invalid")
    relative = value.get("path")
    if not isinstance(relative, str):
        raise DirectorWizardError(f"{label} binding is invalid")
    path = (root / relative).resolve(strict=True)
    if root != path and root not in path.parents:
        raise DirectorWizardError(f"{label} escapes workspace")
    if (
        not path.is_file()
        or path.stat().st_size != value.get("bytes")
        or _sha(path) != value.get("sha256")
    ):
        raise DirectorWizardError(f"{label} binding mismatch")
    return path


def _workspace(manifest_path: Path | str) -> dict[str, Any]:
    manifest = Path(manifest_path).resolve(strict=True)
    root = manifest.parent
    document = _read(manifest, "wizard manifest")
    state = _read(root / "state.json", "wizard state")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or state.get("schema_version") != SCHEMA_VERSION
    ):
        raise DirectorWizardError("wizard workspace schema is invalid")
    blender = Path(str(document.get("blender_path", ""))).resolve(strict=True)
    if not blender.is_file():
        raise DirectorWizardError("workspace Blender executable is missing")
    return {
        "manifest": manifest,
        "root": root,
        "document": document,
        "state": state,
        "blender": blender,
    }


def create_workspace(blender_path: Path | str, output_dir: Path | str) -> Path:
    blender = Path(blender_path).resolve(strict=True)
    if not blender.is_file():
        raise DirectorWizardError("Blender executable must be an existing file")
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise DirectorWizardError(f"output already exists: {output}")
    staging = output.parent / f".{output.name}.{uuid4().hex}.staging"
    try:
        staging.mkdir(parents=True)
        manifest = staging / "wizard_manifest.json"
        _write(
            manifest,
            {
                "schema_version": SCHEMA_VERSION,
                "blender_path": str(blender),
                "created_at": _now(),
            },
        )
        _write(
            staging / "state.json",
            {
                "schema_version": SCHEMA_VERSION,
                "current_scene_plan": None,
                "approved_scene_plan": None,
                "current_reference": None,
                "approved_reference": None,
                "next_scene_plan": 1,
                "next_reference": 1,
            },
        )
        os.replace(staging, output)
        result = output / manifest.name
        _workspace(result)
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _scene_record(workspace: Mapping[str, Any], scene_plan_id: str) -> dict[str, Any]:
    if not _SCENE_PLAN_ID.fullmatch(scene_plan_id):
        raise DirectorWizardError("scene plan ID is invalid")
    record = _read(
        workspace["root"] / "scene_plans" / scene_plan_id / "record.json",
        f"{scene_plan_id} record",
    )
    if record.get("scene_plan_id") != scene_plan_id:
        raise DirectorWizardError("scene plan record identity mismatch")
    return record


def _scene_sources(
    workspace: Mapping[str, Any], record: Mapping[str, Any]
) -> tuple[Path, Path, Path, Path]:
    root = workspace["root"]
    return (
        _verify_record(root, record.get("prompt"), "scene plan prompt"),
        _verify_record(root, record.get("draft"), "scene plan draft"),
        _verify_record(root, record.get("shotscript"), "scene plan ShotScript"),
        _verify_record(root, record.get("trajectory"), "scene plan trajectory"),
    )


@_locked_workspace
def save_scene_plan(
    manifest_path: Path | str,
    draft: object,
    *,
    prompt: str,
    parent: str | None,
) -> dict[str, Any]:
    workspace = _workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    story_prompt = prompt.strip() if isinstance(prompt, str) else ""
    if not story_prompt:
        raise DirectorWizardError("story prompt is empty")
    if parent is not None:
        parent_record = _scene_record(workspace, parent)
        if parent_record.get("stale"):
            raise DirectorWizardError("parent scene plan is stale")
    try:
        plan = ScenePlanDraft.from_dict(draft)
    except ScenePlanError as exc:
        raise DirectorWizardError(str(exc)) from exc
    scene_plan_id = f"SP{state['next_scene_plan']}"
    plans = root / "scene_plans"
    plans.mkdir(parents=True, exist_ok=True)
    directory = plans / scene_plan_id
    staging = plans / f".{scene_plan_id}.{uuid4().hex}.staging"
    published = False
    try:
        staging.mkdir(parents=False, exist_ok=False)
        prompt_path = staging / "prompt.txt"
        prompt_path.write_text(story_prompt + "\n", encoding="utf-8")
        draft_path = staging / "draft.json"
        shotscript_path = staging / "shotscript.json"
        trajectory_path = staging / "trajectory.json"
        _write(draft_path, plan.to_dict())
        _write(shotscript_path, plan.to_shotscript(story_prompt))
        _write(trajectory_path, plan.to_trajectory())

        def future_record(path: Path) -> dict[str, object]:
            value = _record(path, root)
            value["path"] = (directory / path.name).relative_to(root).as_posix()
            return value

        record = {
            "schema_version": SCHEMA_VERSION,
            "scene_plan_id": scene_plan_id,
            "parent": parent,
            "stale": False,
            "source": "local",
            "prompt": future_record(prompt_path),
            "draft": future_record(draft_path),
            "shotscript": future_record(shotscript_path),
            "trajectory": future_record(trajectory_path),
            "generation_evidence": None,
            "created_at": _now(),
        }
        record_path = staging / "record.json"
        _write(record_path, record)
        persisted = _read(record_path, f"{scene_plan_id} staged record")
        if persisted != record:
            raise DirectorWizardError("staged scene plan record mismatch")
        for name, path in (
            ("prompt", prompt_path),
            ("draft", draft_path),
            ("shotscript", shotscript_path),
            ("trajectory", trajectory_path),
        ):
            binding = record[name]
            if (
                binding["bytes"] != path.stat().st_size
                or binding["sha256"] != _sha(path)
            ):
                raise DirectorWizardError(f"staged scene plan {name} binding mismatch")
        os.replace(staging, directory)
        published = True
        published_record = _scene_record(workspace, scene_plan_id)
        _scene_sources(workspace, published_record)
        state.update(
            {
                "current_scene_plan": scene_plan_id,
                "next_scene_plan": state["next_scene_plan"] + 1,
            }
        )
        _write(root / "state.json", state)
        return record
    except BaseException:
        shutil.rmtree(directory if published else staging, ignore_errors=True)
        raise


@_locked_workspace
def generate_scene_plan(
    manifest_path: Path | str,
    prompt: str,
    duration_seconds: float,
    *,
    feedback: str | None = None,
) -> dict[str, Any]:
    workspace = _workspace(manifest_path)
    state, root = workspace["state"], workspace["root"]
    parent = state.get("current_scene_plan")
    previous_draft = None
    if parent is not None:
        previous = _scene_record(workspace, str(parent))
        _unused_prompt, draft_path, _unused_script, _unused_trajectory = _scene_sources(
            workspace, previous
        )
        previous_draft = _read(draft_path, "previous scene plan draft")
    generation = root / ".generations" / f"SP{state['next_scene_plan']}-{uuid4().hex}"
    evidence = request_scene_plan(
        story_prompt=prompt,
        duration_seconds=duration_seconds,
        output_dir=generation,
        feedback=feedback,
        previous_draft=previous_draft,
    )
    draft_path = generation / "draft.json"
    draft = _read(draft_path, "generated scene plan draft")
    record = save_scene_plan(
        manifest_path, draft, prompt=prompt, parent=str(parent) if parent else None
    )
    workspace = _workspace(manifest_path)
    record_path = (
        workspace["root"] / "scene_plans" / record["scene_plan_id"] / "record.json"
    )
    record = _read(record_path, f"{record['scene_plan_id']} record")
    evidence_path = generation / "evidence.json"
    if not evidence_path.is_file():
        _write(evidence_path, evidence)
    target = record_path.parent / "generation"
    os.replace(generation, target)
    record["source"] = "agent"
    record["generation_evidence"] = _record(target / "evidence.json", workspace["root"])
    _write(record_path, record)
    return record


@_locked_workspace
def approve_scene_plan(
    manifest_path: Path | str, scene_plan_id: str, *, author_id: str
) -> Path:
    workspace = _workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    author = author_id.strip() if isinstance(author_id, str) else ""
    if not author:
        raise DirectorWizardError("scene plan approval requires a named human")
    record = _scene_record(workspace, scene_plan_id)
    if record.get("stale"):
        raise DirectorWizardError("stale scene plan cannot be approved")
    _prompt, draft, shotscript, trajectory = _scene_sources(workspace, record)
    approval = root / "scene_plans" / scene_plan_id / "approval.json"
    if approval.exists():
        raise DirectorWizardError("scene plan is already approved")
    _write(
        approval,
        {
            "schema_version": SCHEMA_VERSION,
            "scene_plan_id": scene_plan_id,
            "draft_sha256": _sha(draft),
            "shotscript_sha256": _sha(shotscript),
            "trajectory_sha256": _sha(trajectory),
            "author_id": author,
            "approved_at": _now(),
        },
    )
    approved_number = int(scene_plan_id.removeprefix("SP"))
    for path in (root / "scene_plans").glob("SP*/record.json"):
        candidate = _read(path, "scene plan record")
        candidate_id = str(candidate.get("scene_plan_id", ""))
        if _SCENE_PLAN_ID.fullmatch(candidate_id) and int(candidate_id[2:]) > approved_number:
            candidate["stale"] = True
            _write(path, candidate)
    state.update(
        {
            "current_scene_plan": scene_plan_id,
            "approved_scene_plan": scene_plan_id,
            "current_reference": None,
            "approved_reference": None,
        }
    )
    _write(root / "state.json", state)
    return approval


def _approved_scene(
    workspace: Mapping[str, Any], scene_plan_id: str
) -> tuple[dict[str, Any], Path, Path, Path, Path]:
    state, root = workspace["state"], workspace["root"]
    if (
        state.get("approved_scene_plan") != scene_plan_id
        or state.get("current_scene_plan") != scene_plan_id
    ):
        raise DirectorWizardError("reference render requires the current approved scene plan")
    record = _scene_record(workspace, scene_plan_id)
    prompt, draft, shotscript, trajectory = _scene_sources(workspace, record)
    approval = _read(
        root / "scene_plans" / scene_plan_id / "approval.json",
        "scene plan approval",
    )
    if (
        approval.get("draft_sha256") != _sha(draft)
        or approval.get("shotscript_sha256") != _sha(shotscript)
        or approval.get("trajectory_sha256") != _sha(trajectory)
    ):
        raise DirectorWizardError("scene plan approval binding mismatch")
    return record, prompt, draft, shotscript, trajectory


def _reference_renderer(trajectory: Path) -> Callable[..., Path]:
    def render(
        *,
        blender: Path,
        shotscript: Path,
        output_dir: Path,
        fps: int,
        resolution: tuple[int, int],
    ) -> Path:
        command = [
            sys.executable,
            "-m",
            "videoactagent.blender_runner",
            "--blender",
            str(blender),
            "--shotscript",
            str(shotscript),
            "--trajectory",
            str(trajectory),
            "--output-dir",
            str(output_dir),
            "--render-style",
            "diagnostic",
            "--fps",
            str(fps),
            "--resolution",
            f"{resolution[0]}x{resolution[1]}",
            "--timeout",
            "300",
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=330,
        )
        (output_dir.parent / "reference.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
        if (
            completed.returncode != 0
            or "TRAJECTORY_PROXY_OK" not in completed.stdout + completed.stderr
        ):
            raise DirectorWizardError("initial real Blender reference render failed")
        source = output_dir / "trajectory_proxy.mp4"
        if not source.is_file():
            raise DirectorWizardError("initial Blender reference video is missing")
        target = output_dir / "reference.mp4"
        source.replace(target)
        return target

    return render


@_locked_workspace
def render_reference(
    manifest_path: Path | str, scene_plan_id: str
) -> dict[str, Any]:
    workspace = _workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    _scene, prompt, draft, shotscript, trajectory = _approved_scene(
        workspace, scene_plan_id
    )
    reference_id = f"R{state['next_reference']}"
    target = root / "references" / reference_id
    staging = target.parent / f".{reference_id}.{uuid4().hex}.staging"
    try:
        staging.mkdir(parents=True)
        pipeline_manifest = prepare_workspace(
            prompt,
            shotscript,
            workspace["blender"],
            staging / "pipeline",
            reference_renderer=_reference_renderer(trajectory),
        )
        trajectory_value = _read(trajectory, "scene plan trajectory")
        staging_record = save_staging(
            pipeline_manifest, trajectory_value, base_staging_id="S1"
        )
        video = pipeline_manifest.parent / "reference" / "reference.mp4"
        record = {
            "schema_version": SCHEMA_VERSION,
            "reference_id": reference_id,
            "scene_plan_id": scene_plan_id,
            "staging_id": staging_record["staging_id"],
            "stale": False,
            "scene_plan_draft_sha256": _sha(draft),
            "pipeline_manifest": _record(pipeline_manifest, staging),
            "video": _record(video, staging),
            "created_at": _now(),
        }
        _write(staging / "record.json", record)
        os.replace(staging, target)
        state.update(
            {
                "current_reference": reference_id,
                "approved_reference": None,
                "next_reference": state["next_reference"] + 1,
            }
        )
        _write(root / "state.json", state)
        return record
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _reference_record(
    workspace: Mapping[str, Any], reference_id: str
) -> tuple[dict[str, Any], Path, Path]:
    if not _REFERENCE_ID.fullmatch(reference_id):
        raise DirectorWizardError("reference ID is invalid")
    root = workspace["root"]
    record = _read(root / "references" / reference_id / "record.json", "reference record")
    if record.get("reference_id") != reference_id:
        raise DirectorWizardError("reference record identity mismatch")
    manifest = _verify_record(root / "references" / reference_id, record.get("pipeline_manifest"), "reference pipeline")
    video = _verify_record(root / "references" / reference_id, record.get("video"), "reference video")
    return record, manifest, video


@_locked_workspace
def approve_reference(
    manifest_path: Path | str, reference_id: str, *, author_id: str
) -> Path:
    workspace = _workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    author = author_id.strip() if isinstance(author_id, str) else ""
    if not author:
        raise DirectorWizardError("reference approval requires a named human")
    if state.get("current_reference") != reference_id:
        raise DirectorWizardError("only the current reference can be approved")
    record, pipeline_manifest, video = _reference_record(workspace, reference_id)
    scene_plan_id = str(record.get("scene_plan_id", ""))
    _scene, _prompt, draft, _shotscript, _trajectory = _approved_scene(
        workspace, scene_plan_id
    )
    if record.get("stale") or record.get("scene_plan_draft_sha256") != _sha(draft):
        raise DirectorWizardError("reference source binding mismatch")
    approval = root / "references" / reference_id / "approval.json"
    if approval.exists():
        raise DirectorWizardError("reference is already approved")
    _write(
        approval,
        {
            "schema_version": SCHEMA_VERSION,
            "reference_id": reference_id,
            "scene_plan_id": scene_plan_id,
            "pipeline_manifest_sha256": _sha(pipeline_manifest),
            "video_sha256": _sha(video),
            "author_id": author,
            "approved_at": _now(),
        },
    )
    state["approved_reference"] = reference_id
    _write(root / "state.json", state)
    return approval


def _scene_document(workspace: Mapping[str, Any], scene_plan_id: str) -> dict[str, Any]:
    record = _scene_record(workspace, scene_plan_id)
    prompt, draft, _shotscript, _trajectory = _scene_sources(workspace, record)
    evidence: dict[str, Any] = {
        "status": "local", "api_call_count": 0, "source": "local_form"
    }
    if record.get("generation_evidence") is not None:
        evidence_path = _verify_record(
            workspace["root"], record["generation_evidence"], "generation evidence"
        )
        evidence = _read(evidence_path, "generation evidence")
    return {
        "scene_plan_id": scene_plan_id,
        "parent": record.get("parent"),
        "source": record.get("source"),
        "stale": bool(record.get("stale")),
        "approved": workspace["state"].get("approved_scene_plan") == scene_plan_id,
        "prompt": prompt.read_text(encoding="utf-8").strip(),
        "draft": _read(draft, "scene plan draft"),
        "source_hash": record["draft"]["sha256"],
        "evidence": evidence,
    }


def _evidence_api_call_count(path: Path, label: str) -> int:
    evidence = _read(path, label)
    count = evidence.get("api_call_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DirectorWizardError(f"{label} api_call_count is invalid")
    return count


def _scene_api_call_count(workspace: Mapping[str, Any]) -> int:
    total = 0
    for path in (workspace["root"] / "scene_plans").glob("SP*/record.json"):
        scene_plan_id = path.parent.name
        record = _scene_record(workspace, scene_plan_id)
        if record.get("generation_evidence") is None:
            continue
        evidence = _verify_record(
            workspace["root"],
            record["generation_evidence"],
            f"{scene_plan_id} generation evidence",
        )
        total += _evidence_api_call_count(
            evidence, f"{scene_plan_id} generation evidence"
        )
    return total


def _pipeline_api_call_count(pipeline_manifest: Path) -> int:
    root = pipeline_manifest.parent
    total = 0
    for path in (root / "plans").glob("P*/record.json"):
        plan_id = path.parent.name
        if not re.fullmatch(r"P[1-9][0-9]*", plan_id):
            raise DirectorWizardError("camera plan ID is invalid")
        record = _read(path, f"{plan_id} camera plan record")
        if record.get("plan_id") != plan_id:
            raise DirectorWizardError("camera plan record identity mismatch")
        evidence = _verify_record(
            root, record.get("evidence"), f"{plan_id} camera plan evidence"
        )
        total += _evidence_api_call_count(
            evidence, f"{plan_id} camera plan evidence"
        )
    return total


def session_document(manifest_path: Path | str) -> dict[str, object]:
    workspace = _workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    current_scene = state.get("current_scene_plan")
    current_reference = state.get("current_reference")
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "current_scene_plan": current_scene,
        "approved_scene_plan": state.get("approved_scene_plan"),
        "current_reference": current_reference,
        "approved_reference": state.get("approved_reference"),
        "workflow_step": "prompt_entry" if current_scene is None else "scene_plan_review",
        "api_call_count": _scene_api_call_count(workspace),
    }
    if current_scene is not None:
        scene = _scene_document(workspace, str(current_scene))
        result["scene_plan"] = scene
        result["story_prompt"] = scene["prompt"]
        result["duration_seconds"] = scene["draft"]["duration_seconds"]
    if state.get("approved_scene_plan") is not None:
        result["workflow_step"] = "reference_review"
    if current_reference is not None:
        record, _pipeline, _video = _reference_record(workspace, str(current_reference))
        result["reference"] = {
            "reference_id": current_reference,
            "scene_plan_id": record["scene_plan_id"],
            "stale": bool(record.get("stale")),
            "approved": state.get("approved_reference") == current_reference,
            "video_url": f"/references/{current_reference}/reference.mp4",
            "source_hash": record["video"]["sha256"],
        }
    approved_reference = state.get("approved_reference")
    if approved_reference is not None:
        record, pipeline_manifest, _video = _reference_record(
            workspace, str(approved_reference)
        )
        downstream = multicam_session_document(pipeline_manifest)
        result.update(downstream)
        result.update(
            {
                "schema_version": SCHEMA_VERSION,
                "current_scene_plan": current_scene,
                "approved_scene_plan": state.get("approved_scene_plan"),
                "current_reference": current_reference,
                "approved_reference": approved_reference,
                "scene_plan": _scene_document(workspace, str(current_scene)),
                "reference": {
                    "reference_id": approved_reference,
                    "scene_plan_id": record["scene_plan_id"],
                    "stale": False,
                    "approved": True,
                    "video_url": f"/references/{approved_reference}/reference.mp4",
                    "source_hash": record["video"]["sha256"],
                },
                "reference_url": f"/references/{approved_reference}/reference.mp4",
                "story_prompt": result["story_prompt"],
                "duration_seconds": result["duration_seconds"],
                "api_call_count": result["api_call_count"]
                + _pipeline_api_call_count(pipeline_manifest),
            }
        )
        if (
            downstream.get("workflow_step") == "result"
            and downstream.get("current_iteration") is not None
            and downstream.get("approved_iteration") == downstream.get("current_iteration")
        ):
            result["workflow_step"] = "complete"
    return result


def _job_key(manifest: Path, job_id: str) -> str:
    return f"{manifest}:{job_id}"


def _job_value(manifest: Path, job_id: str) -> dict[str, object]:
    with _JOBS_LOCK:
        value = _JOBS.get(_job_key(manifest, job_id))
        if value is None:
            raise DirectorWizardError("reference job does not exist")
        return dict(value)


def _update_job(manifest: Path, job_id: str, **changes: object) -> None:
    with _JOBS_LOCK:
        key = _job_key(manifest, job_id)
        if key not in _JOBS:
            raise DirectorWizardError("reference job does not exist")
        _JOBS[key].update(changes)


def _run_reference_job(manifest: Path, scene_plan_id: str, job_id: str) -> None:
    _update_job(manifest, job_id, status="rendering", started_at=_now())
    try:
        reference = render_reference(manifest, scene_plan_id)
        _update_job(
            manifest,
            job_id,
            status="succeeded",
            reference_id=reference["reference_id"],
            finished_at=_now(),
            error=None,
        )
    except Exception as exc:
        _update_job(
            manifest,
            job_id,
            status="failed",
            finished_at=_now(),
            error=f"{type(exc).__name__}: {exc}",
        )


class _Handler(_MulticamHandler):
    wizard_manifest: Path

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024 * 1024:
            raise DirectorWizardError("invalid request size")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DirectorWizardError(f"request body is not JSON: {exc}") from exc
        if not isinstance(value, Mapping):
            raise DirectorWizardError("request body must be one object")
        return dict(value)

    def _downstream(self) -> None:
        workspace = _workspace(self.wizard_manifest)
        approved = workspace["state"].get("approved_reference")
        if approved is None:
            raise DirectorWizardError("reference must be approved first")
        _record_value, manifest, _video = _reference_record(workspace, str(approved))
        self.manifest = manifest
        self.root = manifest.parent

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._send(self.html, "text/html; charset=utf-8")
                return
            if path == "/api/session":
                self._json(session_document(self.wizard_manifest))
                return
            match = re.fullmatch(r"/api/jobs/(reference-R[1-9][0-9]*)", path)
            if match:
                self._json(_job_value(self.wizard_manifest, match.group(1)))
                return
            match = re.fullmatch(
                r"/references/(R[1-9][0-9]*)/reference\.mp4", path
            )
            if match:
                workspace = _workspace(self.wizard_manifest)
                _record_value, _manifest, video = _reference_record(
                    workspace, match.group(1)
                )
                self._video(video)
                return
            if (
                path == "/reference/reference.mp4"
                or re.fullmatch(r"/api/jobs/render-M[1-9][0-9]*", path)
                or path.startswith("/media/")
            ):
                self._downstream()
                super().do_GET()
                return
            self._json({"error": "route not found"}, HTTPStatus.NOT_FOUND)
        except (
            DirectorWizardError, DirectorMulticamError, OSError, TypeError, ValueError
        ) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            payload = self._payload()
            if path == "/api/scene-plans" and set(payload) <= {
                "story_prompt",
                "duration_seconds",
                "feedback",
                "previous_draft",
            } and {"story_prompt", "duration_seconds"} <= set(payload):
                record = generate_scene_plan(
                    self.wizard_manifest,
                    str(payload["story_prompt"]),
                    float(payload["duration_seconds"]),
                    feedback=(
                        str(payload["feedback"])
                        if payload.get("feedback") is not None
                        else None
                    ),
                )
                self._json(record, HTTPStatus.CREATED)
                return
            match = re.fullmatch(r"/api/scene-plans/(SP[1-9][0-9]*)/save", path)
            if match and set(payload) <= {
                "draft",
                "story_prompt",
                "parent_scene_plan_id",
            } and {"draft", "story_prompt"} <= set(payload):
                parent = payload.get("parent_scene_plan_id", match.group(1))
                if str(parent) != match.group(1):
                    raise DirectorWizardError("scene plan save parent does not match URL")
                record = save_scene_plan(
                    self.wizard_manifest,
                    payload["draft"],
                    prompt=str(payload["story_prompt"]),
                    parent=match.group(1),
                )
                self._json(record, HTTPStatus.CREATED)
                return
            match = re.fullmatch(r"/api/scene-plans/(SP[1-9][0-9]*)/approve", path)
            if match and set(payload) == {"author_id"}:
                approval = approve_scene_plan(
                    self.wizard_manifest,
                    match.group(1),
                    author_id=str(payload["author_id"]),
                )
                self._json({"approved": True, "approval_sha256": _sha(approval)})
                return
            match = re.fullmatch(r"/api/scene-plans/(SP[1-9][0-9]*)/render", path)
            if match and not payload:
                workspace = _workspace(self.wizard_manifest)
                _approved_scene(workspace, match.group(1))
                reference_id = f"R{workspace['state']['next_reference']}"
                job_id = f"reference-{reference_id}"
                key = _job_key(self.wizard_manifest, job_id)
                with _JOBS_LOCK:
                    if key in _JOBS and _JOBS[key].get("status") in {"queued", "rendering"}:
                        raise DirectorWizardError("reference render is already running")
                    _JOBS[key] = {
                        "job_id": job_id,
                        "reference_id": reference_id,
                        "scene_plan_id": match.group(1),
                        "status": "queued",
                        "queued_at": _now(),
                        "error": None,
                    }
                Thread(
                    target=_run_reference_job,
                    args=(self.wizard_manifest, match.group(1), job_id),
                    daemon=True,
                ).start()
                self._json(
                    {
                        "job_id": job_id,
                        "reference_id": reference_id,
                        "status": "queued",
                    },
                    HTTPStatus.ACCEPTED,
                )
                return
            match = re.fullmatch(r"/api/references/(R[1-9][0-9]*)/approve", path)
            if match and set(payload) == {"author_id"}:
                approval = approve_reference(
                    self.wizard_manifest,
                    match.group(1),
                    author_id=str(payload["author_id"]),
                )
                self._json({"approved": True, "approval_sha256": _sha(approval)})
                return
            if (
                path == "/api/plans"
                or path == "/api/staging"
                or re.fullmatch(r"/api/staging/S[1-9][0-9]*/approve", path)
                or re.fullmatch(r"/api/plans/P[1-9][0-9]*/(?:approve|render)", path)
                or re.fullmatch(r"/api/iterations/M[1-9][0-9]*/approve", path)
            ):
                self._downstream()
                # The parent handler must parse the request body itself.
                encoded = _json_bytes(payload)
                from io import BytesIO

                self.rfile = BytesIO(encoded)
                self.headers.replace_header("Content-Length", str(len(encoded)))
                super().do_POST()
                return
            self._json({"error": "route not found"}, HTTPStatus.NOT_FOUND)
        except (
            DirectorWizardError, DirectorMulticamError, OSError, TypeError, ValueError
        ) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def create_server(
    manifest_path: Path | str, port: int = 8770
) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise DirectorWizardError("port must be in 0..65535")
    manifest = Path(manifest_path).resolve(strict=True)
    workspace = _workspace(manifest)

    class BoundHandler(_Handler):
        pass

    BoundHandler.wizard_manifest = manifest
    BoundHandler.manifest = manifest
    BoundHandler.root = workspace["root"]
    BoundHandler.html = (
        Path(__file__).resolve().parents[1] / "static" / "director_multicam_panel.html"
    ).read_bytes()
    return ThreadingHTTPServer(("127.0.0.1", port), BoundHandler)


def serve_workspace(manifest_path: Path | str, port: int = 8770) -> None:
    server = create_server(manifest_path, port)
    print(f"DIRECTOR_WIZARD_SERVING http://127.0.0.1:{server.server_address[1]}")
    server.serve_forever()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--blender", type=Path, required=True)
    start.add_argument("--workspace", type=Path, required=True)
    start.add_argument("--port", type=int, default=8770)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = args.workspace.resolve(strict=False)
        manifest = workspace / "wizard_manifest.json"
        if workspace.exists():
            current = _workspace(manifest)
            if current["blender"] != args.blender.resolve(strict=True):
                raise DirectorWizardError("existing workspace uses a different Blender path")
        else:
            manifest = create_workspace(args.blender, workspace)
        serve_workspace(manifest, args.port)
    except (
        DirectorWizardError, DirectorMulticamError, OSError, TypeError, ValueError
    ) as exc:
        print(f"DIRECTOR_WIZARD_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
