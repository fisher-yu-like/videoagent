from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from videoactagent.asset_catalog import AssetMissingError, load_catalog


def _write_catalog(root: Path, *, missing_model: bool = False) -> Path:
    catalog_dir = root / "assets" / "characters"
    catalog_dir.mkdir(parents=True)
    model = root / "assets" / "male.glb"
    model.parent.mkdir(parents=True, exist_ok=True)
    if not missing_model:
        model.write_bytes(b"real catalog fixture bytes")
    for relative in ("profile.json", "rig_map.json", "license.txt"):
        (catalog_dir / relative).write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(model.read_bytes()).hexdigest() if model.is_file() else "0" * 64
    catalog = catalog_dir / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": "character-asset-catalog-1.0", "assets": [{
        "asset_id": "human_male_v1", "kind": "rigged_humanoid", "model_path": "assets/male.glb",
        "profile_path": "assets/characters/profile.json", "rig_map_path": "assets/characters/rig_map.json",
        "license_path": "assets/characters/license.txt", "source_sha256": digest,
    }]}, indent=2), encoding="utf-8")
    return catalog


def test_catalog_loads_versioned_asset_and_reports_sha256(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    asset = catalog.require("human_male_v1")
    assert asset.kind == "rigged_humanoid"
    assert len(asset.source_sha256) == 64


def test_catalog_fails_closed_for_missing_model(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path, missing_model=True))
    with pytest.raises(AssetMissingError):
        catalog.materialize("human_male_v1", tmp_path / "run")


def test_materialize_copies_model_and_sidecars(tmp_path: Path) -> None:
    catalog = load_catalog(_write_catalog(tmp_path))
    model = catalog.materialize("human_male_v1", tmp_path / "run")
    assert model.name == "model.glb"
    assert model.is_file()
    assert (model.parent / "profile.json").is_file()
    assert (model.parent / "rig_map.json").is_file()
    assert (model.parent / "license.txt").is_file()
