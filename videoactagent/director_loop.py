"""Browser-first human director loop for complete Blender proxy review."""

from __future__ import annotations

import argparse
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
from threading import Lock, Thread
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from videoactagent.coded_draft import CodedDraftError, _decode_video
from videoactagent.director_annotation import (
    DirectorAnnotationError,
    compile_director_annotation,
)
from videoactagent.trajectory import canonical_bytes
from videoactagent.trajectory_author import _verify_coded_bundle
from videoactagent.trajectory_prompt import PROMPT_COMPILER_VERSION
from videoactagent.restyle_prompt import (
    RESTYLE_COMPILER_VERSION,
    RestylePromptError,
    load_restyle_profile,
)


DIRECTOR_LOOP_SCHEMA_VERSION = "1.1"
_ITERATION = re.compile(r"^D(?:0|[1-9][0-9]*)$")
_COMPILER_INPUTS = frozenset({
    "annotation", "actor_trajectory", "camera_trajectory", "compiled_prompt",
    "trajectory_prompt", "restyle_prompt",
})
_JOB_LOCK = Lock()


class DirectorLoopError(ValueError):
    """Raised when a loop iteration cannot be proven or published."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n").encode("utf-8")


def _read(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DirectorLoopError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise DirectorLoopError(f"{label} must be one object")
    return value


def _require_current_schema(value: Mapping[str, Any], label: str) -> None:
    schema = value.get("schema_version")
    if schema == "1.0":
        raise DirectorLoopError(
            f"{label} uses legacy schema 1.0; migration required: prepare a new "
            "workspace and reuse the human annotation"
        )
    if schema != DIRECTOR_LOOP_SCHEMA_VERSION:
        raise DirectorLoopError(f"{label} schema is invalid")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        if _sha(temporary) != _sha(source) or temporary.stat().st_size != source.stat().st_size:
            raise DirectorLoopError("staged snapshot changed while copying")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _object_from_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DirectorLoopError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise DirectorLoopError(f"{label} must be one object")
    return value


def _record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
    }


def _safe(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise DirectorLoopError(f"{label} path is missing")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DirectorLoopError(f"unsafe {label} path")
    target = (root / candidate).resolve(strict=True)
    try:
        target.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise DirectorLoopError(f"{label} path escapes workspace") from exc
    if not target.is_file():
        raise DirectorLoopError(f"{label} is not a file")
    return target


def _verify_record(root: Path, value: object, label: str) -> Path:
    required = {"path", "sha256", "bytes"}
    if (
        not isinstance(value, Mapping)
        or not required.issubset(value)
        or set(value) - required not in (set(), {"media"}, {"provenance"})
    ):
        raise DirectorLoopError(f"{label} record is invalid")
    path = _safe(root, value.get("path"), label)
    if value.get("sha256") != _sha(path) or value.get("bytes") != path.stat().st_size:
        raise DirectorLoopError(f"{label} hash/size mismatch")
    return path


def _verify_annotation_compiler_versions(
    root: Path, value: object, label: str, *,
    trajectory_version: object, restyle_version: object,
) -> Path:
    path = _verify_record(root, value, label)
    annotation = _read(path, label)
    if (
        trajectory_version != PROMPT_COMPILER_VERSION
        or restyle_version != RESTYLE_COMPILER_VERSION
        or annotation.get("prompt_compiler_version") != trajectory_version
        or annotation.get("restyle_compiler_version") != restyle_version
    ):
        raise DirectorLoopError(f"{label} compiler version binding is invalid")
    return path


def _copy(source: Path, target: Path, root: Path) -> dict[str, object]:
    before = _sha(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if _sha(source) != before or _sha(target) != before:
        raise DirectorLoopError("source changed while copying")
    return _record(target, root)


def _media(path: Path, timeline: Mapping[str, Any]) -> dict[str, object]:
    try:
        metadata, _frames = _decode_video(
            path,
            expected_frames=int(timeline["frame_count"]),
            expected_fps=int(timeline["fps"]),
            expected_duration=float(timeline["duration_seconds"]),
            expected_resolution=tuple(timeline["resolution"]),
            selected_indices=set(),
        )
    except (CodedDraftError, KeyError, TypeError, ValueError) as exc:
        raise DirectorLoopError(f"complete proxy media validation failed: {exc}") from exc
    return metadata


def prepare_workspace(
    bundle_path: Path | str, blender_path: Path | str, output_dir: Path | str,
    restyle_profile_path: Path | str | None = None,
) -> Path:
    verified = _verify_coded_bundle(Path(bundle_path))
    blender = Path(blender_path).resolve(strict=True)
    if not blender.is_file():
        raise DirectorLoopError("Blender executable is missing")
    output = Path(output_dir).resolve(strict=False)
    if output.exists():
        raise DirectorLoopError(f"output already exists: {output}")
    root = output.parent / f".{output.name}.{uuid4().hex}.staging"
    root.mkdir(parents=True)
    try:
        source_root = verified["root"]
        bundle = _read(verified["bundle"], "coded-draft bundle")
        source_records = {
            "bundle": _copy(verified["bundle"], root / "source" / "bundle.json", root),
            "manifest": _copy(verified["manifest"], root / "source" / "manifest.json", root),
            "shotscript": _copy(verified["shotscript"], root / "source" / "shotscript.json", root),
            "semantic_plan": _copy(verified["semantic"], root / "source" / "semantic_plan.json", root),
        }
        prompt_binding = bundle.get("source_bindings", {}).get("prompt")
        if not isinstance(prompt_binding, Mapping):
            raise DirectorLoopError("coded-draft prompt binding is missing")
        prompt_source = _safe(source_root, prompt_binding.get("snapshot_path"), "prompt")
        if prompt_binding.get("snapshot_sha256") != _sha(prompt_source):
            raise DirectorLoopError("coded-draft prompt binding mismatch")
        source_records["prompt"] = _copy(prompt_source, root / "source" / "prompt.txt", root)

        semantic = _read(root / "source" / "semantic_plan.json", "semantic plan")
        script = _read(root / "source" / "shotscript.json", "ShotScript")
        profile_snapshot = root / "source" / "restyle_profile.json"
        if restyle_profile_path is not None:
            profile_source = Path(restyle_profile_path).resolve(strict=True)
            if not profile_source.is_file():
                raise DirectorLoopError("restyle profile is not a file")
            profile_record = _copy(profile_source, profile_snapshot, root)
            profile_record["provenance"] = "provided"
        else:
            appearance = semantic.get("appearance_instruction")
            environment = script.get("environment_preset")
            if not isinstance(appearance, str) or not appearance.strip():
                raise DirectorLoopError("appearance_instruction is required for generic restyle profile")
            if not isinstance(environment, str) or not environment.strip():
                raise DirectorLoopError("environment_preset is required for generic restyle profile")
            generic_profile = {
                "schema_version": "1.0",
                "scene_id": verified["story_id"],
                "subjects": [
                    {
                        "actor_id": actor,
                        "description": (
                            f"a distinct complete photoreal human identified as {actor}; "
                            f"{appearance.strip()}"
                        ),
                    }
                    for actor in verified["actors"]
                ],
                "environment": f"a grounded {environment.strip()} environment",
                "lighting": appearance.strip(),
                "quality": (
                    "photoreal live-action humans with natural anatomy, realistic skin and cloth"
                ),
            }
            profile_snapshot.write_bytes(_json_bytes(generic_profile))
            profile_record = _record(profile_snapshot, root)
            profile_record["provenance"] = "derived_generic"
        try:
            profile = load_restyle_profile(_read(profile_snapshot, "restyle profile"))
        except RestylePromptError as exc:
            raise DirectorLoopError(f"restyle profile is invalid: {exc}") from exc
        if profile.scene_id != verified["story_id"]:
            raise DirectorLoopError("restyle profile scene_id differs from workspace story_id")
        if {subject.actor_id for subject in profile.subjects} != set(verified["actors"]):
            raise DirectorLoopError("restyle profile subjects differ from workspace actors")
        source_records["restyle_profile"] = profile_record

        diagnostic_value = bundle.get("diagnostic_video")
        clay_value = bundle.get("conditioning_video")
        if not isinstance(diagnostic_value, Mapping) or not isinstance(clay_value, Mapping):
            raise DirectorLoopError("coded-draft videos are missing")
        diagnostic_source = _verify_record(source_root, {
            key: diagnostic_value.get(key) for key in ("path", "sha256", "bytes")
        }, "D0 diagnostic")
        clay_source = _verify_record(source_root, {
            key: clay_value.get(key) for key in ("path", "sha256", "bytes")
        }, "D0 clay")
        d0 = root / "iterations" / "D0"
        diagnostic_record = _copy(diagnostic_source, d0 / "diagnostic.mp4", root)
        clay_record = _copy(clay_source, d0 / "clay.mp4", root)
        expected = bundle.get("conditioning_video", {}).get("media")
        if not isinstance(expected, Mapping):
            raise DirectorLoopError("coded-draft expected media is missing")
        timeline = {
            "frame_count": expected.get("frame_count"),
            "fps": expected.get("fps"),
            "duration_seconds": expected.get("duration_seconds"),
            "resolution": expected.get("resolution"),
        }
        diagnostic_media = _media(d0 / "diagnostic.mp4", timeline)
        clay_media = _media(d0 / "clay.mp4", timeline)
        if diagnostic_media["decoded_pixel_sha256"] == clay_media["decoded_pixel_sha256"]:
            raise DirectorLoopError("D0 diagnostic and clay videos are pixel-identical")

        keyframes = []
        for item in verified["keyframes"]:
            record = _copy(item["path"], root / "reference" / f"{item['id']}.png", root)
            keyframes.append({
                "id": item["id"], "t": item["t"], "frame_index": item["frame_index"],
                "reference": record,
            })
        descriptions = {
            str(item.get("id")): str(item.get("visible_state", ""))
            for item in semantic.get("semantic_keyframes", []) if isinstance(item, Mapping)
        }
        for frame in keyframes:
            frame["semantic_description"] = descriptions.get(frame["id"], "")

        d0_document = {
            "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION, "iteration_id": "D0", "parent_iteration_id": None,
            "status": "succeeded", "created_at": _now(), "human_authored": False,
            "diagnostic": {**diagnostic_record, "media": diagnostic_media},
            "clay": {**clay_record, "media": clay_media},
        }
        _write_atomic(d0 / "iteration.json", _json_bytes(d0_document))
        manifest_doc = {
            "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION, "story_id": verified["story_id"],
            "shot_id": verified["shot_id"], "actors": verified["actors"],
            "world_bounds": verified["world_bounds"], "timeline": timeline,
            "keyframes": keyframes, "source": source_records,
            "blender_path": str(blender), "initial_iteration": "D0",
            "camera_reference": script["shots"][0]["camera"],
        }
        manifest = root / "director_loop_manifest.json"
        _write_atomic(manifest, _json_bytes(manifest_doc))
        _write_atomic(root / "state.json", _json_bytes({
            "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION, "current_iteration": "D0",
            "approved_iteration": None, "approval": None, "next_iteration": 1,
        }))
        verify_workspace(manifest)
        os.replace(root, output)
        return output / manifest.name
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _iteration(root: Path, iteration_id: str) -> tuple[Path, dict[str, Any]]:
    if not _ITERATION.fullmatch(iteration_id):
        raise DirectorLoopError("iteration ID is invalid")
    directory = root / "iterations" / iteration_id
    document = _read(directory / "iteration.json", f"{iteration_id} iteration")
    _require_current_schema(document, f"{iteration_id} iteration")
    if document.get("iteration_id") != iteration_id or document.get("status") != "succeeded":
        raise DirectorLoopError(f"iteration {iteration_id} is not succeeded")
    for style in ("diagnostic", "clay"):
        _verify_record(root, document.get(style), f"{iteration_id} {style}")
    if document.get("human_authored") is True:
        if (
            document.get("trajectory_compiler_version") != PROMPT_COMPILER_VERSION
            or document.get("restyle_compiler_version") != RESTYLE_COMPILER_VERSION
        ):
            raise DirectorLoopError(f"{iteration_id} compiler version binding is invalid")
        inputs = document.get("inputs")
        if not isinstance(inputs, Mapping) or set(inputs) != _COMPILER_INPUTS:
            raise DirectorLoopError(f"{iteration_id} input inventory is invalid")
        paths = {}
        for name in _COMPILER_INPUTS:
            if name == "annotation":
                paths[name] = _verify_annotation_compiler_versions(
                    root, inputs[name], f"{iteration_id} annotation",
                    trajectory_version=document["trajectory_compiler_version"],
                    restyle_version=document["restyle_compiler_version"],
                )
            else:
                paths[name] = _verify_record(root, inputs[name], f"{iteration_id} {name}")
        if paths["compiled_prompt"].read_bytes() != paths["trajectory_prompt"].read_bytes():
            raise DirectorLoopError("compiled_prompt differs from trajectory_prompt")
        source = document.get("source")
        if not isinstance(source, Mapping) or set(source) != {"restyle_profile"}:
            raise DirectorLoopError(f"{iteration_id} source inventory is invalid")
        _verify_record(root, source["restyle_profile"], f"{iteration_id} restyle_profile")
        _verify_human_iteration_semantics(root, document, paths)
    return directory, document


def verify_workspace(manifest_path: Path | str) -> dict[str, Any]:
    manifest = Path(manifest_path).resolve(strict=True)
    if manifest.name != "director_loop_manifest.json":
        raise DirectorLoopError("workspace manifest name is invalid")
    root = manifest.parent
    doc = _read(manifest, "director loop manifest")
    required = {
        "schema_version", "story_id", "shot_id", "actors", "world_bounds", "timeline",
        "keyframes", "source", "blender_path", "initial_iteration", "camera_reference",
    }
    _require_current_schema(doc, "director loop manifest")
    if set(doc) != required:
        raise DirectorLoopError("director loop manifest schema is invalid")
    blender = Path(str(doc.get("blender_path"))).resolve(strict=True)
    if not blender.is_file():
        raise DirectorLoopError("Blender executable is missing")
    sources = doc.get("source")
    if not isinstance(sources, Mapping):
        raise DirectorLoopError("workspace sources are missing")
    for name in (
        "bundle", "manifest", "shotscript", "semantic_plan", "prompt", "restyle_profile"
    ):
        _verify_record(root, sources.get(name), f"source {name}")
    profile_record = sources.get("restyle_profile")
    if (
        not isinstance(profile_record, Mapping)
        or profile_record.get("provenance") not in {"provided", "derived_generic"}
    ):
        raise DirectorLoopError("restyle profile provenance is invalid")
    profile_path = _verify_record(root, profile_record, "source restyle_profile")
    try:
        profile = load_restyle_profile(_read(profile_path, "restyle profile"))
    except RestylePromptError as exc:
        raise DirectorLoopError(f"restyle profile is invalid: {exc}") from exc
    if profile.scene_id != doc.get("story_id"):
        raise DirectorLoopError("restyle profile scene_id differs from workspace story_id")
    if {subject.actor_id for subject in profile.subjects} != set(doc.get("actors", [])):
        raise DirectorLoopError("restyle profile subjects differ from workspace actors")
    frames = doc.get("keyframes")
    if not isinstance(frames, list) or len(frames) != 5:
        raise DirectorLoopError("workspace must contain K0--K4")
    for index, frame in enumerate(frames):
        if not isinstance(frame, Mapping) or frame.get("id") != f"K{index}":
            raise DirectorLoopError("workspace keyframe schedule is invalid")
        _verify_record(root, frame.get("reference"), f"K{index} reference")
    state_path = root / "state.json"
    state = _read(state_path, "director loop state")
    _require_current_schema(state, "director loop state")
    if set(state) != {
        "schema_version", "current_iteration", "approved_iteration", "approval", "next_iteration"
    }:
        raise DirectorLoopError("director loop state is invalid")
    current = state.get("current_iteration")
    if not isinstance(current, str):
        raise DirectorLoopError("current iteration is missing")
    _directory, current_document = _iteration(root, current)
    if (
        current_document.get("human_authored") is True
        and current_document["source"]["restyle_profile"] != profile_record
    ):
        raise DirectorLoopError("iteration restyle_profile differs from workspace source")
    return {"manifest": manifest, "root": root, "document": doc, "state": state}


def _contract(workspace: Mapping[str, Any]) -> dict[str, Any]:
    doc = workspace["document"]
    root = workspace["root"]
    timeline = doc["timeline"]
    inheritance = derive_inherited_keyframes(workspace)
    prompt_path = _verify_record(root, doc["source"]["prompt"], "source prompt")
    semantic_path = _verify_record(root, doc["source"]["semantic_plan"], "semantic plan")
    try:
        story_prompt = prompt_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise DirectorLoopError(f"cannot read source prompt: {exc}") from exc
    semantic = _read(semantic_path, "semantic plan")
    appearance = semantic.get("appearance_instruction")
    profile_path = _verify_record(
        root, doc["source"]["restyle_profile"], "source restyle_profile"
    )
    profile = _read(profile_path, "restyle profile")
    return {
        "story_id": doc["story_id"], "shot_id": doc["shot_id"],
        "duration_seconds": timeline["duration_seconds"],
        "sample_count": timeline["frame_count"], "actors": doc["actors"],
        "keyframes": [{"id": item["id"], "t": item["t"]} for item in doc["keyframes"]],
        "inheritance_sha256": inheritance["inheritance_sha256"],
        "inherited_keyframes": inheritance["keyframes"],
        "story_prompt": story_prompt, "appearance_instruction": appearance,
        "restyle_profile": profile,
    }


def _lerp(start: object, end: object, t: float, label: str) -> list[float]:
    if (
        not isinstance(start, list) or not isinstance(end, list)
        or len(start) != 3 or len(end) != 3
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in start + end)
    ):
        raise DirectorLoopError(f"{label} must contain two 3D points")
    return [round(float(a) + (float(b) - float(a)) * t, 12) for a, b in zip(start, end)]


def derive_inherited_keyframes(workspace: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the exact, source-bound K0--K4 state inherited by the next revision."""
    root = workspace["root"]
    doc = workspace["document"]
    state = workspace["state"]
    directory, current = _iteration(root, state["current_iteration"])
    if current.get("human_authored") is True:
        annotation_path = _verify_record(
            root, current.get("inputs", {}).get("annotation"), "current annotation"
        )
        annotation = _read(annotation_path, "current annotation")
        frames = annotation.get("keyframes")
        if not isinstance(frames, list) or len(frames) != 5:
            raise DirectorLoopError("current annotation must contain K0--K4")
        inherited = [
            {key: value for key, value in frame.items() if key != "camera_source"}
            for frame in json.loads(json.dumps(frames))
        ]
        source = {
            "iteration": _record(directory / "iteration.json", root),
            "annotation": _record(annotation_path, root),
        }
    else:
        shotscript_path = _verify_record(root, doc["source"]["shotscript"], "ShotScript")
        semantic_path = _verify_record(root, doc["source"]["semantic_plan"], "semantic plan")
        script = _read(shotscript_path, "ShotScript")
        semantic = _read(semantic_path, "semantic plan")
        shots = script.get("shots")
        if not isinstance(shots, list) or len(shots) != 1 or not isinstance(shots[0], Mapping):
            raise DirectorLoopError("director loop requires one whole-shot ShotScript")
        shot = shots[0]
        raw_actors = shot.get("actors")
        camera = shot.get("camera")
        bounds = doc["world_bounds"]
        if not isinstance(raw_actors, list) or not isinstance(camera, Mapping):
            raise DirectorLoopError("ShotScript actors or camera are invalid")
        actor_specs = {
            item.get("id"): item for item in raw_actors if isinstance(item, Mapping)
        }
        if set(actor_specs) != set(doc["actors"]):
            raise DirectorLoopError("ShotScript actors differ from director manifest")
        inherited = []
        for frame in doc["keyframes"]:
            t = float(frame["t"])
            world_positions = {
                actor: _lerp(actor_specs[actor].get("start"), actor_specs[actor].get("end"), t, actor)
                for actor in doc["actors"]
            }
            actor_points = {
                actor: {
                    "x": round((point[0] - bounds[0]) / (bounds[1] - bounds[0]), 12),
                    "y": round((bounds[3] - point[1]) / (bounds[3] - bounds[2]), 12),
                }
                for actor, point in world_positions.items()
            }
            camera_position = _lerp(camera.get("start"), camera.get("end"), t, "camera")
            target = camera.get("look_at")
            if target == "fixed_actors_midpoint":
                positions = [
                    _lerp(actor_specs[a].get("start"), actor_specs[a].get("start"), 0.0, a)
                    for a in doc["actors"]
                ]
            elif target == "actors_midpoint":
                positions = list(world_positions.values())
            elif target in world_positions:
                positions = [world_positions[target]]
            else:
                raise DirectorLoopError("ShotScript camera look_at is unsupported")
            look_at = [
                round(sum(point[axis] for point in positions) / len(positions), 12)
                for axis in range(2)
            ] + [1.25]
            inherited.append({
                "id": frame["id"], "t": t, "actors": actor_points,
                "camera": {
                    "position": camera_position, "look_at": look_at,
                    "focal_length_mm": float(camera.get("focal_length_mm")),
                    "shot_size": camera.get("shot_size"), "interpolation": "linear",
                    "roll_degrees": 0.0,
                },
            })
        source = {
            "iteration": _record(directory / "iteration.json", root),
            "shotscript": _record(shotscript_path, root),
            "semantic_plan": _record(semantic_path, root),
        }
    binding = {"source": source, "keyframes": inherited}
    return {
        "keyframes": inherited,
        "inheritance_sha256": hashlib.sha256(_json_bytes(binding)).hexdigest(),
        "source": source,
    }


