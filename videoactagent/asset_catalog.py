"""Versioned, fail-closed character asset catalog for Blender Proxy runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Mapping


CATALOG_SCHEMA_VERSION = "character-asset-catalog-1.0"


class AssetCatalogError(RuntimeError):
    """Base error for invalid or incomplete asset catalogs."""


class AssetMissingError(AssetCatalogError):
    """Raised when a declared asset file is unavailable."""


class AssetIntegrityError(AssetCatalogError):
    """Raised when a declared asset hash or metadata is inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    kind: str
    model_path: Path
    profile_path: Path
    rig_map_path: Path
    license_path: Path
    source_sha256: str


class AssetCatalog:
    def __init__(self, *, repository_root: Path, assets: Mapping[str, AssetSpec]) -> None:
        self.repository_root = repository_root.resolve()
        self.assets = dict(assets)

    def require(self, asset_id: str) -> AssetSpec:
        try:
            return self.assets[str(asset_id)]
        except KeyError as exc:
            raise AssetMissingError(f"asset_id is not registered: {asset_id}") from exc

    def materialize(self, asset_id: str, run_assets_dir: Path) -> Path:
        spec = self.require(asset_id)
        for field_name in ("model_path", "profile_path", "rig_map_path", "license_path"):
            path = getattr(spec, field_name)
            if not path.is_file():
                raise AssetMissingError(f"{asset_id} {field_name} is missing: {path}")
        actual_hash = sha256_file(spec.model_path)
        if actual_hash != spec.source_sha256:
            raise AssetIntegrityError(
                f"{asset_id} model SHA-256 mismatch: expected {spec.source_sha256}, got {actual_hash}"
            )
        destination = Path(run_assets_dir).resolve() / spec.asset_id
        destination.mkdir(parents=True, exist_ok=True)
        for source, filename in (
            (spec.model_path, "model.glb"),
            (spec.profile_path, "profile.json"),
            (spec.rig_map_path, "rig_map.json"),
            (spec.license_path, "license.txt"),
        ):
            shutil.copy2(source, destination / filename)
        return destination / "model.glb"


def _repo_root_for_catalog(catalog_path: Path) -> Path:
    # Repository catalogs live at <repo>/assets/characters/catalog.json.
    return catalog_path.resolve().parents[2]


def _resolve_root_relative(repository_root: Path, value: object, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AssetCatalogError(f"catalog field {field_name} must be a non-empty string")
    path = (repository_root / value).resolve()
    try:
        path.relative_to(repository_root)
    except ValueError as exc:
        raise AssetCatalogError(f"catalog path escapes repository root: {value}") from exc
    return path


def load_catalog(catalog_path: Path | str, *, repository_root: Path | None = None) -> AssetCatalog:
    path = Path(catalog_path).resolve(strict=True)
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise AssetCatalogError("character asset catalog schema_version is invalid")
    entries = document.get("assets")
    if not isinstance(entries, list):
        raise AssetCatalogError("character asset catalog assets must be a list")
    root = (repository_root or _repo_root_for_catalog(path)).resolve()
    assets: dict[str, AssetSpec] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AssetCatalogError("character asset entry must be an object")
        asset_id = entry.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip() or asset_id in assets:
            raise AssetCatalogError(f"asset_id is missing or duplicated: {asset_id}")
        kind = entry.get("kind")
        if kind != "rigged_humanoid":
            raise AssetCatalogError(f"{asset_id} kind must be rigged_humanoid")
        source_sha256 = str(entry.get("source_sha256", "")).lower()
        if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
            raise AssetIntegrityError(f"{asset_id} source_sha256 must be 64 lowercase hex characters")
        assets[asset_id] = AssetSpec(
            asset_id=asset_id,
            kind=kind,
            model_path=_resolve_root_relative(root, entry.get("model_path"), "model_path"),
            profile_path=_resolve_root_relative(root, entry.get("profile_path"), "profile_path"),
            rig_map_path=_resolve_root_relative(root, entry.get("rig_map_path"), "rig_map_path"),
            license_path=_resolve_root_relative(root, entry.get("license_path"), "license_path"),
            source_sha256=source_sha256,
        )
    return AssetCatalog(repository_root=root, assets=assets)
