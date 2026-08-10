"""Deterministic verification boundary for rendered Blender Proxy bundles.

The verifier checks provenance, plan-to-log consistency and real media files. It
does not make visual quality claims; those remain human/VLM review outcomes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .state import WorldState


VERIFIER_SCHEMA_VERSION = "proxy-verifier-1.0"
FEEDBACK_CATEGORIES = (
    "scene_structure",
    "character_trajectory",
    "object_trajectory",
    "camera_trajectory",
    "physical_event",
    "appearance_only",
)
_TOLERANCE = 1e-4


class ProxyVerifierError(ValueError):
    """Raised when verifier inputs are missing or malformed."""


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> object:
    if not path.is_file():
        raise ProxyVerifierError(f"{label} is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyVerifierError(f"{label} is not valid JSON: {path}") from exc


def _finite_vector(value: object, length: int = 3) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            return None
        result.append(float(item))
    return result


def _vector_error(actual: object, expected: object) -> float | None:
    a = _finite_vector(actual)
    e = _finite_vector(expected)
    if a is None or e is None:
        return None
    return max(abs(left - right) for left, right in zip(a, e))


def _state_frames(value: object) -> list[dict[str, Any]]:
    frames = value.get("frames") if isinstance(value, Mapping) else value
    if not isinstance(frames, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in frames:
        if not isinstance(item, Mapping) or not isinstance(item.get("frame"), int):
            continue
        entities = item.get("entities")
        if isinstance(entities, list):
            # Legacy Blender runs stored one row per entity using location /
            # rotation_radians_xyz. Normalize them into the v2 mapping shape
            # without changing the source artifact.
            entity_map: dict[str, dict[str, Any]] = {}
            for row in entities:
                if not isinstance(row, Mapping):
                    continue
                entity_id = row.get("object_id") or row.get("id")
                if not isinstance(entity_id, str):
                    continue
                position = row.get("position", row.get("location"))
                rotation = row.get("rotation", row.get("rotation_radians_xyz"))
                entity_map[entity_id] = {"position": position, "rotation": rotation}
            normalized.append({"frame": item["frame"], "entities": entity_map})
        elif isinstance(entities, Mapping):
            normalized.append({"frame": item["frame"], "entities": dict(entities)})
        else:
            normalized.append({"frame": item["frame"], "entities": {}})
    return normalized


def _camera_entries(value: object, field: str) -> dict[str, dict[str, Any]]:
    """Normalize both frame-oriented and camera-oriented log formats."""

    result: dict[str, dict[str, Any]] = {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping) and isinstance(item.get("camera_id"), str):
                camera_id = item["camera_id"]
                # Current contract: one object per camera containing authored
                # and applied rows. Preserve it verbatim.
                if isinstance(item.get("authored"), list) or isinstance(item.get("applied"), list):
                    result[camera_id] = dict(item)
                    continue
                # Some real Code Agent outputs used a flat frame-oriented row.
                # Normalize it instead of rejecting an otherwise auditable MP4.
                if isinstance(item.get("frame"), int):
                    entry = result.setdefault(camera_id, {
                        "camera_id": camera_id,
                        "target_id": item.get("target_id", item.get("target_object_id")),
                        "authored": [],
                        "applied": [],
                    })
                    entry["target_id"] = entry.get("target_id") or item.get("target_id", item.get("target_object_id"))
                    authored_rotation = item.get("authored_rotation", item.get("authored_orientation"))
                    applied_rotation = item.get("applied_rotation", item.get("applied_orientation"))
                    entry["authored"].append({
                        "frame": item["frame"],
                        "position": item.get("authored_position", item.get("position")),
                        "rotation": authored_rotation,
                    })
                    entry["applied"].append({
                        "frame": item["frame"],
                        "position": item.get("applied_position", item.get("position")),
                        "rotation": applied_rotation,
                    })
                    continue
                result[camera_id] = dict(item)
            elif isinstance(item, Mapping) and isinstance(item.get("frame"), int) and isinstance(item.get("cameras"), list):
                # Legacy Blender runs used frame-oriented camera rows with
                # object_id/location/rotation_radians_xyz. Keep them auditable
                # by normalizing to the authored-row contract.
                for row in item["cameras"]:
                    if not isinstance(row, Mapping):
                        continue
                    camera_id = row.get("camera_id") or row.get("object_id") or row.get("id")
                    if not isinstance(camera_id, str):
                        continue
                    authored = result.setdefault(camera_id, {"authored": [], "target_id": row.get("target_id")})["authored"]
                    authored.append({
                        "frame": item["frame"],
                        "position": row.get("position", row.get("location")),
                        "rotation": row.get("rotation", row.get("rotation_radians_xyz")),
                    })
        return result
    if not isinstance(value, Mapping):
        return result
    if isinstance(value.get("cameras"), list):
        for item in value["cameras"]:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                result[item["id"]] = dict(item)
        return result
    frames = value.get("frames")
    if isinstance(frames, list):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for frame in frames:
            if not isinstance(frame, Mapping) or not isinstance(frame.get(field), Mapping):
                continue
            entries = frame[field]
            for camera_id, row in entries.items():
                if isinstance(row, Mapping):
                    grouped.setdefault(str(camera_id), []).append({"frame": frame["frame"], **dict(row)})
        for camera_id, rows in grouped.items():
            result[camera_id] = {field: rows}
    return result


def _rows_by_frame(entry: Mapping[str, Any], field: str) -> dict[int, Mapping[str, Any]]:
    rows = entry.get(field, [])
    if isinstance(rows, list):
        return {row["frame"]: row for row in rows if isinstance(row, Mapping) and isinstance(row.get("frame"), int)}
    return {}


def _check(checks: list[dict[str, Any]], check_id: str, category: str, status: str, message: str, **evidence: Any) -> None:
    checks.append({
        "check_id": check_id,
        "category": category,
        "status": status,
        "message": message,
        "evidence": evidence,
    })


def verify_asset_catalog_materialization(*, registry_path: Path | str, asset_log_path: Path | str, proxy_style: str) -> dict[str, Any]:
    """Verify that every asset_humanoid character consumed its catalog asset."""
    if proxy_style != "asset_humanoid":
        return {"check_id": "asset.catalog_materialization", "category": "scene_structure", "status": "skipped", "message": "catalog assets are only required for asset_humanoid", "evidence": {}}
    registry = _read_json(Path(registry_path), "asset registry")
    asset_log = _read_json(Path(asset_log_path), "asset log")
    expected = {
        str(item.get("asset_id")): item for item in registry.get("assets", [])
        if isinstance(item, Mapping) and item.get("kind") == "character"
    } if isinstance(registry, Mapping) else {}
    rows = [item for item in asset_log if isinstance(item, Mapping)] if isinstance(asset_log, list) else []
    actual = {str(item.get("asset_id")): item for item in rows if item.get("kind") == "character"}
    missing = sorted(set(expected) - set(actual))
    duplicate_ids = len([item for item in rows if item.get("kind") == "character"]) != len(actual)
    failures = []
    for entity_id, item in expected.items():
        observed = actual.get(entity_id, {})
        if item.get("source_kind") != "asset_catalog_glb":
            failures.append({"entity_id": entity_id, "error": "registry_source_kind"})
            continue
        if observed.get("source_asset_sha256") != item.get("source_asset_sha256") or not observed.get("rig_map_sha256"):
            failures.append({"entity_id": entity_id, "error": "hash_or_rig_mapping"})
        if observed.get("shared_world_instance") is not True or int(observed.get("parts", 0)) < 1:
            failures.append({"entity_id": entity_id, "error": "instance_missing"})
    passed = bool(expected) and not missing and not duplicate_ids and not failures
    return {
        "check_id": "asset.catalog_materialization",
        "category": "scene_structure",
        "status": "passed" if passed else "failed",
        "message": "catalog assets and rig hashes were consumed once" if passed else "catalog asset materialization is incomplete",
        "evidence": {"expected": sorted(expected), "actual": sorted(actual), "missing": missing, "duplicate_ids": duplicate_ids, "failures": failures},
    }


def verify_shared_world_identity(*, asset_log_path: Path | str, camera_log_path: Path | str, expected_camera_count: int) -> dict[str, Any]:
    """Ensure every authored camera references the same catalog asset hashes."""
    asset_log = _read_json(Path(asset_log_path), "asset log")
    camera_log = _read_json(Path(camera_log_path), "camera log")
    rows = [item for item in asset_log if isinstance(item, Mapping) and item.get("kind") == "character"] if isinstance(asset_log, list) else []
    expected_hashes = {str(item.get("catalog_asset_id")): str(item.get("source_asset_sha256")) for item in rows if item.get("catalog_asset_id") and item.get("source_asset_sha256")}
    cameras = [item for item in camera_log if isinstance(item, Mapping)] if isinstance(camera_log, list) else []
    mismatches = []
    for camera in cameras:
        observed = camera.get("shared_asset_hashes")
        if observed != expected_hashes:
            mismatches.append({"camera_id": camera.get("camera_id"), "observed": observed, "expected": expected_hashes})
    passed = bool(expected_hashes) and len(cameras) == int(expected_camera_count) and not mismatches
    return {
        "check_id": "asset.shared_world_identity",
        "category": "scene_structure",
        "status": "passed" if passed else "failed",
        "message": "all cameras reference one shared asset hash set" if passed else "camera asset hash sets differ or are incomplete",
        "evidence": {"expected_camera_count": expected_camera_count, "actual_camera_count": len(cameras), "expected_hashes": expected_hashes, "mismatches": mismatches},
    }


def _director_plan_check(world: WorldState, plan: Mapping[str, Any], checks: list[dict[str, Any]]) -> None:
    required = {
        "schema_version", "scene_plan", "physical_state_plan",
        "character_trajectory_plan", "object_trajectory_plan", "camera_trajectory_plan",
    }
    missing = sorted(required - set(plan))
    if missing or plan.get("schema_version") != "director-plan-1.0":
        _check(checks, "plan.director_schema", "scene_structure", "failed", "DirectorPlan schema is invalid", missing=missing)
        return
    scene = plan.get("scene_plan")
    world_doc = world.to_dict()
    world_scene = world_doc["scene_plan"]
    scene_id = scene.get("scene_id") if isinstance(scene, Mapping) else None
    if scene_id != world_scene["scene_id"]:
        _check(checks, "plan.director_scene", "scene_structure", "failed", "DirectorPlan scene id differs from WorldState", expected=world_scene["scene_id"], actual=scene_id)
    else:
        _check(checks, "plan.director_scene", "scene_structure", "passed", "DirectorPlan scene id matches WorldState", scene_id=scene_id)


def verify_proxy(
    *,
    world_state_path: Path | str,
    render_output_dir: Path | str,
    director_plan_path: Path | str | None = None,
    code_agent_evidence_path: Path | str | None = None,
    generated_script_path: Path | str | None = None,
    probe_video: Callable[[Path], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify a real Proxy bundle and return an auditable report.

    A missing visual review is reported as ``unknown`` and keeps the verdict at
    ``pending_review``. It is never silently treated as success.
    """

    world_path = Path(world_state_path).resolve()
    output = Path(render_output_dir).resolve()
    world = WorldState.from_dict(_read_json(world_path, "WorldState"))
    world_doc = world.to_dict()
    manifest_path = output / "render_manifest.json"
    manifest = _read_json(manifest_path, "render_manifest")
    if not isinstance(manifest, Mapping):
        raise ProxyVerifierError("render_manifest must be an object")

    source_hashes: dict[str, str] = {"world_state": sha256_file(world_path), "render_manifest": sha256_file(manifest_path)}
    if code_agent_evidence_path is not None:
        evidence_path = Path(code_agent_evidence_path).resolve()
        source_hashes["code_agent_evidence"] = sha256_file(evidence_path)
    if generated_script_path is not None:
        script_path = Path(generated_script_path).resolve()
        source_hashes["generated_script"] = sha256_file(script_path)

    checks: list[dict[str, Any]] = []
    expected_hash = world.world_state_hash()
    if manifest.get("world_state_hash") == expected_hash:
        _check(checks, "provenance.world_state_hash", "provenance", "passed", "manifest binds the input WorldState", world_state_hash=expected_hash)
    else:
        _check(checks, "provenance.world_state_hash", "provenance", "failed", "manifest WorldState hash differs", expected=expected_hash, actual=manifest.get("world_state_hash"))

    expected_scene = world_doc["scene_plan"]
    metadata_ok = (
        manifest.get("schema_version") == "pipeline-v2-render-manifest-1.0"
        and manifest.get("frame_count") == world.frame_count
        and manifest.get("fps") == world.fps
        and manifest.get("resolution") == [640, 360]
    )
    _check(checks, "manifest.metadata", "scene_structure", "passed" if metadata_ok else "failed", "render manifest metadata matches WorldState" if metadata_ok else "render manifest metadata differs", frame_count=manifest.get("frame_count"), fps=manifest.get("fps"), resolution=manifest.get("resolution"))

    state_raw = _read_json(output / "state_log.json", "state_log")
    camera_raw = _read_json(output / "camera_log.json", "camera_log")
    applied_raw = _read_json(output / "applied_state_log.json", "applied_state_log")
    state_frames = _state_frames(state_raw)
    state_by_frame = {frame["frame"]: frame for frame in state_frames}
    entity_ids = {entity["id"] for entity in expected_scene["entities"]}
    observed_ids = set(state_frames[0].get("entities", {})) if state_frames else set()
    _check(checks, "scene.entities", "scene_structure", "passed" if observed_ids == entity_ids else "failed", "state log entities match ScenePlan" if observed_ids == entity_ids else "state log entities differ", expected=sorted(entity_ids), actual=sorted(observed_ids))
    _check(checks, "scene.state_frames", "scene_structure", "passed" if len(state_frames) == world.frame_count else "failed", "state log covers every frame" if len(state_frames) == world.frame_count else "state log frame count differs", expected=world.frame_count, actual=len(state_frames))

    events = world_doc["physical_state_plan"]["events"]
    event_ok = all(0 <= event["frame"] < world.frame_count and set(event["participants"]).issubset(entity_ids) for event in events)
    _check(checks, "physical.events", "physical_event", "passed" if event_ok else "failed", "physical event references are within the timeline" if event_ok else "physical event references are invalid", event_count=len(events))

    trajectories = (("character_trajectory_plan", "character_trajectory"), ("object_trajectory_plan", "object_trajectory"))
    for plan_key, category in trajectories:
        max_position_error = 0.0
        max_rotation_error = 0.0
        missing: list[dict[str, Any]] = []
        for track in world_doc[plan_key]["tracks"]:
            for point in track["points"]:
                row = state_by_frame.get(point["frame"], {}).get("entities", {}).get(track["target_id"])
                if not isinstance(row, Mapping):
                    missing.append({"target_id": track["target_id"], "frame": point["frame"]})
                    continue
                position_error = _vector_error(row.get("position"), point["position"])
                rotation_error = _vector_error(row.get("rotation"), point["rotation"])
                if position_error is None or rotation_error is None:
                    missing.append({"target_id": track["target_id"], "frame": point["frame"]})
                    continue
                max_position_error = max(max_position_error, position_error)
                max_rotation_error = max(max_rotation_error, rotation_error)
        passed = not missing and max_position_error <= _TOLERANCE and max_rotation_error <= _TOLERANCE
        _check(checks, f"trajectory.{category}", category, "passed" if passed else "failed", "trajectory keyframes match state log" if passed else "trajectory keyframes differ from state log", max_position_error=max_position_error, max_rotation_error=max_rotation_error, missing=missing)

    camera_entries = _camera_entries(camera_raw, "cameras")
    applied_entries = _camera_entries(applied_raw, "cameras")
    # A real flat Code Agent log may keep applied camera rows alongside the
    # authored rows in camera_log while applied_state_log contains only entity
    # transforms. Reuse those explicitly present applied rows; do not infer
    # them when the camera log has no applied data.
    if not applied_entries and camera_entries and all(isinstance(entry.get("applied"), list) for entry in camera_entries.values()):
        applied_entries = camera_entries
    camera_specs = world_doc["camera_trajectory_plan"]["cameras"]
    camera_ids = {spec["id"] for spec in camera_specs}
    manifest_ids = {item.get("camera_id") for item in manifest.get("videos", []) if isinstance(item, Mapping)}
    _check(checks, "camera.ids", "camera_trajectory", "passed" if manifest_ids == camera_ids else "failed", "manifest camera ids match plan" if manifest_ids == camera_ids else "manifest camera ids differ", expected=sorted(camera_ids), actual=sorted(manifest_ids))
    for spec in camera_specs:
        entry = camera_entries.get(spec["id"])
        applied = applied_entries.get(spec["id"])
        authored_rows = _rows_by_frame(entry or {}, "authored")
        applied_rows = _rows_by_frame(applied or {}, "applied")
        max_position_error = 0.0
        max_rotation_error = 0.0
        missing: list[int] = []
        for point in spec["points"]:
            row = authored_rows.get(point["frame"])
            if row is None:
                missing.append(point["frame"])
                continue
            position_error = _vector_error(row.get("position"), point["position"])
            rotation_error = _vector_error(row.get("rotation"), point["rotation"])
            if position_error is None or rotation_error is None:
                missing.append(point["frame"])
                continue
            max_position_error = max(max_position_error, position_error)
            max_rotation_error = max(max_rotation_error, rotation_error)
        target_id = entry.get("target_id") if entry else None
        passed = entry is not None and applied is not None and target_id == spec["target"].get("object_id") and not missing and max_position_error <= _TOLERANCE and max_rotation_error <= _TOLERANCE and len(applied_rows) == world.frame_count
        _check(checks, f"camera.{spec['id']}", "camera_trajectory", "passed" if passed else "failed", "camera authored/applied logs match CameraTrajectoryPlan" if passed else "camera logs differ from CameraTrajectoryPlan", target_id=target_id, expected_target=spec["target"].get("object_id"), max_position_error=max_position_error, max_rotation_error=max_rotation_error, missing=missing, applied_frames=len(applied_rows))

    constraints = world_doc["physical_state_plan"].get("parameters", {}).get("motion_constraints", [])
    grounded_ids = [entity["id"] for entity in expected_scene["entities"] if any(entity["id"].lower() in str(item).lower() and "grounded" in str(item).lower() for item in constraints)]
    grounding_errors: dict[str, float] = {}
    for entity_id in grounded_ids:
        grounding_errors[entity_id] = max((abs(float(frame.get("entities", {}).get(entity_id, {}).get("position", [0, 0, 0])[2])) for frame in state_frames), default=float("inf"))
    if grounded_ids:
        grounding_ok = all(error <= 0.02 for error in grounding_errors.values())
        _check(checks, "physical.grounding", "physical_event", "passed" if grounding_ok else "failed", "grounded entities remain on the floor" if grounding_ok else "grounding constraint is violated", errors=grounding_errors)
    else:
        _check(checks, "physical.grounding", "physical_event", "unknown", "no explicit grounded constraint was provided")

    media_results: list[dict[str, Any]] = []
    videos = manifest.get("videos") if isinstance(manifest.get("videos"), list) else []
    media_ok = True
    for item in videos:
        if not isinstance(item, Mapping):
            media_ok = False
            continue
        relative = Path(str(item.get("path", "")))
        path = output / relative
        valid_path = bool(str(item.get("path", ""))) and not relative.is_absolute() and ".." not in relative.parts
        exists = valid_path and path.is_file() and path.stat().st_size > 0
        digest = sha256_file(path) if exists else None
        item_ok = exists and digest == item.get("sha256") and path.stat().st_size == item.get("bytes")
        metadata: dict[str, Any] = {"camera_id": item.get("camera_id"), "path": item.get("path"), "manifest": dict(item), "hash_matches": digest == item.get("sha256") if exists else False}
        if probe_video is not None and exists:
            probed = dict(probe_video(path))
            metadata["probe"] = probed
            item_ok = item_ok and str(probed.get("nb_frames")) == str(world.frame_count) and str(probed.get("width")) == "640" and str(probed.get("height")) == "360" and str(probed.get("r_frame_rate")) == f"{world.fps}/1" and abs(float(probed.get("duration", 0.0)) - world.frame_count / world.fps) <= 0.02
        media_results.append(metadata)
        media_ok = media_ok and item_ok
    _check(checks, "media.files", "media", "passed" if media_ok and len(videos) == world.camera_count else "failed", "all manifest videos are real and hash-matched" if media_ok and len(videos) == world.camera_count else "one or more manifest videos are invalid", video_count=len(videos))
    if probe_video is None:
        _check(checks, "media.ffprobe", "media", "unknown", "no real ffprobe probe was supplied")
    else:
        _check(checks, "media.ffprobe", "media", "passed" if media_ok else "failed", "real ffprobe metadata matches the manifest" if media_ok else "real ffprobe metadata differs")

    if code_agent_evidence_path is not None:
        evidence = _read_json(Path(code_agent_evidence_path), "CodeAgent evidence")
        evidence_ok = isinstance(evidence, Mapping) and evidence.get("status") == "succeeded"
        script_line_ending_unknown = False
        if generated_script_path is not None and isinstance(evidence, Mapping):
            expected_script_hash = evidence.get("script_validation", {}).get("script_sha256") if isinstance(evidence.get("script_validation"), Mapping) else None
            evidence_ok = evidence_ok and expected_script_hash == source_hashes["generated_script"]
            if not evidence_ok and expected_script_hash:
                actual_bytes = Path(generated_script_path).read_bytes()
                normalized_hash = hashlib.sha256(actual_bytes.replace(b"\r\n", b"\n")).hexdigest()
                script_line_ending_unknown = normalized_hash == expected_script_hash
        if script_line_ending_unknown:
            _check(checks, "provenance.code_agent", "provenance", "unknown", "CodeAgent hash matches only after Windows line-ending normalization; rerun with byte-preserving writer")
        else:
            _check(checks, "provenance.code_agent", "provenance", "passed" if evidence_ok else "failed", "CodeAgent evidence binds the generated script" if evidence_ok else "CodeAgent evidence does not bind the generated script")

    if director_plan_path is not None:
        plan = _read_json(Path(director_plan_path), "DirectorPlan")
        if isinstance(plan, Mapping):
            source_hashes["director_plan"] = sha256_file(Path(director_plan_path))
            _director_plan_check(world, plan, checks)
    else:
        _check(checks, "plan.director_source", "scene_structure", "unknown", "no original DirectorPlan was supplied")

    _check(checks, "visual.human_or_vlm", "visual_review", "unknown", "visual quality and creative intent require human or VLM review")
    failed = [item for item in checks if item["status"] == "failed"]
    unknown = [item for item in checks if item["status"] == "unknown"]
    suggested = sorted({item["category"] for item in failed if item["category"] in FEEDBACK_CATEGORIES})
    return {
        "schema_version": VERIFIER_SCHEMA_VERSION,
        "verdict": "fail" if failed else ("pending_review" if unknown else "pass"),
        "required_human_review": bool(unknown),
        "source_hashes": source_hashes,
        "world_state_hash": expected_hash,
        "checks": checks,
        "media": {"videos": media_results},
        "allowed_feedback_categories": list(FEEDBACK_CATEGORIES),
        "suggested_feedback_categories": suggested,
    }