def _compile_candidate(workspace: Mapping[str, Any], payload: Mapping[str, Any]):
    state = workspace["state"]
    expected_id = f"D{state['next_iteration']}"
    if payload.get("iteration_id") != expected_id or payload.get("parent_iteration_id") != state["current_iteration"]:
        raise DirectorLoopError("iteration is stale or not the next version")
    try:
        return compile_director_annotation(payload, _contract(workspace))
    except DirectorAnnotationError as exc:
        raise DirectorLoopError(str(exc)) from exc


def _expected_compiler_artifacts(
    workspace: Mapping[str, Any], annotation_bytes: bytes, label: str, *,
    trajectory_version: object, restyle_version: object,
) -> dict[str, bytes]:
    annotation = _object_from_bytes(annotation_bytes, f"{label} annotation")
    if (
        trajectory_version != PROMPT_COMPILER_VERSION
        or restyle_version != RESTYLE_COMPILER_VERSION
        or annotation.get("prompt_compiler_version") != trajectory_version
        or annotation.get("restyle_compiler_version") != restyle_version
    ):
        raise DirectorLoopError(f"{label} annotation compiler version binding is invalid")
    compiled = _compile_candidate(workspace, annotation)
    trajectory_bytes = (compiled.trajectory_prompt + "\n").encode("utf-8")
    return {
        "annotation": compiled.canonical_annotation,
        "actor_trajectory": canonical_bytes(compiled.actor_trajectory),
        "camera_trajectory": compiled.camera_document,
        "trajectory_prompt": trajectory_bytes,
        "compiled_prompt": trajectory_bytes,
        "restyle_prompt": (compiled.restyle_prompt + "\n").encode("utf-8"),
    }


