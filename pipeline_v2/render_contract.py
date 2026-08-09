"""Adapter from the canonical WorldState to a VideoCoCo-style render contract."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import json
from typing import Any

from .state import WorldState


CONTRACT_VERSION = "physical-render-contract-1.0"


def _canonical_hash(value: object) -> str:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_physical_render_contract(world: WorldState, director_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Create an implementation-neutral contract without inventing missing physics."""

    document = world.to_dict()
    source_plan = dict(director_plan) if isinstance(director_plan, Mapping) else {}
    source_physical = source_plan.get("physical_state_plan", {})
    if not isinstance(source_physical, Mapping):
        source_physical = {}
    state_physical = document["physical_state_plan"]
    unresolved: list[str] = []
    for field in ("must_show", "must_avoid", "causal_constraints", "semantic_keyframes", "transitions"):
        if field not in source_physical and field not in source_plan:
            unresolved.append(field)

    scene = document["scene_plan"]
    entities = [dict(item) for item in scene["entities"]]
    raw_requirements = {
        "must_show": copy.deepcopy(source_physical.get("must_show", source_plan.get("must_show", []))),
        "must_avoid": copy.deepcopy(source_physical.get("must_avoid", source_plan.get("must_avoid", []))),
    }
    contract = {
        "schema_version": CONTRACT_VERSION,
        "source_world_state_hash": world.world_state_hash(),
        "coordinate_conventions": {
            "entity_position": "world-space meters",
            "entity_rotation": "radians XYZ",
            "camera_position": "world-space meters",
            "camera_rotation": "degrees XYZ semantic look-at metadata as stored in camera trajectory points; resolve target with Blender to_track_quat instead of direct Euler assignment",
            "camera_roll": "degrees",
        },
        "scene": {
            "scene_id": scene["scene_id"],
            "environment_preset": scene["environment_preset"],
            "duration_seconds": scene["duration_seconds"],
            "fps": scene["fps"],
            "frame_count": scene["frame_count"],
            "entities": entities,
        },
        "physical_state_plan": {
            "events": copy.deepcopy(state_physical["events"]),
            "parameters": copy.deepcopy(state_physical.get("parameters", {})),
            "source_fields": copy.deepcopy(dict(source_physical)),
            # VideoCoCo's planner keeps these audit-facing fields separate
            # from implementation details. Preserve them when a Director
            # provider supplies them instead of burying them in source_fields.
            "semantic_keyframes": copy.deepcopy(source_physical.get("semantic_keyframes", source_plan.get("semantic_keyframes", []))),
            "transitions": copy.deepcopy(source_physical.get("transitions", source_plan.get("transitions", []))),
            "causal_constraints": copy.deepcopy(source_physical.get("causal_constraints", source_plan.get("causal_constraints", []))),
        },
        "character_trajectory_plan": copy.deepcopy(document["character_trajectory_plan"]),
        "object_trajectory_plan": copy.deepcopy(document["object_trajectory_plan"]),
        "camera_trajectory_plan": copy.deepcopy(document["camera_trajectory_plan"]),
        "requirements": raw_requirements,
        "preview_audit": {
            "keyframe_frame_map": {"K0": 0, "K1": 29, "K2": 59, "K3": 89, "K4": 119},
            "must_show": copy.deepcopy(raw_requirements["must_show"]),
            "must_avoid": copy.deepcopy(raw_requirements["must_avoid"]),
            "checks": [
                "all shared-world entities are visible in at least one assigned camera",
                "no camera is aimed at a wall, ceiling, or nearly black frame",
                "causal states are not visible before their authored transition",
            ],
        },
        "unresolved_fields": unresolved,
        "assumptions": [
            "The Blender Code Agent must preserve WorldState positions and camera responsibilities.",
            "Missing physical requirements remain unresolved until a planner or human supplies them.",
        ],
    }
    contract["contract_hash"] = _canonical_hash(contract)
    return contract
