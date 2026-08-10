from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoactagent.rig_mapping import REQUIRED_UNIFIED_BONES, RigMappingError, load_rig_map


def test_unified_mapping_requires_root_hands_and_feet() -> None:
    mapping = load_rig_map(Path("assets/characters/human_male_v1/rig_map.json"))
    assert set(REQUIRED_UNIFIED_BONES) <= set(mapping.unified_to_asset)


def test_mapping_rejects_duplicate_asset_bones(tmp_path: Path) -> None:
    mapping = {name: f"bone_{index}" for index, name in enumerate(REQUIRED_UNIFIED_BONES)}
    mapping["hand.R"] = mapping["hand.L"]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps({"schema_version": "rig-map-1.0", "asset_id": "x", "unified_to_asset": mapping}), encoding="utf-8")
    with pytest.raises(RigMappingError):
        load_rig_map(path)


def test_mapping_allows_intentional_root_pelvis_alias() -> None:
    mapping = load_rig_map(Path("assets/characters/human_male_v1/rig_map.json"))
    assert mapping.unified_to_asset["root"] == mapping.unified_to_asset["pelvis"]
