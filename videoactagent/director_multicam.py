"""Independent Agent-planned multicamera director workspace."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Thread
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from videoactagent.deepseek_planner import request_multicam_plan
from videoactagent.director_annotation import CameraKeyframe
from videoactagent.multicam_plan import MulticamPlanError, load_multicam_plan
from videoactagent.multicam_eval import evaluate_multicam_iteration
from videoactagent.multicam_rig import compile_camera_rig
from videoactagent.shotscript import ShotScript, ShotScriptError


SCHEMA_VERSION = "multicam-director-1.0"
_TIMES = (0.0, 0.2, 0.5, 0.8, 1.0)


class DirectorMulticamError(ValueError):
    """Raised when multicamera state or evidence is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _write(path: Path, value: object) -> None:
    data = _json_bytes(value)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _read(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DirectorMulticamError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise DirectorMulticamError(f"{label} must be one object")
    return value


def _record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
    }


def _verify_record(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        raise DirectorMulticamError(f"{label} record is invalid")
    relative = value.get("path")
    if not isinstance(relative, str):
        raise DirectorMulticamError(f"{label} path is invalid")
    path = (root / relative).resolve(strict=True)
    if root != path and root not in path.parents:
        raise DirectorMulticamError(f"{label} escapes workspace")
    if not path.is_file() or path.stat().st_size != value.get("bytes") or _sha(path) != value.get("sha256"):
        raise DirectorMulticamError(f"{label} binding mismatch")
    return path


def _default_reference_renderer(
    *, blender: Path, shotscript: Path, output_dir: Path,
    fps: int, resolution: tuple[int, int],
) -> Path:
    command = [
        sys.executable, "-m", "videoactagent.blender_runner",
        "--blender", str(blender), "--shotscript", str(shotscript),
        "--output-dir", str(output_dir), "--render-style", "diagnostic",
        "--fps", str(fps), "--resolution", f"{resolution[0]}x{resolution[1]}",
        "--timeout", "300",
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=330,
    )
    (output_dir.parent / "reference.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0 or "BLENDER_PROXY_OK" not in completed.stdout + completed.stderr:
        raise DirectorMulticamError("initial real Blender reference render failed")
    source = output_dir / "station_proxy.mp4"
    if not source.is_file():
        raise DirectorMulticamError("initial Blender reference video is missing")
    target = output_dir / "reference.mp4"
    source.replace(target)
    return target


def _actor_keyframes(script: ShotScript) -> list[dict[str, Any]]:
    if len(script.shots) != 1:
        raise DirectorMulticamError("multicam director requires one continuous shot")
    shot = script.shots[0]
    min_x, max_x, min_y, max_y = script.world_bounds
    frames = []
    for index, time in enumerate(_TIMES):
        actors = {}
        for actor in shot.actors:
            x = actor.start.x + (actor.end.x - actor.start.x) * time
            y = actor.start.y + (actor.end.y - actor.start.y) * time
            actors[actor.actor_id] = {
                "x": round((x - min_x) / (max_x - min_x), 12),
                "y": round((max_y - y) / (max_y - min_y), 12),
            }
        frames.append({"id": f"K{index}", "t": time, "actors": actors})
    return frames


def prepare_workspace(
    prompt_path: Path | str, shotscript_path: Path | str,
    blender_path: Path | str, output_dir: Path | str, *,
    reference_renderer: Callable[..., Path] = _default_reference_renderer,
) -> Path:
    prompt_source = Path(prompt_path).resolve(strict=True)
    shotscript_source = Path(shotscript_path).resolve(strict=True)
    blender = Path(blender_path).resolve(strict=True)
    if not prompt_source.is_file() or not shotscript_source.is_file() or not blender.is_file():
        raise DirectorMulticamError("prompt, ShotScript and Blender must be existing files")
    prompt = prompt_source.read_text(encoding="utf-8").strip()
    if not prompt:
        raise DirectorMulticamError("story prompt is empty")
    try:
        script = ShotScript.from_path(shotscript_source)
    except ShotScriptError as exc:
        raise DirectorMulticamError(str(exc)) from exc
    if len(script.shots) != 1:
        raise DirectorMulticamError("multicam director requires one whole-story shot")
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise DirectorMulticamError(f"output already exists: {output}")
    staging = output.parent / f".{output.name}.{uuid4().hex}.staging"
    try:
        source = staging / "source"
        source.mkdir(parents=True)
        prompt_snapshot = source / "prompt.txt"
        shotscript_snapshot = source / "shotscript.json"
        shutil.copyfile(prompt_source, prompt_snapshot)
        shutil.copyfile(shotscript_source, shotscript_snapshot)
        reference_dir = staging / "reference"
        reference = reference_renderer(
            blender=blender,
            shotscript=shotscript_snapshot,
            output_dir=reference_dir,
            fps=script.fps,
            resolution=(960, 540),
        )
        if not reference.is_file() or reference.stat().st_size == 0:
            raise DirectorMulticamError("reference renderer produced no video")
        frames = _actor_keyframes(script)
        manifest_value = {
            "schema_version": SCHEMA_VERSION,
            "story_id": script.scene_id,
            "shot_id": script.shots[0].shot_id,
            "actors": [actor.actor_id for actor in script.shots[0].actors],
            "world_bounds": list(script.world_bounds),
            "timeline": {
                "duration_seconds": script.shots[0].duration,
                "fps": script.fps,
                "frame_count": round(script.shots[0].duration * script.fps),
                "resolution": [960, 540],
            },
            "actor_keyframes": frames,
            "source": {
                "prompt": _record(prompt_snapshot, staging),
                "shotscript": _record(shotscript_snapshot, staging),
                "reference": _record(reference, staging),
            },
            "blender_path": str(blender),
        }
        manifest = staging / "multicam_manifest.json"
        _write(manifest, manifest_value)
        _write(staging / "state.json", {
            "schema_version": SCHEMA_VERSION,
            "current_plan": None,
            "approved_plan": None,
            "current_iteration": None,
            "approved_iteration": None,
            "next_plan": 1,
            "next_iteration": 1,
        })
        os.replace(staging, output)
        result = output / manifest.name
        verify_workspace(result)
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def prepare_suite(
    config_path: Path | str, blender_path: Path | str, output_dir: Path | str,
) -> Path:
    """Prepare six real reference Proxy workspaces without any LLM/API call."""
    config = Path(config_path).resolve(strict=True)
    blender = Path(blender_path).resolve(strict=True)
    value = _read(config, "multicam suite")
    cases = value.get("cases")
    if value.get("schema_version") != "multicam-suite-1.0" or not isinstance(cases, list):
        raise DirectorMulticamError("multicam suite config is invalid")
    scene_ids = [case.get("scene_id") for case in cases if isinstance(case, Mapping)]
    if len(cases) != 6 or len(scene_ids) != 6 or len(set(scene_ids)) != 6:
        raise DirectorMulticamError("multicam suite requires six unique scenes")
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise DirectorMulticamError(f"output already exists: {output}")
    staging = output.parent / f".{output.name}.{uuid4().hex}.staging"
    try:
        staging.mkdir(parents=True)
        records = []
        for case in cases:
            if not isinstance(case, Mapping) or set(case) != {"scene_id", "prompt", "shotscript"}:
                raise DirectorMulticamError("suite case fields are invalid")
            prompt = (config.parent / str(case["prompt"])).resolve(strict=True)
            shotscript = (config.parent / str(case["shotscript"])).resolve(strict=True)
            script = ShotScript.from_path(shotscript)
            if script.scene_id != case["scene_id"]:
                raise DirectorMulticamError("suite scene ID does not match its ShotScript")
            manifest = prepare_workspace(
                prompt, shotscript, blender, staging / script.scene_id,
            )
            records.append({
                "scene_id": script.scene_id,
                "manifest": manifest.relative_to(staging).as_posix(),
                "manifest_sha256": _sha(manifest),
            })
        suite_manifest = staging / "suite_manifest.json"
        _write(suite_manifest, {
            "schema_version": "multicam-suite-run-1.0",
            "prepared_at": _now(), "api_call_count": 0, "cases": records,
        })
        os.replace(staging, output)
        return output / suite_manifest.name
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_workspace(manifest_path: Path | str) -> dict[str, Any]:
    manifest = Path(manifest_path).resolve(strict=True)
    root = manifest.parent
    document = _read(manifest, "multicam manifest")
    state = _read(root / "state.json", "multicam state")
    if document.get("schema_version") != SCHEMA_VERSION or state.get("schema_version") != SCHEMA_VERSION:
        raise DirectorMulticamError("multicam workspace schema is invalid")
    for name in ("prompt", "shotscript", "reference"):
        _verify_record(root, document.get("source", {}).get(name), f"source {name}")
    if not Path(document.get("blender_path", "")).is_file():
        raise DirectorMulticamError("workspace Blender executable is missing")
    return {"root": root, "document": document, "state": state}


def _scene_context(workspace: Mapping[str, Any], locked: str | None) -> dict[str, Any]:
    document, root = workspace["document"], workspace["root"]
    prompt = _verify_record(root, document["source"]["prompt"], "source prompt")
    return {
        "scene_id": document["story_id"],
        "story_prompt": prompt.read_text(encoding="utf-8").strip(),
        "actors": document["actors"],
        "world_bounds": document["world_bounds"],
        "keyframes": document["actor_keyframes"],
        "locked_through_keyframe": locked,
    }


def create_plan(
    manifest_path: Path | str, *, locked_through_keyframe: str | None = None,
    planner: Callable[..., Mapping[str, Any]] = request_multicam_plan,
) -> dict[str, Any]:
    workspace = verify_workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    plan_id = f"P{state['next_plan']}"
    state["next_plan"] += 1
    _write(root / "state.json", state)
    directory = root / "plans" / plan_id
    evidence = planner(
        scene_context=_scene_context(workspace, locked_through_keyframe),
        output_dir=directory,
        environ=os.environ,
    )
    plan_path = directory / "plan.json"
    plan_value = _read(plan_path, f"{plan_id} plan")
    try:
        load_multicam_plan(
            plan_value,
            scene_id=workspace["document"]["story_id"],
            actors=workspace["document"]["actors"],
            locked_through_keyframe=locked_through_keyframe,
        )
    except MulticamPlanError as exc:
        raise DirectorMulticamError(str(exc)) from exc
    record = {
        "plan_id": plan_id,
        "plan": _record(plan_path, root),
        "evidence": _record(directory / "evidence.json", root),
        "status": evidence.get("status"),
    }
    _write(directory / "record.json", record)
    state = _read(root / "state.json", "multicam state")
    state["current_plan"] = plan_id
    state["approved_plan"] = None
    _write(root / "state.json", state)
    return record


def approve_plan(
    manifest_path: Path | str, plan_id: str, *, author_id: str,
) -> Path:
    workspace = verify_workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    if state.get("approved_plan") is not None:
        raise DirectorMulticamError("a plan is already approved")
    if state.get("current_plan") != plan_id or not author_id.strip():
        raise DirectorMulticamError("only the current plan can be approved by a named human")
    record = _read(root / "plans" / plan_id / "record.json", f"{plan_id} record")
    plan = _verify_record(root, record.get("plan"), f"{plan_id} plan")
    plan_value = _read(plan, f"{plan_id} plan")
    approval = root / "plans" / plan_id / "approval.json"
    if approval.exists():
        raise DirectorMulticamError("plan is already approved")
    _write(approval, {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_sha256": _sha(plan),
        "locked_through_keyframe": plan_value.get("locked_through_keyframe"),
        "author_id": author_id.strip(),
        "approved_at": _now(),
    })
    state["approved_plan"] = plan_id
    _write(root / "state.json", state)
    return approval


def _actor_trajectory(document: Mapping[str, Any]) -> dict[str, Any]:
    tracks = []
    for actor in document["actors"]:
        tracks.append({
            "track_id": f"actor_{actor}_path",
            "target": {"type": "actor", "id": actor},
            "primitive": "polyline",
            "semantic": "move",
            "points": [
                {
                    "t": frame["t"],
                    "x": frame["actors"][actor]["x"],
                    "y": frame["actors"][actor]["y"],
                    "visible": True,
                }
                for frame in document["actor_keyframes"]
            ],
        })
    return {
        "schema_version": "0.1",
        "scene_id": document["story_id"],
        "shot_id": document["shot_id"],
        "coordinate_space": "normalized_0_1_top_left",
        "duration_seconds": document["timeline"]["duration_seconds"],
        "sample_count": document["timeline"]["frame_count"],
        "tracks": tracks,
    }


def _validated_camera_bundle(
    value: object, *, scene_id: str, shot_id: str, duration: float,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "scene_id", "shot_id", "duration_seconds", "cameras"
    }:
        raise DirectorMulticamError("edited camera bundle fields are invalid")
    if (
        value.get("schema_version") != "1.0" or value.get("scene_id") != scene_id
        or value.get("shot_id") != shot_id or float(value.get("duration_seconds", -1)) != duration
        or not isinstance(value.get("cameras"), Mapping)
        or set(value["cameras"]) != {"camera_a", "camera_b", "camera_c"}
    ):
        raise DirectorMulticamError("edited camera bundle identity is invalid")
    cameras: dict[str, Any] = {}
    for camera_id, camera in value["cameras"].items():
        states = camera.get("states") if isinstance(camera, Mapping) else None
        if not isinstance(states, list) or len(states) != 5:
            raise DirectorMulticamError(f"{camera_id} must contain K0--K4")
        clean = []
        for index, state in enumerate(states):
            try:
                keyframe = CameraKeyframe(
                    keyframe_id=str(state["keyframe_id"]), t=float(state["t"]),
                    position=tuple(float(item) for item in state["position"]),
                    look_at=tuple(float(item) for item in state["look_at"]),
                    focal_length_mm=float(state["focal_length_mm"]),
                    shot_size=str(state["shot_size"]),
                    interpolation=str(state["interpolation"]),
                    roll_degrees=float(state["roll_degrees"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DirectorMulticamError(f"{camera_id} K{index} is invalid") from exc
            if keyframe.keyframe_id != f"K{index}" or abs(keyframe.t - _TIMES[index]) > 1e-9:
                raise DirectorMulticamError(f"{camera_id} keyframe order/time is invalid")
            values = (*keyframe.position, *keyframe.look_at, keyframe.focal_length_mm, keyframe.roll_degrees)
            if not all(isinstance(item, (int, float)) and float("-inf") < item < float("inf") for item in values):
                raise DirectorMulticamError(f"{camera_id} contains non-finite camera values")
            if keyframe.focal_length_mm <= 0:
                raise DirectorMulticamError(f"{camera_id} focal length must be positive")
            clean.append(keyframe.to_dict())
        cameras[camera_id] = {"states": clean}
    return {
        "schema_version": "1.0", "scene_id": scene_id, "shot_id": shot_id,
        "duration_seconds": duration, "cameras": cameras,
    }


def prepare_render(
    manifest_path: Path | str, plan_id: str, *, camera_bundle_override: object = None,
) -> Path:
    workspace = verify_workspace(manifest_path)
    root, document, state = workspace["root"], workspace["document"], workspace["state"]
    if state.get("approved_plan") != plan_id:
        raise DirectorMulticamError("plan must be human approved before rendering")
    approval = _read(root / "plans" / plan_id / "approval.json", "plan approval")
    plan_path = root / "plans" / plan_id / "plan.json"
    if approval.get("plan_sha256") != _sha(plan_path):
        raise DirectorMulticamError("approved plan binding mismatch")
    plan = load_multicam_plan(
        _read(plan_path, "approved plan"),
        scene_id=document["story_id"],
        actors=document["actors"],
        locked_through_keyframe=approval.get("locked_through_keyframe"),
    )
    cameras = compile_camera_rig(
        plan=plan,
        world_bounds=tuple(document["world_bounds"]),
        actor_keyframes=document["actor_keyframes"],
    )
    iteration_id = f"M{state['next_iteration']}"
    directory = root / "iterations" / iteration_id
    inputs = directory / "input"
    inputs.mkdir(parents=True, exist_ok=False)
    actor_path = inputs / "actor_trajectory.json"
    camera_path = inputs / "camera_bundle.json"
    _write(actor_path, _actor_trajectory(document))
    compiled_bundle = {
        "schema_version": "1.0",
        "scene_id": document["story_id"],
        "shot_id": document["shot_id"],
        "duration_seconds": document["timeline"]["duration_seconds"],
        "cameras": {
            camera_id: {"states": [state.to_dict() for state in states]}
            for camera_id, states in cameras.items()
        },
    }
    camera_bundle = (
        compiled_bundle if camera_bundle_override is None else _validated_camera_bundle(
            camera_bundle_override, scene_id=document["story_id"], shot_id=document["shot_id"],
            duration=float(document["timeline"]["duration_seconds"]),
        )
    )
    _write(camera_path, camera_bundle)
    job = directory / "job.json"
    _write(job, {
        "schema_version": SCHEMA_VERSION,
        "job_id": f"render-{iteration_id}",
        "iteration_id": iteration_id,
        "plan_id": plan_id,
        "status": "queued",
        "created_at": _now(),
        "inputs": {
            "actor_trajectory": _record(actor_path, root),
            "camera_bundle": _record(camera_path, root),
        },
    })
    state["next_iteration"] += 1
    _write(root / "state.json", state)
    return job


def _update_job(job_path: Path, **changes: object) -> dict[str, Any]:
    job = _read(job_path, "render job")
    job.update(changes)
    job["updated_at"] = _now()
    _write(job_path, job)
    return job


def run_render_job(manifest_path: Path | str, job_path: Path | str) -> None:
    """Run exactly one real Blender process and publish only measured evidence."""
    manifest = Path(manifest_path).resolve(strict=True)
    job = Path(job_path).resolve(strict=True)
    try:
        workspace = verify_workspace(manifest)
        root, document = workspace["root"], workspace["document"]
        job_value = _read(job, "render job")
        if job_value.get("status") != "queued":
            raise DirectorMulticamError("render job is not queued")
        plan_id = job_value.get("plan_id")
        plan_path = root / "plans" / str(plan_id) / "plan.json"
        plan = load_multicam_plan(
            _read(plan_path, "approved plan"),
            scene_id=document["story_id"], actors=document["actors"],
        )
        inputs = job.parent / "input"
        _verify_record(root, job_value["inputs"]["actor_trajectory"], "actor trajectory")
        _verify_record(root, job_value["inputs"]["camera_bundle"], "camera bundle")
        shotscript = _verify_record(root, document["source"]["shotscript"], "ShotScript")
        output = job.parent / "render"
        command = [
            sys.executable, "-m", "videoactagent.multicam_blender_runner",
            "--blender", document["blender_path"],
            "--shotscript", str(shotscript),
            "--trajectory", str(inputs / "actor_trajectory.json"),
            "--camera-bundle", str(inputs / "camera_bundle.json"),
            "--output-dir", str(output), "--render-style", "diagnostic",
            "--fps", str(document["timeline"]["fps"]),
            "--resolution", "x".join(str(value) for value in document["timeline"]["resolution"]),
            "--timeout", "300",
        ]
        _update_job(job, status="rendering", command=command)
        completed = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=330,
        )
        log = job.parent / "render.log"
        log.write_text(
            "COMMAND\n" + json.dumps(command) + "\nSTDOUT\n" + completed.stdout
            + "\nSTDERR\n" + completed.stderr,
            encoding="utf-8",
        )
        if completed.returncode != 0 or "MULTICAM_PROXY_OK" not in completed.stdout + completed.stderr:
            raise DirectorMulticamError("real Blender multicamera render failed; see render.log")
        render_manifest = output / "multicam_manifest.json"
        render_value = _read(render_manifest, "Blender render manifest")
        evaluation_value = evaluate_multicam_iteration(plan=plan, render_manifest=render_value)
        evaluation = job.parent / "evaluation.json"
        _write(evaluation, evaluation_value)
        outputs = {
            "render_manifest": _record(render_manifest, root),
            "evaluation": _record(evaluation, root),
            "log": _record(log, root),
            "videos": {
                camera_id: _record(output / record["video"]["path"], root)
                for camera_id, record in render_value["cameras"].items()
            },
        }
        if not evaluation_value["automatic_passed"]:
            _update_job(job, status="failed_checks", outputs=outputs)
            return
        _update_job(job, status="succeeded", outputs=outputs)
        state = _read(root / "state.json", "multicam state")
        state["current_iteration"] = job_value["iteration_id"]
        state["approved_iteration"] = None
        _write(root / "state.json", state)
    except BaseException as exc:
        try:
            _update_job(job, status="failed", error=f"{type(exc).__name__}: {exc}")
        except BaseException:
            pass


def approve_iteration(
    manifest_path: Path | str, iteration_id: str, *, author_id: str,
) -> Path:
    workspace = verify_workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    if state.get("current_iteration") != iteration_id or not author_id.strip():
        raise DirectorMulticamError("only the current successful Proxy can be approved")
    job = _read(root / "iterations" / iteration_id / "job.json", "render job")
    if job.get("status") != "succeeded":
        raise DirectorMulticamError("Proxy checks have not succeeded")
    outputs = job.get("outputs", {})
    evaluation_path = _verify_record(root, outputs.get("evaluation"), "evaluation")
    if not _read(evaluation_path, "evaluation").get("automatic_passed"):
        raise DirectorMulticamError("automatic geometry checks failed")
    approval = root / "iterations" / iteration_id / "approval.json"
    if approval.exists():
        raise DirectorMulticamError("Proxy is already approved")
    _write(approval, {
        "schema_version": SCHEMA_VERSION,
        "iteration_id": iteration_id,
        "plan_sha256": _sha(root / "plans" / job["plan_id"] / "plan.json"),
        "render_manifest_sha256": outputs["render_manifest"]["sha256"],
        "evaluation_sha256": outputs["evaluation"]["sha256"],
        "video_sha256": {
            camera_id: record["sha256"] for camera_id, record in outputs["videos"].items()
        },
        "author_id": author_id.strip(), "approved_at": _now(),
    })
    state["approved_iteration"] = iteration_id
    _write(root / "state.json", state)
    return approval


def session_document(manifest_path: Path | str) -> dict[str, Any]:
    workspace = verify_workspace(manifest_path)
    document, state = workspace["document"], workspace["state"]
    result = {
        "schema_version": SCHEMA_VERSION,
        "story_id": document["story_id"],
        "actors": document["actors"],
        "world_bounds": document["world_bounds"],
        "timeline": document["timeline"],
        "actor_keyframes": document["actor_keyframes"],
        "current_plan": state["current_plan"],
        "approved_plan": state["approved_plan"],
        "current_iteration": state["current_iteration"],
        "approved_iteration": state["approved_iteration"],
        "reference_url": "/reference/reference.mp4",
    }
    if state["current_plan"]:
        plan_value = _read(
            workspace["root"] / "plans" / state["current_plan"] / "plan.json", "current plan"
        )
        result["plan"] = plan_value
        plan = load_multicam_plan(
            plan_value, scene_id=document["story_id"], actors=document["actors"],
            locked_through_keyframe=plan_value.get("locked_through_keyframe"),
        )
        states = compile_camera_rig(
            plan=plan, world_bounds=tuple(document["world_bounds"]),
            actor_keyframes=document["actor_keyframes"],
        )
        result["camera_bundle"] = {
            "schema_version": "1.0", "scene_id": document["story_id"],
            "shot_id": document["shot_id"],
            "duration_seconds": document["timeline"]["duration_seconds"],
            "cameras": {
                camera_id: {"states": [state.to_dict() for state in camera_states]}
                for camera_id, camera_states in states.items()
            },
        }
    if state["current_iteration"]:
        iteration = state["current_iteration"]
        job = _read(workspace["root"] / "iterations" / iteration / "job.json", "render job")
        result["iteration"] = {
            "iteration_id": iteration,
            "status": job["status"],
            "videos": {
                camera_id: f"/media/{iteration}/{camera_id}.mp4"
                for camera_id in ("camera_a", "camera_b", "camera_c")
            },
            "evaluation": _read(
                workspace["root"] / "iterations" / iteration / "evaluation.json", "evaluation"
            ),
        }
    return result


def _byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise DirectorMulticamError("invalid Range header")
    if not match.group(1):
        length = int(match.group(2))
        start, end = max(0, size - length), size - 1
    else:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    if start < 0 or start >= size or end < start:
        raise DirectorMulticamError("unsatisfiable Range header")
    return start, min(end, size - 1), True


class _Handler(BaseHTTPRequestHandler):
    manifest: Path
    root: Path
    html: bytes

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._send(self.html, "text/html; charset=utf-8")
            elif path == "/api/session":
                self._json(session_document(self.manifest))
            elif path == "/reference/reference.mp4":
                self._video(_verify_record(
                    self.root,
                    verify_workspace(self.manifest)["document"]["source"]["reference"],
                    "reference",
                ))
            elif path.startswith("/api/jobs/"):
                job_id = unquote(path.removeprefix("/api/jobs/"))
                if not re.fullmatch(r"render-M[1-9][0-9]*", job_id):
                    raise DirectorMulticamError("job ID is invalid")
                self._json(_read(
                    self.root / "iterations" / job_id.removeprefix("render-") / "job.json",
                    "render job",
                ))
            elif path.startswith("/media/"):
                match = re.fullmatch(r"/media/(M[1-9][0-9]*)/(camera_[abc])\.mp4", path)
                if match is None:
                    raise DirectorMulticamError("media path is invalid")
                job = _read(
                    self.root / "iterations" / match.group(1) / "job.json", "render job"
                )
                video = _verify_record(
                    self.root, job.get("outputs", {}).get("videos", {}).get(match.group(2)),
                    "camera video",
                )
                self._video(video)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (DirectorMulticamError, OSError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise DirectorMulticamError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise DirectorMulticamError("request body must be one object")
            if path == "/api/plans" and set(payload) <= {"locked_through_keyframe"}:
                record = create_plan(
                    self.manifest,
                    locked_through_keyframe=payload.get("locked_through_keyframe"),
                )
                self._json(record, HTTPStatus.CREATED)
                return
            match = re.fullmatch(r"/api/plans/(P[1-9][0-9]*)/approve", path)
            if match and set(payload) == {"author_id"}:
                approval = approve_plan(
                    self.manifest, match.group(1), author_id=str(payload["author_id"])
                )
                self._json({"approved": True, "approval_sha256": _sha(approval)})
                return
            match = re.fullmatch(r"/api/plans/(P[1-9][0-9]*)/render", path)
            if match and set(payload) <= {"camera_bundle"}:
                job = prepare_render(
                    self.manifest, match.group(1),
                    camera_bundle_override=payload.get("camera_bundle"),
                )
                Thread(target=run_render_job, args=(self.manifest, job), daemon=True).start()
                value = _read(job, "render job")
                self._json({"job_id": value["job_id"], "status": "queued"}, HTTPStatus.ACCEPTED)
                return
            match = re.fullmatch(r"/api/iterations/(M[1-9][0-9]*)/approve", path)
            if match and set(payload) == {"author_id"}:
                approval = approve_iteration(
                    self.manifest, match.group(1), author_id=str(payload["author_id"])
                )
                self._json({"approved": True, "approval_sha256": _sha(approval)})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (DirectorMulticamError, OSError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _video(self, path: Path) -> None:
        start, end, partial = _byte_range(self.headers.get("Range"), path.stat().st_size)
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{path.stat().st_size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(_json_bytes(value), "application/json", status)

    def _send(self, data: bytes, media_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        return


def serve_workspace(manifest_path: Path | str, port: int = 8770) -> None:
    if not 1 <= port <= 65535:
        raise DirectorMulticamError("port must be in 1..65535")
    manifest = Path(manifest_path).resolve(strict=True)
    workspace = verify_workspace(manifest)
    _Handler.manifest = manifest
    _Handler.root = workspace["root"]
    _Handler.html = (
        Path(__file__).resolve().parents[1] / "static" / "director_multicam_panel.html"
    ).read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"DIRECTOR_MULTICAM_SERVING http://127.0.0.1:{port}")
    server.serve_forever()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--prompt", type=Path, required=True)
    prepare.add_argument("--shotscript", type=Path, required=True)
    prepare.add_argument("--blender", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--manifest", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8770)
    suite = sub.add_parser("prepare-suite")
    suite.add_argument("--config", type=Path, required=True)
    suite.add_argument("--blender", type=Path, required=True)
    suite.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_workspace(
                args.prompt, args.shotscript, args.blender, args.output_dir
            )
            print(f"DIRECTOR_MULTICAM_PREPARED {manifest}")
        elif args.command == "prepare-suite":
            manifest = prepare_suite(args.config, args.blender, args.output_dir)
            print(f"DIRECTOR_MULTICAM_SUITE_PREPARED {manifest}")
        else:
            serve_workspace(args.manifest, args.port)
    except (DirectorMulticamError, OSError, ValueError) as exc:
        print(f"DIRECTOR_MULTICAM_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
