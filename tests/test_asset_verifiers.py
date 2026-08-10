from __future__ import annotations

import json
from pathlib import Path

from pipeline_v2.proxy_verifier import verify_asset_catalog_materialization, verify_shared_world_identity


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _registry() -> dict:
    return {"assets": [{
        "asset_id": "person_a", "kind": "character", "source_kind": "asset_catalog_glb",
        "catalog_asset_id": "human_male_v1", "source_asset_sha256": "a" * 64,
    }]}


def _asset_log() -> list[dict]:
    return [{
        "asset_id": "person_a", "kind": "character", "catalog_asset_id": "human_male_v1",
        "source_asset_sha256": "a" * 64, "rig_map_sha256": "b" * 64,
        "parts": 1, "shared_world_instance": True,
    }]


def test_asset_catalog_materialization_requires_every_character(tmp_path: Path) -> None:
    report = verify_asset_catalog_materialization(
        registry_path=_write(tmp_path / "registry.json", _registry()),
        asset_log_path=_write(tmp_path / "asset_log.json", _asset_log()),
        proxy_style="asset_humanoid",
    )
    assert report["status"] == "passed"


def test_shared_world_identity_fails_when_camera_hashes_differ(tmp_path: Path) -> None:
    cameras = [{"camera_id": str(index), "shared_asset_hashes": {"human_male_v1": "wrong"}} for index in range(4)]
    report = verify_shared_world_identity(
        asset_log_path=_write(tmp_path / "asset_log.json", _asset_log()),
        camera_log_path=_write(tmp_path / "camera_log.json", cameras),
        expected_camera_count=4,
    )
    assert report["status"] == "failed"


def test_shared_world_identity_passes_for_four_matching_cameras(tmp_path: Path) -> None:
    cameras = [{"camera_id": str(index), "shared_asset_hashes": {"human_male_v1": "a" * 64}} for index in range(4)]
    report = verify_shared_world_identity(
        asset_log_path=_write(tmp_path / "asset_log.json", _asset_log()),
        camera_log_path=_write(tmp_path / "camera_log.json", cameras),
        expected_camera_count=4,
    )
    assert report["status"] == "passed"
