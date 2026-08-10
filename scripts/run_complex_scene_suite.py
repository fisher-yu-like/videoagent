"""Run three complex prompt -> shared-world Proxy -> Seedance chains.

The Blender script is materialized locally by Codex from an explicit planner
spec.  It is deliberately deterministic and keeps one world/timeline for all
four cameras.  Seedance is submitted once per scene with the selected master
camera; no retries are hidden in this runner.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import os
import shutil
import re
import subprocess
import sys
import textwrap
import time
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_v2.blender_sandbox import run_blender_sandbox
from pipeline_v2.appearance_prompt import compile_appearance_prompt
from pipeline_v2.backend_adapter import prepare_backend_adapter, write_backend_adapter_bundle
from pipeline_v2.final_video_verifier import aggregate_final_video_reports, verify_final_video, write_final_video_report
from pipeline_v2.proxy_verifier import verify_asset_catalog_materialization, verify_proxy, verify_shared_world_identity
from pipeline_v2.vlm_feedback import request_vlm_feedback
from pipeline_v2.state import SCHEMA_VERSION, WorldState
from videoactagent.asset_catalog import AssetCatalogError, AssetSpec, load_catalog
from videoactagent.backends.jd import (
    build_seedance_reference_video,
    build_seedance_t2v,
    download_once,
    extract_status,
    query_once,
    submit_once,
)
from videoactagent.seedance_auto_chain import run_uploaded_camera_jobs
from videoactagent.complex_scene_prompts_v2 import iter_scene_specs, scene_spec
from videoactagent.run_record import RunDirectory
from videoactagent.seedance_upload import (
    load_upload_config,
    normalize_proxy,
    upload_proxy,
    write_upload_record,
)


SEEDANCE_SUBMISSION_BUDGET = 3
PROXY_STYLE_CHOICES = ("clay", "canonical", "storyhuman", "skeleton", "asset_humanoid", "diagnostic")
SUCCESS_STATUSES = {"success", "succeeded", "completed", "done"}
FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "expired"}
FFPROBE = Path(r"D:\ACLOS\Cross\recorder-release\ffprobe.exe")
FFMPEG = Path(r"D:\ACLOS\Cross\recorder-release\ffmpeg.exe")


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_style_choices() -> tuple[str, ...]:
    return PROXY_STYLE_CHOICES


def output_dir_for(root: Path | str, scene_id: str, run_id: str) -> Path:
    return Path(root).resolve() / f"{scene_id}_{run_id}"


def select_seedance_camera(spec: Mapping[str, Any]) -> dict[str, Any]:
    cameras = spec.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("scene has no cameras")
    for camera in cameras:
        if camera.get("camera_id") == "master":
            return dict(camera)
    return dict(cameras[0])


def select_seedance_cameras(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return every authored camera in manifest order for independent tasks."""
    cameras = spec.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("scene has no cameras")
    return [dict(camera) for camera in cameras]


def compile_scene_world(spec: Mapping[str, Any]) -> WorldState:
    """Map the explicit scene spec into the unchanged pipeline_v2 contract."""
    tracks = list(spec["tracks"])
    character_tracks = [
        {"target_id": row["target_id"], "points": row["points"]}
        for row in tracks if row["kind"] == "character"
    ]
    object_tracks = [
        {"target_id": row["target_id"], "points": row["points"]}
        for row in tracks if row["kind"] == "object"
    ]
    cameras = []
    for camera in spec["cameras"]:
        cameras.append({
            "id": camera["camera_id"],
            "role": camera["role"],
            "target": {"object_id": camera["target"]},
            "lens_mm": camera["lens_mm"],
            "roll_deg": camera["roll_deg"],
            "points": camera["points"],
        })
    # SceneSpec may carry asset_id for the asset catalog, but pipeline_v2's
    # WorldState entity contract is intentionally strict and remains unchanged.
    world_entities = [
        {key: entity[key] for key in ("id", "kind", "asset")}
        for entity in spec["entities"]
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "scene_plan": {
            "scene_id": spec["scene_id"],
            "environment_preset": "neutral_shared_world_park_or_market",
            "duration_seconds": 5.0,
            "fps": 24,
            "frame_count": 120,
            "entities": world_entities,
        },
        "physical_state_plan": {
            "events": spec["physical_events"],
            "parameters": {
                "motion_constraints": [
                    "all characters and grounded props remain on z=0 unless their track explicitly lifts them",
                    "coupled objects follow their authored trajectory without teleportation",
                    "camera roll remains zero and target identity remains fixed",
                ],
                "action_phases": spec["action_phases"],
            },
        },
        "character_trajectory_plan": {"tracks": character_tracks},
        "object_trajectory_plan": {"tracks": object_tracks},
        "camera_trajectory_plan": {"cameras": cameras},
    }
    return WorldState.from_dict(document)


def director_plan_for(world: WorldState) -> dict[str, Any]:
    """Adapt WorldState to the verifier's DirectorPlan provenance label."""
    return {**world.to_dict(), "schema_version": "director-plan-1.0"}


def coupling_rules_for(spec: Mapping[str, Any]) -> dict[str, str]:
    """Return hard parent relations implied by authored object semantics.

    These relations are implemented in Blender as shared-world parent
    transforms, not just duplicated absolute keyframes.  That prevents a
    suitcase, backpack, or loaded box from drifting away from the actor/cart
    that owns it when a later revision changes the parent trajectory.
    """
    entity_ids = {str(item.get("id")) for item in spec.get("entities", []) if isinstance(item, Mapping)}
    rules: dict[str, str] = {}
    if {"backpack", "person_a"}.issubset(entity_ids):
        rules["backpack"] = "person_a"
    if {"box_a", "handcart"}.issubset(entity_ids):
        rules["box_a"] = "handcart"
    if {"box_b", "handcart"}.issubset(entity_ids):
        rules["box_b"] = "handcart"
    if {"luggage", "traveler"}.issubset(entity_ids):
        rules["luggage"] = "traveler"
    if {"suitcase", "traveler"}.issubset(entity_ids):
        rules["suitcase"] = "traveler"
    return rules