def _verify_compiler_artifact_paths(
    workspace: Mapping[str, Any], paths: Mapping[str, Path], label: str, *,
    trajectory_version: object, restyle_version: object,
) -> dict[str, bytes]:
    if set(paths) != _COMPILER_INPUTS:
        raise DirectorLoopError(f"{label} input inventory is invalid")
    try:
        resolved = {name: path.resolve(strict=True) for name, path in paths.items()}
        data = {name: path.read_bytes() for name, path in resolved.items()}
    except OSError as exc:
        raise DirectorLoopError(f"cannot read {label} compiler artifacts: {exc}") from exc
    if len(set(resolved.values())) != len(_COMPILER_INPUTS):
        raise DirectorLoopError(f"{label} input paths must be distinct")
    expected = _expected_compiler_artifacts(
        workspace, data["annotation"], label,
        trajectory_version=trajectory_version,
        restyle_version=restyle_version,
    )
    for name in sorted(_COMPILER_INPUTS):
        if data[name] != expected[name]:
            raise DirectorLoopError(f"compiled director artifact mismatch: {name}")
    try:
        changed = [name for name, path in resolved.items() if path.read_bytes() != data[name]]
    except OSError as exc:
        raise DirectorLoopError(f"cannot recheck {label} compiler artifacts: {exc}") from exc
    if changed:
        raise DirectorLoopError(
            f"{label} compiler artifacts changed during semantic verification: {changed}"
        )
    return data


