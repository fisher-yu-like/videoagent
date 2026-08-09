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
from pipeline_v2.proxy_verifier import verify_proxy
from pipeline_v2.vlm_feedback import request_vlm_feedback
from pipeline_v2.state import SCHEMA_VERSION, WorldState
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
    document = {
        "schema_version": SCHEMA_VERSION,
        "scene_plan": {
            "scene_id": spec["scene_id"],
            "environment_preset": "neutral_shared_world_park_or_market",
            "duration_seconds": 5.0,
            "fps": 24,
            "frame_count": 120,
            "entities": spec["entities"],
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


def asset_registry_for(spec: Mapping[str, Any], proxy_style: str, asset_paths: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Create a deterministic sidecar registry without changing WorldState.

    The strict pipeline_v2 schema intentionally remains unchanged.  This
    registry is the StoryBlender-inspired source of truth for how an entity is
    materialised in Blender and lets old clay runs coexist with canonical runs.
    """
    if proxy_style not in {"clay", "canonical", "skeleton"}:
        raise ValueError("proxy_style must be clay, canonical, or skeleton")
    assets = []
    for entity in spec["entities"]:
        eid = str(entity["id"])
        kind = str(entity["kind"])
        asset_path = (asset_paths or {}).get(eid)
        if kind == "character" and proxy_style == "canonical" and asset_path:
            source_kind = "canonical_glb"
            dimensions = [0.95, 0.55, 2.55]
            parts = ["imported_glb"]
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
        swing = 0.34 if moving else 0.16
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
            "motion_mode": "alternating_stride" if moving else "grounded_weight_shift",
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
                ("hand.L", (0.44 - math.sin(safe_arm_angle("left_arm", phase_left)) * 0.55, 0.0, 1.40 + abs(math.sin(safe_arm_angle("left_arm", phase_left))) * 0.70), (0.0, safe_arm_angle("left_arm", phase_left), 0.0)),
                ("upper_arm.R", (-0.50, 0.0, 2.02), (0.0, safe_arm_angle("right_arm", phase_right), 0.0)),
                ("forearm.R", (-0.50, 0.0, 1.56), (0.0, safe_arm_angle("right_arm", phase_right), 0.0)),
                ("hand.R", (-0.44 - math.sin(safe_arm_angle("right_arm", phase_right)) * 0.55, 0.0, 1.40 + abs(math.sin(safe_arm_angle("right_arm", phase_right))) * 0.70), (0.0, safe_arm_angle("right_arm", phase_right), 0.0)),
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
    return {"schema_version": "skeleton-motion-profile-1.0", "scene_id": str(spec["scene_id"]), "characters": characters, "source": "authored root/gesture tracks compiled to explicit bone landmarks"}


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


def appearance_profile_for(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Create appearance facts without leaking motion/camera instructions."""
    subjects = []
    for entity in spec["entities"]:
        entity_id = str(entity["id"])
        if entity["kind"] == "character":
            description = "natural live-action human actor with stable identity, realistic anatomy, coherent clothing, hair, hands and face"
        else:
            description = "believable real-world prop with coherent material, scale and surface details"
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
        ],
    }


def copy_canonical_assets(*, spec: Mapping[str, Any], scene_output: Path, asset_dir: Path | None) -> dict[str, str]:
    """Copy optional per-entity GLBs into the immutable run directory."""
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
            continue
        destination = destination_root / source.name
        shutil.copy2(source, destination)
        copied[entity_id] = (Path("assets") / source.name).as_posix()
    return copied


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
        parser.add_argument("--render-style", default="clay", choices=["clay", "canonical", "skeleton", "diagnostic"])
        parser.add_argument("--resolution", default="640x360")
        args = parser.parse_args(values)
        out = Path(args.output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        world_state = json.loads(Path(args.world_state).read_text(encoding="utf-8"))
        registry_path = Path(args.world_state).resolve().parent / "asset_registry.json"
        asset_registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else {"assets": []}
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
            if not isinstance(registry_asset, dict) or registry_asset.get("source_kind") != "canonical_glb":
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
                if obj.parent is None:
                    matrix = obj.matrix_world.copy()
                    obj.parent = root
                    obj.matrix_world = matrix
                obj.name = eid + "__glb__" + obj.name
            return True

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
        asset_log = []

        # A single shared neutral floor/backdrop makes spatial relationships visible.
        add_cube("ground", (0, 0, -0.18), (9.0, 7.0, 0.18), dark, 0.03)
        # Keep the backdrop behind every authored camera ray.  At y=5.5 it
        # occludes the reverse camera and produces a real black video.
        add_cube("backdrop", (0, 12.0, 3.0), (9.0, 0.18, 3.0), dark, 0.03)

        for entity in scene_plan["entities"]:
            eid, kind = entity["id"], entity["kind"]
            root = add_root(eid)
            roots[eid] = root
            registry_asset = next((item for item in asset_registry.get("assets", []) if item.get("asset_id") == eid), None)
            if kind == "character":
                imported_glb = try_import_canonical_glb(eid, root, registry_asset)
                if imported_glb:
                    pass
                elif args.render_style == "skeleton":
                    add_skeleton_humanoid(eid, root)
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
                parent_local(add_cube(eid + "__handle", (0, -0.55, 1.0), (0.08, 0.08, 0.65), light), root, (0, -0.55, 1.0))
                for wheel_x in (-0.65, 0.65):
                    parent_local(add_cylinder(eid + "__wheel" + str(wheel_x), (wheel_x, 0, 0.12), 0.18, 0.10, light), root, (wheel_x, 0, 0.12))
            elif eid.startswith("box_"):
                parent_local(add_cube(eid + "__body", (0, 0, 0.38), (0.38, 0.38, 0.38), light), root, (0, 0, 0.38))
            elif eid.startswith("paper_"):
                parent_local(add_cube(eid + "__sheet", (0, 0, 0), (0.42, 0.30, 0.025), light, 0.01), root, (0, 0, 0))
            elif eid == "counter":
                parent_local(add_cube(eid + "__body", (0, 0, 1.0), (1.4, 0.65, 1.0), dark), root, (0, 0, 1.0))
            else:
                parent_local(add_cube(eid + "__body", (0, 0, 0.45), (0.45, 0.45, 0.45), light), root, (0, 0, 0.45))

            points = tracks.get(eid)
            if points is None:
                raise RuntimeError("missing authored track for " + eid)
            asset_log.append({"asset_id": eid, "kind": kind, "source_kind": (registry_asset or {}).get("source_kind", "unknown"), "parts": len([obj for obj in bpy.context.scene.objects if obj.name.startswith(eid + "__")]), "shared_world_instance": True})
            for frame in range(frame_count):
                pos, rot = interp(points, frame)
                root.location = pos
                root.rotation_mode = "XYZ"
                root.rotation_euler = rot
                root.keyframe_insert(data_path="location", frame=frame)
                root.keyframe_insert(data_path="rotation_euler", frame=frame)

        arm_pose_log = []
        for gesture in gesture_tracks:
            arm = arms.get((gesture["target_id"], gesture["limb"]))
            if arm is None:
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
                arm.rotation_euler[1] = float(angle)
                arm.location.z = 2.02
                arm.keyframe_insert(data_path="rotation_euler", frame=frame)
                arm.keyframe_insert(data_path="location", frame=frame)
                applied_angles.append(float(angle))
            shoulder_x = 0.44 if gesture["limb"] == "left_arm" else -0.44
            clearances = [abs(shoulder_x - math.sin(angle) * 0.29) for angle in applied_angles]
            arm_pose_log.append({"target_id": gesture["target_id"], "limb": gesture["limb"], "min_angle": min(applied_angles), "max_angle": max(applied_angles), "min_elbow_clearance": min(clearances)})

        # StoryBlender-style animation layer: keep the authored root path and
        # add a small deterministic gait/weight-shift pass.  Contacts are
        # logged for auditing; they are not presented as an IK solution.
        motion_log = []
        for motion in canonical_motion_tracks:
            target_id = str(motion.get("target_id"))
            for limb_name, samples in motion.get("limb_tracks", {}).items():
                parts = legs.get((target_id, limb_name))
                if not parts or not samples:
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
                    parts["upper"].rotation_euler[1] = angle
                    parts["lower"].rotation_euler[1] = -0.55 * angle
                    parts["foot"].rotation_euler[1] = 0.20 * angle
                    for part in parts.values():
                        part.keyframe_insert(data_path="rotation_euler", frame=frame)
            motion_log.append({"target_id": target_id, "motion_mode": motion.get("motion_mode"), "foot_contacts": motion.get("foot_contacts", [])})

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
            camera_logs.append({"camera_id": cid, "target_id": camera_plan["target"]["object_id"], "authored": authored, "applied": applied})
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
        (out / "state_log.json").write_text(json.dumps(state_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "applied_state_log.json").write_text(json.dumps(state_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "camera_log.json").write_text(json.dumps(camera_logs, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "asset_log.json").write_text(json.dumps(asset_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "motion_log.json").write_text(json.dumps(motion_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "arm_pose_log.json").write_text(json.dumps(arm_pose_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "skeleton_pose_log.json").write_text(json.dumps(skeleton_pose_log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (out / "render_manifest.json").write_text(json.dumps({"schema_version": "pipeline-v2-render-manifest-1.0", "world_state_hash": world_hash, "proxy_style": args.render_style, "asset_registry": str(registry_path.name) if registry_path.is_file() else None, "frame_count": frame_count, "fps": fps, "resolution": [width, height], "videos": videos}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
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
    return config.__class__(
        access_key=config.access_key, secret_key=config.secret_key, bucket=config.bucket,
        endpoint=config.endpoint, region=config.region, expires_seconds=config.expires_seconds,
        temp_upload_enabled=True, temp_upload_endpoint=config.temp_upload_endpoint,
        temp_upload_provider=config.temp_upload_provider,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_seedance_single(*, scene_output: Path, spec: Mapping[str, Any], model: str, poll_interval: float, max_polls: int) -> dict[str, Any]:
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
    try:
        for video in videos:
            camera_id = str(video["camera_id"])
            proxy = scene_output / "sandbox" / str(video["path"])
            normalized = normalize_proxy(proxy, normalized_dir / f"{camera_id}_seedance.mp4")
            asset = upload_proxy(normalized, run_id=f"{spec['scene_id']}_{camera_id}", config=config)
            assets.append(asset)
            write_upload_record(asset, uploads_dir / f"{camera_id}.json")
    except Exception as exc:
        result = {"status": "upload_failed", "stage": "upload", "error": str(exc), "api_calls": {"submit": 0, "query": 0, "download": 0}}
        _write_json(job_dir / "failure.json", result)
        return result
    prompt = spec["appearance_prompt"]
    base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")
    result = run_uploaded_camera_jobs(
        job_id=spec["scene_id"],
        prompt=prompt,
        assets=assets,
        camera_ids=[str(video["camera_id"]) for video in videos],
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
    if proxy_style in {"canonical", "skeleton"}:
        for asset_id, item in expected.items():
            if item.get("kind") == "character":
                observed = actual.get(asset_id, {})
                required_parts = 1 if item.get("source_kind") == "canonical_glb" else 12
                if observed.get("source_kind") != item.get("source_kind") or int(observed.get("parts", 0)) < required_parts:
                    canonical_failures.append(asset_id)
    passed = not missing and not duplicate_ids and not canonical_failures
    return {
        "check_id": "asset.canonical_materialization",
        "category": "scene_structure",
        "status": "passed" if passed else "failed",
        "message": "canonical assets are present once in the shared world" if passed else "canonical asset materialization is incomplete",
        "evidence": {"expected_assets": sorted(expected), "actual_assets": sorted(actual), "missing": missing, "duplicate_ids": duplicate_ids, "canonical_failures": canonical_failures, "proxy_style": proxy_style},
    }


def verify_motion_materialization(*, motion_path: Path, motion_log_path: Path, proxy_style: str) -> dict[str, Any]:
    """Verify that the canonical motion sidecar was consumed by Blender."""
    if proxy_style != "canonical":
        return {"check_id": "motion.canonical_materialization", "category": "character_trajectory", "status": "skipped", "message": "canonical motion sidecar is only required for canonical Proxy renders", "evidence": {}}
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
    if proxy_style != "canonical":
        return {"check_id": "character.arm_head_clearance", "category": "character_trajectory", "status": "skipped", "message": "canonical arm clearance is only required for canonical Proxy renders", "evidence": {}}
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
    try:
        document = request_vlm_feedback(
            proxy_report=final_report,
            frame_paths=review_frames,
            output_dir=review_dir,
            story_context=str(spec["prompt"]),
            review_stage="final",
        )
        return {"status": "approved" if document["verdict"] == "approve" else "revision_requested", "mode": "vlm", "api_calls": 1, "feedback": document}
    except Exception as exc:
        return {"status": "vlm_failed", "mode": "vlm", "api_calls": 1, "error": f"{type(exc).__name__}: {exc}"}


def run_scene(spec: Mapping[str, Any], *, output_root: Path, blender: Path, model: str, proxy_style: str, asset_dir: Path | None, realization_mode: str, proxy_review_mode: str, final_review_mode: str, allow_unreviewed_backend: bool, poll_interval: float, max_polls: int, run_api: bool = True) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    scene_output = output_dir_for(output_root, spec["scene_id"], run_id)
    scene_output.mkdir(parents=True, exist_ok=False)
    _write_json(scene_output / "scene_spec.json", spec)
    (scene_output / "prompt.txt").write_text(spec["prompt"] + "\n", encoding="utf-8")
    world = compile_scene_world(spec)
    world_path = scene_output / "world_state.json"
    _write_json(world_path, world.to_dict())
    copied_assets = copy_canonical_assets(spec=spec, scene_output=scene_output, asset_dir=asset_dir)
    asset_registry_path = scene_output / "asset_registry.json"
    _write_json(asset_registry_path, asset_registry_for(spec, proxy_style, copied_assets))
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
    evidence = {"status": "succeeded", "agent": "Codex-local-complex-scene-compiler", "world_state_hash": world.world_state_hash(), "script_validation": {"script_sha256": sha256_file(script_path)}, "planner_sha256": sha256_file(planner_path), "gesture_tracks_sha256": sha256_file(gesture_path), "motion_tracks_sha256": sha256_file(motion_path), "skeleton_motion_sha256": sha256_file(skeleton_motion_path), "asset_registry_sha256": sha256_file(asset_registry_path), "scene_layout_sha256": sha256_file(layout_path), "proxy_style": proxy_style, "notes": ["one shared Blender world", "four synchronized cameras", "fine-grained gesture keyframes", "gesture sidecar consumed by Blender", "canonical asset registry sidecar", "canonical motion profile with leg tracks and foot contacts", "skeleton motion sidecar with explicit per-bone landmarks"]}
    _write_json(code_dir / "evidence.json", evidence)
    sandbox_dir = scene_output / "sandbox"
    try:
        manifest = run_blender_sandbox(blender_executable=blender, generated_script=script_path, world_state_path=world_path, output_dir=sandbox_dir, render_style=proxy_style, resolution=(640, 360), timeout_seconds=1800, expected_world_state_hash=world.world_state_hash())
        verifier = verify_proxy(world_state_path=world_path, render_output_dir=sandbox_dir, director_plan_path=director_path, code_agent_evidence_path=code_dir / "evidence.json", generated_script_path=script_path, probe_video=_probe)
        asset_check = verify_asset_materialization(registry_path=asset_registry_path, asset_log_path=sandbox_dir / "asset_log.json", proxy_style=proxy_style)
        verifier.setdefault("checks", []).append(asset_check)
        if asset_check["status"] == "failed":
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
    parser.add_argument("--proxy-style", choices=["clay", "canonical", "skeleton"], default="clay")
    parser.add_argument("--asset-dir", type=Path, help="optional directory containing <entity_id>.glb canonical assets")
    parser.add_argument("--readability-revision", action="store_true", help="apply the next Director revision requested by VLM; preserves target identities/roles and entity action order while widening coverage")
    parser.add_argument("--storyblender-revision", action="store_true", help="apply Stage-A StoryBlender-style camera/layout reflection without changing authored entity or gesture tracks")
    parser.add_argument("--arm-clearance-revision", action="store_true", help="apply the next immutable revision with arm/head collision constraints")
    parser.add_argument("--skeleton-revision", action="store_true", help="apply revision_008 with procedural armature and explicit IK bone trajectories")
    parser.add_argument("--camera-staging-revision", action="store_true", help="apply revision_009 to separate action lanes and lower rear/overhead coverage")
    parser.add_argument("--camera-readability-revision", action="store_true", help="apply revision_010 with grounded three-quarter camera coverage")
    parser.add_argument("--proxy-legibility-revision", action="store_true", help="apply revision_011 with distinct props and stronger action landmarks")
    parser.add_argument("--motion-legibility-revision", action="store_true", help="apply revision_012 with raised gesture targets and wide front-side coverage")
    parser.add_argument("--realization-mode", choices=["reference_video", "t2v"], default="reference_video", help="reference_video consumes the Proxy; t2v is a text-only baseline and does not receive the Proxy")
    parser.add_argument("--proxy-review", choices=["none", "manual", "vlm"], default="manual", help="Proxy visual approval gate; VLM makes exactly one review request")
    parser.add_argument("--final-review", choices=["none", "manual", "vlm"], default="manual", help="final video review gate; VLM makes exactly one review request")
    parser.add_argument("--allow-unreviewed-backend", action="store_true", help="compatibility override; never use for reported experiments")
    parser.add_argument("--skip-seedance", action="store_true", help="render and verify Proxy only; make no external API calls")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=20)
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
    if len(selected) > SEEDANCE_SUBMISSION_BUDGET:
        raise SystemExit(f"selected scenes exceed hard Seedance budget {SEEDANCE_SUBMISSION_BUDGET}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    root = args.output_root.resolve() / f"complex_scene_suite_{timestamp}"
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for spec in selected:
        results.append(run_scene(spec, output_root=root, blender=args.blender.resolve(), model=args.model, proxy_style=args.proxy_style, asset_dir=args.asset_dir.resolve() if args.asset_dir else None, realization_mode=args.realization_mode, proxy_review_mode=args.proxy_review, final_review_mode=args.final_review, allow_unreviewed_backend=args.allow_unreviewed_backend, poll_interval=args.poll_interval, max_polls=args.max_polls, run_api=not args.skip_seedance))
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
