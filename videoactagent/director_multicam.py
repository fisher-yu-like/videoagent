"""Independent Agent-planned multicamera director workspace."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from uuid import uuid4

from videoactagent.deepseek_planner import request_multicam_plan
from videoactagent.multicam_plan import MulticamPlanError, load_multicam_plan
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


def prepare_render(manifest_path: Path | str, plan_id: str) -> Path:
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
    _write(camera_path, {
        "schema_version": "1.0",
        "scene_id": document["story_id"],
        "shot_id": document["shot_id"],
        "duration_seconds": document["timeline"]["duration_seconds"],
        "cameras": {
            camera_id: {"states": [state.to_dict() for state in states]}
            for camera_id, states in cameras.items()
        },
    })
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


def session_document(manifest_path: Path | str) -> dict[str, Any]:
    workspace = verify_workspace(manifest_path)
    document, state = workspace["document"], workspace["state"]
    return {
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_workspace(
                args.prompt, args.shotscript, args.blender, args.output_dir
            )
            print(f"DIRECTOR_MULTICAM_PREPARED {manifest}")
        else:
            raise DirectorMulticamError("serve is not available until the panel task is complete")
    except (DirectorMulticamError, OSError, ValueError) as exc:
        print(f"DIRECTOR_MULTICAM_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