def _snapshot_compiled_job_inputs(
    workspace: Mapping[str, Any], job_path: Path, job: Mapping[str, Any], staging: Path,
) -> dict[str, tuple[Path, Path, bytes]]:
    inputs = job.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != _COMPILER_INPUTS:
        raise DirectorLoopError("render job input inventory is invalid")

    snapshots: dict[str, tuple[Path, Path, bytes]] = {}
    source_paths: set[Path] = set()
    for name in sorted(_COMPILER_INPUTS):
        record = inputs[name]
        if not isinstance(record, Mapping) or set(record) != {"path", "sha256", "bytes"}:
            raise DirectorLoopError(f"render job {name} record is invalid")
        source = _safe(job_path.parent, record.get("path"), f"render job {name}")
        if source in source_paths:
            raise DirectorLoopError("render job input paths must be distinct")
        source_paths.add(source)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise DirectorLoopError(f"cannot snapshot render job {name}: {exc}") from exc
        if (
            record.get("sha256") != hashlib.sha256(data).hexdigest()
            or record.get("bytes") != len(data)
        ):
            raise DirectorLoopError(f"render job {name} hash/size mismatch")
        staged = staging / "inputs" / name / source.name
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        snapshots[name] = (source, staged, data)

    staged_data = _verify_compiler_artifact_paths(
        workspace,
        {name: staged for name, (_source, staged, _data) in snapshots.items()},
        "render job",
        trajectory_version=job.get("trajectory_compiler_version"),
        restyle_version=job.get("restyle_compiler_version"),
    )
    for name, (source, staged, _data) in list(snapshots.items()):
        snapshots[name] = (source, staged, staged_data[name])
    return snapshots