def asset_registry_for(
    spec: Mapping[str, Any],
    proxy_style: str,
    asset_paths: Mapping[str, str] | None = None,
    asset_catalog_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a deterministic sidecar registry without changing WorldState.

    The strict pipeline_v2 schema intentionally remains unchanged.  This
    registry is the StoryBlender-inspired source of truth for how an entity is
    materialised in Blender and lets old clay runs coexist with canonical runs.
    """
    if proxy_style not in PROXY_STYLE_CHOICES:
        raise ValueError("proxy_style must be one of: " + ", ".join(PROXY_STYLE_CHOICES))
    assets = []
    for entity in spec["entities"]:
        eid = str(entity["id"])
        kind = str(entity["kind"])
        asset_path = (asset_paths or {}).get(eid)
        catalog_record = (asset_catalog_records or {}).get(eid, {})
        if kind == "character" and proxy_style == "asset_humanoid" and asset_path:
            source_kind = "asset_catalog_glb"
            dimensions = [0.95, 0.55, 2.55]
            parts = ["imported_glb", "rigged_armature"]
        elif kind == "character" and proxy_style == "canonical" and asset_path:
            source_kind = "canonical_glb"
            dimensions = [0.95, 0.55, 2.55]
            parts = ["imported_glb"]
        elif kind == "character" and proxy_style == "storyhuman":
            source_kind = "storyhuman_procedural_v1"
            dimensions = [0.95, 0.55, 2.55]
            parts = [
                "head", "hair", "neck", "rounded_torso", "pelvis", "capsule_limbs",
                "shoulders", "elbows", "knees", "hands", "rounded_feet",
            ]
        elif kind == "character" and proxy_style in {"canonical", "skeleton"}:
            source_kind = "procedural_skeleton_v1" if proxy_style == "skeleton" else "canonical_procedural_v3"
            dimensions = [0.95, 0.55, 2.55]
            parts = [
                "head", "neck", "torso", "pelvis", "upper_arm.L", "lower_arm.L",
                "hand.L", "upper_arm.R", "lower_arm.R", "hand.R", "upper_leg.L",
                "lower_leg.L", "foot.L", "upper_leg.R", "lower_leg.R", "foot.R",
            ]
        else:
            source_kind = "primitive_fallback"
            dimensions = [0.9, 0.7, 2.5] if kind == "character" else [1.0, 1.0, 1.0]
            parts = ["primitive"]
        payload = {
            "asset_id": eid,
            "kind": kind,
            "source_kind": source_kind,
            "dimensions_m": dimensions,
            "front_axis": "-Y",
            "up_axis": "Z",
            "parts": parts,
            "material_profile": "neutral_clay",
            "registry_version": "asset-registry-1.1",
            "status": "approved",
        }
        if asset_path:
            payload["path"] = asset_path
        if proxy_style == "asset_humanoid" and kind == "character":
            payload["catalog_asset_id"] = str(entity.get("asset_id", ""))
            payload["source_asset_sha256"] = str(catalog_record.get("source_sha256", ""))
            payload["rig_map_sha256"] = str(catalog_record.get("rig_map_sha256", ""))
            payload["rig_map"] = dict(catalog_record.get("rig_map", {}))
        payload["asset_sha256"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assets.append(payload)
    return {
        "schema_version": "asset-registry-1.1",
        "scene_id": spec["scene_id"],
        "proxy_style": proxy_style,
        "shared_world": True,
        "assets": assets,
    }


def scene_layout_for(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Persist explicit spatial/camera ownership alongside the strict plan."""
    return {
        "schema_version": "scene-layout-1.0",
        "scene_id": spec["scene_id"],
        "relations": list(spec.get("spatial_relations", [])),
        "camera_targets": [
            {"camera_id": camera["camera_id"], "target_id": camera["target"]}
            for camera in spec["cameras"]
        ],
        "trajectory_source": "WorldState.character_trajectory_plan + object_trajectory_plan + camera_trajectory_plan",
    }


def canonical_motion_profile_for(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Compile root tracks into a small, auditable body-motion sidecar.

    StoryBlender separates spatial layout from character animation.  We keep
    the authored root trajectory untouched and add only deterministic limb
    evidence for the proxy: alternating leg swing plus explicit foot-contact
    flags at the same semantic keyframes.  This is a proxy adapter, not an
    IK solver or a claim of photoreal motion.
    """
    characters = []
    revision_id = str(spec.get("revision", {}).get("id", ""))
    try:
        revision_number = int(revision_id.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        revision_number = 0
    tracks = {str(track["target_id"]): track for track in spec.get("tracks", [])}
    for entity in spec.get("entities", []):
        if entity.get("kind") != "character":
            continue
        target_id = str(entity["id"])
        points = list(tracks.get(target_id, {}).get("points", []))
        if not points:
            continue
        positions = [tuple(float(value) for value in point["position"]) for point in points]
        total_distance = sum(
            math.sqrt(sum((positions[index][axis] - positions[index - 1][axis]) ** 2 for axis in range(2)))
            for index in range(1, len(positions))
        )
        moving = total_distance > 0.45
        side_step_lead = target_id == "person_a" and revision_number >= 20
        swing = 0.0 if side_step_lead else (0.34 if moving else 0.16)
        phase = [0.0, swing, -swing, swing * 0.8, 0.0]
        left = [{"frame": int(point["frame"]), "angle": float(value)} for point, value in zip(points, phase)]
        right = [{"frame": int(point["frame"]), "angle": float(-value)} for point, value in zip(points, phase)]
        contacts = []
        for index, point in enumerate(points):
            contacts.append({
                "frame": int(point["frame"]),
                "left": bool(index in {0, 2, 4}),
                "right": bool(index in {0, 1, 3}),
            })
        characters.append({
            "target_id": target_id,
            "root_track_frames": [int(point["frame"]) for point in points],
            "limb_tracks": {"left_leg": left, "right_leg": right},
            "foot_contacts": contacts,
            "motion_mode": "side_step_transfer" if side_step_lead else "alternating_stride" if moving else "grounded_weight_shift",
        })
    return {
        "schema_version": "canonical-motion-profile-1.0",
        "scene_id": str(spec["scene_id"]),
        "characters": characters,
        "source": "authored root trajectory plus deterministic proxy gait adapter",
        "limitations": ["no IK", "no retargeted animation clip", "foot contacts are audit landmarks, not solved constraints"],
    }


def safe_arm_angle(limb: str, angle: float) -> float:
    """Keep an articulated arm from crossing the head/torso corridor.

    The procedural asset rotates around the upper-arm center rather than a
    solved shoulder joint.  We therefore constrain only the inward direction;
    outward gestures retain their authored amplitude.
    """
    value = float(angle)
    if limb == "left_arm":
        return max(-1.25, min(0.78, value))
    if limb == "right_arm":
        return max(-0.78, min(1.25, value))
    return max(-0.78, min(0.78, value))


def skeleton_motion_profile_for(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Compile authored character tracks into explicit local bone landmarks."""
    tracks = {str(track["target_id"]): track for track in spec.get("tracks", [])}
    gestures = {(str(item["target_id"]), str(item["limb"])): item["points"] for item in spec.get("gesture_tracks", [])}
    revision_id = str(spec.get("revision", {}).get("id", ""))
    try:
        revision_number = int(revision_id.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        revision_number = 0
    # Revisions after 013 inherit the same authored gesture contract.  The
    # previous equality check silently dropped the explicit hand landmarks for
    # revisions 014-016, which made later renders visually static again.
    explicit_silhouette = revision_number >= 13

    def hand_landmark(side: str, phase: float) -> tuple[float, float, float]:
        """Map semantic gesture phases to readable, reachable silhouettes."""
        sign = 1.0 if side == "L" else -1.0
        value = float(phase)
        if value >= 0.5:
            return (sign * 0.56, 0.0, 2.58)  # raised above shoulder
        if value <= -0.5:
            return (sign * 1.02, 0.0, 2.04)  # outward side-step silhouette
        return (sign * 0.52, 0.0, 1.40)  # relaxed/down pose

    characters = []
    bone_names = [
        "root", "pelvis", "spine", "head",
        "upper_arm.L", "forearm.L", "hand.L", "upper_arm.R", "forearm.R", "hand.R",
        "upper_leg.L", "lower_leg.L", "foot.L", "upper_leg.R", "lower_leg.R", "foot.R",
    ]
    for entity in spec.get("entities", []):
        if entity.get("kind") != "character":
            continue
        target_id = str(entity["id"])
        points = list(tracks.get(target_id, {}).get("points", []))
        if len(points) < 5:
            continue
        bone_tracks = {name: [] for name in bone_names}
        left_gesture = gestures.get((target_id, "left_arm"), [(0, 0.0), (30, 0.0), (60, 0.0), (90, 0.0), (119, 0.0)])
        right_gesture = gestures.get((target_id, "right_arm"), [(0, 0.0), (30, 0.0), (60, 0.0), (90, 0.0), (119, 0.0)])
        for index, point in enumerate(points[:5]):
            frame = int(point["frame"])
            root_pos = [float(value) for value in point["position"]]
            root_rot = [float(value) for value in point["rotation"]]
            phase_left = float(left_gesture[min(index, len(left_gesture) - 1)][1])
            phase_right = float(right_gesture[min(index, len(right_gesture) - 1)][1])
            for name, local_position, rotation in (
                ("root", (0.0, 0.0, 0.0), root_rot),
                ("pelvis", (0.0, 0.0, 1.28), (0.0, 0.0, 0.0)),
                ("spine", (0.0, 0.0, 1.84), (0.0, 0.0, 0.0)),
                ("head", (0.0, 0.0, 2.72), (0.0, 0.0, 0.0)),
                ("upper_arm.L", (0.50, 0.0, 2.02), (0.0, safe_arm_angle("left_arm", phase_left), 0.0)),
                ("forearm.L", (0.50, 0.0, 1.56), (0.0, safe_arm_angle("left_arm", phase_left), 0.0)),
                ("hand.L", hand_landmark("L", phase_left) if explicit_silhouette else (0.44 - math.sin(safe_arm_angle("left_arm", phase_left)) * 0.55, 0.0, 1.40 + abs(math.sin(safe_arm_angle("left_arm", phase_left))) * 0.70), (0.0, safe_arm_angle("left_arm", phase_left), 0.0)),
                ("upper_arm.R", (-0.50, 0.0, 2.02), (0.0, safe_arm_angle("right_arm", phase_right), 0.0)),
                ("forearm.R", (-0.50, 0.0, 1.56), (0.0, safe_arm_angle("right_arm", phase_right), 0.0)),
                ("hand.R", hand_landmark("R", phase_right) if explicit_silhouette else (-0.44 - math.sin(safe_arm_angle("right_arm", phase_right)) * 0.55, 0.0, 1.40 + abs(math.sin(safe_arm_angle("right_arm", phase_right))) * 0.70), (0.0, safe_arm_angle("right_arm", phase_right), 0.0)),
                ("upper_leg.L", (0.18, 0.0, 0.98), (0.0, 0.0, 0.0)),
                ("lower_leg.L", (0.18, 0.0, 0.42), (0.0, 0.0, 0.0)),
                ("foot.L", (0.18 + (0.12 if index in {1, 3} else -0.04), -0.13, 0.12), (0.0, 0.0, 0.0)),
                ("upper_leg.R", (-0.18, 0.0, 0.98), (0.0, 0.0, 0.0)),
                ("lower_leg.R", (-0.18, 0.0, 0.42), (0.0, 0.0, 0.0)),
                ("foot.R", (-0.18 + (-0.12 if index in {1, 3} else 0.04), -0.13, 0.12), (0.0, 0.0, 0.0)),
            ):
                bone_tracks[name].append({"frame": frame, "position": [root_pos[0] + local_position[0], root_pos[1] + local_position[1], root_pos[2] + local_position[2]], "rotation": list(rotation)})
        characters.append({
            "target_id": target_id,
            "bones": list(bone_names),
            "bone_tracks": bone_tracks,
            "foot_contacts": [{"frame": int(point["frame"]), "left": index % 2 == 0, "right": index % 2 == 1} for index, point in enumerate(points[:5])],
            "joint_limits": {"elbow": [-1.25, 1.25], "knee": [-2.6, 0.1], "head_clearance_m": 0.12},
            "ik_targets": {"hand.L": "hand_target.L", "hand.R": "hand_target.R", "foot.L": "foot_target.L", "foot.R": "foot_target.R"},
        })
    source = "authored root/gesture tracks compiled to explicit down/out/up hand landmarks" if explicit_silhouette else "authored root/gesture tracks compiled to explicit bone landmarks"
    return {"schema_version": "skeleton-motion-profile-1.0", "scene_id": str(spec["scene_id"]), "characters": characters, "source": source}


def readability_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Apply one explicit Director-level camera framing revision.

    Target identities and frame indices remain auditable, while the Director
    revision makes the authored action legible: the dancer has an explicit
    side-step zig-zag, the passerby has a wider crossing lane, and the elevated
    camera is a sustained wide overview rather than another close follow.
    """
    revised = copy.deepcopy(dict(spec))
    for camera in revised["cameras"]:
        for point in camera["points"]:
            point["position"] = [float(value) * 1.35 for value in point["position"]]
        if camera["camera_id"] in {"lateral", "reverse"}:
            camera["lens_mm"] = min(float(camera["lens_mm"]), 32.0)
        elif camera["camera_id"] == "master":
            camera["lens_mm"] = min(float(camera["lens_mm"]), 36.0)
        else:
            camera["lens_mm"] = min(float(camera["lens_mm"]), 40.0)
        if camera["camera_id"] == "elevated":
            camera["target"] = "person_b"
            camera["lens_mm"] = 28.0
    for track in revised.get("tracks", []):
        if track.get("target_id") == "person_a":
            # Preserve the left-to-center progression while making the two
            # side-step phases observable in the shared-world coordinates.
            y_values = (-1.0, -0.85, 0.80, -0.75, 0.65)
            for point, y_value in zip(track["points"], y_values):
                point["position"][1] = y_value
        elif track.get("target_id") == "person_b":
            for point in track["points"]:
                point["position"][0] = float(point["position"][0]) + 0.70
        elif track.get("target_id") == "person_c":
            for point in track["points"]:
                point["position"][1] = float(point["position"][1]) + 1.10
        elif track.get("target_id") == "backpack":
            person_a = next(item for item in revised["tracks"] if item.get("target_id") == "person_a")
            for point, actor_point in zip(track["points"], person_a["points"]):
                point["position"] = [float(actor_point["position"][0]), float(actor_point["position"][1]) + 0.55, 0.0]
        elif track.get("target_id") == "speaker":
            person_b = next(item for item in revised["tracks"] if item.get("target_id") == "person_b")
            for point, actor_point in zip(track["points"], person_b["points"]):
                point["position"] = [float(actor_point["position"][0]) + 1.05, float(actor_point["position"][1]), 0.0]
    for gesture in revised.get("gesture_tracks", []):
        if gesture.get("target_id") == "person_a":
            gesture["points"] = [(0, 0.0), (30, -1.55), (60, 1.55), (90, -1.55), (119, 0.0)] if gesture.get("limb") == "left_arm" else [(0, 0.0), (30, 1.35), (60, -1.35), (90, 1.45), (119, 0.0)]
        elif gesture.get("target_id") in {"person_b", "person_c"}:
            gesture["points"] = [(0, 0.0), (30, 0.0), (60, 1.2), (90, 1.2), (119, 0.0)]
    revised["revision"] = {"id": "revision_005", "reason": "VLM found the dance phases, passerby crossing, and sustained wide coverage visually ambiguous", "preserved": ["shared world", "camera roles", "frame indices", "backpack coupling relation", "speaker grounded relation"]}
    return revised


def storyblender_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a layout/camera reflection without rewriting authored motion.

    The failed Stage-A review identified staging, coverage, and gesture
    ambiguity, not a new story.  This revision keeps every entity/root/object
    track equivalent, amplifies only the authored gesture phases, and widens
    camera blocking so the continuity graph remains readable in all views.
    """
    revised = copy.deepcopy(dict(spec))
    for camera in revised.get("cameras", []):
        if camera.get("camera_id") == "elevated":
            camera["points"] = [
                {**point, "position": [float(point["position"][0]) * 1.15, float(point["position"][1]) * 1.15, float(point["position"][2]) * 1.02]}
                for point in camera.get("points", [])
            ]
            camera["lens_mm"] = min(float(camera.get("lens_mm", 42.0)), 32.0)
        else:
            camera["points"] = [
                {**point, "position": [float(value) * 1.35 for value in point["position"]]}
                for point in camera.get("points", [])
            ]
            camera["lens_mm"] = min(float(camera.get("lens_mm", 42.0)), 36.0)
    for gesture in revised.get("gesture_tracks", []):
        if gesture.get("target_id") == "person_a" and gesture.get("limb") == "left_arm":
            gesture["points"] = [(0, 0.0), (30, -1.45), (60, 1.50), (90, -1.25), (119, 0.0)]
        elif gesture.get("target_id") == "person_a" and gesture.get("limb") == "right_arm":
            gesture["points"] = [(0, 0.0), (30, 1.15), (60, -1.10), (90, 1.25), (119, 0.0)]
        elif gesture.get("target_id") in {"person_b", "person_c"}:
            gesture["points"] = [(0, 0.0), (30, 0.0), (60, 1.15), (90, 1.35), (119, 0.0)]
    revised["revision"] = {
        "id": "revision_006",
        "parent_revision": "revision_005",
        "reason": "StoryBlender-style reflection widened staging after the first Stage-A review found clustered actors, occluded props, and unreadable camera coverage",
        "preserved": ["all entity root tracks", "all object coupling relations", "shared world", "camera roles", "frame indices"],
    }
    return revised


def arm_clearance_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create the next immutable revision after the arm/head defect review."""
    revised = storyblender_revision(spec)
    revised["revision"] = {
        "id": "revision_007",
        "parent_revision": "revision_006",
        "reason": "CodeAgent repaired arm pose pivot/clearance: inward swing is constrained and upper-arm center no longer lifts into the head",
        "preserved": ["StoryBlender revision_006 staging", "all entity root tracks", "all object coupling relations", "camera roles", "frame indices"],
    }
    return revised


def skeleton_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create immutable revision_008 for the procedural skeleton/IK branch."""
    revised = arm_clearance_revision(spec)
    revised["revision"] = {
        "id": "revision_008",
        "parent_revision": "revision_007",
        "reason": "add procedural armature, hand/foot IK targets, pole targets, and explicit per-bone trajectories while preserving root/object/camera plans",
        "preserved": ["revision_007 staging", "all entity root tracks", "all object coupling relations", "camera roles", "frame indices"],
    }
    return revised


def camera_staging_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_009 after the skeleton review found staging ambiguity.

    This is a Director-level correction: the explicit bone/IK contract remains
    unchanged, while the musician, passerby and camera responsibilities are
    spatially separated enough for every view to expose the authored action.
    It deliberately does not rewrite appearance or final-video prompts.
    """
    revised = skeleton_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}

    musician = tracks.get("person_b")
    if musician:
        for point in musician.get("points", []):
            point["position"][0] = float(point["position"][0]) + 0.80
            point["position"][1] = float(point["position"][1]) + 0.55

    passerby = tracks.get("person_c")
    if passerby:
        # Keep the crossing in the rear lane, but make its whole arc visible
        # instead of letting it merge into the dancer/musician cluster.
        rear_lane = (3.80, 3.50, 3.20, 2.90, 2.60)
        for point, y_value in zip(passerby.get("points", []), rear_lane):
            point["position"][1] = float(y_value)

    speaker = tracks.get("speaker")
    if speaker and musician:
        for point, musician_point in zip(speaker.get("points", []), musician.get("points", [])):
            point["position"] = [
                float(musician_point["position"][0]) + 0.85,
                float(musician_point["position"][1]),
                float(point["position"][2]),
            ]

    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["lens_mm"] = min(float(camera.get("lens_mm", 42.0)), 40.0)
        elif camera_id == "lateral":
            camera["lens_mm"] = min(float(camera.get("lens_mm", 42.0)), 40.0)
        elif camera_id == "reverse":
            camera["target"] = "person_c"
            camera["role"] = "reverse three-quarter coverage of passerby crossing and return wave"
            camera["lens_mm"] = min(float(camera.get("lens_mm", 42.0)), 38.0)
        elif camera_id == "elevated":
            camera["role"] = "lowered diagonal wide overview of the complete action"
            camera["target"] = "person_a"
            camera["lens_mm"] = min(float(camera.get("lens_mm", 42.0)), 34.0)
            for point in camera.get("points", []):
                point["position"][2] = min(float(point["position"][2]) * 0.78, 7.2)

    revised["revision"] = {
        "id": "revision_009",
        "parent_revision": "revision_008",
        "reason": "VLM found skeleton geometry acceptable but action order was obscured by clustered actors, rear coverage and an overly elevated view; separate staging lanes and lower the overview camera",
        "preserved": ["procedural skeleton and IK bone schema", "gesture timing", "backpack coupling", "frame indices", "shared world"],
    }
    return revised


def camera_readability_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_010 with grounded, readable coverage for the beats.

    Revision_009 separated the action lanes, but its inherited camera paths
    still produced rear/elevated crops.  This revision rewrites only camera
    positions, targets and lenses; all entity and explicit bone trajectories
    remain the revision_009 source of truth.
    """
    revised = camera_staging_revision(spec)
    camera_points = {
        "master": [(0, -12.0, 3.0), (1.0, -11.2, 3.0), (2.0, -10.5, 3.1), (3.0, -9.8, 3.2), (4.0, -9.0, 3.3)],
        "lateral": [(-10.0, -3.0, 2.8), (-9.0, -2.2, 2.8), (-8.0, -1.5, 2.9), (-7.0, -0.8, 3.0), (-6.0, 0.0, 3.1)],
        "reverse": [(9.0, 4.0, 3.1), (8.2, 3.2, 3.0), (7.4, 2.4, 3.0), (6.6, 1.6, 3.1), (5.8, 0.8, 3.2)],
        "elevated": [(0.0, 4.8, 5.8), (1.0, 4.0, 5.7), (2.0, 3.2, 5.6), (3.0, 2.4, 5.4), (4.0, 1.6, 5.2)],
    }
    roles = {
        "master": "grounded master keeping dancer, musician, passerby and speaker readable",
        "lateral": "grounded lateral follow showing the two dance phases in profile",
        "reverse": "grounded three-quarter coverage of the interaction and passerby return wave",
        "elevated": "diagonal wide overview, lowered enough to preserve gesture readability",
    }
    lenses = {"master": 38.0, "lateral": 36.0, "reverse": 38.0, "elevated": 32.0}
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["target"] = "person_a"
        camera["role"] = roles[camera_id]
        camera["lens_mm"] = lenses[camera_id]
    revised["revision"] = {
        "id": "revision_010",
        "parent_revision": "revision_009",
        "reason": "VLM still found inherited rear/elevated crops and ambiguous interaction timing; replace camera paths with grounded three-quarter coverage while preserving all authored trajectories",
        "preserved": ["procedural skeleton and IK bone schema", "all character/object tracks", "gesture timing", "backpack coupling", "shared world", "frame indices"],
    }
    return revised


def proxy_legibility_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_011 after visual review found proxy role ambiguity.

    The Director keeps the same world and camera contract.  Only explicit
    action landmarks are strengthened here; the Blender materializer applies
    the matching small-prop geometry below so backpack and speaker cannot be
    mistaken for a second torso.
    """
    revised = camera_readability_revision(spec)
    for gesture in revised.get("gesture_tracks", []):
        target_id = str(gesture.get("target_id"))
        limb = str(gesture.get("limb"))
        if target_id == "person_a" and limb == "left_arm":
            gesture["points"] = [(0, 0.0), (30, -1.15), (60, 1.10), (90, -1.05), (119, 0.0)]
        elif target_id == "person_a" and limb == "right_arm":
            gesture["points"] = [(0, 0.0), (30, 0.85), (60, -0.90), (90, 0.95), (119, 0.0)]
        elif target_id == "person_b" and limb == "right_arm":
            gesture["points"] = [(0, 0.0), (30, 0.0), (60, 1.15), (90, 1.15), (119, 0.0)]
        elif target_id == "person_c" and limb == "right_arm":
            gesture["points"] = [(0, 0.0), (30, 0.0), (60, 0.0), (90, 1.10), (119, 0.0)]
    revised["revision"] = {
        "id": "revision_011",
        "parent_revision": "revision_010",
        "reason": "VLM found the proxy roles, prop attachment and action beats ambiguous; strengthen authored gesture landmarks and use distinct small grounded prop geometry",
        "preserved": ["all revision_010 camera paths", "all character/object root tracks", "procedural skeleton and IK schema", "shared world", "frame indices"],
    }
    return revised


def motion_legibility_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_012 with visible gesture silhouettes and front reverse.

    This revision addresses the last VLM observation at the materialized
    trajectory level: the authored arm angles are kept, but the sidecar
    compiler now maps a non-zero wave to a raised hand target instead of a
    folded hand below the shoulder.
    """
    revised = proxy_legibility_revision(spec)
    camera_points = {
        "master": [(0.0, -14.0, 3.0), (1.0, -13.0, 3.0), (2.0, -12.0, 3.1), (3.0, -11.0, 3.2), (4.0, -10.0, 3.3)],
        "lateral": [(-12.0, -3.0, 2.8), (-11.0, -2.2, 2.8), (-10.0, -1.5, 2.9), (-9.0, -0.8, 3.0), (-8.0, 0.0, 3.1)],
        "reverse": [(10.0, -4.0, 3.1), (9.2, -3.2, 3.0), (8.4, -2.4, 3.0), (7.6, -1.6, 3.1), (6.8, -0.8, 3.2)],
        "elevated": [(0.0, 5.8, 5.0), (1.0, 5.0, 4.9), (2.0, 4.2, 4.8), (3.0, 3.4, 4.7), (4.0, 2.6, 4.6)],
    }
    lenses = {"master": 30.0, "lateral": 32.0, "reverse": 34.0, "elevated": 28.0}
    roles = {
        "master": "wide grounded master keeping the entire five-second performance in context",
        "lateral": "wide lateral follow preserving full-body side-step silhouettes",
        "reverse": "front-side three-quarter continuity view, never a rear close-up",
        "elevated": "low diagonal overview with full group and ground props visible",
    }
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["target"] = "person_a"
        camera["lens_mm"] = lenses[camera_id]
        camera["role"] = roles[camera_id]
    revised["revision"] = {
        "id": "revision_012",
        "parent_revision": "revision_011",
        "reason": "VLM found gestures visually static and views rear/tight; raise non-zero hand targets in the skeleton compiler and use wide front-side coverage",
        "preserved": ["all character/object root tracks", "gesture event frames", "prop coupling", "procedural skeleton and IK schema", "shared world", "frame indices"],
    }
    return revised


def readable_stage_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_013 with separated stage lanes and readable beats."""
    revised = motion_legibility_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}

    def set_points(target_id: str, points: list[tuple[int, tuple[float, float, float], float]]) -> None:
        track = tracks[target_id]
        track["points"] = [
            {"frame": int(frame), "position": list(position), "rotation": [0.0, 0.0, float(yaw)]}
            for frame, position, yaw in points
        ]

    set_points("person_a", [
        (0, (-3.8, -1.0, 0.0), 0.0),
        (30, (-2.3, -1.0, 0.0), 0.2),
        (60, (-0.8, -0.8, 0.0), 0.45),
        (90, (0.3, -0.6, 0.0), 0.75),
        (119, (1.2, -0.5, 0.0), 1.0),
    ])
    set_points("person_b", [
        (0, (2.7, 0.8, 0.0), 3.14),
        (30, (2.7, 0.8, 0.0), 3.14),
        (60, (2.7, 0.8, 0.0), 3.14),
        (90, (2.7, 0.8, 0.0), 3.14),
        (119, (2.7, 0.8, 0.0), 3.14),
    ])
    set_points("person_c", [
        (0, (4.8, 3.2, 0.0), -1.4),
        (30, (3.8, 3.2, 0.0), -1.4),
        (60, (2.8, 3.2, 0.0), -1.4),
        (90, (1.8, 3.2, 0.0), -1.4),
        (119, (0.8, 3.2, 0.0), -1.4),
    ])
    dancer = tracks["person_a"]
    backpack = tracks["backpack"]
    for point, dancer_point in zip(backpack["points"], dancer["points"]):
        point["position"] = [float(dancer_point["position"][0]), float(dancer_point["position"][1]) + 0.55, 1.0]
        point["rotation"] = list(dancer_point["rotation"])
    speaker = tracks["speaker"]
    musician = tracks["person_b"]
    for point, musician_point in zip(speaker["points"], musician["points"]):
        point["position"] = [float(musician_point["position"][0]) + 0.75, float(musician_point["position"][1]), 0.35]

    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("person_a", "left_arm")]["points"] = [(0, 0.0), (30, 1.0), (60, -1.0), (90, 1.0), (119, 0.0)]
    gestures[("person_a", "right_arm")]["points"] = [(0, 0.0), (30, -1.0), (60, 1.0), (90, -1.0), (119, 0.0)]
    gestures[("person_b", "right_arm")]["points"] = [(0, 0.0), (30, 0.0), (60, 1.0), (90, 1.0), (119, 0.0)]
    gestures[("person_c", "right_arm")]["points"] = [(0, 0.0), (30, 0.0), (60, 0.0), (90, 1.0), (119, 0.0)]

    camera_points = {
        "master": [(0.0, -14.0, 3.4), (0.5, -13.0, 3.4), (1.0, -12.0, 3.5), (1.5, -11.0, 3.6), (2.0, -10.0, 3.6)],
        "lateral": [(-12.0, -1.0, 3.2), (-11.0, -0.5, 3.2), (-10.0, 0.0, 3.3), (-9.0, 0.5, 3.3), (-8.0, 1.0, 3.4)],
        "reverse": [(11.0, -8.0, 3.5), (10.0, -7.0, 3.5), (9.0, -6.0, 3.6), (8.0, -5.0, 3.6), (7.0, -4.0, 3.7)],
        "elevated": [(0.0, 11.0, 8.5), (1.0, 10.5, 8.3), (2.0, 10.0, 8.1), (3.0, 9.5, 7.9), (4.0, 9.0, 7.7)],
    }
    camera_targets = {"master": "person_a", "lateral": "person_a", "reverse": "person_b", "elevated": "person_b"}
    camera_roles = {
        "master": "wide front master showing all three stage lanes and grounded speaker",
        "lateral": "wide side follow preserving dancer full-body silhouettes",
        "reverse": "front three-quarter musician interaction view, not a rear close-up",
        "elevated": "high wide stage overview centered on the fixed musician lane",
    }
    camera_lenses = {"master": 35.0, "lateral": 32.0, "reverse": 36.0, "elevated": 35.0}
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["target"] = camera_targets[camera_id]
        camera["role"] = camera_roles[camera_id]
        camera["lens_mm"] = camera_lenses[camera_id]
    revised["revision"] = {
        "id": "revision_013",
        "parent_revision": "revision_012",
        "reason": "VLM found repetitive gestures, actor overlap and overview drift; stage actors in fixed lanes, center wide coverage on the musician lane, and compile explicit down/out/up hand silhouettes",
        "preserved": ["shared world", "object coupling semantics", "K0-K4 event order", "one task per camera rule", "frame indices"],
    }
    return revised


def musician_role_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_014 with a visible musician cue and stable speaker gap."""
    revised = readable_stage_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    speaker = tracks["speaker"]
    musician = tracks["person_b"]
    for point, musician_point in zip(speaker.get("points", []), musician.get("points", [])):
        point["position"] = [float(musician_point["position"][0]) + 1.0, float(musician_point["position"][1]), 0.35]
    revised["revision"] = {
        "id": "revision_014",
        "parent_revision": "revision_013",
        "reason": "VLM found the speaker coordinate-consistent but visually attributed to the dancer; add a non-entity musician microphone cue and keep a visible one-metre ground gap",
        "preserved": ["all revision_013 character tracks", "gesture timing", "camera responsibilities", "shared world", "entity count", "frame indices"],
    }
    return revised


def fixed_stage_camera_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_015 with fixed-center coverage for visible travel."""
    revised = musician_role_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    musician = tracks["person_b"]
    for point in musician.get("points", []):
        point["rotation"] = [0.0, 0.0, 0.0]
    speaker = tracks["speaker"]
    for point, musician_point in zip(speaker.get("points", []), musician.get("points", [])):
        point["position"] = [float(musician_point["position"][0]) + 0.60, float(musician_point["position"][1]), 0.35]

    camera_points = {
        "master": [(0.0, -14.0, 3.4), (0.0, -14.0, 3.4), (0.0, -14.0, 3.4), (0.0, -14.0, 3.4), (0.0, -14.0, 3.4)],
        "lateral": [(-12.0, -1.0, 3.2), (-12.0, -1.0, 3.2), (-12.0, -1.0, 3.2), (-12.0, -1.0, 3.2), (-12.0, -1.0, 3.2)],
        "reverse": [(11.0, -8.0, 3.5), (10.0, -8.0, 3.5), (9.0, -8.0, 3.5), (8.0, -8.0, 3.5), (7.0, -8.0, 3.5)],
        "elevated": [(0.0, -12.0, 8.5), (0.0, -12.0, 8.5), (0.0, -12.0, 8.5), (0.0, -12.0, 8.5), (0.0, -12.0, 8.5)],
    }
    roles = {
        "master": "fixed-center master exposing dancer travel from left to center",
        "lateral": "fixed-center lateral profile exposing side-step silhouettes",
        "reverse": "front three-quarter continuity on the musician and speaker",
        "elevated": "front-facing high overview centered on the stationary musician lane",
    }
    lenses = {"master": 35.0, "lateral": 32.0, "reverse": 36.0, "elevated": 35.0}
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["target"] = "person_b"
        camera["role"] = roles[camera_id]
        camera["lens_mm"] = lenses[camera_id]
    revised["revision"] = {
        "id": "revision_015",
        "parent_revision": "revision_014",
        "reason": "VLM found dancer travel canceled by a dancer-following master and musician/speaker relationships unclear from rear views; lock cameras to the stationary musician lane and face the musician toward the front coverage",
        "preserved": ["all revision_014 entity tracks except musician facing", "explicit hand landmarks", "microphone role cue", "shared world", "frame indices"],
    }
    return revised


def composition_fit_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_016 by fitting the complete stage into every view."""
    revised = fixed_stage_camera_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}

    def set_x(target_id: str, values: tuple[float, ...]) -> None:
        for point, value in zip(tracks[target_id].get("points", []), values):
            point["position"][0] = float(value)

    set_x("person_a", (-2.6, -1.6, -0.6, 0.4, 1.2))
    set_x("person_b", (2.0, 2.0, 2.0, 2.0, 2.0))
    set_x("person_c", (3.8, 3.3, 2.8, 2.3, 1.8))
    musician = tracks["person_b"]
    speaker = tracks["speaker"]
    dancer = tracks["person_a"]
    for point, musician_point in zip(speaker.get("points", []), musician.get("points", [])):
        point["position"] = [float(musician_point["position"][0]) + 0.60, float(musician_point["position"][1]), 0.35]
    for point, dancer_point in zip(tracks["backpack"].get("points", []), dancer.get("points", [])):
        point["position"][0] = float(dancer_point["position"][0])

    camera_points = {
        "master": [(0.0, -11.5, 3.2), (0.0, -11.5, 3.2), (0.0, -11.5, 3.2), (0.0, -11.5, 3.2), (0.0, -11.5, 3.2)],
        "lateral": [(-10.0, -1.0, 3.0), (-10.0, -1.0, 3.0), (-10.0, -1.0, 3.0), (-10.0, -1.0, 3.0), (-10.0, -1.0, 3.0)],
        "reverse": [(9.0, -6.0, 3.3), (8.5, -6.0, 3.3), (8.0, -6.0, 3.3), (7.5, -6.0, 3.3), (7.0, -6.0, 3.3)],
        "elevated": [(0.0, -10.0, 7.5), (0.0, -10.0, 7.5), (0.0, -10.0, 7.5), (0.0, -10.0, 7.5), (0.0, -10.0, 7.5)],
    }
    lenses = {"master": 40.0, "lateral": 38.0, "reverse": 40.0, "elevated": 38.0}
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["target"] = "person_b"
        camera["lens_mm"] = lenses[camera_id]
    revised["revision"] = {
        "id": "revision_016",
        "parent_revision": "revision_015",
        "reason": "VLM found dancer and passerby cropped or too small; fit all actor lanes and grounded speaker inside a closer fixed-center composition without changing event order",
        "preserved": ["all revision_015 gesture tracks", "musician microphone cue", "speaker coupling relation", "shared world", "frame indices"],
    }
    return revised


def event_separation_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_017 from the VLM's remaining visual failures.

    This is still a shared-world proxy revision.  It separates the passerby
    into a clearly rear, right-to-left lane, makes the two dancer side-steps
    spatially distinct, and places a deliberately visible backpack marker on
    the dancer's front-side shoulder line.  The authored entity IDs, K0-K4
    frames, and one-world/four-camera contract remain unchanged.
    """
    revised = composition_fit_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}

    def set_points(target_id: str, positions: list[tuple[float, float, float]], yaws: list[float] | None = None) -> None:
        track = tracks[target_id]
        yaws = yaws or [float(point["rotation"][2]) for point in track.get("points", [])]
        for point, position, yaw in zip(track.get("points", []), positions, yaws):
            point["position"] = [float(value) for value in position]
            point["rotation"] = [0.0, 0.0, float(yaw)]

    # The alternating y positions are the two visible side-step beats.  The
    # dancer remains in front of the musician lane and never crosses him.
    set_points(
        "person_a",
        [(-2.6, -1.20, 0.0), (-1.6, -0.25, 0.0), (-0.6, -1.15, 0.0), (0.4, -0.20, 0.0), (1.2, -0.75, 0.0)],
        [0.0, 0.25, 0.50, 0.75, 1.0],
    )
    # Keep the passerby well behind the performance and move monotonically
    # right-to-left so the crossing cannot be mistaken for a foreground merge.
    set_points(
        "person_c",
        [(4.4, 4.20, 0.0), (3.0, 4.20, 0.0), (1.6, 4.20, 0.0), (0.2, 4.20, 0.0), (-2.2, 4.20, 0.0)],
        [-1.57, -1.57, -1.57, -1.57, -1.57],
    )

    dancer = tracks["person_a"]
    backpack = tracks["backpack"]
    for point, dancer_point in zip(backpack.get("points", []), dancer.get("points", [])):
        # Front-side offset makes the coupling visible from the four cameras;
        # it is still a deterministic child track of the same dancer.
        point["position"] = [float(dancer_point["position"][0]) - 0.55, float(dancer_point["position"][1]) - 0.18, 0.95]
        point["rotation"] = list(dancer_point["rotation"])

    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("person_a", "left_arm")]["points"] = [(0, 0.0), (30, 1.0), (60, -1.0), (90, 1.0), (119, 0.0)]
    gestures[("person_a", "right_arm")]["points"] = [(0, 0.0), (30, -1.0), (60, 1.0), (90, -1.0), (119, 0.0)]
    gestures[("person_b", "right_arm")]["points"] = [(0, 0.0), (30, 1.0), (60, 1.0), (90, 0.0), (119, 0.0)]
    gestures[("person_c", "right_arm")]["points"] = [(0, 0.0), (30, 0.0), (60, 0.0), (90, 1.0), (119, 0.0)]

    revised["revision"] = {
        "id": "revision_017",
        "parent_revision": "revision_016",
        "reason": "VLM found the passerby path, ordered action beats, and backpack identity ambiguous; use a monotonic rear crossing lane, alternating dancer side-step coordinates, explicit response gestures, and a visible coupled backpack marker",
        "preserved": ["shared world", "entity IDs", "K0-K4 frame indices", "camera roles", "speaker coupling", "one task per camera rule"],
    }
    return revised


def rigged_staging_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_018 for the rigged-human proxy branch.

    The bench is a static prop, so moving it sideways is a Director-level
    occlusion repair.  The dancer's final authored yaw is also made explicit
    so a skinned mesh has a visible turn toward the rear crossing lane.
    """
    revised = event_separation_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    bench = tracks["bench"]
    for point in bench.get("points", []):
        point["position"] = [-4.4, 1.8, 0.45]
    dancer = tracks["person_a"]
    dancer["points"][-2]["rotation"][2] = 1.20
    dancer["points"][-1]["rotation"][2] = 1.57
    revised["revision"] = {
        "id": "revision_018",
        "parent_revision": "revision_017",
        "reason": "standard rigged-human proxy still had bench occlusion and an ambiguous final turn; move the static bench off the passerby lane and set an explicit final dancer yaw toward the rear crossing",
        "preserved": ["shared world", "entity IDs", "K0-K4 frame indices", "all character/object trajectories except bench placement", "camera roles", "one task per camera rule"],
    }
    return revised


def choreography_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_019 with explicit side-step choreography and shot roles."""
    revised = rigged_staging_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    dancer = tracks["person_a"]
    dancer_points = [
        (-2.6, -1.20, 0.0, 0.0),
        (-1.2, -0.80, 0.0, 0.0),
        (-0.2, -0.80, 0.0, 0.35),
        (-1.0, -0.80, 0.0, 0.70),
        (0.5, 1.00, 0.0, 1.57),
    ]
    for point, (x, y, z, yaw) in zip(dancer.get("points", []), dancer_points):
        point["position"] = [x, y, z]
        point["rotation"] = [0.0, 0.0, yaw]

    backpack = tracks["backpack"]
    for point, dancer_point in zip(backpack.get("points", []), dancer.get("points", [])):
        point["position"] = [float(dancer_point["position"][0]) - 0.55, float(dancer_point["position"][1]) - 0.18, 0.95]
        point["rotation"] = list(dancer_point["rotation"])

    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("person_a", "left_arm")]["points"] = [(0, 0.0), (30, 1.0), (60, 1.0), (90, -1.0), (119, 0.0)]
    gestures[("person_a", "right_arm")]["points"] = [(0, 0.0), (30, -1.0), (60, -1.0), (90, 1.0), (119, 0.0)]
    gestures[("person_b", "right_arm")]["points"] = [(0, 0.0), (30, 1.0), (60, 0.0), (90, 0.0), (119, 0.0)]
    gestures[("person_c", "right_arm")]["points"] = [(0, 0.0), (30, 0.0), (60, 0.0), (90, 1.0), (119, 0.0)]

    camera_points = {
        "master": [(0.0, -13.0, 3.4)] * 5,
        "lateral": [(-9.0, -2.0, 3.2)] * 5,
        "reverse": [(8.0, -6.0, 3.5), (8.0, -6.0, 3.5), (8.0, -6.0, 3.5), (7.5, -6.0, 3.5), (7.0, -6.0, 3.5)],
        "elevated": [(0.0, -11.0, 8.0)] * 5,
    }
    targets = {"master": "person_a", "lateral": "person_a", "reverse": "person_c", "elevated": "person_b"}
    roles = {
        "master": "wide front master for entry, two side-steps and final turn",
        "lateral": "side profile isolating alternating left/right side-steps",
        "reverse": "front three-quarter passerby response and rear crossing",
        "elevated": "wide shared-world overview preserving all event order",
    }
    lenses = {"master": 36.0, "lateral": 34.0, "reverse": 36.0, "elevated": 36.0}
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["target"] = targets[camera_id]
        camera["role"] = roles[camera_id]
        camera["lens_mm"] = lenses[camera_id]
    revised["revision"] = {
        "id": "revision_019",
        "parent_revision": "revision_018",
        "reason": "VLM still read the lead as running; hold the center entry, alternate left/right side-step positions, make the K4 rear turn explicit, and assign dedicated camera responsibilities to the lead, passerby, and overview",
        "preserved": ["shared world", "entity IDs", "K0-K4 frame indices", "backpack and speaker coupling", "standard rigged asset interface", "one task per camera rule"],
    }
    return revised


def side_step_motion_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_020 with a no-running lead motion profile and wider shots."""
    revised = choreography_revision(spec)
    camera_points = {
        "master": [(0.0, -14.0, 3.4)] * 5,
        "lateral": [(-11.0, -2.0, 3.2)] * 5,
        "reverse": [(9.0, -7.0, 3.5), (9.0, -7.0, 3.5), (9.0, -7.0, 3.5), (8.5, -7.0, 3.5), (8.0, -7.0, 3.5)],
        "elevated": [(0.0, -12.0, 8.5)] * 5,
    }
    lenses = {"master": 32.0, "lateral": 30.0, "reverse": 32.0, "elevated": 32.0}
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        camera["points"] = [
            {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
            for frame, position in zip((0, 30, 60, 90, 119), camera_points[camera_id])
        ]
        camera["lens_mm"] = lenses[camera_id]
    revised["revision"] = {
        "id": "revision_020",
        "parent_revision": "revision_019",
        "reason": "VLM still read alternating leg swing as running; mark the lead as a side-step transfer motion, keep the torso upright, drive forearm companions for explicit waves, and widen camera coverage",
        "preserved": ["shared world", "entity IDs", "K0-K4 frame indices", "camera targets and roles", "backpack/speaker coupling", "standard rigged asset interface", "one task per camera rule"],
    }
    return revised


def bvh_motion_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_021 using real BVH clips for the lead humanoid.

    The authored WorldState roots and camera plans remain unchanged.  The
    revision only changes the local pose source for ``person_a``; the Blender
    sandbox retargets two real ACCAD clips in sequence and keeps the source
    hashes in the render manifest.
    """
    revised = side_step_motion_revision(spec)
    revised["revision"] = {
        "id": "revision_021",
        "parent_revision": "revision_020",
        "reason": "replace the procedural side-step overlay with real BVH retarget motion for the lead while preserving the shared world and authored root trajectory",
        "preserved": [
            "shared world",
            "entity IDs",
            "K0-K4 frame indices",
            "all authored character/object root trajectories",
            "camera targets and roles",
            "one task per camera rule",
        ],
        "motion_sources": ["Female1_C24_SideStepLeft.bvh", "Female1_C25_SideStepRight.bvh"],
    }
    return revised


def bvh_readability_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_022 with separated event lanes and explicit beats."""
    revised = bvh_motion_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    lead = tracks["person_a"]
    lead_points = [
        (-2.6, -1.35, 0.0, 0.0),
        (-1.35, -1.35, 0.0, 0.0),
        (-0.35, -1.35, 0.0, 0.35),
        (-1.20, -1.35, 0.0, 0.70),
        (0.00, -1.65, 0.0, 1.57),
    ]
    for point, (x, y, z, yaw) in zip(lead.get("points", []), lead_points):
        point["position"] = [x, y, z]
        point["rotation"] = [0.0, 0.0, yaw]
    backpack = tracks["backpack"]
    for point, lead_point in zip(backpack.get("points", []), lead.get("points", [])):
        point["position"] = [float(lead_point["position"][0]) - 0.55, float(lead_point["position"][1]) - 0.18, 0.95]
        point["rotation"] = list(lead_point["rotation"])
    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("person_a", "left_arm")]["points"] = [(0, 0.0), (18, 0.0), (30, 1.1), (42, 0.0), (119, 0.0)]
    gestures[("person_a", "right_arm")]["points"] = [(0, 0.0), (48, 0.0), (60, -1.0), (72, 0.0), (119, 0.0)]
    gestures[("person_b", "right_arm")]["points"] = [(0, 0.0), (24, 0.0), (36, 1.0), (48, 0.0), (119, 0.0)]
    gestures[("person_c", "right_arm")]["points"] = [(0, 0.0), (78, 0.0), (90, 1.1), (102, 0.0), (119, 0.0)]
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["role"] = "wide front master with separated lead-to-musician wave and final turn"
        elif camera_id == "lateral":
            camera["role"] = "isolated side profile for the two non-overlapping lead side-step beats"
        elif camera_id == "reverse":
            camera["points"] = [
                {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
                for frame, position in zip((0, 30, 60, 90, 119), [(10.5, -5.0, 4.1)] * 3 + [(10.0, -5.0, 4.1), (9.5, -5.0, 4.1)])
            ]
            camera["role"] = "three-quarter reverse isolating the passerby's return wave and rear crossing"
        elif camera_id == "elevated":
            camera["role"] = "high overview proving the three separated lanes and event order"
    revised["revision"] = {
        "id": "revision_022",
        "parent_revision": "revision_021",
        "reason": "VLM found trajectory overlap and unreadable action beats; separate the lead from the musician, time explicit wave beats, and adjust reverse coverage while preserving real BVH retarget motion",
        "preserved": ["shared world", "real BVH sources", "entity IDs", "K0-K4 frame indices", "camera count", "one task per camera rule"],
    }
    return revised


def bvh_upright_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_023 with upright BVH limbs and stronger event spacing."""
    revised = bvh_readability_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    lead = tracks["person_a"]
    lead_points = [
        (-2.8, -1.55, 0.0, 0.0),
        (-1.35, -1.55, 0.0, 0.0),
        (0.20, -1.55, 0.0, 0.25),
        (-1.35, -1.55, 0.0, 0.55),
        (0.20, -1.55, 0.0, 1.35),
    ]
    for point, (x, y, z, yaw) in zip(lead.get("points", []), lead_points):
        point["position"] = [x, y, z]
        point["rotation"] = [0.0, 0.0, yaw]
    backpack = tracks["backpack"]
    for point, lead_point in zip(backpack.get("points", []), lead.get("points", [])):
        point["position"] = [float(lead_point["position"][0]) - 0.55, float(lead_point["position"][1]) - 0.18, 0.95]
        point["rotation"] = list(lead_point["rotation"])
    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("person_b", "right_arm")]["points"] = [(0, 0.0), (24, 0.0), (36, 1.35), (48, 0.0), (119, 0.0)]
    gestures[("person_c", "right_arm")]["points"] = [(0, 0.0), (78, 0.0), (90, 1.35), (102, 0.0), (119, 0.0)]
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["points"] = [{"frame": frame, "position": [0.0, -12.0, 3.0], "rotation": [0.0, 0.0, 0.0]} for frame in (0, 30, 60, 90, 119)]
            camera["lens_mm"] = 36.0
        elif camera_id == "lateral":
            camera["points"] = [{"frame": frame, "position": [-9.0, -1.0, 2.8], "rotation": [0.0, 0.0, 0.0]} for frame in (0, 30, 60, 90, 119)]
            camera["lens_mm"] = 36.0
        elif camera_id == "reverse":
            camera["lens_mm"] = 40.0
        elif camera_id == "elevated":
            camera["points"] = [{"frame": frame, "position": [0.0, -11.0, 7.8], "rotation": [0.0, 0.0, 0.0]} for frame in (0, 30, 60, 90, 119)]
            camera["lens_mm"] = 36.0
    revised["revision"] = {
        "id": "revision_023",
        "parent_revision": "revision_022",
        "reason": "VLM read the BVH torso as repeated backward leaning; keep the real limb retarget but hold the lead upright, widen the root side-step path, strengthen musician/passersby wave beats, and tighten the coverage",
        "preserved": ["shared world", "real BVH limb sources", "entity IDs", "K0-K4 frame indices", "four camera responsibilities", "one task per camera rule"],
    }
    return revised


def bvh_choreography_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_024 with explicit leg alternation over the BVH base."""
    revised = bvh_upright_revision(spec)
    revised["revision"] = {
        "id": "revision_024",
        "parent_revision": "revision_023",
        "reason": "VLM still saw a static open-arm stance; retain real BVH limb motion and add a bounded authored leg/arm choreography overlay so the two side-step phases and interaction beats are explicit",
        "preserved": ["shared world", "real BVH source hashes", "upright retarget policy", "entity IDs", "K0-K4 frame indices", "four camera responsibilities", "one task per camera rule"],
    }
    return revised


def indoor_market_readability_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_025 for the market counter/cart readability defects."""
    revised = copy.deepcopy(spec)
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["target"] = "counter"
            camera["role"] = "front master showing customer, grounded cart, helper and vendor counter"
            camera["lens_mm"] = 38.0
        elif camera_id == "lateral":
            camera["target"] = "handcart"
            camera["role"] = "cart-side context preserving wheels, boxes and vendor lane"
            camera["lens_mm"] = 38.0
        elif camera_id == "reverse":
            camera["target"] = "counter"
            camera["role"] = "wide vendor-facing exchange view with counter below the vendor, not filling frame"
            camera["lens_mm"] = 34.0
            camera["points"] = [
                {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
                for frame, position in zip((0, 30, 60, 90, 119), [(10.0, -4.5, 4.2)] * 3 + [(9.5, -4.5, 4.2), (9.0, -4.5, 4.2)])
            ]
        elif camera_id == "elevated":
            camera["target"] = "counter"
            camera["role"] = "high layout view preserving the complete cart-to-counter path and paper settling"
            camera["lens_mm"] = 38.0
            camera["points"] = [
                {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
                for frame, position in zip((0, 30, 60, 90, 119), [(0.0, -1.0, 9.0)] * 5)
            ]
    revised["revision"] = {
        "id": "revision_025",
        "parent_revision": "base_indoor_market_exchange",
        "reason": "VLM found reverse counter occlusion and an ungrounded handcart; compact the counter proxy, use vertical grounded wheel geometry, and widen reverse/elevated camera coverage",
        "preserved": ["all customer/vendor/helper root trajectories", "cart-box coupling", "paper flutter keyframes", "entity IDs", "four camera count", "shared world"],
    }
    return revised


def indoor_market_counter_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_026 with a table-like counter and wider overhead view."""
    revised = indoor_market_readability_revision(spec)
    for camera in revised.get("cameras", []):
        if str(camera.get("camera_id")) == "elevated":
            camera["target"] = "handcart"
            camera["lens_mm"] = 30.0
            camera["role"] = "distant overhead establishing cart path, counter, vendor and helper in one frame"
            camera["points"] = [
                {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
                for frame, position in zip((0, 30, 60, 90, 119), [(0.0, -7.0, 12.0)] * 5)
            ]
    revised["revision"] = {
        "id": "revision_026",
        "parent_revision": "revision_025",
        "reason": "VLM still read the counter as a torso block and the elevated view as a counter close-up; replace the market counter with a table-like proxy and pull the overhead camera back to cover the full exchange",
        "preserved": ["all character/object root trajectories", "cart-box coupling", "paper flutter keyframes", "revision_025 wheel orientation", "entity IDs", "shared world"],
    }
    return revised


def indoor_market_event_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_027 with explicit vendor, pause, gesture and flutter beats."""
    revised = indoor_market_counter_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    for point in tracks["vendor"].get("points", []):
        point["position"] = [3.2, 2.45, 0.0]
    for point in tracks["helper"].get("points", []):
        point["position"][1] = 2.7
    # Customer pause at K1/K2, with cart and boxes kept coupled at the same
    # position before the exchange resumes.
    for target_id in ("customer", "handcart", "box_a", "box_b"):
        points = tracks[target_id].get("points", [])
        if len(points) >= 3:
            points[2]["position"] = list(points[1]["position"])
    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("customer", "right_arm")]["points"] = [(0, 0.0), (24, 0.0), (36, 1.25), (54, 1.25), (66, 0.0), (119, 0.0)]
    gestures[("helper", "right_arm")]["points"] = [(0, 0.0), (48, 0.0), (60, 1.0), (78, 1.0), (90, 0.0), (119, 0.0)]
    for target_id in ("paper_a", "paper_b"):
        points = tracks[target_id].get("points", [])
        if len(points) >= 5:
            points[2]["position"][2] = 3.2
            points[3]["position"][2] = 1.8
    for camera in revised.get("cameras", []):
        if str(camera.get("camera_id")) == "reverse":
            camera["points"] = [
                {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
                for frame, position in zip((0, 30, 60, 90, 119), [(12.0, -8.0, 5.0)] * 5)
            ]
            camera["lens_mm"] = 32.0
    revised["revision"] = {
        "id": "revision_027",
        "parent_revision": "revision_026",
        "reason": "VLM found vendor occlusion and ambiguous event timing; move vendor/helper to explicit depth layers, add a customer pause and raised-hand interval, preserve cart-box coupling, increase paper flutter height, and widen reverse coverage",
        "preserved": ["table-like counter geometry", "vertical cart wheels", "all entity IDs", "shared world", "four camera responsibilities"],
    }
    return revised


def indoor_market_storyhuman_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_028 for rounded-human staging and grounded cart contact."""
    revised = indoor_market_event_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    # The rounded human must stand behind the handle, not overlap the cart
    # platform; helper depth is moved into the readable shared action lane.
    for point in tracks["customer"].get("points", []):
        point["position"][1] = -1.45
    for point in tracks["helper"].get("points", []):
        point["position"][1] = 0.20
    # Object roots are origins, so the cart wheels/base are built from z=0.
    # Boxes keep their authored x/y offsets and are parent-coupled to it.
    for point in tracks["handcart"].get("points", []):
        point["position"][2] = 0.0
    for target_id in ("box_a", "box_b"):
        for point in tracks[target_id].get("points", []):
            point["position"][2] = 0.72
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["role"] = "front master showing a distinct pusher behind the grounded handcart, helper and vendor"
        elif camera_id == "lateral":
            camera["role"] = "cart-side profile showing the handle, grounded wheels, boxes and pusher behind"
        elif camera_id == "elevated":
            camera["role"] = "wide overhead view showing customer-to-handle relation, helper lane and cart path"
    revised["revision"] = {
        "id": "revision_028",
        "parent_revision": "revision_027",
        "reason": "Storyhuman VLM found the pusher visually embedded in the cart and the cart floating; separate the pusher behind the handle, move helper into the action lane, and ground the cart/boxes from root z=0",
        "preserved": ["rounded articulated human proxy", "all entity IDs", "K0-K4 event frames", "cart-box parent coupling", "four camera responsibilities", "shared world"],
    }
    return revised


def indoor_market_storyhuman_readability_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_029 with separated helper and explicit paper landing."""
    revised = indoor_market_storyhuman_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    customer_points = tracks["customer"].get("points", [])
    helper_points = tracks["helper"].get("points", [])
    for index, point in enumerate(helper_points):
        # Keep helper in a distinct rear lane and offset x from the customer
        # during the exchange rather than allowing a perspective overlap.
        point["position"][1] = 1.0
        if index < len(customer_points):
            point["position"][0] = float(customer_points[index]["position"][0]) - 0.80
    paper_points = {
        "paper_a": [(-4.0, -0.45, 1.45), (-2.8, -0.45, 1.45), (-2.0, -0.30, 2.80), (0.0, 0.20, 2.00), (2.6, 1.30, 0.05)],
        "paper_b": [(-3.6, -0.10, 1.50), (-2.4, -0.10, 1.50), (-1.8, -0.05, 3.00), (0.30, 0.40, 2.10), (2.9, 1.40, 0.06)],
    }
    for target_id, positions in paper_points.items():
        for point, position in zip(tracks[target_id].get("points", []), positions):
            point["position"] = list(position)
    revised["revision"] = {
        "id": "revision_029",
        "parent_revision": "revision_028",
        "reason": "VLM still saw customer/helper overlap and an ambiguous two-sheet event; place helper in a separated rear lane and make both paper origins, flutter arcs, and counter-side landings explicit",
        "preserved": ["rounded articulated human proxy", "grounded cart and root coupling", "all entity IDs", "K0-K4 event frames", "four camera responsibilities", "shared world"],
    }
    return revised


def indoor_market_asset_contact_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_030 with a graspable cart handle and role-specific cues.

    This revision is intentionally structural.  The VLM found that the
    Quaternius customer and the cart were separated in the side views, and
    that the helper's raised gesture could be confused with the customer's.
    Keep the authored root paths and event frames, but make the handle a
    horizontal grip at the customer's hand height and move the helper's cue
    to the opposite arm.
    """
    revised = indoor_market_storyhuman_readability_revision(spec)
    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    helper = gestures.pop(("helper", "right_arm"), None)
    if helper is not None:
        helper["limb"] = "left_arm"
        gestures[("helper", "left_arm")] = helper
    # Rebuild in the original list order with the helper's identity-specific
    # left-arm cue.  The customer's right-hand cue remains unchanged.
    revised["gesture_tracks"] = list(gestures.values())
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    # Keep the customer directly behind the horizontal handle and the helper
    # in the separated rear lane.  These are world-space roots shared by all
    # four cameras, so the contact is not a per-view annotation.
    for point in tracks["customer"].get("points", []):
        point["position"][1] = -1.45
    for point in tracks["helper"].get("points", []):
        point["position"][1] = 1.0
    revised["revision"] = {
        "id": "revision_030",
        "parent_revision": "revision_029",
        "reason": "VLM found the customer separated from the cart and the helper gesture ambiguous; use a horizontal graspable handle at hand height, preserve customer right-hand signal, and move the helper cue to the left arm",
        "preserved": ["same Quaternius male/female assets", "all root tracks", "cart-box coupling", "K0-K4 frame indices", "four camera responsibilities", "shared world"],
    }
    return revised


def indoor_market_asset_visual_cleanup_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create immutable revision_031 after the first revision_030 render.

    The world/action contract is inherited unchanged; only the imported
    asset's role accent and the handle geometry are materialized by the
    Blender asset branch.
    """
    revised = indoor_market_asset_contact_revision(spec)
    revised["revision"] = {
        "id": "revision_031",
        "parent_revision": "revision_030",
        "reason": "remove the torso marker that hid arm action, retain a small shoulder role accent, and keep the shifted horizontal cart grip aligned with the customer's hand span",
        "preserved": list(revised["revision"].get("preserved", [])),
    }
    return revised


def indoor_market_exchange_staging_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_032 with one connected exchange space and tighter views."""
    revised = indoor_market_asset_visual_cleanup_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    for target_id in ("vendor", "counter"):
        for point in tracks[target_id].get("points", []):
            point["position"] = [2.1, 0.9, float(point["position"][2])]
    paper_landing = {
        "paper_a": [1.65, 0.55, 0.05],
        "paper_b": [1.85, 0.65, 0.06],
    }
    for target_id, position in paper_landing.items():
        points = tracks[target_id].get("points", [])
        if points:
            points[-1]["position"] = list(position)
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["target"] = "handcart"
            camera["role"] = "front exchange master keeping the moving customer, horizontal grip, helper and vendor in one connected plane"
            camera["lens_mm"] = 42.0
        elif camera_id == "lateral":
            camera["target"] = "handcart"
            camera["role"] = "cart-side medium view showing the customer's hand on the grip and the vendor at the end of the path"
            camera["lens_mm"] = 42.0
        elif camera_id == "reverse":
            camera["target"] = "handcart"
            camera["points"] = [
                {"frame": frame, "position": [8.0, -6.0, 4.0], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 40.0
            camera["role"] = "reverse medium exchange view with cart, customer, helper and vendor all inside the authored interaction space"
        elif camera_id == "elevated":
            camera["target"] = "handcart"
            camera["points"] = [
                {"frame": frame, "position": [1.5, -4.5, 8.0], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 40.0
            camera["role"] = "tight elevated exchange view preserving the cart, both characters, vendor and paper landing"
    revised["revision"] = {
        "id": "revision_032",
        "parent_revision": "revision_031",
        "reason": "VLM found the vendor/counter disconnected from the cart and reverse/elevated cameras too wide; stage one connected exchange space, move paper landings beside the counter, and tighten all authored coverage around the cart target",
        "preserved": ["same Quaternius male/female assets", "customer/helper root tracks", "cart-box coupling", "K0-K4 frame indices", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_action_physics_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_033 with longer action beats and granular paper motion."""
    revised = indoor_market_exchange_staging_revision(spec)
    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("customer", "right_arm")]["points"] = [(0, 0.0), (24, 0.0), (36, 1.25), (84, 1.25), (96, 0.0), (119, 0.0)]
    gestures[("helper", "left_arm")]["points"] = [(0, 0.0), (48, 0.0), (60, 1.0), (90, 1.0), (102, 0.0), (119, 0.0)]
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    paper_tracks = {
        "paper_a": [
            ([-4.0, -0.45, 1.45], [0.0, 0.0, 0.0]),
            ([-2.8, -0.45, 1.45], [0.0, 0.0, 0.0]),
            ([-2.0, -0.30, 2.10], [0.45, 0.20, 0.80]),
            ([0.0, 0.20, 1.15], [-0.60, -0.30, -0.70]),
            ([1.65, 0.55, 0.05], [0.10, 0.0, 0.25]),
        ],
        "paper_b": [
            ([-3.6, -0.10, 1.50], [0.0, 0.0, 0.0]),
            ([-2.4, -0.10, 1.50], [0.0, 0.0, 0.0]),
            ([-1.8, -0.05, 2.30], [-0.35, 0.25, -0.55]),
            ([0.30, 0.40, 1.20], [0.55, -0.20, 0.65]),
            ([1.85, 0.65, 0.06], [-0.10, 0.0, -0.25]),
        ],
    }
    for target_id, entries in paper_tracks.items():
        for point, (position, rotation) in zip(tracks[target_id].get("points", []), entries):
            point["position"] = list(position)
            point["rotation"] = list(rotation)
    for camera in revised.get("cameras", []):
        if str(camera.get("camera_id")) == "lateral":
            camera["points"] = [
                {"frame": frame, "position": [-6.0, -7.0, 3.2], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 44.0
            camera["role"] = "front-side medium view showing the customer hand, horizontal grip, wheel contact, two boxes and vendor"
    revised["revision"] = {
        "id": "revision_033",
        "parent_revision": "revision_032",
        "reason": "VLM found short or hidden gesture beats, weak pusher contact, oversized rigid paper planes and an indistinct payload; extend identity-specific gestures, add small rotated flutter arcs, reframe the side camera on the grip, and use two visibly different boxes",
        "preserved": ["same Quaternius male/female assets", "connected exchange staging", "cart-box coupling", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_identity_grounding_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_034 with stable role clothing and grounded staging."""
    revised = indoor_market_action_physics_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    # Shorten only the empty approach while preserving the left-to-right order
    # and all authored event frames, so the vendor is visible from K0 onward.
    root_x = {
        "customer": [-3.0, -2.0, -2.0, 0.2, 1.2],
        "helper": [-3.8, -2.8, -2.8, -0.6, 0.4],
        "handcart": [-2.8, -1.8, -1.8, 0.4, 1.4],
        "box_a": [-2.8, -1.8, -1.8, 0.4, 1.4],
        "box_b": [-2.8, -1.8, -1.8, 0.4, 1.4],
    }
    for target_id, xs in root_x.items():
        for point, x in zip(tracks[target_id].get("points", []), xs):
            point["position"][0] = float(x)
    for target_id in ("vendor", "counter"):
        for point in tracks[target_id].get("points", []):
            point["position"] = [1.3, 0.1, float(point["position"][2])]
    paper_positions = {
        "paper_a": [
            ([-2.4, -0.45, 1.15], [0.0, 0.0, 0.0]),
            ([-1.4, -0.45, 1.15], [0.0, 0.0, 0.0]),
            ([-0.8, -0.30, 1.70], [0.45, 0.20, 0.80]),
            ([0.4, 0.20, 1.10], [-0.60, -0.30, -0.70]),
            ([1.35, 0.20, 0.05], [0.10, 0.0, 0.25]),
        ],
        "paper_b": [
            ([-2.0, -0.10, 1.18], [0.0, 0.0, 0.0]),
            ([-1.0, -0.10, 1.18], [0.0, 0.0, 0.0]),
            ([-0.6, -0.05, 1.85], [-0.35, 0.25, -0.55]),
            ([0.6, 0.40, 1.15], [0.55, -0.20, 0.65]),
            ([1.50, 0.25, 0.06], [-0.10, 0.0, -0.25]),
        ],
    }
    for target_id, entries in paper_positions.items():
        for point, (position, rotation) in zip(tracks[target_id].get("points", []), entries):
            point["position"] = list(position)
            point["rotation"] = list(rotation)
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["points"] = [
                {"frame": frame, "position": [0.0, -7.0, 3.4], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 32.0
        elif camera_id == "reverse":
            camera["points"] = [
                {"frame": frame, "position": [6.0, -5.0, 3.5], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 36.0
        elif camera_id == "elevated":
            camera["points"] = [
                {"frame": frame, "position": [1.5, -4.0, 7.0], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 36.0
    revised["revision"] = {
        "id": "revision_034",
        "parent_revision": "revision_033",
        "reason": "VLM found role identity and exchange staging unstable from the opening frame; shorten the empty approach, add role-specific clothing accents, bring vendor/counter into the cart lane, lower the paper lift, and tighten the four authored views while preserving left-to-right motion",
        "preserved": ["same Quaternius male/female assets", "customer/helper gesture phases", "cart-box coupling", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_helper_settle_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_035 with a visible helper turn and paper landing zone."""
    revised = indoor_market_identity_grounding_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    helper_points = tracks["helper"].get("points", [])
    helper_yaw = [0.0, 0.0, 0.0, -0.85, -0.85]
    for point, yaw in zip(helper_points, helper_yaw):
        point["rotation"][2] = yaw
        point["position"][1] = 0.45
    gestures = {(str(item.get("target_id")), str(item.get("limb"))): item for item in revised.get("gesture_tracks", [])}
    gestures[("helper", "left_arm")]["points"] = [(0, 0.0), (60, 0.0), (78, 1.15), (96, 1.15), (108, 0.0), (119, 0.0)]
    landing = {
        "paper_a": [0.8, 0.75, 0.05],
        "paper_b": [1.0, 0.85, 0.06],
    }
    for target_id, position in landing.items():
        points = tracks[target_id].get("points", [])
        if points:
            points[-1]["position"] = list(position)
            points[-1]["rotation"] = [0.0, 0.0, 0.35 if target_id == "paper_a" else -0.35]
    for camera in revised.get("cameras", []):
        if str(camera.get("camera_id")) == "elevated":
            camera["points"] = [
                {"frame": frame, "position": [1.2, -3.5, 6.0], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 42.0
            camera["role"] = "lower elevated exchange view preserving helper turn and two visible papers settling beside the counter"
    revised["revision"] = {
        "id": "revision_035",
        "parent_revision": "revision_034",
        "reason": "VLM found the helper gesture open-ended and the paper landing hidden; move helper into a readable middle lane with an explicit turn, shorten the left-arm cue, place both sheets on a visible ground landing zone, and lower the elevated camera",
        "preserved": ["role-specific clothing accents", "shortened cart approach", "cart-box coupling", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_proxy_cleanup_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_036 after removing a proxy-only head obstruction."""
    revised = indoor_market_helper_settle_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    landing = {"paper_a": [1.0, -0.35, 0.05], "paper_b": [1.2, -0.45, 0.06]}
    for target_id, position in landing.items():
        points = tracks[target_id].get("points", [])
        if points:
            points[-1]["position"] = list(position)
    for camera in revised.get("cameras", []):
        if str(camera.get("camera_id")) == "elevated":
            camera["points"] = [
                {"frame": frame, "position": [1.0, -3.0, 5.2], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 44.0
            camera["role"] = "lower elevated view with unobstructed heads, cart grip and visible papers settling beside the counter"
    revised["revision"] = {
        "id": "revision_036",
        "parent_revision": "revision_035",
        "reason": "VLM found colored role blocks covering character heads and the paper landing hidden; remove the proxy-only torso vest blocks, retain only small shoulder accents, place papers in the front ground landing zone, and lower the elevated camera again",
        "preserved": ["role-specific asset IDs", "customer/helper trajectories and turn", "cart-box coupling", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_push_ik_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_037 using a shared cart-parented customer hand IK target."""
    revised = indoor_market_proxy_cleanup_revision(spec)
    revised["revision"] = {
        "id": "revision_037",
        "parent_revision": "revision_036",
        "reason": "VLM repeatedly found the customer beside rather than pushing the cart; bind the imported customer's right forearm with a two-bone IK target parented to the shared handcart while preserving the authored pause/raise/resume trajectory",
        "preserved": ["head-unobscured proxy", "role shoulder accents", "paper landing zone", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_push_ik_gesture_window_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_038 with IK contact outside the authored raise window."""
    revised = indoor_market_push_ik_revision(spec)
    revised["revision"] = {
        "id": "revision_038",
        "parent_revision": "revision_037",
        "reason": "the push IK correctly held the hand on the cart but overrode the pause/raise beat; use an explicit constraint influence window so contact is active during travel and released only during the authored gesture",
        "preserved": ["shared cart-parented IK target", "head-unobscured proxy", "paper landing zone", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_counter_grounding_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_039 with a grounded, visible vendor counter."""
    revised = indoor_market_push_ik_gesture_window_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    for target_id in ("vendor", "counter"):
        for point in tracks[target_id].get("points", []):
            point["position"] = [0.8, 0.3, 0.0]
    for camera in revised.get("cameras", []):
        camera_id = str(camera.get("camera_id"))
        if camera_id == "master":
            camera["points"] = [
                {"frame": frame, "position": [0.0, -6.5, 3.2], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 36.0
            camera["role"] = "front market exchange view with grounded counter, vendor behind it, pusher and helper all separated"
        elif camera_id == "reverse":
            camera["points"] = [
                {"frame": frame, "position": [5.0, -4.5, 3.2], "rotation": [0.0, 0.0, 0.0]}
                for frame in (0, 30, 60, 90, 119)
            ]
            camera["lens_mm"] = 40.0
            camera["role"] = "reverse market exchange view preserving counter/vendor sightline and cart travel"
    revised["revision"] = {
        "id": "revision_039",
        "parent_revision": "revision_038",
        "reason": "VLM found the counter floating as background geometry and the vendor/helper roles obscured; lower vendor and counter roots to the world floor, bring the stall into the cart lane, and refocus master/reverse sightlines on the grounded exchange",
        "preserved": ["IK contact window", "customer/helper trajectories", "paper landing zone", "four camera IDs", "shared world"],
    }
    return revised


def indoor_market_pause_landing_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_040 with a real hold before the resumed push."""
    revised = indoor_market_counter_grounding_revision(spec)
    tracks = {str(track.get("target_id")): track for track in revised.get("tracks", [])}
    # K2 and K3 share one root position: the 36-84 IK release is now a true
    # pause, and the left-to-right cart resumes only after the hand signal.
    for target_id in ("customer", "handcart", "box_a", "box_b"):
        points = tracks[target_id].get("points", [])
        if len(points) >= 4:
            points[3]["position"] = list(points[2]["position"])
    for target_id, position in {"paper_a": [0.6, 0.55, 0.08], "paper_b": [0.9, 0.65, 0.08]}.items():
        points = tracks[target_id].get("points", [])
        if points:
            points[-1]["position"] = list(position)
            points[-1]["rotation"] = [0.0, 0.0, 0.25 if target_id == "paper_a" else -0.25]
    revised["revision"] = {
        "id": "revision_040",
        "parent_revision": "revision_039",
        "reason": "VLM found the customer moving through the raised-hand beat and the two papers ending in different visual zones; hold customer/cart/boxes at the exchange pause until K3, resume the push afterward, and place both sheets in one visible counter-side landing zone",
        "preserved": ["counter/vendor world-floor grounding", "IK influence window", "role shoulder accents", "four camera IDs", "shared world"],
    }
    return revised


def warehouse_loading_readability_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_041 with an exterior reverse coverage path and readable slip flight.

    The first warehouse Proxy run was physically authored but failed the visual
    gate: the reverse camera ended inside the counter's projection, and the
    five sparse slip keyframes made the flutter read as a jump.  This revision
    changes only those Director-level staging choices; character/object
    identities, cart coupling, pause timing, and the four-camera contract stay
    intact.
    """
    revised = copy.deepcopy(spec)
    camera = next(item for item in revised["cameras"] if item["camera_id"] == "reverse")
    camera["points"] = [
        {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, 0.0]}
        for frame, position in zip(
            (0, 30, 60, 90, 119),
            ((9.0, 8.0, 5.0), (7.8, 6.0, 4.5), (6.8, 4.6, 4.0), (5.9, 3.6, 3.6), (5.2, 2.8, 3.2)),
        )
    ]
    camera["role"] = "exterior reverse three-quarter coverage of the cart brake, assistant turn and counter handoff"
    camera["lens_mm"] = 34.0
    shared_frames = (0, 24, 48, 60, 66, 72, 84, 90, 102, 119)

    def resample(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(points, key=lambda item: int(item["frame"]))
        result = []
        for frame in shared_frames:
            if frame <= int(ordered[0]["frame"]):
                source = ordered[0]
                result.append({"frame": frame, "position": list(source["position"]), "rotation": list(source.get("rotation", [0.0, 0.0, 0.0]))})
                continue
            if frame >= int(ordered[-1]["frame"]):
                source = ordered[-1]
                result.append({"frame": frame, "position": list(source["position"]), "rotation": list(source.get("rotation", [0.0, 0.0, 0.0]))})
                continue
            for left, right in zip(ordered, ordered[1:]):
                left_frame = int(left["frame"])
                right_frame = int(right["frame"])
                if left_frame <= frame <= right_frame:
                    alpha = (frame - left_frame) / float(right_frame - left_frame)
                    position = [float(a) + alpha * (float(b) - float(a)) for a, b in zip(left["position"], right["position"])]
                    left_rotation = left.get("rotation", [0.0, 0.0, 0.0])
                    right_rotation = right.get("rotation", [0.0, 0.0, 0.0])
                    rotation = [float(a) + alpha * (float(b) - float(a)) for a, b in zip(left_rotation, right_rotation)]
                    result.append({"frame": frame, "position": position, "rotation": rotation})
                    break
        return result

    for authored_camera in revised["cameras"]:
        authored_camera["points"] = resample(authored_camera["points"])
    tracks = {str(track["target_id"]): track for track in revised["tracks"]}
    for track in revised["tracks"]:
        track["points"] = resample(track["points"])

    def point(frame: int, position: tuple[float, float, float], yaw: float = 0.0) -> dict[str, Any]:
        return {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, yaw]}

    tracks["paper_a"]["points"] = [
        point(0, (-3.4, -0.45, 1.18)),
        point(24, (-2.84, -0.45, 1.18)),
        point(48, (-1.95, -0.45, 1.18)),
        point(60, (-1.6, -0.45, 1.18)),
        point(66, (-1.35, -0.40, 1.50), 0.25),
        point(72, (-1.05, -0.15, 1.78), -0.35),
        point(84, (-0.45, -0.30, 1.43), 0.45),
        point(90, (-0.15, -0.05, 1.12), -0.15),
        point(102, (0.35, 0.25, 0.78), -0.25),
        point(119, (0.9, 0.5, 0.06), 0.25),
    ]
    tracks["paper_b"]["points"] = [
        point(0, (-3.2, -0.10, 1.20)),
        point(24, (-2.64, -0.10, 1.20)),
        point(48, (-1.85, -0.10, 1.20)),
        point(60, (-1.6, -0.10, 1.20)),
        point(66, (-1.25, 0.05, 1.58), -0.30),
        point(72, (-0.90, 0.30, 1.96), 0.40),
        point(84, (-0.20, 0.05, 1.56), -0.45),
        point(90, (0.10, 0.20, 1.18), 0.20),
        point(102, (0.55, 0.38, 0.82), 0.30),
        point(119, (1.1, 0.6, 0.08), -0.25),
    ]
    revised["appearance_prompt"] = str(revised["appearance_prompt"]) + (
        " The slips must remain visibly attached to the top-box source until the brake release, "
        "then follow a continuous, irregular, gravity-driven flutter with several visible changes of height and heading before landing."
    )
    revised["revision"] = {
        "id": "revision_041",
        "parent_revision": None,
        "reason": "Proxy VLM found the reverse camera occluded by the receiving counter and the sparse packing-slip keyframes unreadable; move the reverse camera along an exterior three-quarter arc and densify the source-connected slip flight without changing the authored pause, coupling, or entity set",
        "preserved": ["shared world", "four camera IDs", "cart/box coupling", "grounded wheels", "worker pause and signal order", "assistant trajectory"],
    }
    return revised


def warehouse_loading_coverage_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_042 with cart-targeted reverse coverage and assistant lane separation."""
    revised = warehouse_loading_readability_revision(spec)
    reverse = next(item for item in revised["cameras"] if item["camera_id"] == "reverse")
    reverse["target"] = "handcart"
    reverse["role"] = "wide exterior reverse three-quarter coverage keeping the cart, worker, assistant and counter in one readable handoff lane"
    reverse["lens_mm"] = 30.0
    helper = next(track for track in revised["tracks"] if track["target_id"] == "helper")
    frames = [int(point["frame"]) for point in helper["points"]]
    positions = [
        (-3.25, 0.45, 0.0), (-2.85, 0.42, 0.0), (-2.25, 0.38, 0.0),
        (-1.85, 0.34, 0.0), (-1.55, 0.30, 0.0), (-1.20, 0.25, 0.0),
        (-0.65, 0.20, 0.0), (-0.25, 0.15, 0.0), (0.35, 0.05, 0.0), (0.85, 0.0, 0.0),
    ]
    rotations = [-0.55, -0.50, -0.45, -0.40, -0.35, -0.30, -0.20, -0.15, -0.10, -0.05]
    helper["points"] = [
        {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, rotation]}
        for frame, position, rotation in zip(frames, positions, rotations)
    ]
    revised["gesture_tracks"] = [
        {"target_id": "customer", "limb": "right_arm", "points": [(0, 0.0), (48, 0.0), (60, 1.1), (84, 1.1), (96, 0.0), (119, 0.0)]},
        {"target_id": "helper", "limb": "left_arm", "points": [(0, 0.0), (60, 0.0), (78, 0.65), (96, 0.65), (108, 0.0), (119, 0.0)]},
    ]
    revised["revision"] = {
        "id": "revision_042",
        "parent_revision": "revision_041",
        "reason": "VLM still found the counter dominant in reverse coverage and the assistant spatially detached; target the reverse camera at the moving cart, widen the lens, move the assistant into a single trailing action lane, and reduce the pointing amplitude while preserving the source-connected slip and brake order",
        "preserved": ["revision_041 exterior camera path", "shared world", "cart/box coupling", "grounded wheels", "worker pause and signal order", "packing-slip frames"],
    }
    return revised


def warehouse_loading_physics_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_043 with non-intersecting pusher/vendor staging and wider action coverage."""
    revised = warehouse_loading_coverage_revision(spec)
    tracks = {str(track["target_id"]): track for track in revised["tracks"]}
    cart_x = [float(point["position"][0]) for point in tracks["handcart"]["points"]]
    customer = tracks["customer"]
    for point, x in zip(customer["points"], cart_x):
        point["position"] = [x - 0.9, -0.8, 0.0]
        point["rotation"] = [0.0, 0.0, 0.1 if x > -2.0 else 0.0]
    vendor = tracks["vendor"]
    for point in vendor["points"]:
        point["position"] = [1.8, 1.8, 0.0]
        point["rotation"] = [0.0, 0.0, 3.14]

    def widen(camera_id: str, anchors: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]], lens: float, role: str) -> None:
        camera = next(item for item in revised["cameras"] if item["camera_id"] == camera_id)
        for point in camera["points"]:
            frame = int(point["frame"])
            if frame <= 60:
                alpha = frame / 60.0
                left, right = anchors[0], anchors[1]
            else:
                alpha = (frame - 60) / 59.0
                left, right = anchors[1], anchors[2]
            point["position"] = [float(a) + alpha * (float(b) - float(a)) for a, b in zip(left, right)]
        camera["lens_mm"] = lens
        camera["role"] = role

    widen("master", ((0.0, -14.0, 4.5), (0.0, -10.0, 4.0), (1.0, -6.5, 3.8)), 32.0, "wide master proving the separated pusher, grounded cart lane, counter and assistant before the brake")
    widen("lateral", ((-11.0, -3.5, 3.5), (-8.0, -2.5, 3.2), (-4.5, -1.5, 3.0)), 34.0, "cart-side tracking view keeping the pusher behind the horizontal grip and both boxes on the wheel line")
    widen("elevated", ((0.0, 8.0, 9.0), (1.0, 5.0, 7.5), (1.5, 1.5, 6.0)), 30.0, "lower diagonal crane view proving the cart lane, trailing assistant, receiving counter and slip landing")
    revised["appearance_prompt"] = str(revised["appearance_prompt"]) + (
        " Keep the worker's torso behind the cart handle with one hand visibly gripping it; keep the supervisor behind the receiving table, "
        "and show the assistant as a separate trailing person rather than intersecting the cart or boxes."
    )
    revised["revision"] = {
        "id": "revision_043",
        "parent_revision": "revision_042",
        "reason": "VLM found character-cart intersections, an isolated receiving table, weak wheel grounding and over-tight master/lateral/elevated coverage; separate the pusher and supervisor in depth and widen the three views at the Director/Proxy layer",
        "preserved": ["revision_042 cart-targeted reverse", "shared world", "cart/box coupling", "source-connected slip frames", "brake/pause/signal order", "trailing assistant lane"],
    }
    return revised


def warehouse_loading_paper_revision(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create revision_044 with flat, visibly tilted paper-sheet poses."""
    revised = warehouse_loading_physics_revision(spec)
    tracks = {str(track["target_id"]): track for track in revised["tracks"]}
    paper_a_rotations = [
        (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0),
        (0.4, -0.25, 0.25), (-0.65, 0.35, -0.35), (0.75, -0.30, 0.45),
        (-0.50, 0.25, -0.15), (0.25, -0.15, -0.25), (0.0, 0.0, 0.25),
    ]
    paper_b_rotations = [
        (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0),
        (-0.45, 0.30, -0.30), (0.70, -0.40, 0.40), (-0.80, 0.25, -0.45),
        (0.55, -0.20, 0.20), (-0.30, 0.18, 0.30), (0.0, 0.0, -0.25),
    ]
    for target_id, rotations in (("paper_a", paper_a_rotations), ("paper_b", paper_b_rotations)):
        points = tracks[target_id]["points"]
        for point, rotation in zip(points, rotations):
            point["rotation"] = list(rotation)
    revised["appearance_prompt"] = str(revised["appearance_prompt"]) + (
        " The two packing slips are thin rectangular paper sheets with visible flat faces and changing tilt, not cubes, colored markers, or floating UI dots."
    )
    revised["revision"] = {
        "id": "revision_044",
        "parent_revision": "revision_043",
        "reason": "VLM isolated the remaining failure to the packing-slip representation; use larger thin sheet proxies with explicit pitch/roll flutter and keep source, airborne and landing frames unchanged",
        "preserved": ["revision_043 separated staging", "cart-targeted reverse", "shared world", "cart/box coupling", "brake/pause/signal order", "assistant lane"],
    }
    return revised


def appearance_profile_for(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create appearance facts without leaking motion/camera instructions."""
    identity_bible = {}
    if str(spec.get("scene_id")) == "indoor_market_exchange":
        identity_bible = {
            "vendor": "the same middle-aged woman in a dark blue apron and cream shirt in every view",
            "customer": "the same adult man in a rust-red jacket and dark trousers in every view",
            "helper": "the same adult woman in a mustard work vest and dark trousers in every view",
            "handcart": "the same small silver two-wheel handcart with one fixed handle in every view",
            "box_a": "the same large brown cardboard box on the left side of the handcart in every view",
            "box_b": "the same smaller brown cardboard box on the right side of the handcart in every view",
        }
    elif str(spec.get("scene_id")) == "plaza_dance_circle":
        identity_bible = {
            "person_a": "the same adult dancer in a navy jacket and dark trousers in every view",
            "backpack": "the same orange backpack fixed to the dancer torso in every view",
        }
    subjects = []
    for entity in spec["entities"]:
        entity_id = str(entity["id"])
        if entity["kind"] == "character":
            description = identity_bible.get(entity_id, "natural live-action human actor with stable identity, realistic anatomy, coherent clothing, hair, hands and face")
        else:
            description = identity_bible.get(entity_id, "believable real-world prop with coherent material, scale and surface details")
        subjects.append({"entity_id": entity_id, "description": description})
    return {
        "schema_version": "appearance-profile-1.0",
        "scene_id": spec["scene_id"],
        "subjects": subjects,
        "environment": "natural live-action location with physically plausible architecture and props",
        "lighting": "continuous natural cinematic lighting with stable exposure and realistic contact shadows",
        "quality": "photorealistic live-action image quality, natural skin and fabric detail, stable identity, no low-poly geometry",
        "must_avoid": [
            "extra people or duplicated props",
            "clay, white cylinders, primitive limbs, labels, guide lines or storyboard overlays",
            "identity drift, face drift, clothing drift or texture flicker",
            "unrealistic plastic skin, broken anatomy, floating props or inconsistent materials",
            "cross-view identity or prop drift between the independently rendered reference-video tasks",
        ],
    }


def seedance_camera_prompt_for(spec: Mapping[str, Any], camera_id: str, *, identity_anchor_first: bool = True) -> str:
    """Compile a camera-specific Seedance restyle prompt.

    The previous runner sent identical text to every independent task.  That
    left the backend free to reinterpret each reference as a new shot.  This
    prompt keeps the shared cast/props from the appearance compiler while
    binding the task to its authored camera role and visible proxy plate.
    """
    camera = next((item for item in spec.get("cameras", []) if str(item.get("camera_id")) == str(camera_id)), None)
    if camera is None:
        raise ValueError(f"unknown camera_id: {camera_id}")
    entity_ids = [str(item.get("id")) for item in spec.get("entities", []) if isinstance(item, Mapping)]
    entity_text = ", ".join(entity_ids)
    role = str(camera.get("role", ""))
    target = str(camera.get("target", ""))
    reference_order = (
        "If two reference videos are supplied, the first is the canonical shared identity/world anchor and the second is the authored camera motion plate; use both together, never replace the anchor cast, clothing, cart, box count, counter, or lighting with the second video's independent interpretation."
        if identity_anchor_first else
        "If two reference videos are supplied, the first is the authored camera motion plate and controls viewpoint, framing, target, occlusion order, and path; the second is only a canonical identity/material anchor. Do not let the identity anchor override the first video's camera role or turn a lateral/reverse/elevated view into a front master view."
    )
    return (
        str(spec["appearance_prompt"]).strip()
        + "\n\nCamera-specific structural lock\n"
        + f"This is the camera_id={camera_id} task. Authored camera responsibility: {role}. Authored camera target: {target}.\n"
        + "Treat the uploaded reference video as a locked structural plate, not as loose inspiration: produce a one-to-one live-action restyle of every frame with the same camera path, framing, timing, occlusion order, and spatial layout.\n"
        + f"The shared-world entity set is exactly: {entity_text}. every visible proxy person and prop in the input must be restyled and remain present in its authored image region; do not omit, merge, duplicate, or replace any visible entity.\n"
        + reference_order + "\n"
        + "Do not create a new environment, a new shot, a new camera angle, a cut, a mannequin scene, or a pallet/cart substitute. Preserve the supplied camera role and all visible white-proxy silhouettes while changing only their appearance to coherent live action."
    )


def copy_canonical_assets(*, spec: Mapping[str, Any], scene_output: Path, asset_dir: Path | None) -> dict[str, str]:
    """Copy optional per-entity GLBs into the immutable run directory.

    A single verified humanoid source (``CesiumMan.glb`` or
    ``canonical_humanoid.glb``) may be reused for several roles.  The run
    still receives one immutable, role-named copy so the asset registry and
    hashes remain explicit and auditable.
    """
    if asset_dir is None:
        return {}
    source_root = asset_dir.resolve(strict=True)
    destination_root = scene_output / "assets"
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for entity in spec["entities"]:
        if entity.get("kind") != "character":
            continue
        entity_id = str(entity["id"])
        source = source_root / (entity_id + ".glb")
        if not source.is_file():
            for fallback_name in ("canonical_humanoid.glb", "CesiumMan.glb", "humanoid.glb"):
                candidate = source_root / fallback_name
                if candidate.is_file():
                    source = candidate
                    break
        if not source.is_file():
            continue
        destination = destination_root / (entity_id + ".glb")
        shutil.copy2(source, destination)
        copied[entity_id] = (Path("assets") / destination.name).as_posix()
    return copied


def materialize_catalog_assets(*, spec: Mapping[str, Any], scene_output: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Materialize every character asset declared by SceneSpec, fail closed."""
    catalog_path = PROJECT_ROOT / "assets" / "characters" / "catalog.json"
    catalog = load_catalog(catalog_path, repository_root=PROJECT_ROOT)
    asset_paths: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    destination_root = scene_output / "assets" / "characters"
    for entity in spec.get("entities", []):
        if not isinstance(entity, Mapping) or entity.get("kind") != "character":
            continue
        entity_id = str(entity["id"])
        asset_id = str(entity.get("asset_id", ""))
        if not asset_id:
            raise AssetCatalogError(f"character entity has no asset_id: {entity_id}")
        model_path = catalog.materialize(asset_id, destination_root)
        asset = catalog.require(asset_id)
        asset_paths[entity_id] = (Path("assets") / "characters" / asset_id / "model.glb").as_posix()
        records[entity_id] = {
            "entity_id": entity_id,
            "catalog_asset_id": asset_id,
            "source_sha256": asset.source_sha256,
            "rig_map_sha256": sha256_file(asset.rig_map_path),
            "rig_map": json.loads(asset.rig_map_path.read_text(encoding="utf-8")),
            "materialized_model": str(model_path.relative_to(scene_output).as_posix()),
        }
    return asset_paths, records


def prepare_appearance_and_adapter(*, scene_output: Path, spec: Mapping[str, Any], world: WorldState, proxy_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Compile and hash-bind the appearance prompt after Proxy approval."""
    appearance_dir = scene_output / "appearance"
    appearance_dir.mkdir(parents=True, exist_ok=False)
    profile = appearance_profile_for(spec)
    compiled = compile_appearance_prompt(world=world, proxy_manifest=proxy_manifest, profile=profile)
    _write_json(appearance_dir / "profile.json", profile)
    _write_json(appearance_dir / "compiled.json", compiled)
    (appearance_dir / "prompt.txt").write_text(compiled["prompt"] + "\n", encoding="utf-8")
    adapter = prepare_backend_adapter(
        backend="seedance_t2v",
        world=world,
        proxy_manifest=proxy_manifest,
        proxy_root=scene_output / "sandbox",
        appearance_prompt=compiled,
        model="Doubao-Seedance-2.0",
    )
    adapter_dir = appearance_dir / "backend_adapter_seedance_t2v"
    write_backend_adapter_bundle(adapter, adapter_dir)
    return {
        "profile": str(appearance_dir / "profile.json"),
        "compiled": str(appearance_dir / "compiled.json"),
        "prompt": str(appearance_dir / "prompt.txt"),
        "backend_adapter": str(adapter_dir / "bundle.json"),
        "prompt_sha256": compiled["prompt_sha256"],
        "adapter_sha256": adapter["bundle_sha256"],
    }


def _blender_script() -> str:
    """Return the standalone Blender program used by every complex scene."""
    return textwrap.dedent(
        r'''
        import argparse, hashlib, json, math, mathutils, re, sys
        from pathlib import Path
        import bpy
        from mathutils import Vector

        values = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
        parser = argparse.ArgumentParser()
        parser.add_argument("--world-state", required=True)
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--render-style", default="clay", choices=["clay", "canonical", "storyhuman", "skeleton", "asset_humanoid", "diagnostic"])
        parser.add_argument("--resolution", default="640x360")
        parser.add_argument("--motion-bvh")
        parser.add_argument("--motion-bvh-alt")
        args = parser.parse_args(values)
        out = Path(args.output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        world_state = json.loads(Path(args.world_state).read_text(encoding="utf-8"))
        registry_path = Path(args.world_state).resolve().parent / "asset_registry.json"
        asset_registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else {"assets": []}
        shared_asset_hashes = {
            str(item.get("catalog_asset_id")): str(item.get("source_asset_sha256"))
            for item in asset_registry.get("assets", [])
            if isinstance(item, dict) and item.get("source_kind") == "asset_catalog_glb"
        }
        gesture_path = Path(args.world_state).resolve().parent / "gesture_tracks.json"
        gesture_tracks = json.loads(gesture_path.read_text(encoding="utf-8")).get("gesture_tracks", []) if gesture_path.is_file() else []
        motion_path = Path(args.world_state).resolve().parent / "motion_tracks.json"
        canonical_motion_tracks = json.loads(motion_path.read_text(encoding="utf-8")).get("characters", []) if motion_path.is_file() else []
        skeleton_motion_path = Path(args.world_state).resolve().parent / "skeleton_motion.json"
        skeleton_motion = json.loads(skeleton_motion_path.read_text(encoding="utf-8")).get("characters", []) if skeleton_motion_path.is_file() else []
        skeleton_motion_by_id = {str(item.get("target_id")): item for item in skeleton_motion}
        canonical = json.dumps(world_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        world_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        scene_plan = world_state["scene_plan"]
        frame_count = int(scene_plan["frame_count"])
        fps = int(scene_plan["fps"])
        width, height = (int(x) for x in re.split(r"[xX]", args.resolution))

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        scene = bpy.context.scene
        scene.frame_start, scene.frame_end = 0, frame_count - 1
        scene.render.fps = fps
        scene.render.resolution_x, scene.render.resolution_y = width, height
        scene.render.resolution_percentage = 100
        scene.render.image_settings.media_type = "VIDEO"
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
        scene.render.film_transparent = False
        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        except Exception:
            try:
                scene.render.engine = "BLENDER_WORKBENCH"
            except Exception:
                pass

        bvh_sources = []
        bvh_metadata = []
        for raw_motion_path in (args.motion_bvh, args.motion_bvh_alt):
            if not raw_motion_path:
                continue
            motion_path = Path(raw_motion_path).resolve()
            if not motion_path.is_file() or motion_path.suffix.lower() != ".bvh":
                raise RuntimeError("motion BVH file is missing: " + str(motion_path))
            before = set(obj.name for obj in bpy.context.scene.objects)
            bpy.ops.import_anim.bvh(filepath=str(motion_path), target="ARMATURE", global_scale=0.01, use_fps_scale=False, update_scene_fps=False)
            imported_motion = [obj for obj in bpy.context.scene.objects if obj.name not in before and obj.type == "ARMATURE"]
            if not imported_motion:
                raise RuntimeError("Blender imported no armature from BVH: " + str(motion_path))
            source_armature = imported_motion[-1]
            source_armature.hide_render = True
            source_armature.hide_viewport = True
            action = source_armature.animation_data.action if source_armature.animation_data else None
            if action is None:
                raise RuntimeError("BVH armature has no action: " + str(motion_path))
            bvh_sources.append(source_armature)
            bvh_metadata.append({"path": str(motion_path), "sha256": hashlib.sha256(motion_path.read_bytes()).hexdigest(), "frame_range": [float(action.frame_range[0]), float(action.frame_range[1])]})

        world = bpy.data.worlds.new(scene_plan["scene_id"] + "_SharedWorld")
        scene.world = world
        world.use_nodes = True
        world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.035, 0.035, 0.035, 1.0)
        world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.32
        clay = bpy.data.materials.new("NeutralClay")
        clay.diffuse_color = (0.58, 0.58, 0.58, 1.0)
        clay.roughness = 0.86
        dark = bpy.data.materials.new("NeutralClayDark")
        dark.diffuse_color = (0.22, 0.22, 0.22, 1.0)
        dark.roughness = 0.92
        light = bpy.data.materials.new("NeutralClayLight")
        light.diffuse_color = (0.76, 0.76, 0.76, 1.0)
        light.roughness = 0.8
        role_a = bpy.data.materials.new("CanonicalRoleA")
        role_a.diffuse_color = (0.20, 0.42, 0.62, 1.0)
        role_a.roughness = 0.82
        role_b = bpy.data.materials.new("CanonicalRoleB")
        role_b.diffuse_color = (0.58, 0.30, 0.18, 1.0)
        role_b.roughness = 0.82
        role_c = bpy.data.materials.new("CanonicalRoleC")
        role_c.diffuse_color = (0.20, 0.52, 0.28, 1.0)
        role_c.roughness = 0.82
        backpack_accent = bpy.data.materials.new("BackpackAccent")
        backpack_accent.diffuse_color = (0.86, 0.38, 0.08, 1.0)
        backpack_accent.roughness = 0.78

        def add_cube(name, location, scale, material=clay, bevel=0.08):
            bpy.ops.mesh.primitive_cube_add(location=location)
            obj = bpy.context.object
            obj.name = name
            obj.scale = scale
            obj.data.materials.append(material)
            if bevel:
                modifier = obj.modifiers.new("soft_edges", "BEVEL")
                modifier.width = bevel
                modifier.segments = 2
            return obj

        def add_cylinder(name, location, radius, depth, material=clay):
            bpy.ops.mesh.primitive_cylinder_add(vertices=20, radius=radius, depth=depth, location=location)
            obj = bpy.context.object
            obj.name = name
            obj.data.materials.append(material)
            return obj

        def add_sphere(name, location, scale, material=clay):
            bpy.ops.mesh.primitive_uv_sphere_add(segments=20, ring_count=12, location=location)
            obj = bpy.context.object
            obj.name = name
            obj.scale = scale
            obj.data.materials.append(material)
            return obj

        def smooth_mesh(obj):
            """Make procedural proxy surfaces read as rounded forms."""
            if getattr(obj, "type", None) == "MESH":
                for polygon in obj.data.polygons:
                    polygon.use_smooth = True
            return obj

        def safe_arm_angle(limb, angle):
            # Upper-arm centers are not solved shoulder joints.  Prevent the
            # inward swing from entering the head/torso corridor while keeping
            # outward authored gestures readable.
            value = float(angle)
            if limb == "left_arm":
                return max(-1.25, min(0.78, value))
            if limb == "right_arm":
                return max(-0.78, min(1.25, value))
            return max(-0.78, min(0.78, value))

        def add_root(name):
            root = bpy.data.objects.new(name, None)
            bpy.context.collection.objects.link(root)
            return root

        def parent_local(obj, root, location):
            obj.parent = root
            obj.location = location
            return obj

        def try_import_canonical_glb(eid, root, registry_asset):
            """Import a supplied GLB once and attach its top-level objects to root."""
            if not isinstance(registry_asset, dict) or registry_asset.get("source_kind") not in {"canonical_glb", "asset_catalog_glb"}:
                return False
            raw_path = registry_asset.get("path")
            if not isinstance(raw_path, str):
                return False
            asset_path = (Path(args.world_state).resolve().parent / raw_path).resolve()
            if not asset_path.is_file() or asset_path.suffix.lower() != ".glb":
                return False
            before = set(obj.name for obj in bpy.context.scene.objects)
            try:
                bpy.ops.import_scene.gltf(filepath=str(asset_path))
            except Exception:
                return False
            imported = [obj for obj in bpy.context.scene.objects if obj.name not in before]
            if not imported:
                return False
            for obj in imported:
                if obj.type == "ARMATURE":
                    obj.data.pose_position = "POSE"
                    for pose_bone in obj.pose.bones:
                        pose_bone.location = (0.0, 0.0, 0.0)
                        pose_bone.rotation_mode = "QUATERNION"
                        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
                        pose_bone.scale = (1.0, 1.0, 1.0)
            for obj in imported:
                if obj.parent is None:
                    matrix = obj.matrix_world.copy()
                    obj.parent = root
                    obj.matrix_world = matrix
                obj.name = eid + "__glb__" + obj.name
            # CesiumMan is authored Y-up.  Normalize the imported hierarchy
            # once at its top-level empty so mesh, armature and skinning keep
            # the same transform.  The authored entity root remains the only
            # source of world-space motion for all cameras.
            axis_root = next((obj for obj in imported if obj.type == "EMPTY"), None)
            if axis_root is not None:
                axis_root.rotation_euler.x = math.radians(90.0)
                axis_root.scale = (2.0, 2.0, 2.0)
                axis_root.location = (0.0, 0.0, 0.0)
            for obj in imported:
                if obj.type == "MESH" and "Icosphere" in obj.name:
                    obj.hide_render = True
                if obj.type == "ARMATURE" and obj.animation_data is not None:
                    # Keep the imported skin action attached; authored root
                    # and camera trajectories are still keyed below.
                    _ = obj.animation_data.action
            return True

        def add_rigged_role_marker(eid, root):
            """Add a small role-color waist marker without replacing the mesh."""
            material = role_b if eid == "customer" else role_a if eid == "helper" else role_c if eid == "vendor" else role_a if eid.endswith("_a") else role_b if eid.endswith("_b") else role_c
            # Keep a tiny shoulder accent for role readability.  The previous
            # torso-spanning strip was mistaken for a prop/occluder and hid
            # the actual arm/hand action from the VLM.
            parent_local(add_cube(eid + "__role_marker", (0.42, -0.04, 2.15), (0.08, 0.04, 0.08), material, 0.03), root, (0.42, -0.04, 2.15))

        def add_canonical_humanoid(eid, root):
            """Materialize one readable articulated human silhouette.

            This is deliberately deterministic and local: it is a canonical
            procedural asset, not a photoreal model.  Every part is attached
            to the same entity root so authored character trajectories remain
            the sole source of global motion and all cameras share one asset.
            """
            if args.render_style == "canonical" and eid.endswith("_a"):
                body_mat, outfit_mat, leg_mat = clay, role_a, dark
            elif args.render_style == "canonical" and eid.endswith("_b"):
                body_mat, outfit_mat, leg_mat = light, role_b, clay
            elif args.render_style == "canonical":
                body_mat, outfit_mat, leg_mat = dark, role_c, light
            elif eid.endswith("_a"):
                body_mat, outfit_mat, leg_mat = clay, light, dark
            elif eid.endswith("_b"):
                body_mat, outfit_mat, leg_mat = light, dark, clay
            else:
                body_mat, outfit_mat, leg_mat = dark, clay, light
            parent_local(add_cube(eid + "__torso", (0, 0, 1.84), (0.39, 0.23, 0.48), body_mat, 0.10), root, (0, 0, 1.84))
            parent_local(add_cube(eid + "__vest", (0, -0.02, 1.86), (0.43, 0.25, 0.38), outfit_mat, 0.08), root, (0, -0.02, 1.86))
            parent_local(add_cube(eid + "__pelvis", (0, 0, 1.28), (0.36, 0.22, 0.18), leg_mat, 0.08), root, (0, 0, 1.28))
            parent_local(add_cylinder(eid + "__neck", (0, 0, 2.38), 0.13, 0.18, dark), root, (0, 0, 2.38))
            parent_local(add_sphere(eid + "__head", (0, 0, 2.72), (0.30, 0.27, 0.34), body_mat), root, (0, 0, 2.72))
            parent_local(add_sphere(eid + "__hair", (0, 0.02, 2.90), (0.31, 0.28, 0.16), outfit_mat), root, (0, 0.02, 2.90))
            if eid.endswith("_c"):
                parent_local(add_cylinder(eid + "__cap", (0, 0.0, 3.02), 0.34, 0.10, outfit_mat), root, (0, 0.0, 3.02))
            for side, sign in (("L", 1.0), ("R", -1.0)):
                x = 0.50 * sign
                upper = parent_local(add_cylinder(eid + "__upper_arm." + side, (x, 0, 2.02), 0.14, 0.58, body_mat), root, (x, 0, 2.02))
                lower = parent_local(add_cylinder(eid + "__lower_arm." + side, (0, 0, -0.46), 0.12, 0.52, body_mat), upper, (0, 0, -0.46))
                parent_local(add_sphere(eid + "__hand." + side, (0, 0, -0.31), (0.14, 0.14, 0.14), outfit_mat), lower, (0, 0, -0.31))
                arms[(eid, "left_arm" if side == "L" else "right_arm")] = upper
                parent_local(add_sphere(eid + "__shoulder." + side, (x * 0.88, 0, 2.15), (0.17, 0.17, 0.17), clay), root, (x * 0.88, 0, 2.15))
                upper_leg = parent_local(add_cylinder(eid + "__upper_leg." + side, (0.18 * sign, 0, 0.98), 0.17, 0.62, leg_mat), root, (0.18 * sign, 0, 0.98))
                lower_leg = parent_local(add_cylinder(eid + "__lower_leg." + side, (0.18 * sign, 0, 0.42), 0.14, 0.55, body_mat), root, (0.18 * sign, 0, 0.42))
                foot = parent_local(add_cube(eid + "__foot." + side, (0.18 * sign, -0.13, 0.12), (0.16, 0.28, 0.11), leg_mat, 0.05), root, (0.18 * sign, -0.13, 0.12))
                legs[(eid, "left_leg" if side == "L" else "right_leg")] = {"upper": upper_leg, "lower": lower_leg, "foot": foot}

        def add_storyhuman_humanoid(eid, root):
            """Materialize a rounded, articulated StoryBlender-style proxy.

            This remains a neutral planning proxy, not a claim of photorealism:
            the silhouette has an actual head/neck, torso/pelvis volume,
            shoulder/elbow/knee joints, capsule-like limbs, hands and shoes.
            Every part is still attached to the authored entity root.
            """
            if eid.endswith("_a"):
                skin_mat, outfit_mat, leg_mat = light, role_a, dark
            elif eid.endswith("_b"):
                skin_mat, outfit_mat, leg_mat = light, role_b, dark
            else:
                skin_mat, outfit_mat, leg_mat = light, role_c, dark
            torso = smooth_mesh(parent_local(add_sphere(eid + "__story_torso", (0, 0, 1.78), (0.43, 0.27, 0.56), outfit_mat), root, (0, 0, 1.78)))
            smooth_mesh(parent_local(add_sphere(eid + "__story_chest", (0, -0.03, 2.02), (0.39, 0.25, 0.30), outfit_mat), root, (0, -0.03, 2.02)))
            smooth_mesh(parent_local(add_sphere(eid + "__story_pelvis", (0, 0, 1.27), (0.36, 0.25, 0.24), leg_mat), root, (0, 0, 1.27)))
            smooth_mesh(parent_local(add_cylinder(eid + "__story_neck", (0, 0, 2.36), 0.13, 0.18, skin_mat), root, (0, 0, 2.36)))
            smooth_mesh(parent_local(add_sphere(eid + "__story_head", (0, 0, 2.72), (0.29, 0.27, 0.34), skin_mat), root, (0, 0, 2.72)))
            smooth_mesh(parent_local(add_sphere(eid + "__story_hair", (0, 0.04, 2.91), (0.30, 0.28, 0.17), dark), root, (0, 0.04, 2.91)))
            for side, sign in (("L", 1.0), ("R", -1.0)):
                shoulder_x = 0.45 * sign
                smooth_mesh(parent_local(add_sphere(eid + "__story_shoulder." + side, (shoulder_x, 0, 2.14), (0.17, 0.17, 0.17), outfit_mat), root, (shoulder_x, 0, 2.14)))
                upper = smooth_mesh(parent_local(add_cylinder(eid + "__story_upper_arm." + side, (shoulder_x, 0, 1.87), 0.145, 0.56, skin_mat), root, (shoulder_x, 0, 1.87)))
                lower = smooth_mesh(parent_local(add_cylinder(eid + "__story_lower_arm." + side, (0, 0, -0.43), 0.125, 0.50, skin_mat), upper, (0, 0, -0.43)))
                smooth_mesh(parent_local(add_sphere(eid + "__story_elbow." + side, (0, 0, -0.72), (0.14, 0.14, 0.14), skin_mat), upper, (0, 0, -0.72)))
                smooth_mesh(parent_local(add_sphere(eid + "__story_hand." + side, (0, 0, -0.73), (0.14, 0.13, 0.15), skin_mat), lower, (0, 0, -0.73)))
                arms[(eid, "left_arm" if side == "L" else "right_arm")] = upper
                upper_leg = smooth_mesh(parent_local(add_cylinder(eid + "__story_upper_leg." + side, (0.18 * sign, 0, 0.98), 0.18, 0.62, leg_mat), root, (0.18 * sign, 0, 0.98)))
                lower_leg = smooth_mesh(parent_local(add_cylinder(eid + "__story_lower_leg." + side, (0.18 * sign, 0, 0.42), 0.145, 0.55, leg_mat), root, (0.18 * sign, 0, 0.42)))
                smooth_mesh(parent_local(add_sphere(eid + "__story_knee." + side, (0.18 * sign, 0, 0.67), (0.17, 0.16, 0.16), leg_mat), root, (0.18 * sign, 0, 0.67)))
                foot = parent_local(add_cube(eid + "__story_foot." + side, (0.18 * sign, -0.15, 0.12), (0.18, 0.30, 0.12), dark, 0.10), root, (0.18 * sign, -0.15, 0.12))
                legs[(eid, "left_leg" if side == "L" else "right_leg")] = {"upper": upper_leg, "lower": lower_leg, "foot": foot}

        def interp(points, frame):
            if frame <= points[0]["frame"]:
                return list(points[0]["position"]), list(points[0]["rotation"])
            if frame >= points[-1]["frame"]:
                return list(points[-1]["position"]), list(points[-1]["rotation"])
            for left, right in zip(points, points[1:]):
                if left["frame"] <= frame <= right["frame"]:
                    ratio = float(frame - left["frame"]) / float(right["frame"] - left["frame"])
                    pos = [left["position"][i] * (1-ratio) + right["position"][i] * ratio for i in range(3)]
                    rot = [left["rotation"][i] * (1-ratio) + right["rotation"][i] * ratio for i in range(3)]
                    return pos, rot
            return list(points[-1]["position"]), list(points[-1]["rotation"])

        def solve_two_link(root_point, target_point, upper_length, lower_length, pole_sign):
            root_vec = Vector(root_point)
            target_vec = Vector(target_point)
            delta = target_vec - root_vec
            distance = max(delta.length, 1e-7)
            minimum = abs(upper_length - lower_length) + 1e-6
            maximum = upper_length + lower_length
            reachable = minimum <= distance <= maximum
            clamped_distance = min(max(distance, minimum), maximum)
            direction = delta.normalized() if delta.length > 1e-7 else Vector((0, 0, -1))
            target_vec = root_vec + direction * clamped_distance
            pole = Vector((0, 1 if pole_sign >= 0 else -1, 0))
            pole_projection = pole - direction * pole.dot(direction)
            pole_axis = pole_projection.normalized() if pole_projection.length > 1e-7 else Vector((1, 0, 0))
            along = (upper_length * upper_length - lower_length * lower_length + clamped_distance * clamped_distance) / (2.0 * clamped_distance)
            height = math.sqrt(max(0.0, upper_length * upper_length - along * along))
            elbow_vec = root_vec + direction * along + pole_axis * height
            return root_vec, elbow_vec, target_vec, reachable

        def orient_segment(obj, start, end):
            start_vec, end_vec = Vector(start), Vector(end)
            obj.location = (start_vec + end_vec) * 0.5
            obj.rotation_mode = "XYZ"
            obj.rotation_euler = (end_vec - start_vec).to_track_quat("Z", "Y").to_euler()

        def add_skeleton_armature(eid, root):
            data = bpy.data.armatures.new(eid + "__procedural_skeleton_v1")
            armature = bpy.data.objects.new(eid + "__procedural_skeleton_v1", data)
            bpy.context.collection.objects.link(armature)
            armature.parent = root
            armature.hide_render = True
            bpy.context.view_layer.objects.active = armature
            armature.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            bones = {}
            definitions = {
                "root": ((0, 0, 0), (0, 0, 0.20), None),
                "pelvis": ((0, 0, 1.10), (0, 0, 1.40), "root"),
                "spine": ((0, 0, 1.40), (0, 0, 2.20), "pelvis"),
                "head": ((0, 0, 2.20), (0, 0, 2.80), "spine"),
                "upper_arm.L": ((0.42, 0, 2.10), (0.50, 0, 1.55), "spine"),
                "forearm.L": ((0.50, 0, 1.55), (0.50, 0, 1.10), "upper_arm.L"),
                "hand.L": ((0.50, 0, 1.10), (0.50, 0, 0.90), "forearm.L"),
                "upper_arm.R": ((-0.42, 0, 2.10), (-0.50, 0, 1.55), "spine"),
                "forearm.R": ((-0.50, 0, 1.55), (-0.50, 0, 1.10), "upper_arm.R"),
                "hand.R": ((-0.50, 0, 1.10), (-0.50, 0, 0.90), "forearm.R"),
                "upper_leg.L": ((0.18, 0, 1.25), (0.18, 0, 0.65), "pelvis"),
                "lower_leg.L": ((0.18, 0, 0.65), (0.18, 0, 0.12), "upper_leg.L"),
                "foot.L": ((0.18, 0, 0.12), (0.18, -0.30, 0.12), "lower_leg.L"),
                "upper_leg.R": ((-0.18, 0, 1.25), (-0.18, 0, 0.65), "pelvis"),
                "lower_leg.R": ((-0.18, 0, 0.65), (-0.18, 0, 0.12), "upper_leg.R"),
                "foot.R": ((-0.18, 0, 0.12), (-0.18, -0.30, 0.12), "lower_leg.R"),
            }
            for name, (head, tail, parent_name) in definitions.items():
                bone = data.edit_bones.new(eid + "__" + name)
                bone.head, bone.tail = head, tail
                if parent_name:
                    bone.parent = bones[parent_name]
                    bone.use_connect = False
                bones[name] = bone
            bpy.ops.object.mode_set(mode="OBJECT")
            targets = {}
            for target_name in ("hand_target.L", "hand_target.R", "foot_target.L", "foot_target.R", "pole.L", "pole.R"):
                target = bpy.data.objects.new(eid + "__ik_target__" + target_name, None)
                bpy.context.collection.objects.link(target)
                target.parent = root
                target.empty_display_type = "SPHERE"
                target.empty_display_size = 0.06
                target.hide_render = True
                targets[target_name] = target
            for bone_name, target_name, pole_name in (("forearm.L", "hand_target.L", "pole.L"), ("forearm.R", "hand_target.R", "pole.R"), ("lower_leg.L", "foot_target.L", "pole.L"), ("lower_leg.R", "foot_target.R", "pole.R")):
                pose_bone = armature.pose.bones.get(eid + "__" + bone_name)
                if pose_bone is None:
                    continue
                constraint = pose_bone.constraints.new("IK")
                constraint.target = targets[target_name]
                constraint.pole_target = targets[pole_name]
                constraint.chain_count = 2
                constraint.use_stretch = False
            return {"armature": armature, "bones": bones, "targets": targets}

        def add_skeleton_humanoid(eid, root):
            body_mat = clay if eid.endswith("_a") else light
            outfit_mat = role_a if eid.endswith("_a") else role_b if eid.endswith("_b") else role_c
            leg_mat = dark
            torso = parent_local(add_cube(eid + "__skeleton_torso", (0, 0, 1.84), (0.39, 0.23, 0.48), body_mat, 0.10), root, (0, 0, 1.84))
            parent_local(add_cube(eid + "__skeleton_vest", (0, -0.02, 1.86), (0.43, 0.25, 0.38), outfit_mat, 0.08), root, (0, -0.02, 1.86))
            parent_local(add_cube(eid + "__skeleton_pelvis", (0, 0, 1.28), (0.36, 0.22, 0.18), leg_mat, 0.08), root, (0, 0, 1.28))
            parent_local(add_sphere(eid + "__skeleton_head", (0, 0, 2.72), (0.30, 0.27, 0.34), body_mat), root, (0, 0, 2.72))
            if eid.endswith("_b"):
                parent_local(add_cylinder(eid + "__mic_stand", (0, -0.42, 0.92), 0.035, 1.35, dark), root, (0, -0.42, 0.92))
                parent_local(add_sphere(eid + "__mic_head", (0, -0.42, 1.62), (0.10, 0.10, 0.10), dark), root, (0, -0.42, 1.62))
            parts = {"torso": torso, "arms": {}, "legs": {}, "hands": {}, "feet": {}}
            for side, sign in (("L", 1.0), ("R", -1.0)):
                parts["arms"][side] = {"upper": parent_local(add_cylinder(eid + "__skeleton_upper_arm." + side, (sign * 0.46, 0, 1.82), 0.14, 0.58, body_mat), root, (sign * 0.46, 0, 1.82)), "lower": parent_local(add_cylinder(eid + "__skeleton_forearm." + side, (sign * 0.46, 0, 1.30), 0.12, 0.52, body_mat), root, (sign * 0.46, 0, 1.30))}
                parts["hands"][side] = parent_local(add_sphere(eid + "__skeleton_hand." + side, (sign * 0.46, 0, 1.02), (0.14, 0.14, 0.14), outfit_mat), root, (sign * 0.46, 0, 1.02))
                parts["legs"][side] = {"upper": parent_local(add_cylinder(eid + "__skeleton_upper_leg." + side, (sign * 0.18, 0, 0.98), 0.17, 0.62, leg_mat), root, (sign * 0.18, 0, 0.98)), "lower": parent_local(add_cylinder(eid + "__skeleton_lower_leg." + side, (sign * 0.18, 0, 0.42), 0.14, 0.55, body_mat), root, (sign * 0.18, 0, 0.42))}
                parts["feet"][side] = parent_local(add_cube(eid + "__skeleton_foot." + side, (sign * 0.18, -0.13, 0.12), (0.16, 0.28, 0.11), leg_mat, 0.05), root, (sign * 0.18, -0.13, 0.12))
            parts["rig"] = add_skeleton_armature(eid, root)
            skeleton_parts[eid] = parts

        tracks = {}
        for track in world_state["character_trajectory_plan"]["tracks"] + world_state["object_trajectory_plan"]["tracks"]:
            tracks[track["target_id"]] = track["points"]
        roots, arms, legs, skeleton_parts = {}, {}, {}, {}
        rigged_armatures = {}
        rigged_bone_maps = {}
        asset_log = []
        coupling_rules = {}
        entity_ids = {str(entity.get("id")) for entity in scene_plan["entities"]}
        if {"backpack", "person_a"}.issubset(entity_ids):
            coupling_rules["backpack"] = "person_a"
        if {"box_a", "handcart"}.issubset(entity_ids):
            coupling_rules["box_a"] = "handcart"
        if {"box_b", "handcart"}.issubset(entity_ids):
            coupling_rules["box_b"] = "handcart"
        if {"luggage", "traveler"}.issubset(entity_ids):
            coupling_rules["luggage"] = "traveler"
        if {"suitcase", "traveler"}.issubset(entity_ids):
            coupling_rules["suitcase"] = "traveler"
        for entity in scene_plan["entities"]:
            roots[str(entity["id"])] = add_root(str(entity["id"]))
        for child_id, parent_id in coupling_rules.items():
            # Parent at the root level while retaining local offsets derived
            # from the authored world tracks below.
            roots[child_id].parent = roots[parent_id]
            roots[child_id].matrix_parent_inverse = mathutils.Matrix.Identity(4)

        # A single shared neutral floor/backdrop makes spatial relationships visible.
        add_cube("ground", (0, 0, -0.18), (9.0, 7.0, 0.18), dark, 0.03)
        # Keep the backdrop behind every authored camera ray.  At y=5.5 it
        # occludes the reverse camera and produces a real black video.
        add_cube("backdrop", (0, 12.0, 3.0), (9.0, 0.18, 3.0), dark, 0.03)

        for entity in scene_plan["entities"]:
            eid, kind = entity["id"], entity["kind"]
            root = roots[eid]
            registry_asset = next((item for item in asset_registry.get("assets", []) if item.get("asset_id") == eid), None)
            if args.render_style == "asset_humanoid" and kind == "character":
                embedded_rig_map = (registry_asset or {}).get("rig_map")
                if isinstance(embedded_rig_map, dict):
                    rig_map = embedded_rig_map
                else:
                    rig_map_path = (Path(args.world_state).resolve().parent / "assets" / "characters" / str((registry_asset or {}).get("catalog_asset_id", "")) / "rig_map.json").resolve()
                    if not rig_map_path.is_file():
                        raise RuntimeError("asset_humanoid rig_map.json is missing: " + str(rig_map_path))
                    rig_map = json.loads(rig_map_path.read_text(encoding="utf-8"))
                if not isinstance(rig_map.get("unified_to_asset"), dict):
                    raise RuntimeError("asset_humanoid rig_map.json is invalid")
                rigged_bone_maps[str(eid)] = dict(rig_map["unified_to_asset"])
            if kind == "character":
                imported_glb = None if args.render_style == "storyhuman" else try_import_canonical_glb(eid, root, registry_asset)
                if imported_glb:
                    add_rigged_role_marker(eid, root)
                    armature = next((obj for obj in bpy.context.scene.objects if obj.name.startswith(eid + "__glb__") and obj.type == "ARMATURE"), None)
                    if armature is not None:
                        # Use a new authored action so gesture keyframes are
                        # real pose data rather than a log-only annotation.
                        if armature.animation_data is not None:
                            armature.animation_data.action = None
                        armature.animation_data_create()
                        armature.animation_data.action = bpy.data.actions.new(eid + "__authored_pose")
                        rigged_armatures[eid] = armature
                elif args.render_style == "skeleton":
                    add_skeleton_humanoid(eid, root)
                elif args.render_style == "storyhuman":
                    add_storyhuman_humanoid(eid, root)
                elif args.render_style == "canonical":
                    add_canonical_humanoid(eid, root)
                else:
                    parent_local(add_cylinder(eid + "__torso", (0, 0, 1.0), 0.34, 1.55, clay), root, (0, 0, 1.0))
                    parent_local(add_sphere(eid + "__head", (0, 0, 2.15), (0.38, 0.38, 0.38), light), root, (0, 0, 2.15))
                    for side, x in (("left_arm", -0.48), ("right_arm", 0.48)):
                        arm = parent_local(add_cylinder(eid + "__" + side, (0, 0, 1.35), 0.11, 0.9, clay), root, (x, 0, 1.35))
                        arm.rotation_euler[1] = 0.35 if x < 0 else -0.35
                        arms[(eid, side)] = arm
                    parent_local(add_cylinder(eid + "__leg_l", (-0.18, 0, 0.25), 0.12, 0.7, dark), root, (-0.18, 0, 0.25))
                    parent_local(add_cylinder(eid + "__leg_r", (0.18, 0, 0.25), 0.12, 0.7, dark), root, (0.18, 0, 0.25))
            elif eid in {"backpack"}:
                parent_local(add_cube(eid + "__body", (0, 0.56, 0.66), (0.24, 0.14, 0.32), dark), root, (0, 0.56, 0.66))
                parent_local(add_cube(eid + "__strap_l", (-0.14, 0.38, 0.78), (0.03, 0.04, 0.30), light, 0.02), root, (-0.14, 0.38, 0.78))
                parent_local(add_cube(eid + "__strap_r", (0.14, 0.38, 0.78), (0.03, 0.04, 0.30), light, 0.02), root, (0.14, 0.38, 0.78))
                # A front-side accent is intentionally redundant with the
                # neutral body: it keeps the coupled prop identifiable in a
                # low-resolution proxy without changing its trajectory.
                parent_local(add_cube(eid + "__visible_pack", (0, 0.12, 0.72), (0.30, 0.12, 0.38), backpack_accent, 0.08), root, (0, 0.12, 0.72))
                parent_local(add_cube(eid + "__visible_flap", (0, -0.02, 0.98), (0.20, 0.04, 0.06), light, 0.03), root, (0, -0.02, 0.98))
            elif eid in {"speaker"}:
                parent_local(add_cube(eid + "__body", (0, 0, 0.28), (0.36, 0.25, 0.26), dark), root, (0, 0, 0.28))
                parent_local(add_cube(eid + "__front", (0, -0.27, 0.28), (0.20, 0.03, 0.16), light, 0.02), root, (0, -0.27, 0.28))
                parent_local(add_cube(eid + "__base", (0, 0, 0.035), (0.42, 0.30, 0.035), dark, 0.02), root, (0, 0, 0.035))
            elif eid in {"bench"}:
                parent_local(add_cube(eid + "__seat", (0, 0, 0.45), (1.3, 0.38, 0.12), light), root, (0, 0, 0.45))
                parent_local(add_cube(eid + "__back", (0, 0.3, 1.0), (1.3, 0.10, 0.55), light), root, (0, 0.3, 1.0))
            elif eid in {"racket_a", "racket_b"}:
                parent_local(add_cylinder(eid + "__handle", (0, 0, 0.45), 0.07, 0.9, dark), root, (0, 0, 0.45))
                parent_local(add_cylinder(eid + "__head", (0, 0, 1.0), 0.34, 0.08, light), root, (0, 0, 1.0))
            elif eid == "shuttlecock":
                parent_local(add_sphere(eid + "__body", (0, 0, 0), (0.12, 0.12, 0.20), light), root, (0, 0, 0))
            elif eid == "net":
                parent_local(add_cube(eid + "__top", (0, 0, 1.05), (2.6, 0.04, 0.04), light, 0.01), root, (0, 0, 1.05))
                parent_local(add_cylinder(eid + "__post_l", (-2.55, 0, 0.55), 0.04, 1.1, light), root, (-2.55, 0, 0.55))
                parent_local(add_cylinder(eid + "__post_r", (2.55, 0, 0.55), 0.04, 1.1, light), root, (2.55, 0, 0.55))
            elif eid == "handcart":
                parent_local(add_cube(eid + "__base", (0, 0, 0.38), (0.9, 0.55, 0.14), dark), root, (0, 0, 0.38))
                # A horizontal bar is a visible grip.  The previous vertical
                # post could not be contacted by the customer's hand and was
                # read as a floating pole in lateral/reverse views.
                parent_local(add_cube(eid + "__handle", (-0.35, -0.55, 1.0), (0.75, 0.08, 0.08), light), root, (-0.35, -0.55, 1.0))
                for wheel_x in (-0.65, 0.65):
                    wheel = parent_local(add_cylinder(eid + "__wheel" + str(wheel_x), (wheel_x, 0, 0.30), 0.30, 0.12, light), root, (wheel_x, 0, 0.30))
                    # Blender cylinders are born with their axis on Z.  A
                    # cart wheel must stand on the floor and roll around X;
                    # the old horizontal discs made the cart read as floating.
                    wheel.rotation_euler[0] = math.radians(90.0)
            elif eid.startswith("box_"):
                if eid == "box_b":
                    parent_local(add_cube(eid + "__body", (0, 0, 0.30), (0.28, 0.28, 0.30), dark), root, (0, 0, 0.30))
                else:
                    parent_local(add_cube(eid + "__body", (0, 0, 0.38), (0.38, 0.38, 0.38), light), root, (0, 0, 0.38))
            elif eid.startswith("paper_"):
                slip_scale = (0.62, 0.42, 0.006) if scene_plan["scene_id"] == "warehouse_loading_maneuver" else (0.24, 0.18, 0.015)
                parent_local(add_cube(eid + "__sheet", (0, 0, 0), slip_scale, light, 0.01), root, (0, 0, 0))
            elif eid == "counter":
                if scene_plan["scene_id"] in {"indoor_market_exchange", "warehouse_loading_maneuver"}:
                    # A solid cube was read as a character torso in VLM
                    # frames.  Use a visibly open table silhouette instead.
                    parent_local(add_cube(eid + "__top", (0, 0, 1.25), (1.05, 0.48, 0.12), dark, 0.05), root, (0, 0, 1.25))
                    for leg_x in (-0.86, 0.86):
                        for leg_y in (-0.34, 0.34):
                            parent_local(add_cube(eid + "__leg" + str(leg_x) + str(leg_y), (leg_x, leg_y, 0.58), (0.10, 0.10, 0.58), dark, 0.03), root, (leg_x, leg_y, 0.58))
                else:
                    counter_scale = (1.4, 0.65, 1.0)
                    counter_height = float(counter_scale[2])
                    parent_local(add_cube(eid + "__body", (0, 0, counter_height), counter_scale, dark), root, (0, 0, counter_height))
            else:
                parent_local(add_cube(eid + "__body", (0, 0, 0.45), (0.45, 0.45, 0.45), light), root, (0, 0, 0.45))

            points = tracks.get(eid)
            if points is None:
                raise RuntimeError("missing authored track for " + eid)
            asset_log.append({"asset_id": eid, "kind": kind, "source_kind": (registry_asset or {}).get("source_kind", "unknown"), "catalog_asset_id": (registry_asset or {}).get("catalog_asset_id"), "source_asset_sha256": (registry_asset or {}).get("source_asset_sha256"), "rig_map_sha256": (registry_asset or {}).get("rig_map_sha256"), "parts": len([obj for obj in bpy.context.scene.objects if obj.name.startswith(eid + "__")]), "shared_world_instance": True})
            for frame in range(frame_count):
                pos, rot = interp(points, frame)
                root.rotation_mode = "XYZ"
                if eid in coupling_rules:
                    parent_points = tracks[coupling_rules[eid]]
                    parent_pos, parent_rot = interp(parent_points, frame)
                    parent_matrix = mathutils.Matrix.Translation(Vector(parent_pos)) @ mathutils.Euler(parent_rot, "XYZ").to_matrix().to_4x4()
                    world_matrix = mathutils.Matrix.Translation(Vector(pos)) @ mathutils.Euler(rot, "XYZ").to_matrix().to_4x4()
                    local_matrix = parent_matrix.inverted() @ world_matrix
                    root.location = local_matrix.translation
                    root.rotation_euler = local_matrix.to_euler("XYZ")
                else:
                    root.location = pos
                    root.rotation_euler = rot
                root.keyframe_insert(data_path="location", frame=frame)
                root.keyframe_insert(data_path="rotation_euler", frame=frame)

        # Bind the catalog customer's right hand to the moving handcart with
        # a real two-bone IK constraint.  A static authored joint-angle track
        # can look numerically correct while leaving the hand beside the grip;
        # this shared target follows the cart root in every camera.
        push_ik_log = []
        if args.render_style == "asset_humanoid" and "customer" in rigged_armatures and "customer" in roots and "handcart" in roots:
            grip_target = bpy.data.objects.new("customer__push_grip_target", None)
            bpy.context.collection.objects.link(grip_target)
            grip_target.parent = roots["handcart"]
            grip_target.location = (-0.65, -0.55, 1.05)
            grip_target.empty_display_type = "SPHERE"
            grip_target.empty_display_size = 0.05
            grip_target.hide_render = True
            grip_pole = bpy.data.objects.new("customer__push_grip_pole", None)
            bpy.context.collection.objects.link(grip_pole)
            grip_pole.parent = roots["customer"]
            grip_pole.location = (0.0, 0.80, 1.70)
            grip_pole.empty_display_type = "CUBE"
            grip_pole.empty_display_size = 0.05
            grip_pole.hide_render = True
            customer_armature = rigged_armatures["customer"]
            customer_map = rigged_bone_maps.get("customer", {})
            lowerarm_name = customer_map.get("lower_arm.R") or customer_map.get("forearm.R")
            if not lowerarm_name:
                raise RuntimeError("customer push IK mapping has no lower_arm.R/forearm.R")
            lowerarm = customer_armature.pose.bones.get(lowerarm_name)
            if lowerarm is None:
                raise RuntimeError("customer push IK target bone is missing: " + str(lowerarm_name))
            constraint = lowerarm.constraints.new("IK")
            constraint.name = "customer__push_grip_ik"
            constraint.target = grip_target
            constraint.pole_target = grip_pole
            constraint.chain_count = 2
            constraint.use_stretch = False
            # Let the authored pause/raise gesture interrupt the push only in
            # its explicit event window; contact is restored before and after.
            for frame, influence in ((0, 1.0), (30, 1.0), (36, 0.0), (84, 0.0), (96, 1.0), (119, 1.0)):
                constraint.influence = influence
                constraint.keyframe_insert(data_path="influence", frame=frame)
            push_ik_log.append({"target_id": "customer", "limb": "right_arm", "target": grip_target.name, "pole": grip_pole.name, "bone": lowerarm_name, "chain_count": 2, "parent_entity": "handcart", "shared_world": True})

        # Optional real-motion branch.  The BVH armatures are hidden source
        # rigs; only their local rotations are retargeted onto the lead
        # CesiumMan armature.  Root translation remains exclusively authored
        # by WorldState so all four cameras observe the same physical path.
        bvh_target_ids = set()
        bvh_pose_stats = {}
        if bvh_sources and "person_a" in rigged_armatures:
            bvh_target_ids.add("person_a")
            bvh_bone_map = {
                "Hips": "Skeleton_torso_joint_1",
                "ToSpine": "Skeleton_torso_joint_2",
                "Spine": "Skeleton_torso_joint_3",
                "Spine1": "torso_joint_3",
                "Neck": "Skeleton_neck_joint_1",
                "Head": "Skeleton_neck_joint_2",
                "LeftArm": "Skeleton_arm_joint_L__4_",
                "LeftForeArm": "Skeleton_arm_joint_L__3_",
                "LeftHand": "Skeleton_arm_joint_L__2_",
                "RightArm": "Skeleton_arm_joint_R",
                "RightForeArm": "Skeleton_arm_joint_R__2_",
                "RightHand": "Skeleton_arm_joint_R__3_",
                "LeftUpLeg": "leg_joint_L_1",
                "LeftLeg": "leg_joint_L_2",
                "LeftFoot": "leg_joint_L_3",
                "LeftToeBase": "leg_joint_L_5",
                "RightUpLeg": "leg_joint_R_1",
                "RightLeg": "leg_joint_R_2",
                "RightFoot": "leg_joint_R_3",
                "RightToeBase": "leg_joint_R_5",
            }
            target_rig = rigged_armatures["person_a"]
            target_by_name = {bone.name: bone for bone in target_rig.pose.bones}
            target_rig.animation_data.action = bpy.data.actions.new("person_a__bvh_retarget")
            bvh_pose_stats = {"left_arm": [], "right_arm": [], "left_leg": [], "right_leg": []}
            bvh_mapped_bones = set()

            def _source_quaternion(source_bone):
                # Blender's BVH importer normally keeps channel rotations in
                # Euler mode.  Reading rotation_quaternion in that mode returns
                # the identity and silently discards the real motion.
                if source_bone.rotation_mode == "QUATERNION":
                    return source_bone.rotation_quaternion.copy()
                if source_bone.rotation_mode == "AXIS_ANGLE":
                    return source_bone.rotation_axis_angle.to_quaternion()
                return source_bone.rotation_euler.to_quaternion()

            def _source_frame(action, output_frame, clip_index):
                start, end = float(action.frame_range[0]), float(action.frame_range[1])
                if len(bvh_sources) > 1:
                    half = max(1, frame_count // len(bvh_sources))
                    clip_index = min(len(bvh_sources) - 1, int(output_frame) // half)
                    local_frame = int(output_frame) - clip_index * half
                    local_count = half if clip_index < len(bvh_sources) - 1 else max(1, frame_count - clip_index * half)
                    ratio = min(1.0, max(0.0, float(local_frame) / float(max(1, local_count - 1))))
                else:
                    ratio = min(1.0, max(0.0, float(output_frame) / float(max(1, frame_count - 1))))
                return start + ratio * (end - start), clip_index

            for output_frame in range(frame_count):
                src_action = bvh_sources[0].animation_data.action
                src_frame, clip_index = _source_frame(src_action, output_frame, 0)
                if len(bvh_sources) > 1:
                    src_action = bvh_sources[clip_index].animation_data.action
                    src_frame, _ = _source_frame(src_action, output_frame, clip_index)
                scene.frame_set(int(round(src_frame)))
                source_rig = bvh_sources[clip_index]
                source_by_name = {bone.name: bone for bone in source_rig.pose.bones}
                for source_name, target_name in bvh_bone_map.items():
                    # retarget limbs only: ACCAD's side-step pelvis/spine lean
                    # is held at the authored upright staging pose.
                    if source_name in {"Hips", "ToSpine", "Spine", "Spine1", "Neck", "Head"}:
                        continue
                    source_bone = source_by_name.get(source_name)
                    target_bone = target_by_name.get(target_name)
                    if source_bone is None or target_bone is None:
                        continue
                    bvh_mapped_bones.add(source_name)
                    target_bone.rotation_mode = "QUATERNION"
                    target_bone.rotation_quaternion = _source_quaternion(source_bone)
                    target_bone.keyframe_insert(data_path="rotation_quaternion", frame=output_frame)
                # Keep the real motion as the base layer, then add the
                # authored wave beats as a small local overlay so the semantic
                # event remains visible without replacing mocap.
                for gesture in gesture_tracks:
                    if str(gesture.get("target_id")) != "person_a":
                        continue
                    points = gesture.get("points", [])
                    if not points:
                        continue
                    if output_frame <= int(points[0][0]):
                        overlay_angle = float(points[0][1])
                    elif output_frame >= int(points[-1][0]):
                        overlay_angle = float(points[-1][1])
                    else:
                        overlay_angle = float(points[-1][1])
                        for left_point, right_point in zip(points, points[1:]):
                            if int(left_point[0]) <= output_frame <= int(right_point[0]):
                                ratio = float(output_frame - int(left_point[0])) / float(max(1, int(right_point[0]) - int(left_point[0])))
                                overlay_angle = float(left_point[1]) * (1.0 - ratio) + float(right_point[1]) * ratio
                                break
                    overlay_angle = safe_arm_angle(str(gesture.get("limb")), overlay_angle)
                    overlay_names = {"left_arm": ("Skeleton_arm_joint_L__4_", "Skeleton_arm_joint_L__3_"), "right_arm": ("Skeleton_arm_joint_R", "Skeleton_arm_joint_R__2_")}.get(str(gesture.get("limb")), ())
                    for bone_index, bone_name in enumerate(overlay_names):
                        target_bone = target_by_name.get(bone_name)
                        if target_bone is None:
                            continue
                        arm_sign = -1.0 if str(gesture.get("limb")) == "left_arm" else 1.0
                        overlay = mathutils.Euler((float(overlay_angle) * arm_sign * (1.0 if bone_index == 0 else 0.65), 0.0, 0.0), "XYZ").to_quaternion()
                        target_bone.rotation_quaternion = target_bone.rotation_quaternion @ overlay
                        target_bone.keyframe_insert(data_path="rotation_quaternion", frame=output_frame)
                # A bounded authored leg overlay makes the two semantic
                # side-step phases visible even when the real clip's local
                # leg angles are small after retargeting.  It is additive to,
                # and logged alongside, the real BVH motion.
                step_points = ((0, 0.0), (30, 0.82), (60, -0.82), (90, 0.82), (119, 0.0))
                if output_frame <= step_points[0][0]:
                    step_angle = step_points[0][1]
                elif output_frame >= step_points[-1][0]:
                    step_angle = step_points[-1][1]
                else:
                    step_angle = step_points[-1][1]
                    for left_point, right_point in zip(step_points, step_points[1:]):
                        if left_point[0] <= output_frame <= right_point[0]:
                            ratio = float(output_frame - left_point[0]) / float(max(1, right_point[0] - left_point[0]))
                            step_angle = left_point[1] * (1.0 - ratio) + right_point[1] * ratio
                            break
                for side, sign in (("L", 1.0), ("R", -1.0)):
                    for bone_name, factor in ((f"leg_joint_{side}_1", 1.0), (f"leg_joint_{side}_2", -0.70), (f"leg_joint_{side}_3", 0.25)):
                        target_bone = target_by_name.get(bone_name)
                        if target_bone is None:
                            continue
                        target_bone.rotation_mode = "QUATERNION"
                        overlay = mathutils.Euler((float(step_angle) * sign * factor, 0.0, 0.0), "XYZ").to_quaternion()
                        target_bone.rotation_quaternion = target_bone.rotation_quaternion @ overlay
                        target_bone.keyframe_insert(data_path="rotation_quaternion", frame=output_frame)
                left = target_by_name.get(bvh_bone_map["LeftArm"])
                right = target_by_name.get(bvh_bone_map["RightArm"])
                if left is not None:
                    bvh_pose_stats["left_arm"].append(float(left.rotation_quaternion.to_euler()[0]))
                if right is not None:
                    bvh_pose_stats["right_arm"].append(float(right.rotation_quaternion.to_euler()[0]))
                left_leg = target_by_name.get(bvh_bone_map["LeftUpLeg"])
                right_leg = target_by_name.get(bvh_bone_map["RightUpLeg"])
                if left_leg is not None:
                    bvh_pose_stats["left_leg"].append(float(left_leg.rotation_quaternion.to_euler()[0]))
                if right_leg is not None:
                    bvh_pose_stats["right_leg"].append(float(right_leg.rotation_quaternion.to_euler()[0]))
            if not {"LeftUpLeg", "RightUpLeg"}.issubset(bvh_mapped_bones):
                raise RuntimeError("BVH retarget mapped no CesiumMan leg bones")
            scene.frame_set(0)

        # Rigged catalog assets often import in a T-pose.  Materialize a
        # deterministic arms-down rest pose for every frame before applying
        # authored gesture deltas; otherwise characters without a left-arm
        # gesture remain in T-pose and the action is not visually readable.
        for target_id, rigged in rigged_armatures.items():
            bone_map = rigged_bone_maps.get(str(target_id), {})
            arm_bones = {
                "left_arm": (bone_map.get("upper_arm.L"), bone_map.get("forearm.L")),
                "right_arm": (bone_map.get("upper_arm.R"), bone_map.get("forearm.R")),
            }
            for limb, (upper_name, forearm_name) in arm_bones.items():
                upper = rigged.pose.bones.get(upper_name) if upper_name else None
                forearm = rigged.pose.bones.get(forearm_name) if forearm_name else None
                if upper is None or forearm is None:
                    raise RuntimeError(f"rigged asset rest pose mapping did not resolve {target_id}:{limb}")
                base_angle = -0.85 if limb == "left_arm" else 0.85
                for frame in range(frame_count):
                    upper.rotation_mode = "XYZ"
                    forearm.rotation_mode = "XYZ"
                    upper.rotation_euler = (0.0, 0.0, base_angle)
                    forearm.rotation_euler = (0.0, 0.0, 0.0)
                    upper.keyframe_insert(data_path="rotation_euler", frame=frame)
                    forearm.keyframe_insert(data_path="rotation_euler", frame=frame)

        arm_pose_log = []
        for gesture in gesture_tracks:
            arm = arms.get((gesture["target_id"], gesture["limb"]))
            rigged = rigged_armatures.get(gesture["target_id"])
            if arm is None and rigged is None:
                continue
            if str(gesture["target_id"]) in bvh_target_ids:
                key = str(gesture["limb"])
                angles = list(bvh_pose_stats.get(key, [])) or [0.0]
                shoulder_x = 0.44 if key == "left_arm" else -0.44
                clearances = [abs(shoulder_x - math.sin(angle) * 0.29) for angle in angles]
                arm_pose_log.append({"target_id": gesture["target_id"], "limb": gesture["limb"], "min_angle": min(angles), "max_angle": max(angles), "min_elbow_clearance": min(clearances), "asset_kind": "bvh_retarget"})
                continue
            points = gesture["points"]
            applied_angles = []
            for frame in range(frame_count):
                if frame <= points[0][0]:
                    angle = points[0][1]
                elif frame >= points[-1][0]:
                    angle = points[-1][1]
                else:
                    angle = points[-1][1]
                    for left, right in zip(points, points[1:]):
                        if left[0] <= frame <= right[0]:
                            t = float(frame-left[0]) / float(right[0]-left[0])
                            angle = left[1] * (1-t) + right[1] * t
                            break
                # Rotate around the authored upper-arm center without lifting
                # that center into the head.  The inward clamp prevents the
                # elbow/forearm chain from crossing the head corridor.
                angle = safe_arm_angle(gesture["limb"], angle)
                if arm is not None:
                    arm.rotation_euler[1] = float(angle)
                    arm.location.z = 2.02
                    arm.keyframe_insert(data_path="rotation_euler", frame=frame)
                    arm.keyframe_insert(data_path="location", frame=frame)
                else:
                    bone_map = rigged_bone_maps.get(str(gesture["target_id"]))
                    rigged_bone_names = {
                        "left_arm": ("Skeleton_arm_joint_L__4_", "Skeleton_arm_joint_L__3_"),
                        "right_arm": ("Skeleton_arm_joint_R", "Skeleton_arm_joint_R__2_"),
                    }
                    if bone_map:
                        rigged_bone_names = {
                            "left_arm": (bone_map.get("upper_arm.L"), bone_map.get("forearm.L")),
                            "right_arm": (bone_map.get("upper_arm.R"), bone_map.get("forearm.R")),
                        }
                    pose_bones = [rigged.pose.bones.get(name) for name in rigged_bone_names.get(gesture["limb"], ())]
                    pose_bones = [pose_bone for pose_bone in pose_bones if pose_bone is not None]
                    if not pose_bones:
                        raise RuntimeError(f"rigged asset bone mapping did not resolve {gesture['target_id']}:{gesture['limb']}")
                    for bone_index, pose_bone in enumerate(pose_bones):
                        pose_bone.rotation_mode = "XYZ"
                        base_angle = -0.85 if gesture["limb"] == "left_arm" else 0.85
                        direction = 1.0 if gesture["limb"] == "left_arm" else -1.0
                        pose_angle = base_angle + direction * float(angle) if bone_index == 0 else direction * float(angle) * 0.35
                        pose_bone.rotation_euler[2] = pose_angle
                        pose_bone.keyframe_insert(data_path="rotation_euler", frame=frame)
                applied_angles.append(float(angle))
            shoulder_x = 0.44 if gesture["limb"] == "left_arm" else -0.44
            clearances = [abs(shoulder_x - math.sin(angle) * 0.29) for angle in applied_angles]
            arm_pose_log.append({"target_id": gesture["target_id"], "limb": gesture["limb"], "min_angle": min(applied_angles), "max_angle": max(applied_angles), "min_elbow_clearance": min(clearances), "asset_kind": "procedural" if arm is not None else "rigged_glb"})

        # StoryBlender-style animation layer: keep the authored root path and
        # add a small deterministic gait/weight-shift pass.  Contacts are
        # logged for auditing; they are not presented as an IK solution.
        motion_log = []
        for motion in canonical_motion_tracks:
            target_id = str(motion.get("target_id"))
            if target_id in bvh_target_ids:
                pose_stats = {}
                for limb_name in ("left_arm", "right_arm", "left_leg", "right_leg"):
                    values = list(bvh_pose_stats.get(limb_name, [])) or [0.0]
                    pose_stats[limb_name] = {"min_angle": min(values), "max_angle": max(values), "sample_count": len(values)}
                motion_log.append({"target_id": target_id, "motion_mode": "bvh_retarget+authored_side_step", "foot_contacts": motion.get("foot_contacts", []), "asset_kind": "bvh_retarget", "source_count": len(bvh_sources), "mapped_bones": sorted(bvh_mapped_bones), "pose_stats": pose_stats, "overlay": "bounded_authored_side_step"})
                continue
            for limb_name, samples in motion.get("limb_tracks", {}).items():
                parts = legs.get((target_id, limb_name))
                rigged = rigged_armatures.get(target_id)
                if (not parts and rigged is None) or not samples:
                    continue
                for frame in range(frame_count):
                    if frame <= int(samples[0]["frame"]):
                        angle = float(samples[0]["angle"])
                    elif frame >= int(samples[-1]["frame"]):
                        angle = float(samples[-1]["angle"])
                    else:
                        angle = float(samples[-1]["angle"])
                        for left_sample, right_sample in zip(samples, samples[1:]):
                            left_frame = int(left_sample["frame"])
                            right_frame = int(right_sample["frame"])
                            if left_frame <= frame <= right_frame:
                                ratio = float(frame - left_frame) / float(right_frame - left_frame)
                                angle = float(left_sample["angle"]) * (1.0 - ratio) + float(right_sample["angle"]) * ratio
                                break
                    if parts:
                        parts["upper"].rotation_euler[1] = angle
                        parts["lower"].rotation_euler[1] = -0.55 * angle
                        parts["foot"].rotation_euler[1] = 0.20 * angle
                        for part in parts.values():
                            part.keyframe_insert(data_path="rotation_euler", frame=frame)
                    else:
                        bone_map = rigged_bone_maps.get(target_id)
                        rigged_bone_names = {
                            "left_leg": "leg_joint_L_1",
                            "right_leg": "leg_joint_R_1",
                        }
                        if bone_map:
                            rigged_bone_names = {
                                "left_leg": bone_map.get("upper_leg.L"),
                                "right_leg": bone_map.get("upper_leg.R"),
                            }
                        pose_bone = rigged.pose.bones.get(rigged_bone_names.get(limb_name, ""))
                        if pose_bone is not None:
                            pose_bone.rotation_mode = "XYZ"
                            pose_bone.rotation_euler[0] = float(angle)
                            pose_bone.keyframe_insert(data_path="rotation_euler", frame=frame)
            motion_log.append({"target_id": target_id, "motion_mode": motion.get("motion_mode"), "foot_contacts": motion.get("foot_contacts", []), "asset_kind": "procedural" if legs.get((target_id, "left_leg")) else "rigged_glb"})

        skeleton_pose_log = []
        if args.render_style == "skeleton":
            def sidecar_point(profile, bone_name, frame):
                rows = profile.get("bone_tracks", {}).get(bone_name, [])
                if not rows:
                    return [0.0, 0.0, 0.0]
                if frame <= int(rows[0]["frame"]):
                    return list(rows[0]["position"])
                if frame >= int(rows[-1]["frame"]):
                    return list(rows[-1]["position"])
                for left_row, right_row in zip(rows, rows[1:]):
                    left_frame, right_frame = int(left_row["frame"]), int(right_row["frame"])
                    if left_frame <= frame <= right_frame:
                        ratio = float(frame - left_frame) / float(right_frame - left_frame)
                        return [float(left_row["position"][axis]) * (1.0 - ratio) + float(right_row["position"][axis]) * ratio for axis in range(3)]
                return list(rows[-1]["position"])

            for frame in range(frame_count):
                frame_characters = {}
                for eid, parts in skeleton_parts.items():
                    profile = skeleton_motion_by_id.get(eid, {})
                    root_pos, _ = interp(tracks[eid], frame)
                    def local_point(bone_name):
                        world_point = sidecar_point(profile, bone_name, frame)
                        return Vector((world_point[0] - root_pos[0], world_point[1] - root_pos[1], world_point[2] - root_pos[2]))

                    rig_targets = parts["rig"]["targets"]
                    local_bones = {}
                    for side, sign in (("L", 1.0), ("R", -1.0)):
                        shoulder = Vector((sign * 0.44, 0.0, 2.15))
                        hand_target = local_point("hand." + side)
                        shoulder_point, elbow_point, hand_point, hand_reachable = solve_two_link(shoulder, hand_target, 0.58, 0.52, sign)
                        orient_segment(parts["arms"][side]["upper"], shoulder_point, elbow_point)
                        orient_segment(parts["arms"][side]["lower"], elbow_point, hand_point)
                        parts["hands"][side].location = hand_point
                        rig_targets["hand_target." + side].location = hand_point
                        rig_targets["pole." + side].location = Vector((sign * 0.75, sign * 0.25, 1.65))
                        for obj in (parts["arms"][side]["upper"], parts["arms"][side]["lower"], parts["hands"][side], rig_targets["hand_target." + side], rig_targets["pole." + side]):
                            obj.keyframe_insert(data_path="location", frame=frame)
                            obj.keyframe_insert(data_path="rotation_euler", frame=frame)
                        hip = Vector((sign * 0.18, 0.0, 1.25))
                        foot_target = local_point("foot." + side)
                        hip_point, knee_point, foot_point, foot_reachable = solve_two_link(hip, foot_target, 0.62, 0.55, -sign)
                        orient_segment(parts["legs"][side]["upper"], hip_point, knee_point)
                        orient_segment(parts["legs"][side]["lower"], knee_point, foot_point)
                        parts["feet"][side].location = foot_point
                        rig_targets["foot_target." + side].location = foot_point
                        for obj in (parts["legs"][side]["upper"], parts["legs"][side]["lower"], parts["feet"][side], rig_targets["foot_target." + side]):
                            obj.keyframe_insert(data_path="location", frame=frame)
                            obj.keyframe_insert(data_path="rotation_euler", frame=frame)
                        local_bones["hand." + side] = {"position": [float(root_pos[axis] + hand_point[axis]) for axis in range(3)], "reachable": bool(hand_reachable)}
                        local_bones["foot." + side] = {"position": [float(root_pos[axis] + foot_point[axis]) for axis in range(3)], "reachable": bool(foot_reachable)}
                    for bone_name in profile.get("bones", []):
                        if bone_name in local_bones:
                            continue
                        world_point = sidecar_point(profile, bone_name, frame)
                        local_bones[bone_name] = {"position": [float(value) for value in world_point], "reachable": True}
                    frame_characters[eid] = {"bones": local_bones, "foot_contacts": profile.get("foot_contacts", [])}
                skeleton_pose_log.append({"frame": frame, "characters": frame_characters})

        # Stable area lights make clay silhouettes readable in every view.
        for name, location, energy, size in (("key", (0, -3, 7), 1100, 5.0), ("fill", (-6, 1, 4), 650, 4.0), ("rim", (5, 4, 6), 800, 3.0)):
            data = bpy.data.lights.new(name, "AREA")
            data.energy, data.shape, data.size = energy, "DISK", size
            obj = bpy.data.objects.new(name, data)
            bpy.context.collection.objects.link(obj)
            obj.location = location
            obj.rotation_euler = (Vector((0, 0, 1.0)) - obj.location).to_track_quat("-Z", "Y").to_euler()

        def target_at(target_id, frame):
            pos, _ = interp(tracks[target_id], frame)
            height = 1.25 if scene_plan["entities"][[e["id"] for e in scene_plan["entities"]].index(target_id)]["kind"] == "character" else 0.65
            return Vector((pos[0], pos[1], pos[2] + height))

        camera_logs, videos = [], []
        for camera_plan in world_state["camera_trajectory_plan"]["cameras"]:
            cid = camera_plan["id"]
            data = bpy.data.cameras.new(cid + "_data")
            data.lens = float(camera_plan["lens_mm"])
            cam = bpy.data.objects.new(cid, data)
            bpy.context.collection.objects.link(cam)
            authored = [{"frame": int(row["frame"]), "position": list(row["position"]), "rotation": list(row["rotation"])} for row in camera_plan["points"]]
            applied = []
            for frame in range(frame_count):
                pos, _ = interp(camera_plan["points"], frame)
                cam.location = pos
                cam.rotation_euler = (target_at(camera_plan["target"]["object_id"], frame) - cam.location).to_track_quat("-Z", "Y").to_euler()
                applied.append({"frame": frame, "position": [float(x) for x in cam.location], "rotation": [float(x) for x in cam.rotation_euler]})
                cam.keyframe_insert(data_path="location", frame=frame)
                cam.keyframe_insert(data_path="rotation_euler", frame=frame)
            camera_logs.append({"camera_id": cid, "target_id": camera_plan["target"]["object_id"], "shared_asset_hashes": shared_asset_hashes, "authored": authored, "applied": applied})
            scene.camera = cam
            filename = cid + ".mp4"
            scene.render.filepath = str(out / filename)
            bpy.ops.render.render(animation=True)
            path = out / filename
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("missing rendered video: " + filename)
            videos.append({"path": filename, "camera_id": cid, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "frame_count": frame_count, "fps": fps, "resolution": [width, height]})

        state_log = []
        for frame in range(frame_count):
            entities = {}
            for entity in scene_plan["entities"]:
                pos, rot = interp(tracks[entity["id"]], frame)
                entities[entity["id"]] = {"position": pos, "rotation": rot}
            state_log.append({"frame": frame, "entities": entities})
        coupling_log = []
        for child_id, parent_id in coupling_rules.items():
            offsets = []
            for frame in range(frame_count):
                child_pos, child_rot = interp(tracks[child_id], frame)
                parent_pos, parent_rot = interp(tracks[parent_id], frame)
                parent_matrix = mathutils.Matrix.Translation(Vector(parent_pos)) @ mathutils.Euler(parent_rot, "XYZ").to_matrix().to_4x4()
                child_matrix = mathutils.Matrix.Translation(Vector(child_pos)) @ mathutils.Euler(child_rot, "XYZ").to_matrix().to_4x4()
                local_matrix = parent_matrix.inverted() @ child_matrix
                offsets.append({"frame": frame, "position": [float(value) for value in local_matrix.translation], "rotation": [float(value) for value in local_matrix.to_euler("XYZ")]})
            coupling_log.append({"child_id": child_id, "parent_id": parent_id, "mode": "root_parent_local_track", "frames": offsets})
        (out / "state_log.json").write_text(json.dumps(state_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "applied_state_log.json").write_text(json.dumps(state_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "coupling_log.json").write_text(json.dumps(coupling_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "camera_log.json").write_text(json.dumps(camera_logs, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "asset_log.json").write_text(json.dumps(asset_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "motion_log.json").write_text(json.dumps(motion_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "arm_pose_log.json").write_text(json.dumps(arm_pose_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "push_ik_log.json").write_text(json.dumps(push_ik_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "skeleton_pose_log.json").write_text(json.dumps(skeleton_pose_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "render_manifest.json").write_text(json.dumps({"schema_version": "pipeline-v2-render-manifest-1.0", "world_state_hash": world_hash, "proxy_style": args.render_style, "asset_registry": str(registry_path.name) if registry_path.is_file() else None, "couplings": coupling_rules, "frame_count": frame_count, "fps": fps, "resolution": [width, height], "motion_sources": bvh_metadata, "videos": videos}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        bpy.context.scene.camera = bpy.data.objects[world_state["camera_trajectory_plan"]["cameras"][0]["id"]]
        bpy.ops.wm.save_as_mainfile(filepath=str(out / "shared_world.blend"))
        print("PIPELINE_V2_BLENDER_OK")
        ''')


def _probe(path: Path) -> dict[str, object]:
    output = subprocess.check_output(
        [str(FFPROBE), "-v", "error", "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration", "-of", "json", str(path)],
        text=True,
    )
    document = json.loads(output)
    stream = document["streams"][0]
    return {"width": int(stream["width"]), "height": int(stream["height"]), "r_frame_rate": stream["avg_frame_rate"], "nb_frames": int(stream["nb_frames"]), "codec": stream["codec_name"], "duration": float(document["format"]["duration"])}


def _black_frames(path: Path) -> int:
    # 0.98 treats the intentionally dark neutral-clay world as black. Use a
    # near-zero luma threshold so only genuinely empty/black frames fail.
    command = [str(FFMPEG), "-hide_banner", "-i", str(path), "-vf", "blackdetect=d=0.1:pix_th=0.02", "-an", "-f", "null", "-"]
    completed = subprocess.run(command, capture_output=True, text=True)
    matches = re.findall(r"black_duration:([0-9.]+)", (completed.stdout or "") + (completed.stderr or ""))
    return len(matches)


def _temp_upload_config():
    config = load_upload_config()
    if config.has_tos_credentials or config.temp_upload_enabled:
        return config
    # The previous implicit fallback used tmpfiles, which returned a real
    # connection-reset failure on this host.  Keep TOS as the production path,
    # but use the already-tested Uguu research transport when no credentials
    # are configured; this does not change the Seedance request shape.
    return config.__class__(
        access_key=config.access_key, secret_key=config.secret_key, bucket=config.bucket,
        endpoint=config.endpoint, region=config.region, expires_seconds=config.expires_seconds,
        temp_upload_enabled=True, temp_upload_endpoint="https://uguu.se/upload.php",
        temp_upload_provider="uguu",
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_seedance_single(*, scene_output: Path, spec: Mapping[str, Any], model: str, poll_interval: float, max_polls: int, identity_anchor_camera_id: str | None = None, identity_anchor_path: Path | None = None) -> dict[str, Any]:
    """Submit one independent Seedance task per rendered camera.

    The old helper name is retained for compatibility, but this is no longer
    a single-master submission. Every manifest camera is normalized, uploaded,
    and sent in its own one-reference request; no camera is silently dropped.
    """
    job_dir = scene_output / "seedance"
    job_dir.mkdir(parents=True, exist_ok=False)
    key = os.environ.get("JD_KLING_KEY", "").strip()
    if not key:
        result = {"status": "blocked_preflight", "stage": "credentials", "api_calls": {"submit": 0, "query": 0, "download": 0}, "error": "JD_KLING_KEY is not configured"}
        _write_json(job_dir / "failure.json", result)
        return result
    config = _temp_upload_config()
    manifest = json.loads((scene_output / "sandbox" / "render_manifest.json").read_text(encoding="utf-8"))
    videos = list(manifest.get("videos", []))
    if not videos:
        result = {"status": "upload_failed", "stage": "preflight", "error": "render manifest has no camera videos", "api_calls": {"submit": 0, "query": 0, "download": 0}}
        _write_json(job_dir / "failure.json", result)
        return result
    normalized_dir = job_dir / "normalized"
    uploads_dir = job_dir / "uploads"
    normalized_dir.mkdir()
    uploads_dir.mkdir()
    assets = []
    identity_anchor = None
    try:
        for video in videos:
            camera_id = str(video["camera_id"])
            proxy = scene_output / "sandbox" / str(video["path"])
            normalized = normalize_proxy(proxy, normalized_dir / f"{camera_id}_seedance.mp4")
            asset = upload_proxy(normalized, run_id=f"{spec['scene_id']}_{camera_id}", config=config)
            assets.append(asset)
            write_upload_record(asset, uploads_dir / f"{camera_id}.json")
        if identity_anchor_path is not None:
            anchor_source = Path(identity_anchor_path).resolve(strict=True)
            anchor_media = normalize_proxy(anchor_source, normalized_dir / "identity_anchor_seedance.mp4")
            identity_anchor = upload_proxy(anchor_media, run_id=f"{spec['scene_id']}_identity_anchor", config=config)
            write_upload_record(identity_anchor, uploads_dir / "identity_anchor.json")
    except Exception as exc:
        result = {"status": "upload_failed", "stage": "upload", "error": str(exc), "api_calls": {"submit": 0, "query": 0, "download": 0}}
        _write_json(job_dir / "failure.json", result)
        return result
    prompt = spec["appearance_prompt"]
    camera_ids = [str(video["camera_id"]) for video in videos]
    camera_prompts = {camera_id: seedance_camera_prompt_for(spec, camera_id) for camera_id in camera_ids}
    if identity_anchor is None and identity_anchor_camera_id:
        identity_anchor = next((asset for asset in assets if str(asset.metadata.get("camera_id", "")) == identity_anchor_camera_id), None)
        # Upload records do not carry camera_id in all historical runs, so
        # resolve the anchor by the manifest order when needed.
        if identity_anchor is None:
            anchor_index = camera_ids.index(identity_anchor_camera_id) if identity_anchor_camera_id in camera_ids else -1
            if anchor_index >= 0 and anchor_index < len(assets):
                identity_anchor = assets[anchor_index]
    base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")
    result = run_uploaded_camera_jobs(
        job_id=spec["scene_id"],
        prompt=prompt,
        assets=assets,
        camera_ids=camera_ids,
        prompts=camera_prompts,
        identity_anchor=identity_anchor,
        output_root=job_dir / "camera_tasks",
        api_key=key,
        base_url=base_url,
        model=model,
        poll_interval_seconds=poll_interval,
        max_polls=max_polls,
    )
    result = {**result, "stage": "camera_tasks", "camera_count": len(assets), "upload_provider": config.temp_upload_provider if not config.has_tos_credentials else "tos"}
    _write_json(job_dir / "camera_submission_summary.json", result)
    return result


def real_t2v_prompt(spec: Mapping[str, Any]) -> str:
    """Compile a live-action T2V prompt while retaining the authored story."""
    return (
        "Generate one continuous five-second live-action cinematic video with natural human actors and "
        "believable real-world props. No cuts, no storyboard panels, no clay, no low-poly geometry, no "
        "extra people, no duplicated objects. Preserve the following authored story, action order, object "
        "coupling, and camera responsibilities exactly: " + str(spec["prompt"])
    )


def run_seedance_t2v_single(*, scene_output: Path, spec: Mapping[str, Any], model: str, poll_interval: float, max_polls: int) -> dict[str, Any]:
    """Run the pure T2V baseline; the Proxy remains planning evidence only."""
    job_dir = scene_output / "seedance_t2v"
    job_dir.mkdir(parents=True, exist_ok=False)
    key = os.environ.get("JD_KLING_KEY", "").strip()
    calls = {"submit": 0, "query": 0, "download": 0}
    if not key:
        result = {"status": "blocked_preflight", "stage": "credentials", "api_calls": calls, "error": "JD_KLING_KEY is not configured"}
        _write_json(job_dir / "failure.json", result)
        return result
    prompt = real_t2v_prompt(spec)
    request = build_seedance_t2v(prompt, duration=5)
    proxy = scene_output / "sandbox" / "master.mp4"
    _write_json(job_dir / "prepared_job.json", {
        "job_id": spec["scene_id"],
        "mode": "t2v",
        "model": model,
        "prompt": prompt,
        "request": request,
        "proxy_sha256": sha256_file(proxy) if proxy.is_file() else None,
        "proxy_role": "planning_evidence_only; not passed to T2V endpoint",
        "api_calls": calls,
    })
    run = RunDirectory(job_dir)
    base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")
    try:
        task_id = submit_once(request, key, base_url, run)
        calls["submit"] = 1
    except Exception as exc:
        result = {"status": "submit_failed", "stage": "submit", "error": str(exc), "api_calls": {**calls, "submit": 1}}
        _write_json(job_dir / "failure.json", result)
        return result
    final_status = "unknown"
    last_response = None
    for index in range(max_polls):
        calls["query"] += 1
        last_response = query_once(key, run)
        status = str(extract_status(last_response) or "unknown").lower()
        if status in SUCCESS_STATUSES:
            final_status = "succeeded"
            break
        if status in FAILURE_STATUSES:
            final_status = "task_failed"
            break
        if index + 1 < max_polls:
            time.sleep(poll_interval)
    if final_status == "succeeded":
        try:
            result_path = download_once(run)
            calls["download"] = 1
            probe = _probe(result_path)
            black = _black_frames(result_path)
            result = {"status": "succeeded", "stage": "download", "task_id": task_id, "result_path": str(result_path), "probe": probe, "blackdetect_events": black, "api_calls": calls}
            if probe["nb_frames"] < 115 or probe["duration"] < 4.8 or black != 0:
                result["status"] = "media_failed"
            _write_json(job_dir / "result_summary.json", result)
            return result
        except Exception as exc:
            result = {"status": "download_failed", "stage": "download", "task_id": task_id, "error": str(exc), "api_calls": calls}
            _write_json(job_dir / "failure.json", result)
            return result
    result = {"status": final_status, "stage": "query", "task_id": task_id, "last_response": last_response, "api_calls": calls}
    _write_json(job_dir / "failure.json", result)
    return result


def _extract_sheet(video: Path, output: Path) -> str | None:
    if not FFMPEG.is_file():
        return None
    sheet = output.with_suffix(".sheet.jpg")
    command = [str(FFMPEG), "-y", "-v", "error", "-i", str(video), "-vf", "select='eq(n,0)+eq(n,30)+eq(n,60)+eq(n,90)+eq(n,119)',scale=240:-1,tile=5x1", "-frames:v", "1", str(sheet)]
    completed = subprocess.run(command, capture_output=True, text=True)
    return str(sheet) if completed.returncode == 0 and sheet.is_file() else None


def _extract_review_frames(video: Path, output_dir: Path) -> list[str]:
    """Extract real K0-K4 JPEGs so VLM sees independent frames, not only a tile."""
    if not FFMPEG.is_file():
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "frame_%02d.jpg"
    command = [str(FFMPEG), "-y", "-v", "error", "-i", str(video), "-vf", "select='eq(n,0)+eq(n,30)+eq(n,60)+eq(n,90)+eq(n,119)'", "-vsync", "vfr", "-q:v", "2", str(pattern)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        return []
    return [str(path) for path in sorted(output_dir.glob("frame_*.jpg")) if path.is_file() and path.stat().st_size > 0]


def verify_asset_materialization(*, registry_path: Path, asset_log_path: Path, proxy_style: str) -> dict[str, Any]:
    """Check that canonical assets were materialized once in the real render."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    asset_log = json.loads(asset_log_path.read_text(encoding="utf-8"))
    expected = {str(item["asset_id"]): item for item in registry.get("assets", [])}
    actual = {str(item["asset_id"]): item for item in asset_log}
    missing = sorted(set(expected) - set(actual))
    duplicate_ids = len(asset_log) != len(actual)
    canonical_failures = []
    if proxy_style in {"canonical", "skeleton", "storyhuman", "asset_humanoid"}:
        for asset_id, item in expected.items():
            if item.get("kind") == "character":
                observed = actual.get(asset_id, {})
                required_parts = 1 if item.get("source_kind") in {"canonical_glb", "asset_catalog_glb"} else 12
                source_ok = observed.get("source_kind") == item.get("source_kind")
                catalog_hash_ok = True
                if item.get("source_kind") == "asset_catalog_glb":
                    catalog_hash_ok = observed.get("source_asset_sha256") == item.get("source_asset_sha256") and bool(observed.get("rig_map_sha256"))
                if not source_ok or not catalog_hash_ok or int(observed.get("parts", 0)) < required_parts:
                    canonical_failures.append(asset_id)
    passed = not missing and not duplicate_ids and not canonical_failures
    return {
        "check_id": "asset.canonical_materialization",
        "category": "scene_structure",
        "status": "passed" if passed else "failed",
        "message": "canonical assets are present once in the shared world" if passed else "canonical asset materialization is incomplete",
        "evidence": {"expected_assets": sorted(expected), "actual_assets": sorted(actual), "missing": missing, "duplicate_ids": duplicate_ids, "canonical_failures": canonical_failures, "proxy_style": proxy_style},
    }


def verify_coupling_materialization(*, spec: Mapping[str, Any], coupling_log_path: Path, frame_count: int) -> dict[str, Any]:
    """Verify that authored parent relations were materialized for every frame."""
    expected = coupling_rules_for(spec)
    if not expected:
        return {"check_id": "object.coupling_materialization", "category": "object_trajectory", "status": "skipped", "message": "scene has no authored parent coupling rule", "evidence": {}}
    if not coupling_log_path.is_file() or coupling_log_path.stat().st_size == 0:
        return {"check_id": "object.coupling_materialization", "category": "object_trajectory", "status": "failed", "message": "coupling log is missing", "evidence": {"expected": expected}}
    rows = json.loads(coupling_log_path.read_text(encoding="utf-8"))
    actual = {str(row.get("child_id")): str(row.get("parent_id")) for row in rows if isinstance(row, Mapping)}
    malformed = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("frames"), list) or len(row["frames"]) != frame_count:
            malformed.append(row.get("child_id") if isinstance(row, Mapping) else "non_object")
    passed = actual == expected and not malformed
    return {"check_id": "object.coupling_materialization", "category": "object_trajectory", "status": "passed" if passed else "failed", "message": "authored parent coupling is materialized across the full timeline" if passed else "authored parent coupling is incomplete", "evidence": {"expected": expected, "actual": actual, "malformed": malformed, "frame_count": frame_count}}


def verify_motion_materialization(*, motion_path: Path, motion_log_path: Path, proxy_style: str) -> dict[str, Any]:
    """Verify that the canonical motion sidecar was consumed by Blender."""
    if proxy_style not in {"canonical", "storyhuman"}:
        return {"check_id": "motion.canonical_materialization", "category": "character_trajectory", "status": "skipped", "message": "canonical motion sidecar is only required for canonical or storyhuman Proxy renders", "evidence": {}}
    if not motion_path.is_file() or not motion_log_path.is_file():
        return {"check_id": "motion.canonical_materialization", "category": "character_trajectory", "status": "failed", "message": "canonical motion sidecar or Blender motion log is missing", "evidence": {"motion_path": str(motion_path), "motion_log_path": str(motion_log_path)}}
    profile = json.loads(motion_path.read_text(encoding="utf-8"))
    observed = json.loads(motion_log_path.read_text(encoding="utf-8"))
    expected_ids = {str(item.get("target_id")) for item in profile.get("characters", []) if isinstance(item, Mapping)}
    actual_ids = {str(item.get("target_id")) for item in observed if isinstance(item, Mapping)}
    missing = sorted(expected_ids - actual_ids)
    malformed = []
    for item in profile.get("characters", []):
        if not isinstance(item, Mapping) or not item.get("foot_contacts"):
            malformed.append(str(item.get("target_id")) if isinstance(item, Mapping) else "unknown")
    passed = not missing and not malformed and expected_ids == actual_ids
    return {
        "check_id": "motion.canonical_materialization",
        "category": "character_trajectory",
        "status": "passed" if passed else "failed",
        "message": "canonical limb tracks and foot contacts were consumed" if passed else "canonical motion materialization is incomplete",
        "evidence": {"expected_ids": sorted(expected_ids), "actual_ids": sorted(actual_ids), "missing": missing, "malformed": malformed},
    }


def verify_arm_collision_constraints(*, gesture_path: Path, arm_pose_path: Path, proxy_style: str) -> dict[str, Any]:
    """Verify that applied arm poses stay outside the head corridor."""
    if proxy_style not in {"canonical", "storyhuman"}:
        return {"check_id": "character.arm_head_clearance", "category": "character_trajectory", "status": "skipped", "message": "canonical arm clearance is only required for canonical or storyhuman Proxy renders", "evidence": {}}
    if not gesture_path.is_file() or not arm_pose_path.is_file():
        return {"check_id": "character.arm_head_clearance", "category": "character_trajectory", "status": "failed", "message": "gesture track or arm pose log is missing", "evidence": {}}
    expected = {(str(item.get("target_id")), str(item.get("limb"))) for item in json.loads(gesture_path.read_text(encoding="utf-8")).get("gesture_tracks", []) if isinstance(item, Mapping)}
    observed_rows = json.loads(arm_pose_path.read_text(encoding="utf-8"))
    observed = {(str(item.get("target_id")), str(item.get("limb"))) for item in observed_rows if isinstance(item, Mapping)}
    invalid = []
    for item in observed_rows:
        if not isinstance(item, Mapping):
            invalid.append("non_object")
            continue
        if float(item.get("min_elbow_clearance", 0.0)) < 0.12:
            invalid.append({"target_id": item.get("target_id"), "limb": item.get("limb"), "min_elbow_clearance": item.get("min_elbow_clearance")})
    passed = expected == observed and not invalid
    return {
        "check_id": "character.arm_head_clearance",
        "category": "character_trajectory",
        "status": "passed" if passed else "failed",
        "message": "applied arm poses keep a clearance corridor around the head" if passed else "an applied arm pose can enter the head corridor",
        "evidence": {"expected": sorted(expected), "observed": sorted(observed), "invalid": invalid, "minimum_clearance": 0.12},
    }


def verify_skeleton_materialization(*, motion_path: Path, pose_path: Path, proxy_style: str) -> dict[str, Any]:
    """Verify real frame-major bone/IK logs for the skeleton Proxy branch."""
    if proxy_style != "skeleton":
        return {"check_id": "skeleton.materialization", "category": "character_trajectory", "status": "skipped", "message": "skeleton materialization is only required for the skeleton Proxy branch", "evidence": {}}
    if not motion_path.is_file() or not pose_path.is_file():
        return {"check_id": "skeleton.materialization", "category": "character_trajectory", "status": "failed", "message": "skeleton motion sidecar or pose log is missing", "evidence": {}}
    profile = json.loads(motion_path.read_text(encoding="utf-8"))
    pose_rows = json.loads(pose_path.read_text(encoding="utf-8"))
    expected = {str(item.get("target_id")): set(str(name) for name in item.get("bones", [])) for item in profile.get("characters", []) if isinstance(item, Mapping)}
    failures = []
    observed_frames = {int(row.get("frame")) for row in pose_rows if isinstance(row, Mapping) and isinstance(row.get("frame"), int)}
    for row in pose_rows:
        if not isinstance(row, Mapping):
            failures.append("non_object_frame")
            continue
        for target_id, required_bones in expected.items():
            character = row.get("characters", {}).get(target_id, {}) if isinstance(row.get("characters"), Mapping) else {}
            bones = character.get("bones", {}) if isinstance(character, Mapping) else {}
            missing = sorted(required_bones - set(bones))
            if missing:
                failures.append({"frame": row.get("frame"), "target_id": target_id, "missing_bones": missing})
                continue
            for bone_name in required_bones:
                point = bones[bone_name].get("position") if isinstance(bones[bone_name], Mapping) else None
                if not isinstance(point, list) or len(point) != 3 or any(not math.isfinite(float(value)) for value in point):
                    failures.append({"frame": row.get("frame"), "target_id": target_id, "bone": bone_name, "error": "non_finite_position"})
                if bone_name in {"hand.L", "hand.R", "foot.L", "foot.R"} and isinstance(bones[bone_name], Mapping) and bones[bone_name].get("reachable") is False:
                    failures.append({"frame": row.get("frame"), "target_id": target_id, "bone": bone_name, "error": "ik_unreachable"})
    passed = bool(expected) and bool(pose_rows) and not failures and observed_frames == set(range(max(observed_frames) + 1))
    return {
        "check_id": "skeleton.materialization",
        "category": "character_trajectory",
        "status": "passed" if passed else "failed",
        "message": "all configured bones have finite, reachable frame-major poses" if passed else "skeleton pose materialization is incomplete or invalid",
        "evidence": {"expected_bones": {key: sorted(value) for key, value in expected.items()}, "pose_frame_count": len(observed_frames), "observed_frames_first_last": [min(observed_frames), max(observed_frames)] if observed_frames else [], "failures": failures[:20]},
    }


def verify_proxy_black_frames(*, sandbox_dir: Path, videos: list[Mapping[str, Any]]) -> dict[str, Any]:
    events = []
    for video in videos:
        path = sandbox_dir / str(video["path"])
        count = _black_frames(path)
        if count:
            events.append({"camera_id": video.get("camera_id"), "blackdetect_events": count})
    return {
        "check_id": "media.black_frames",
        "category": "media",
        "status": "passed" if not events else "failed",
        "message": "no black-frame events detected in Proxy videos" if not events else "black-frame events detected in Proxy videos",
        "evidence": {"events": events, "video_count": len(videos)},
    }


def run_proxy_review(*, scene_output: Path, verifier: Mapping[str, Any], review_frames: list[str], spec: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """Run exactly one VLM review when explicitly requested; otherwise defer."""
    if mode not in {"none", "manual", "vlm"}:
        raise ValueError("proxy review mode must be none, manual, or vlm")
    if mode in {"none", "manual"}:
        return {"status": "pending_review", "mode": mode, "api_calls": 0, "reason": "visual approval is not inferred from deterministic checks"}
    review_dir = scene_output / "proxy_vlm_review"
    suffix = 0
    while review_dir.exists():
        suffix += 1
        review_dir = scene_output / f"proxy_vlm_review_{suffix:03d}"
    try:
        document = request_vlm_feedback(
            proxy_report=verifier,
            frame_paths=review_frames,
            output_dir=review_dir,
            story_context=str(spec["prompt"]),
            review_stage="proxy",
        )
        return {"status": "approved" if document["verdict"] == "approve" else "revision_requested", "mode": "vlm", "api_calls": 1, "feedback": document}
    except Exception as exc:
        return {"status": "vlm_failed", "mode": "vlm", "api_calls": 1, "error": f"{type(exc).__name__}: {exc}"}


def run_final_review(*, scene_output: Path, final_report: Mapping[str, Any], final_video: Path | None = None, final_videos: Sequence[tuple[str, Path]] | None = None, spec: Mapping[str, Any], mode: str) -> dict[str, Any]:
    if mode not in {"none", "manual", "vlm"}:
        raise ValueError("final review mode must be none, manual, or vlm")
    if mode in {"none", "manual"}:
        return {"status": "pending_review", "mode": mode, "api_calls": 0, "reason": "final visual quality requires human or VLM review"}
    items = list(final_videos or [])
    if final_video is not None and not items:
        items = [("final", final_video)]
    review_frames = []
    for camera_id, path in items:
        review_frames.extend(_extract_review_frames(path, scene_output / "final_video_review" / str(camera_id)))
    if not review_frames:
        return {"status": "vlm_failed", "mode": "vlm", "api_calls": 0, "error": "final video review frames could not be extracted"}
    review_dir = scene_output / "final_vlm_review"
    camera_audit = "\n".join(
        f"camera_id={camera.get('camera_id')}: role={camera.get('role')}; target={camera.get('target')}"
        for camera in spec.get("cameras", [])
        if isinstance(camera, Mapping)
    )
    review_context = str(spec["prompt"]) + "\n\nCamera trajectory audit requirements:\n" + camera_audit + "\nCheck every supplied camera against its authored role, target, path, visible entity count, object continuity, and action order."
    try:
        document = request_vlm_feedback(
            proxy_report=final_report,
            frame_paths=review_frames,
            output_dir=review_dir,
            story_context=review_context,
            review_stage="final",
        )
        return {"status": "approved" if document["verdict"] == "approve" else "revision_requested", "mode": "vlm", "api_calls": 1, "feedback": document}
    except Exception as exc:
        return {"status": "vlm_failed", "mode": "vlm", "api_calls": 1, "error": f"{type(exc).__name__}: {exc}"}


def run_scene(spec: Mapping[str, Any], *, output_root: Path, blender: Path, model: str, proxy_style: str, asset_dir: Path | None, motion_bvh: Path | None = None, motion_bvh_alt: Path | None = None, realization_mode: str, proxy_review_mode: str, final_review_mode: str, allow_unreviewed_backend: bool, poll_interval: float, max_polls: int, run_api: bool = True) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    scene_output = output_dir_for(output_root, spec["scene_id"], run_id)
    scene_output.mkdir(parents=True, exist_ok=False)
    _write_json(scene_output / "scene_spec.json", spec)
    (scene_output / "prompt.txt").write_text(spec["prompt"] + "\n", encoding="utf-8")
    world = compile_scene_world(spec)
    world_path = scene_output / "world_state.json"
    _write_json(world_path, world.to_dict())
    catalog_records: dict[str, dict[str, Any]] = {}
    try:
        if proxy_style == "asset_humanoid":
            copied_assets, catalog_records = materialize_catalog_assets(spec=spec, scene_output=scene_output)
        else:
            copied_assets = copy_canonical_assets(spec=spec, scene_output=scene_output, asset_dir=asset_dir)
    except AssetCatalogError as exc:
        failure = {"scene_id": spec["scene_id"], "status": "asset_missing", "stage": "asset_catalog", "error": str(exc), "scene_output": str(scene_output), "api_calls": {"submit": 0, "query": 0, "download": 0}}
        _write_json(scene_output / "failure.json", failure)
        return failure
    asset_registry_path = scene_output / "asset_registry.json"
    _write_json(asset_registry_path, asset_registry_for(spec, proxy_style, copied_assets, catalog_records))
    layout_path = scene_output / "scene_layout.json"
    _write_json(layout_path, scene_layout_for(spec))
    planner_path = scene_output / "physical_state_planner.json"
    _write_json(planner_path, {**spec["planner"], "scene": spec["scene_id"], "semantic_keyframes": spec["action_phases"], "gesture_tracks": spec["gesture_tracks"], "source_prompt_sha256": sha256_file(scene_output / "prompt.txt")})
    gesture_path = scene_output / "gesture_tracks.json"
    _write_json(gesture_path, {"schema_version": "gesture-tracks-1.0", "scene_id": spec["scene_id"], "gesture_tracks": spec["gesture_tracks"]})
    motion_path = scene_output / "motion_tracks.json"
    _write_json(motion_path, canonical_motion_profile_for(spec))
    skeleton_motion_path = scene_output / "skeleton_motion.json"
    _write_json(skeleton_motion_path, skeleton_motion_profile_for(spec))
    director_plan = director_plan_for(world)
    director_path = scene_output / "director_plan_normalized.json"
    _write_json(director_path, director_plan)
    contract_path = scene_output / "physical_render_contract.json"
    _write_json(contract_path, {"schema_version": "physical-render-contract-1.0", "scene_id": spec["scene_id"], "world_state_hash": world.world_state_hash(), "action_phases": spec["action_phases"], "quality_gates": spec["planner"]["render_policy"]["quality_gates"]})
    code_dir = scene_output / "code_agent"
    code_dir.mkdir()
    script_path = code_dir / "generated_blender.py"
    script_path.write_text(_blender_script(), encoding="utf-8")
    motion_sources = [str(path.resolve()) for path in (motion_bvh, motion_bvh_alt) if path is not None]
    evidence = {"status": "succeeded", "agent": "Codex-local-complex-scene-compiler", "world_state_hash": world.world_state_hash(), "script_validation": {"script_sha256": sha256_file(script_path)}, "planner_sha256": sha256_file(planner_path), "gesture_tracks_sha256": sha256_file(gesture_path), "motion_tracks_sha256": sha256_file(motion_path), "skeleton_motion_sha256": sha256_file(skeleton_motion_path), "asset_registry_sha256": sha256_file(asset_registry_path), "scene_layout_sha256": sha256_file(layout_path), "proxy_style": proxy_style, "motion_sources": motion_sources, "notes": ["one shared Blender world", "four synchronized cameras", "fine-grained gesture keyframes", "gesture sidecar consumed by Blender", "canonical asset registry sidecar", "canonical motion profile with leg tracks and foot contacts", "skeleton motion sidecar with explicit per-bone landmarks"]}
    _write_json(code_dir / "evidence.json", evidence)
    sandbox_dir = scene_output / "sandbox"
    try:
        manifest = run_blender_sandbox(blender_executable=blender, generated_script=script_path, world_state_path=world_path, output_dir=sandbox_dir, render_style=proxy_style, resolution=(640, 360), timeout_seconds=1800, expected_world_state_hash=world.world_state_hash(), motion_bvh=motion_bvh, motion_bvh_alt=motion_bvh_alt)
        verifier = verify_proxy(world_state_path=world_path, render_output_dir=sandbox_dir, director_plan_path=director_path, code_agent_evidence_path=code_dir / "evidence.json", generated_script_path=script_path, probe_video=_probe)
        asset_check = verify_asset_materialization(registry_path=asset_registry_path, asset_log_path=sandbox_dir / "asset_log.json", proxy_style=proxy_style)
        verifier.setdefault("checks", []).append(asset_check)
        if asset_check["status"] == "failed":
            verifier["verdict"] = "fail"
        catalog_check = verify_asset_catalog_materialization(registry_path=asset_registry_path, asset_log_path=sandbox_dir / "asset_log.json", proxy_style=proxy_style)
        verifier.setdefault("checks", []).append(catalog_check)
        if catalog_check["status"] == "failed":
            verifier["verdict"] = "fail"
        shared_identity_check = verify_shared_world_identity(asset_log_path=sandbox_dir / "asset_log.json", camera_log_path=sandbox_dir / "camera_log.json", expected_camera_count=world.camera_count) if proxy_style == "asset_humanoid" else {"check_id": "asset.shared_world_identity", "category": "scene_structure", "status": "skipped", "message": "shared catalog identity is only required for asset_humanoid", "evidence": {}}
        verifier.setdefault("checks", []).append(shared_identity_check)
        if shared_identity_check["status"] == "failed":
            verifier["verdict"] = "fail"
        coupling_check = verify_coupling_materialization(spec=spec, coupling_log_path=sandbox_dir / "coupling_log.json", frame_count=world.frame_count)
        verifier.setdefault("checks", []).append(coupling_check)
        if coupling_check["status"] == "failed":
            verifier["verdict"] = "fail"
        motion_check = verify_motion_materialization(motion_path=motion_path, motion_log_path=sandbox_dir / "motion_log.json", proxy_style=proxy_style)
        verifier.setdefault("checks", []).append(motion_check)
        if motion_check["status"] == "failed":
            verifier["verdict"] = "fail"
        arm_check = verify_arm_collision_constraints(gesture_path=gesture_path, arm_pose_path=sandbox_dir / "arm_pose_log.json", proxy_style=proxy_style)
        verifier.setdefault("checks", []).append(arm_check)
        if arm_check["status"] == "failed":
            verifier["verdict"] = "fail"
        skeleton_check = verify_skeleton_materialization(motion_path=skeleton_motion_path, pose_path=sandbox_dir / "skeleton_pose_log.json", proxy_style=proxy_style)
        verifier.setdefault("checks", []).append(skeleton_check)
        if skeleton_check["status"] == "failed":
            verifier["verdict"] = "fail"
        black_check = verify_proxy_black_frames(sandbox_dir=sandbox_dir, videos=manifest["videos"])
        verifier.setdefault("checks", []).append(black_check)
        if black_check["status"] == "failed":
            verifier["verdict"] = "fail"
    except Exception as exc:
        failure = {"scene_id": spec["scene_id"], "status": "proxy_failed", "error": str(exc), "scene_output": str(scene_output), "api_calls": {"submit": 0, "query": 0, "download": 0}}
        _write_json(scene_output / "failure.json", failure)
        return failure
    sheets = []
    review_frames = []
    for video in manifest["videos"]:
        sheet = _extract_sheet(sandbox_dir / video["path"], sandbox_dir / Path(video["path"]).stem)
        if sheet:
            sheets.append(sheet)
    # VLM must inspect every authored camera responsibility, not only the first
    # two entries in the manifest.  Each camera gets independent K0-K4 frames.
    for video in manifest.get("videos", []):
        camera_id = str(video.get("camera_id") or Path(video["path"]).stem)
        review_frames.extend(_extract_review_frames(sandbox_dir / video["path"], sandbox_dir / f"vlm_frames_{camera_id}"))
    _write_json(scene_output / "proxy_verifier_report.json", verifier)
    proxy_review = run_proxy_review(scene_output=scene_output, verifier=verifier, review_frames=review_frames, spec=spec, mode=proxy_review_mode)
    # Keep the machine-readable verifier and the explicit visual gate in sync.
    # Deterministic checks alone remain `unknown` for creative quality, but an
    # actual VLM decision must be visible in the revision report and summary.
    if proxy_review.get("status") == "approved":
        verifier["verdict"] = "approve"
        verifier["visual_review_verdict"] = "approve"
    elif proxy_review.get("status") == "revision_requested":
        verifier["verdict"] = "revision_requested"
        verifier["visual_review_verdict"] = "revision_requested"
    _write_json(scene_output / "proxy_verifier_report.json", verifier)
    _write_json(scene_output / "proxy_review.json", proxy_review)
    appearance_artifacts = None
    if proxy_review["status"] == "approved" or allow_unreviewed_backend:
        appearance_artifacts = prepare_appearance_and_adapter(scene_output=scene_output, spec=spec, world=world, proxy_manifest=manifest)
    backend_allowed = (proxy_review["status"] == "approved" or allow_unreviewed_backend) and not any(item.get("status") == "failed" for item in verifier.get("checks", []))
    if run_api and not backend_allowed:
        seedance = {"status": "blocked_proxy_review", "api_calls": {"submit": 0, "query": 0, "download": 0}, "reason": "Proxy requires human/VLM approval before appearance-only compilation and backend submission"}
    elif run_api and realization_mode == "reference_video":
        seedance = run_seedance_single(scene_output=scene_output, spec=spec, model=model, poll_interval=poll_interval, max_polls=max_polls)
    elif run_api and realization_mode == "t2v":
        seedance = run_seedance_t2v_single(scene_output=scene_output, spec=spec, model=model, poll_interval=poll_interval, max_polls=max_polls)
    else:
        seedance = {"status": "not_run", "api_calls": {"submit": 0, "query": 0, "download": 0}, "reason": "--skip-seedance"}
    final_report = None
    final_review = {"status": "not_run", "api_calls": 0}
    result_path = seedance.get("result_path") if isinstance(seedance, Mapping) else None
    camera_result_paths = []
    if isinstance(seedance, Mapping):
        for camera_result in seedance.get("camera_results", []) or []:
            if isinstance(camera_result, Mapping) and isinstance(camera_result.get("result_path"), str):
                path = Path(str(camera_result["result_path"]))
                if path.is_file():
                    camera_result_paths.append((str(camera_result.get("camera_id") or path.stem), path))
    if camera_result_paths:
        reports = {}
        report_dir = scene_output / "final_video_verifier"
        for camera_id, path in camera_result_paths:
            report = verify_final_video(
                path,
                expected_frame_count=world.frame_count,
                expected_fps=world.fps,
                expected_duration=float(world.to_dict()["scene_plan"]["duration_seconds"]),
                probe_video=_probe,
                blackdetect_events=int(next((item.get("blackdetect_events", 0) for item in seedance.get("camera_results", []) if item.get("camera_id") == camera_id), 0)),
                source_hashes={"world_state": world.world_state_hash(), "proxy_manifest": sha256_file(sandbox_dir / "render_manifest.json")},
            )
            reports[camera_id] = report
            write_final_video_report(report, report_dir / f"{camera_id}.json")
        final_report = aggregate_final_video_reports(reports)
        write_final_video_report(final_report, scene_output / "final_video_verifier_report.json")
        final_review = run_final_review(scene_output=scene_output, final_report=final_report, final_videos=camera_result_paths, spec=spec, mode=final_review_mode)
        _write_json(scene_output / "final_review.json", final_review)
    elif isinstance(result_path, str) and Path(result_path).is_file():
        final_report = verify_final_video(
            result_path,
            expected_frame_count=world.frame_count,
            expected_fps=world.fps,
            expected_duration=float(world.to_dict()["scene_plan"]["duration_seconds"]),
            probe_video=_probe,
            blackdetect_events=int(seedance.get("blackdetect_events", 0)),
            source_hashes={"world_state": world.world_state_hash(), "proxy_manifest": sha256_file(sandbox_dir / "render_manifest.json")},
        )
        write_final_video_report(final_report, scene_output / "final_video_verifier_report.json")
        final_review = run_final_review(scene_output=scene_output, final_report=final_report, final_video=Path(result_path), spec=spec, mode=final_review_mode)
        _write_json(scene_output / "final_review.json", final_review)
    result = {"scene_id": spec["scene_id"], "status": "completed", "proxy_style": proxy_style, "realization_mode": realization_mode, "scene_output": str(scene_output), "proxy_verdict": verifier["verdict"], "proxy_review": proxy_review, "appearance_artifacts": appearance_artifacts, "proxy_manifest": str(sandbox_dir / "render_manifest.json"), "proxy_sheets": sheets, "seedance": seedance, "final_video_verifier": final_report, "final_review": final_review}
    _write_json(scene_output / "scene_summary.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "runs" / "results")
    parser.add_argument("--blender", type=Path, default=Path(r"D:\blender\blender.exe"))
    parser.add_argument("--model", default="Doubao-Seedance-2.0")
    parser.add_argument("--proxy-style", choices=render_style_choices(), default="clay")
    parser.add_argument("--asset-dir", type=Path, help="optional directory containing <entity_id>.glb canonical assets")
    parser.add_argument("--readability-revision", action="store_true", help="apply the next Director revision requested by VLM; preserves target identities/roles and entity action order while widening coverage")
    parser.add_argument("--storyblender-revision", action="store_true", help="apply Stage-A StoryBlender-style camera/layout reflection without changing authored entity or gesture tracks")
    parser.add_argument("--arm-clearance-revision", action="store_true", help="apply the next immutable revision with arm/head collision constraints")
    parser.add_argument("--skeleton-revision", action="store_true", help="apply revision_008 with procedural armature and explicit IK bone trajectories")
    parser.add_argument("--camera-staging-revision", action="store_true", help="apply revision_009 to separate action lanes and lower rear/overhead coverage")
    parser.add_argument("--camera-readability-revision", action="store_true", help="apply revision_010 with grounded three-quarter camera coverage")
    parser.add_argument("--proxy-legibility-revision", action="store_true", help="apply revision_011 with distinct props and stronger action landmarks")
    parser.add_argument("--motion-legibility-revision", action="store_true", help="apply revision_012 with raised gesture targets and wide front-side coverage")
    parser.add_argument("--readable-stage-revision", action="store_true", help="apply revision_013 with separated stage lanes and explicit hand silhouettes")
    parser.add_argument("--musician-role-revision", action="store_true", help="apply revision_014 with a visible musician cue and stable speaker gap")
    parser.add_argument("--fixed-stage-camera-revision", action="store_true", help="apply revision_015 with fixed-center coverage that exposes dancer travel")
    parser.add_argument("--composition-fit-revision", action="store_true", help="apply revision_016 with closer camera framing and bounded stage lanes")
    parser.add_argument("--event-separation-revision", action="store_true", help="apply revision_017 with a rear passerby crossing, alternating side-steps, and visible backpack coupling")
    parser.add_argument("--rigged-staging-revision", action="store_true", help="apply revision_018 for rigged-human legibility and bench occlusion repair")
    parser.add_argument("--choreography-revision", action="store_true", help="apply revision_019 with explicit side-step timing and dedicated camera responsibilities")
    parser.add_argument("--side-step-motion-revision", action="store_true", help="apply revision_020 with side-step transfer motion and wider coverage")
    parser.add_argument("--bvh-motion-revision", action="store_true", help="apply revision_021 and retarget real BVH clips onto the lead rig")
    parser.add_argument("--bvh-readability-revision", action="store_true", help="apply revision_022 to separate BVH-backed action lanes and wave beats")
    parser.add_argument("--bvh-upright-revision", action="store_true", help="apply revision_023 with upright BVH limbs and wider side-step spacing")
    parser.add_argument("--bvh-choreography-revision", action="store_true", help="apply revision_024 with an explicit bounded side-step overlay")
    parser.add_argument("--indoor-market-readability-revision", action="store_true", help="apply revision_025 for counter/cart grounding and vendor-side coverage")
    parser.add_argument("--indoor-market-counter-revision", action="store_true", help="apply revision_026 with table-like counter and distant overhead coverage")
    parser.add_argument("--indoor-market-event-revision", action="store_true", help="apply revision_027 with explicit vendor/pause/gesture/flutter beats")
    parser.add_argument("--indoor-market-storyhuman-revision", action="store_true", help="apply revision_028 with rounded-human staging and grounded cart contact")
    parser.add_argument("--indoor-market-storyhuman-readability-revision", action="store_true", help="apply revision_029 with separated helper lane and explicit paper landing")
    parser.add_argument("--indoor-market-asset-contact-revision", action="store_true", help="apply revision_030 with a graspable horizontal cart handle and identity-specific helper gesture")
    parser.add_argument("--indoor-market-asset-visual-cleanup-revision", action="store_true", help="apply revision_031 with a small role accent and shifted cart grip")
    parser.add_argument("--indoor-market-exchange-staging-revision", action="store_true", help="apply revision_032 with connected market staging and tighter camera coverage")
    parser.add_argument("--indoor-market-action-physics-revision", action="store_true", help="apply revision_033 with readable gesture beats, fluttering papers and distinct boxes")
    parser.add_argument("--indoor-market-identity-grounding-revision", action="store_true", help="apply revision_034 with stable role clothing and grounded exchange staging")
    parser.add_argument("--indoor-market-helper-settle-revision", action="store_true", help="apply revision_035 with a readable helper turn and visible paper landing")
    parser.add_argument("--indoor-market-proxy-cleanup-revision", action="store_true", help="apply revision_036 without head-obscuring proxy blocks")
    parser.add_argument("--indoor-market-push-ik-revision", action="store_true", help="apply revision_037 with a shared cart-parented customer hand IK target")
    parser.add_argument("--indoor-market-push-ik-gesture-window-revision", action="store_true", help="apply revision_038 with IK contact outside the authored raise window")
    parser.add_argument("--indoor-market-counter-grounding-revision", action="store_true", help="apply revision_039 with a grounded vendor counter and refocused exchange views")
    parser.add_argument("--indoor-market-pause-landing-revision", action="store_true", help="apply revision_040 with a true customer pause and shared paper landing zone")
    parser.add_argument("--warehouse-loading-readability-revision", action="store_true", help="apply revision_041 with exterior reverse coverage and dense source-connected packing-slip motion")
    parser.add_argument("--warehouse-loading-coverage-revision", action="store_true", help="apply revision_042 with cart-targeted reverse coverage and a trailing assistant lane")
    parser.add_argument("--warehouse-loading-physics-revision", action="store_true", help="apply revision_043 with separated pusher/supervisor staging and wider coverage")
    parser.add_argument("--warehouse-loading-paper-revision", action="store_true", help="apply revision_044 with flat tilted packing-slip proxies")
    parser.add_argument("--motion-bvh", type=Path, help="real BVH clip for the lead motion branch")
    parser.add_argument("--motion-bvh-alt", type=Path, help="optional second real BVH clip concatenated after the first")
    parser.add_argument("--realization-mode", choices=["reference_video", "t2v"], default="reference_video", help="reference_video consumes the Proxy; t2v is a text-only baseline and does not receive the Proxy")
    parser.add_argument("--proxy-review", choices=["none", "manual", "vlm"], default="manual", help="Proxy visual approval gate; VLM makes exactly one review request")
    parser.add_argument("--final-review", choices=["none", "manual", "vlm"], default="manual", help="final video review gate; VLM makes exactly one review request")
    parser.add_argument("--allow-unreviewed-backend", action="store_true", help="compatibility override; never use for reported experiments")
    parser.add_argument("--skip-seedance", action="store_true", help="render and verify Proxy only; make no external API calls")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=30)
    parser.add_argument("--scene-id", action="append", choices=[scene["scene_id"] for scene in iter_scene_specs()])
    args = parser.parse_args()
    if not args.blender.is_file():
        raise SystemExit(f"Blender executable not found: {args.blender}")
    selected = [scene_spec(scene_id) for scene_id in args.scene_id] if args.scene_id else list(iter_scene_specs())
    if args.readability_revision:
        selected = [readability_revision(spec) for spec in selected]
    if args.storyblender_revision:
        selected = [storyblender_revision(spec) for spec in selected]
    if args.arm_clearance_revision:
        selected = [arm_clearance_revision(spec) for spec in selected]
    if args.skeleton_revision:
        selected = [skeleton_revision(spec) for spec in selected]
    if args.camera_staging_revision:
        selected = [camera_staging_revision(spec) for spec in selected]
    if args.camera_readability_revision:
        selected = [camera_readability_revision(spec) for spec in selected]
    if args.proxy_legibility_revision:
        selected = [proxy_legibility_revision(spec) for spec in selected]
    if args.motion_legibility_revision:
        selected = [motion_legibility_revision(spec) for spec in selected]
    if args.readable_stage_revision:
        selected = [readable_stage_revision(spec) for spec in selected]
    if args.musician_role_revision:
        selected = [musician_role_revision(spec) for spec in selected]
    if args.fixed_stage_camera_revision:
        selected = [fixed_stage_camera_revision(spec) for spec in selected]
    if args.composition_fit_revision:
        selected = [composition_fit_revision(spec) for spec in selected]
    if args.event_separation_revision:
        selected = [event_separation_revision(spec) for spec in selected]
    if args.rigged_staging_revision:
        selected = [rigged_staging_revision(spec) for spec in selected]
    if args.choreography_revision:
        selected = [choreography_revision(spec) for spec in selected]
    if args.side_step_motion_revision:
        selected = [side_step_motion_revision(spec) for spec in selected]
    if args.bvh_motion_revision:
        selected = [bvh_motion_revision(spec) for spec in selected]
    if args.bvh_readability_revision:
        selected = [bvh_readability_revision(spec) for spec in selected]
    if args.bvh_upright_revision:
        selected = [bvh_upright_revision(spec) for spec in selected]
    if args.bvh_choreography_revision:
        selected = [bvh_choreography_revision(spec) for spec in selected]
    if args.indoor_market_readability_revision:
        selected = [indoor_market_readability_revision(spec) for spec in selected]
    if args.indoor_market_counter_revision:
        selected = [indoor_market_counter_revision(spec) for spec in selected]
    if args.indoor_market_event_revision:
        selected = [indoor_market_event_revision(spec) for spec in selected]
    if args.indoor_market_storyhuman_revision:
        selected = [indoor_market_storyhuman_revision(spec) for spec in selected]
    if args.indoor_market_storyhuman_readability_revision:
        selected = [indoor_market_storyhuman_readability_revision(spec) for spec in selected]
    if args.indoor_market_asset_contact_revision:
        selected = [indoor_market_asset_contact_revision(spec) for spec in selected]
    if args.indoor_market_asset_visual_cleanup_revision:
        selected = [indoor_market_asset_visual_cleanup_revision(spec) for spec in selected]
    if args.indoor_market_exchange_staging_revision:
        selected = [indoor_market_exchange_staging_revision(spec) for spec in selected]
    if args.indoor_market_action_physics_revision:
        selected = [indoor_market_action_physics_revision(spec) for spec in selected]
    if args.indoor_market_identity_grounding_revision:
        selected = [indoor_market_identity_grounding_revision(spec) for spec in selected]
    if args.indoor_market_helper_settle_revision:
        selected = [indoor_market_helper_settle_revision(spec) for spec in selected]
    if args.indoor_market_proxy_cleanup_revision:
        selected = [indoor_market_proxy_cleanup_revision(spec) for spec in selected]
    if args.indoor_market_push_ik_revision:
        selected = [indoor_market_push_ik_revision(spec) for spec in selected]
    if args.indoor_market_push_ik_gesture_window_revision:
        selected = [indoor_market_push_ik_gesture_window_revision(spec) for spec in selected]
    if args.indoor_market_counter_grounding_revision:
        selected = [indoor_market_counter_grounding_revision(spec) for spec in selected]
    if args.indoor_market_pause_landing_revision:
        selected = [indoor_market_pause_landing_revision(spec) for spec in selected]
    if args.warehouse_loading_readability_revision:
        selected = [warehouse_loading_readability_revision(spec) for spec in selected]
    if args.warehouse_loading_coverage_revision:
        selected = [warehouse_loading_coverage_revision(spec) for spec in selected]
    if args.warehouse_loading_physics_revision:
        selected = [warehouse_loading_physics_revision(spec) for spec in selected]
    if args.warehouse_loading_paper_revision:
        selected = [warehouse_loading_paper_revision(spec) for spec in selected]
    if len(selected) > SEEDANCE_SUBMISSION_BUDGET:
        raise SystemExit(f"selected scenes exceed hard Seedance budget {SEEDANCE_SUBMISSION_BUDGET}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    root = args.output_root.resolve() / f"complex_scene_suite_{timestamp}"
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for spec in selected:
        results.append(run_scene(spec, output_root=root, blender=args.blender.resolve(), model=args.model, proxy_style=args.proxy_style, asset_dir=args.asset_dir.resolve() if args.asset_dir else None, motion_bvh=args.motion_bvh.resolve() if args.motion_bvh else None, motion_bvh_alt=args.motion_bvh_alt.resolve() if args.motion_bvh_alt else None, realization_mode=args.realization_mode, proxy_review_mode=args.proxy_review, final_review_mode=args.final_review, allow_unreviewed_backend=args.allow_unreviewed_backend, poll_interval=args.poll_interval, max_polls=args.max_polls, run_api=not args.skip_seedance))
        _write_json(root / "progress.json", results)
    summary = {"schema_version": "complex-scene-full-chain-1.0", "model": args.model, "proxy_style": args.proxy_style, "realization_mode": args.realization_mode, "proxy_review": args.proxy_review, "final_review": args.final_review, "scene_count": len(selected), "seedance_submission_budget": SEEDANCE_SUBMISSION_BUDGET, "results": results, "api_calls": {key: sum(int(item.get("seedance", {}).get("api_calls", {}).get(key, 0)) for item in results) for key in ("submit", "query", "download")}, "vlm_review_calls": sum(int(item.get("proxy_review", {}).get("api_calls", 0)) + int(item.get("final_review", {}).get("api_calls", 0)) for item in results)}
    _write_json(root / "summary.json", summary)
    lines = ["# Complex Scene Full-Chain Results", "", f"Root: `{root}`", "", f"Model: `{args.model}`", ""]
    for result in results:
        lines += [f"## {result['scene_id']}", f"- Proxy verdict: `{result.get('proxy_verdict', result.get('status'))}`", f"- Proxy bundle: `{result.get('proxy_manifest', '')}`", f"- Seedance: `{result.get('seedance', {}).get('status', 'not_run')}`"]
        if result.get("seedance", {}).get("result_path"):
            lines.append(f"- Final video: `{result['seedance']['result_path']}`")
        if result.get("seedance", {}).get("camera_results"):
            lines.append("- Final videos: one independent task/result per camera (`camera_results`)")
        lines.append("")
    lines += ["## API totals", "", "```json", json.dumps(summary["api_calls"], ensure_ascii=False, indent=2), "```"]
    (root / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") == "completed" for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
