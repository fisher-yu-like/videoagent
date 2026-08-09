"""VideoCoCo-style appearance-only edit prompt compiler.

The approved proxy is the source of motion, timing, blocking, occlusion, and
camera behavior.  This module only compiles appearance facts and never asks an
LLM to re-plan the scene.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .state import WorldState


APPEARANCE_PROFILE_SCHEMA_VERSION = "appearance-profile-1.0"
APPEARANCE_PROMPT_SCHEMA_VERSION = "appearance-only-prompt-1.0"
APPEARANCE_COMPILER_VERSION = "videoactagent-appearance-only-v1"


class AppearancePromptError(ValueError):
    """Raised when appearance input is missing, unbound, or contains motion edits."""


_MOTION_LANGUAGE = re.compile(
    r"\b(camera|trajectory|trajectories|camera\s+path|camera\s+move|camera\s+motion|"
    r"move|moves|moving|motion|walk|walking|pan|panning|tilt|tilting|zoom|zooming|"
    r"orbit|orbiting|dolly|tracking|cut|cuts|cutting|teleport|teleporting|frame|"
    r"framing|position|positions|reposition)\b",
    re.IGNORECASE,
)
_SAFE_RELATIVE = re.compile(r"^[^\\/:*?\"<>|]+(?:/[^\\/:*?\"<>|]+)*$")


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AppearancePromptError(f"{label} must be a non-empty trimmed string")
    return value


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise AppearancePromptError(
            f"{label} fields invalid: missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )


def _appearance_text(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if _MOTION_LANGUAGE.search(text):
        raise AppearancePromptError(
            f"{label} is not appearance-only; remove motion/camera instruction"
        )
    return text


def _validate_profile(profile: Mapping[str, Any], world: WorldState) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise AppearancePromptError("appearance profile must be an object")
    _exact(
        profile,
        {"schema_version", "scene_id", "subjects", "environment", "lighting", "quality", "must_avoid"},
        "appearance profile",
    )
    if profile.get("schema_version") != APPEARANCE_PROFILE_SCHEMA_VERSION:
        raise AppearancePromptError("appearance profile schema_version is invalid")
    if profile.get("scene_id") != world.scene_id:
        raise AppearancePromptError("appearance profile scene_id differs from WorldState")

    entities = {item["id"] for item in world.to_dict()["scene_plan"]["entities"]}
    raw_subjects = profile.get("subjects")
    if not isinstance(raw_subjects, list) or not raw_subjects:
        raise AppearancePromptError("appearance profile subjects must be a non-empty array")
    subjects: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_subjects):
        if not isinstance(raw, Mapping):
            raise AppearancePromptError(f"subjects[{index}] must be an object")
        _exact(raw, {"entity_id", "description"}, f"subjects[{index}]")
        entity_id = _strict_text(raw.get("entity_id"), f"subjects[{index}].entity_id")
        if entity_id not in entities:
            raise AppearancePromptError(f"subjects[{index}] references unknown entity {entity_id!r}")
        if entity_id in seen:
            raise AppearancePromptError(f"duplicate appearance subject {entity_id!r}")
        seen.add(entity_id)
        subjects.append({"entity_id": entity_id, "description": _appearance_text(raw.get("description"), f"subjects[{index}].description")})
    if seen != entities:
        raise AppearancePromptError(
            f"appearance profile must describe every WorldState entity; missing={sorted(entities - seen)}"
        )

    environment = _appearance_text(profile.get("environment"), "environment")
    lighting = _appearance_text(profile.get("lighting"), "lighting")
    quality = _appearance_text(profile.get("quality"), "quality")
    raw_avoid = profile.get("must_avoid")
    if not isinstance(raw_avoid, list) or not raw_avoid:
        raise AppearancePromptError("must_avoid must be a non-empty array")
    must_avoid = [_appearance_text(item, f"must_avoid[{index}]") for index, item in enumerate(raw_avoid)]
    return {
        "schema_version": APPEARANCE_PROFILE_SCHEMA_VERSION,
        "scene_id": world.scene_id,
        "subjects": sorted(subjects, key=lambda item: item["entity_id"]),
        "environment": environment,
        "lighting": lighting,
        "quality": quality,
        "must_avoid": must_avoid,
    }


def _validate_proxy_manifest(proxy_manifest: Mapping[str, Any], world: WorldState) -> dict[str, Any]:
    if not isinstance(proxy_manifest, Mapping):
        raise AppearancePromptError("proxy_manifest must be an object")
    required = {"schema_version", "world_state_hash", "frame_count", "fps", "resolution", "videos"}
    if not required.issubset(proxy_manifest):
        raise AppearancePromptError("proxy_manifest is missing required render fields")
    if proxy_manifest.get("schema_version") != "pipeline-v2-render-manifest-1.0":
        raise AppearancePromptError("proxy_manifest schema_version is invalid")
    if proxy_manifest.get("world_state_hash") != world.world_state_hash():
        raise AppearancePromptError("proxy_manifest WorldState hash differs from WorldState")
    if proxy_manifest.get("frame_count") != world.frame_count or proxy_manifest.get("fps") != world.fps:
        raise AppearancePromptError("proxy_manifest frame metadata differs from WorldState")
    if proxy_manifest.get("resolution") != [640, 360]:
        raise AppearancePromptError("proxy_manifest resolution must be [640, 360]")
    cameras = {camera["id"] for camera in world.to_dict()["camera_trajectory_plan"]["cameras"]}
    videos = proxy_manifest.get("videos")
    if not isinstance(videos, list) or {item.get("camera_id") for item in videos if isinstance(item, Mapping)} != cameras:
        raise AppearancePromptError("proxy_manifest videos must cover every WorldState camera exactly")
    normalized = {
        "schema_version": proxy_manifest["schema_version"],
        "world_state_hash": proxy_manifest["world_state_hash"],
        "frame_count": proxy_manifest["frame_count"],
        "fps": proxy_manifest["fps"],
        "resolution": copy.deepcopy(proxy_manifest["resolution"]),
    }
    normalized["videos"] = sorted(
        [copy.deepcopy(dict(item)) for item in videos],
        key=lambda item: str(item.get("camera_id", "")),
    )
    return normalized


def compile_appearance_prompt(
    *, world: WorldState, proxy_manifest: Mapping[str, Any], profile: Mapping[str, Any]
) -> dict[str, Any]:
    """Compile a deterministic, hash-bound appearance-only edit prompt."""
    if not isinstance(world, WorldState):
        raise AppearancePromptError("world must be a WorldState")
    normalized_profile = _validate_profile(profile, world)
    normalized_manifest = _validate_proxy_manifest(proxy_manifest, world)
    subject_lines = "\n".join(
        f"- {subject['entity_id']}: {subject['description']}"
        for subject in normalized_profile["subjects"]
    )
    avoid_lines = "\n".join(f"- {item}" for item in normalized_profile["must_avoid"])
    prompt = "\n\n".join(
        (
            "Appearance-only edit instruction",
            "Use the approved shared-world clay proxy as the sole source of blocking, timing, occlusion, subject identity, and camera movement. "
            "Preserve the complete event and all camera behavior exactly; change appearance only.",
            "Subjects and appearance",
            subject_lines,
            "Environment",
            normalized_profile["environment"],
            "Lighting",
            normalized_profile["lighting"],
            "Photoreal quality",
            normalized_profile["quality"],
            "Must preserve and must avoid",
            "Must preserve every entity, its identity, its timing, its blocking, and the shared-world composition. "
            "Must avoid:\n" + avoid_lines,
        )
    )
    prompt_bytes = prompt.encode("utf-8")
    return {
        "schema_version": APPEARANCE_PROMPT_SCHEMA_VERSION,
        "compiler_version": APPEARANCE_COMPILER_VERSION,
        "scene_id": world.scene_id,
        "source_world_state_hash": world.world_state_hash(),
        "source_proxy_manifest_hash": _sha256(_canonical(normalized_manifest)),
        "profile_sha256": _sha256(_canonical(normalized_profile)),
        "prompt": prompt,
        "prompt_sha256": _sha256(prompt_bytes),
        "profile": normalized_profile,
    }