def _snapshot_record(path: Path, root: Path, data: bytes) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _snapshot_source_file(source: Path, target: Path, label: str) -> None:
    before = _sha(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if (
        _sha(source) != before
        or _sha(target) != before
        or target.stat().st_size != source.stat().st_size
    ):
        raise DirectorLoopError(f"{label} changed while staging")


def _probe_media_pair(
    diagnostic: Path, clay: Path, timeline: Mapping[str, Any], label: str,
) -> dict[str, dict[str, object]]:
    paths = {"diagnostic": diagnostic, "clay": clay}
    result: dict[str, dict[str, object]] = {}
    for style, path in paths.items():
        before = (_sha(path), path.stat().st_size)
        metadata = _media(path, timeline)
        after = (_sha(path), path.stat().st_size)
        if after != before:
            raise DirectorLoopError(f"{label} {style} changed during media probe")
        result[style] = {
            "sha256": after[0], "bytes": after[1], "media": metadata,
        }
    for style, path in paths.items():
        if (_sha(path), path.stat().st_size) != (
            result[style]["sha256"], result[style]["bytes"]
        ):
            raise DirectorLoopError(f"{label} {style} changed after media probe")
    if (
        result["diagnostic"]["media"]["decoded_pixel_sha256"]
        == result["clay"]["media"]["decoded_pixel_sha256"]
    ):
        raise DirectorLoopError(f"{label} diagnostic and clay videos are pixel-identical")
    return result


def _media_record_from_probe(
    target: Path, root: Path, probe: Mapping[str, object],
) -> dict[str, object]:
    return {
        "path": target.relative_to(root).as_posix(),
        "sha256": probe["sha256"],
        "bytes": probe["bytes"],
        "media": probe["media"],
    }


def _verify_human_iteration_semantics(
    root: Path, document: Mapping[str, Any], input_paths: Mapping[str, Path],
) -> None:
    iteration_id = document.get("iteration_id")
    parent_id = document.get("parent_iteration_id")
    if (
        not isinstance(iteration_id, str)
        or not isinstance(parent_id, str)
        or not _ITERATION.fullmatch(parent_id)
        or int(parent_id[1:]) >= int(iteration_id[1:])
    ):
        raise DirectorLoopError(f"{iteration_id} parent iteration binding is invalid")
    manifest = _read(root / "director_loop_manifest.json", "director loop manifest")
    source = document.get("source")
    profile_record = manifest.get("source", {}).get("restyle_profile")
    if not isinstance(source, Mapping) or source.get("restyle_profile") != profile_record:
        raise DirectorLoopError(f"{iteration_id} restyle_profile binding is invalid")
    semantic_workspace = {
        "root": root,
        "document": manifest,
        "state": {
            "current_iteration": parent_id,
            "next_iteration": int(iteration_id[1:]),
        },
    }
    artifact_bytes = _verify_compiler_artifact_paths(
        semantic_workspace, input_paths, iteration_id,
        trajectory_version=document.get("trajectory_compiler_version"),
        restyle_version=document.get("restyle_compiler_version"),
    )
    inputs = document["inputs"]
    for name, data in artifact_bytes.items():
        record = inputs[name]
        if (
            record.get("sha256") != hashlib.sha256(data).hexdigest()
            or record.get("bytes") != len(data)
        ):
            raise DirectorLoopError(f"{iteration_id} {name} semantic record mismatch")

    media_paths = {
        style: _verify_record(root, document[style], f"{iteration_id} {style}")
        for style in ("diagnostic", "clay")
    }
    probes = _probe_media_pair(
        media_paths["diagnostic"], media_paths["clay"], manifest["timeline"],
        iteration_id,
    )
    for style in ("diagnostic", "clay"):
        record = document[style]
        probe = probes[style]
        if (
            record.get("sha256") != probe["sha256"]
            or record.get("bytes") != probe["bytes"]
            or record.get("media") != probe["media"]
        ):
            raise DirectorLoopError(f"{iteration_id} {style} semantic media mismatch")


def _backup_if_present(path: Path, backup: Path, label: str) -> Path | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise DirectorLoopError(f"existing {label} is not a file")
    _snapshot_source_file(path, backup, label)
    return backup


def _restore_publication(
    backups: list[tuple[Path, Path | bytes | None]],
) -> None:
    failures: list[str] = []
    for target, backup in reversed(backups):
        try:
            if backup is None:
                target.unlink(missing_ok=True)
            elif isinstance(backup, bytes):
                _write_atomic(target, backup)
            else:
                _atomic_copy(backup, target)
        except BaseException as exc:
            failures.append(f"{target}: {exc}")
    if failures:
        raise DirectorLoopError(
            "publication rollback failed: " + "; ".join(failures)
        )


def preview_prompt(manifest_path: Path | str, payload: Mapping[str, Any]) -> dict[str, str]:
    workspace = verify_workspace(manifest_path)
    compiled = _compile_candidate(workspace, payload)
    return {
        "trajectory_compiler_version": PROMPT_COMPILER_VERSION,
        "restyle_compiler_version": RESTYLE_COMPILER_VERSION,
        "trajectory_prompt": compiled.trajectory_prompt,
        "restyle_prompt": compiled.restyle_prompt,
    }


def prepare_iteration(manifest_path: Path | str, payload: Mapping[str, Any]) -> Path:
    workspace = verify_workspace(manifest_path)
    root = workspace["root"]
    state = workspace["state"]
    expected_id = f"D{state['next_iteration']}"
    compiled = _compile_candidate(workspace, payload)
    directory = root / "iterations" / expected_id
    if directory.exists():
        raise DirectorLoopError(f"iteration already exists: {expected_id}")
    staging = directory.parent / f".{expected_id}.{uuid4().hex}.staging"
    input_dir = staging / "input"
    input_dir.mkdir(parents=True)
    try:
        (input_dir / "annotation.json").write_bytes(compiled.canonical_annotation)
        (input_dir / "actor_trajectory.json").write_bytes(canonical_bytes(compiled.actor_trajectory))
        (input_dir / "camera_trajectory.json").write_bytes(compiled.camera_document)
        trajectory_bytes = (compiled.trajectory_prompt + "\n").encode("utf-8")
        (input_dir / "trajectory_prompt.txt").write_bytes(trajectory_bytes)
        (input_dir / "compiled_prompt.txt").write_bytes(trajectory_bytes)
        (input_dir / "restyle_prompt.txt").write_bytes(
            (compiled.restyle_prompt + "\n").encode("utf-8")
        )
        inputs = {
            name: _record(input_dir / filename, staging)
            for name, filename in (
                ("annotation", "annotation.json"),
                ("actor_trajectory", "actor_trajectory.json"),
                ("camera_trajectory", "camera_trajectory.json"),
                ("compiled_prompt", "compiled_prompt.txt"),
                ("trajectory_prompt", "trajectory_prompt.txt"),
                ("restyle_prompt", "restyle_prompt.txt"),
            )
        }
        job = {
            "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION, "job_id": f"render-{expected_id}",
            "iteration_id": expected_id, "parent_iteration_id": state["current_iteration"],
            "status": "queued", "created_at": _now(), "updated_at": _now(),
            "trajectory_compiler_version": PROMPT_COMPILER_VERSION,
            "restyle_compiler_version": RESTYLE_COMPILER_VERSION,
            "source": {
                "restyle_profile": workspace["document"]["source"]["restyle_profile"]
            },
            "inputs": inputs, "stages": [], "error": None,
        }
        (staging / "job.json").write_bytes(_json_bytes(job))
        os.replace(staging, directory)
        return directory / "job.json"
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _update_job(job_path: Path, **changes: object) -> dict[str, Any]:
    with _JOB_LOCK:
        job = _read(job_path, "render job")
        _require_current_schema(job, "render job")
        job.update(changes)
        job["updated_at"] = _now()
        _write_atomic(job_path, _json_bytes(job))
        return job


def publish_iteration(
    manifest_path: Path | str, job_path: Path | str,
    diagnostic_source: Path | str, clay_source: Path | str,
) -> Path:
    with _JOB_LOCK:
        workspace = verify_workspace(manifest_path)
        root = workspace["root"]
        state_path = root / "state.json"
        job_path = Path(job_path).resolve(strict=True)
        staging = root / f".publish-{uuid4().hex}.staging"
        staging.mkdir()
        backups: list[tuple[Path, Path | bytes | None]] = []
        commit_started = False
        try:
            try:
                job_bytes = job_path.read_bytes()
                state_bytes = state_path.read_bytes()
            except OSError as exc:
                raise DirectorLoopError(f"cannot snapshot publication state: {exc}") from exc
            original = staging / "original"
            original.mkdir()
            staged_job_original = original / "job.json"
            staged_state_original = original / "state.json"
            staged_job_original.write_bytes(job_bytes)
            staged_state_original.write_bytes(state_bytes)
            job = _object_from_bytes(job_bytes, "render job")
            state = _object_from_bytes(state_bytes, "director loop state")
            _require_current_schema(job, "render job")
            _require_current_schema(state, "director loop state")
            if state != workspace["state"]:
                raise DirectorLoopError("director loop state changed before publication snapshot")

            iteration_id = job.get("iteration_id")
            if (
                not isinstance(iteration_id, str)
                or job_path.parent != root / "iterations" / iteration_id
            ):
                raise DirectorLoopError("render job is outside its iteration")
            if job.get("status") not in {"queued", "validating"}:
                raise DirectorLoopError("render job is not publishable")
            if job.get("parent_iteration_id") != state["current_iteration"]:
                raise DirectorLoopError("render job parent is stale")
            if (
                job.get("trajectory_compiler_version") != PROMPT_COMPILER_VERSION
                or job.get("restyle_compiler_version") != RESTYLE_COMPILER_VERSION
            ):
                raise DirectorLoopError("render job compiler version binding is invalid")

            staged_workspace = dict(workspace)
            staged_workspace["state"] = state
            input_snapshots = _snapshot_compiled_job_inputs(
                staged_workspace, job_path, job, staging
            )
            job_source = job.get("source")
            profile_record = workspace["document"]["source"]["restyle_profile"]
            if (
                not isinstance(job_source, Mapping)
                or set(job_source) != {"restyle_profile"}
                or job_source["restyle_profile"] != profile_record
            ):
                raise DirectorLoopError("render job restyle_profile binding is invalid")

            diagnostic_source = Path(diagnostic_source).resolve(strict=True)
            clay_source = Path(clay_source).resolve(strict=True)
            staged_diagnostic = staging / "media" / "diagnostic.mp4"
            staged_clay = staging / "media" / "clay.mp4"
            _snapshot_source_file(
                diagnostic_source, staged_diagnostic, "diagnostic source"
            )
            _snapshot_source_file(clay_source, staged_clay, "clay source")
            timeline = workspace["document"]["timeline"]
            _probe_media_pair(
                staged_diagnostic, staged_clay, timeline, "staged publication"
            )

            directory = job_path.parent
            diagnostic = directory / "diagnostic.mp4"
            clay = directory / "clay.mp4"
            iteration_path = directory / "iteration.json"
            approval_path = directory / "approval.json"
            if iteration_path.exists():
                raise DirectorLoopError("iteration manifest already exists")
            if approval_path.exists():
                raise DirectorLoopError("approval already exists before publication")

            backups.extend(
                (target, data)
                for target, _staged, data in input_snapshots.values()
            )
            media_backup = staging / "original" / "media"
            backups.extend([
                (
                    diagnostic,
                    _backup_if_present(
                        diagnostic, media_backup / "diagnostic.mp4", "diagnostic media"
                    ),
                ),
                (
                    clay,
                    _backup_if_present(clay, media_backup / "clay.mp4", "clay media"),
                ),
                (iteration_path, None),
                (job_path, job_bytes),
                (state_path, state_bytes),
            ])

            sealed_input_bytes = _verify_compiler_artifact_paths(
                staged_workspace,
                {
                    name: staged
                    for name, (_target, staged, _data) in input_snapshots.items()
                },
                "staged publication",
                trajectory_version=job.get("trajectory_compiler_version"),
                restyle_version=job.get("restyle_compiler_version"),
            )
            sealed_media = _probe_media_pair(
                staged_diagnostic, staged_clay, timeline, "staged publication"
            )

            document = {
                "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION,
                "iteration_id": iteration_id,
                "parent_iteration_id": job["parent_iteration_id"],
                "status": "succeeded",
                "created_at": job["created_at"],
                "completed_at": _now(),
                "human_authored": True,
                "trajectory_compiler_version": PROMPT_COMPILER_VERSION,
                "restyle_compiler_version": RESTYLE_COMPILER_VERSION,
                "inputs": {
                    name: _snapshot_record(target, root, sealed_input_bytes[name])
                    for name, (target, _staged, _data) in input_snapshots.items()
                },
                "source": {"restyle_profile": profile_record},
                "diagnostic": _media_record_from_probe(
                    diagnostic, root, sealed_media["diagnostic"]
                ),
                "clay": _media_record_from_probe(
                    clay, root, sealed_media["clay"]
                ),
            }
            publish_files = staging / "publish"
            publish_files.mkdir()
            staged_iteration = publish_files / "iteration.json"
            iteration_bytes = _json_bytes(document)
            staged_iteration.write_bytes(iteration_bytes)

            published_job = dict(job)
            published_job.update({
                "status": "succeeded",
                "error": None,
                "updated_at": _now(),
                "iteration_manifest": _snapshot_record(
                    iteration_path, root, iteration_bytes
                ),
            })
            staged_job = publish_files / "job.json"
            staged_job.write_bytes(_json_bytes(published_job))
            published_state = dict(state)
            published_state["current_iteration"] = iteration_id
            published_state["next_iteration"] = int(iteration_id[1:]) + 1
            staged_state = publish_files / "state.json"
            staged_state.write_bytes(_json_bytes(published_state))

            if job_path.read_bytes() != job_bytes or state_path.read_bytes() != state_bytes:
                raise DirectorLoopError("job or state changed before publication commit")

            commit_plan = [
                (staged, target)
                for target, staged, _data in input_snapshots.values()
            ] + [
                (staged_diagnostic, diagnostic),
                (staged_clay, clay),
                (staged_iteration, iteration_path),
                (staged_job, job_path),
                (staged_state, state_path),
            ]
            commit_started = True
            for source, target in commit_plan:
                _atomic_copy(source, target)
            verify_workspace(manifest_path)
            return iteration_path
        except BaseException as exc:
            if commit_started:
                try:
                    _restore_publication(backups)
                except BaseException as rollback_exc:
                    raise DirectorLoopError(
                        f"publication failed ({exc}); rollback failed ({rollback_exc})"
                    ) from exc
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def approve_iteration(
    manifest_path: Path | str, iteration_id: str, author_id: str
) -> Path:
    workspace = verify_workspace(manifest_path)
    root = workspace["root"]
    state = workspace["state"]
    if not isinstance(author_id, str) or not author_id.strip():
        raise DirectorLoopError("approval author_id is required")
    if iteration_id == "D0" or iteration_id != state["current_iteration"]:
        raise DirectorLoopError("only the current succeeded human iteration can be approved")
    directory, iteration = _iteration(root, iteration_id)
    if iteration.get("status") != "succeeded":
        raise DirectorLoopError("iteration is not succeeded")
    if state.get("approved_iteration") is not None:
        raise DirectorLoopError("workspace already has an approved iteration")
    approval_path = directory / "approval.json"
    if approval_path.exists():
        raise DirectorLoopError("approval already exists")
    approval = {
        "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION, "approved": True,
        "iteration_id": iteration_id, "author_id": author_id.strip(),
        "approved_at": _now(), "iteration_manifest": _record(directory / "iteration.json", root),
        "trajectory_compiler_version": iteration["trajectory_compiler_version"],
        "restyle_compiler_version": iteration["restyle_compiler_version"],
        "annotation": iteration["inputs"]["annotation"],
        "actor_trajectory": iteration["inputs"]["actor_trajectory"],
        "camera_trajectory": iteration["inputs"]["camera_trajectory"],
        "compiled_prompt": iteration["inputs"]["compiled_prompt"],
        "trajectory_prompt": iteration["inputs"]["trajectory_prompt"],
        "restyle_prompt": iteration["inputs"]["restyle_prompt"],
        "restyle_profile": iteration["source"]["restyle_profile"],
        "diagnostic": iteration["diagnostic"], "clay": iteration["clay"],
    }
    _write_atomic(approval_path, _json_bytes(approval))
    state = dict(state)
    state["approved_iteration"] = iteration_id
    state["approval"] = _record(approval_path, root)
    _write_atomic(root / "state.json", _json_bytes(state))
    return approval_path


def export_approved_iteration(manifest_path: Path | str) -> dict[str, Any]:
    workspace = verify_workspace(manifest_path)
    root = workspace["root"]
    state = workspace["state"]
    iteration_id = state.get("approved_iteration")
    if not isinstance(iteration_id, str):
        raise DirectorLoopError("workspace is not approved")
    approval_path = _verify_record(root, state.get("approval"), "approval")
    approval = _read(approval_path, "approval")
    _require_current_schema(approval, "approval")
    if approval.get("approved") is not True or approval.get("iteration_id") != iteration_id:
        raise DirectorLoopError("approval binding is invalid")
    directory, iteration = _iteration(root, iteration_id)
    _verify_record(root, approval.get("iteration_manifest"), "approved iteration manifest")
    for name, expected_version in (
        ("trajectory_compiler_version", PROMPT_COMPILER_VERSION),
        ("restyle_compiler_version", RESTYLE_COMPILER_VERSION),
    ):
        if (
            iteration.get(name) != expected_version
            or approval.get(name) != expected_version
            or approval.get(name) != iteration.get(name)
        ):
            raise DirectorLoopError(f"approved {name} compiler version binding is invalid")
    for name in (
        "annotation", "actor_trajectory", "camera_trajectory", "compiled_prompt",
        "trajectory_prompt", "restyle_prompt", "restyle_profile", "diagnostic", "clay",
    ):
        _verify_record(root, approval.get(name), f"approved {name}")
        if name in iteration["inputs"]:
            expected = iteration["inputs"][name]
        elif name == "restyle_profile":
            expected = iteration["source"][name]
        else:
            expected = iteration[name]
        if approval[name] != expected:
            raise DirectorLoopError(f"approved {name} hash binding differs from iteration")
    return {
        "iteration_id": iteration_id,
        "trajectory_compiler_version": iteration["trajectory_compiler_version"],
        "restyle_compiler_version": iteration["restyle_compiler_version"],
        "iteration_manifest": _record(directory / "iteration.json", root),
        "approval": _record(approval_path, root),
        "annotation": iteration["inputs"]["annotation"],
        "actor_trajectory": iteration["inputs"]["actor_trajectory"],
        "camera_trajectory": iteration["inputs"]["camera_trajectory"],
        "compiled_prompt": iteration["inputs"]["compiled_prompt"],
        "trajectory_prompt": iteration["inputs"]["trajectory_prompt"],
        "restyle_prompt": iteration["inputs"]["restyle_prompt"],
        "restyle_profile": iteration["source"]["restyle_profile"],
        "diagnostic": iteration["diagnostic"], "clay": iteration["clay"],
    }


def session_document(manifest_path: Path | str) -> dict[str, Any]:
    workspace = verify_workspace(manifest_path)
    root = workspace["root"]
    doc = workspace["document"]
    state = workspace["state"]
    _directory, current = _iteration(root, state["current_iteration"])
    inheritance = derive_inherited_keyframes(workspace)
    result = {
        "schema_version": DIRECTOR_LOOP_SCHEMA_VERSION, "story_id": doc["story_id"],
        "shot_id": doc["shot_id"], "actors": doc["actors"],
        "world_bounds": doc["world_bounds"], "timeline": doc["timeline"],
        "keyframes": doc["keyframes"], "camera_reference": doc["camera_reference"],
        "current": {
            "iteration_id": state["current_iteration"],
            "diagnostic_url": f"/media/{state['current_iteration']}/diagnostic.mp4",
            "clay_url": f"/media/{state['current_iteration']}/clay.mp4",
            "media": {"diagnostic": current["diagnostic"]["media"],
                      "clay": current["clay"]["media"]},
        },
        "next_iteration_id": f"D{state['next_iteration']}",
        "approved_iteration": state["approved_iteration"],
        "human_values_present": state["current_iteration"] != "D0",
        "inherited_keyframes": inheritance["keyframes"],
        "inheritance_sha256": inheritance["inheritance_sha256"],
        "inheritance_source": inheritance["source"],
    }
    if current.get("human_authored") is True:
        annotation_path = _verify_record(
            root, current.get("inputs", {}).get("annotation"), "current annotation"
        )
        result["current_annotation"] = _read(annotation_path, "current annotation")
    job_path = root / "iterations" / result["next_iteration_id"] / "job.json"
    if job_path.is_file():
        pending_job = _read(job_path, "pending render job")
        _require_current_schema(pending_job, "pending render job")
        result["pending_job"] = pending_job
    return result


def byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if type(size) is not int or size <= 0:
        raise DirectorLoopError("media size is invalid")
    if header is None:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise DirectorLoopError("invalid Range header")
    if not match.group(1):
        length = int(match.group(2))
        if length <= 0:
            raise DirectorLoopError("invalid suffix range")
        start, end = max(0, size - length), size - 1
    else:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    if start < 0 or start >= size or end < start:
        raise DirectorLoopError("unsatisfiable Range header")
    return start, min(end, size - 1), True


def run_render_job(manifest_path: Path | str, job_path: Path | str) -> None:
    manifest_path = Path(manifest_path).resolve(strict=True)
    job_path = Path(job_path).resolve(strict=True)
    try:
        workspace = verify_workspace(manifest_path)
        root, doc = workspace["root"], workspace["document"]
        job = _read(job_path, "render job")
        _require_current_schema(job, "render job")
        directory = job_path.parent
        inputs = directory / "input"
        shotscript = _verify_record(root, doc["source"]["shotscript"], "ShotScript")
        stages = []
        for style in ("diagnostic", "clay"):
            _update_job(job_path, status=f"{style}_running", stages=stages)
            output = directory / "renders" / style
            command = [
                sys.executable, "-m", "videoactagent.blender_runner",
                "--blender", doc["blender_path"], "--shotscript", str(shotscript),
                "--trajectory", str(inputs / "actor_trajectory.json"),
                "--camera-trajectory", str(inputs / "camera_trajectory.json"),
                "--output-dir", str(output), "--render-style", style,
                "--fps", str(int(doc["timeline"]["fps"])),
                "--resolution", "x".join(str(v) for v in doc["timeline"]["resolution"]),
                "--timeout", "300",
            ]
            completed = subprocess.run(
                command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=330,
            )
            log = directory / f"{style}.log"
            log.write_text(
                "COMMAND\n" + json.dumps(command) + "\nSTDOUT\n" + completed.stdout
                + "\nSTDERR\n" + completed.stderr,
                encoding="utf-8",
            )
            if completed.returncode != 0 or "TRAJECTORY_PROXY_OK" not in completed.stdout + completed.stderr:
                raise DirectorLoopError(f"real Blender {style} render failed; see {log.name}")
            stages.append({"style": style, "status": "succeeded", "log": _record(log, root)})
        _update_job(job_path, status="validating", stages=stages)
        publish_iteration(
            manifest_path, job_path,
            directory / "renders" / "diagnostic" / "trajectory_proxy.mp4",
            directory / "renders" / "clay" / "trajectory_proxy.mp4",
        )
    except BaseException as exc:
        try:
            _update_job(job_path, status="failed", error=f"{type(exc).__name__}: {exc}")
        except BaseException:
            pass


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
            elif path.startswith("/api/jobs/"):
                job_id = unquote(path.removeprefix("/api/jobs/"))
                if not re.fullmatch(r"render-D[1-9][0-9]*", job_id):
                    raise DirectorLoopError("job ID is invalid")
                self._json(_read(
                    self.root / "iterations" / job_id.removeprefix("render-") / "job.json",
                    "render job",
                ))
            elif path.startswith("/media/"):
                match = re.fullmatch(r"/media/(D(?:0|[1-9][0-9]*))/(diagnostic|clay)\.mp4", path)
                if match is None:
                    raise DirectorLoopError("media path is invalid")
                directory, _doc = _iteration(self.root, match.group(1))
                self._video(directory / f"{match.group(2)}.mp4")
            elif path.startswith("/files/"):
                relative = unquote(path.removeprefix("/files/"))
                self._send(_safe(self.root, relative, "served file").read_bytes(), "image/png")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (DirectorLoopError, OSError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2 * 1024 * 1024:
                raise DirectorLoopError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise DirectorLoopError("request body must be one object")
            if path == "/api/prompt-preview":
                self._json(preview_prompt(self.manifest, payload))
                return
            if path == "/api/iterations":
                job = prepare_iteration(self.manifest, payload)
                Thread(target=run_render_job, args=(self.manifest, job), daemon=True).start()
                self._json({"job_id": _read(job, "job")["job_id"], "status": "queued"}, HTTPStatus.ACCEPTED)
                return
            match = re.fullmatch(r"/api/iterations/(D[1-9][0-9]*)/approve", path)
            if match is not None and set(payload) == {"author_id"}:
                approval = approve_iteration(self.manifest, match.group(1), payload["author_id"])
                self._json({"approved": True, "approval_sha256": _sha(approval)})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (DirectorLoopError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _video(self, path: Path) -> None:
        start, end, partial = byte_range(self.headers.get("Range"), path.stat().st_size)
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{path.stat().st_size}")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(_json_bytes(value), "application/json", status)

    def _send(self, data: bytes, media: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        return


def serve_workspace(manifest_path: Path | str, port: int = 8769) -> None:
    manifest = Path(manifest_path).resolve(strict=True)
    workspace = verify_workspace(manifest)
    _Handler.manifest = manifest
    _Handler.root = workspace["root"]
    _Handler.html = (Path(__file__).parents[1] / "static" / "director_panel.html").read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"DIRECTOR_LOOP_SERVING http://127.0.0.1:{port}")
    server.serve_forever()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--blender", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--restyle-profile", type=Path)
    serve = sub.add_parser("serve")
    serve.add_argument("--manifest", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8769)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_workspace(
                args.bundle, args.blender, args.output_dir,
                restyle_profile_path=args.restyle_profile,
            )
            print(f"DIRECTOR_LOOP_PREPARED {result}")
        else:
            if not 1 <= args.port <= 65535:
                raise DirectorLoopError("port must be in 1..65535")
            serve_workspace(args.manifest, args.port)
    except (DirectorLoopError, ValueError, OSError) as exc:
        print(f"DIRECTOR_LOOP_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
