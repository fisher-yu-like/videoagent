"""Unified humanoid bone names and fail-closed asset mapping validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping


REQUIRED_UNIFIED_BONES = (
    "root", "pelvis", "spine", "head",
    "upper_arm.L", "forearm.L", "hand.L",
    "upper_arm.R", "forearm.R", "hand.R",
    "upper_leg.L", "lower_leg.L", "foot.L",
    "upper_leg.R", "lower_leg.R", "foot.R",
)


class RigMappingError(ValueError):
    """Raised when a rig map cannot satisfy the unified motion contract."""


@dataclass(frozen=True)
class RigMap:
    schema_version: str
    asset_id: str
    unified_to_asset: Mapping[str, str]


def load_rig_map(path: Path | str) -> RigMap:
    source = Path(path).resolve(strict=True)
    document = json.loads(source.read_text(encoding="utf-8"))
    if document.get("schema_version") != "rig-map-1.0":
        raise RigMappingError("rig map schema_version is invalid")
    asset_id = document.get("asset_id")
    mapping = document.get("unified_to_asset")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise RigMappingError("rig map asset_id must be non-empty")
    if not isinstance(mapping, Mapping):
        raise RigMappingError("rig map unified_to_asset must be an object")
    missing = [name for name in REQUIRED_UNIFIED_BONES if not isinstance(mapping.get(name), str) or not mapping[name].strip()]
    if missing:
        raise RigMappingError(f"rig map is missing unified bones: {missing}")
    values = [str(mapping[name]) for name in REQUIRED_UNIFIED_BONES]
    duplicates: dict[str, list[str]] = {}
    for unified, asset_bone in zip(REQUIRED_UNIFIED_BONES, values):
        duplicates.setdefault(asset_bone, []).append(unified)
    invalid_duplicates = {
        asset_bone: names for asset_bone, names in duplicates.items()
        if len(names) > 1 and set(names) != {"root", "pelvis"}
    }
    if invalid_duplicates:
        raise RigMappingError(f"rig map has duplicate asset bones: {invalid_duplicates}")
    return RigMap(
        schema_version=str(document["schema_version"]),
        asset_id=asset_id,
        unified_to_asset={name: str(mapping[name]) for name in REQUIRED_UNIFIED_BONES},
    )
