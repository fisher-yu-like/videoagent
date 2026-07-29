"""Inspect real local module inputs and outputs without calling a backend API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import imageio_ffmpeg


CHUNK_SIZE = 1024 * 1024
SUPPORTED_KINDS = {"json", "text", "video"}
_MISSING = object()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of *path*, reading at most 1 MiB per iteration."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_json(path: Path) -> dict[str, object]:
    """Parse one JSON object and expose its provenance-relevant fields."""

    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must contain one object")

    return {
        "schema_version": value.get("schema_version"),
        "status": value.get("status"),
        "evidence_source": value.get("evidence_source"),
        "evidence_type": value.get("evidence_type"),
        "claim_boundary": value.get("claim_boundary"),
        "evidence": value.get("evidence"),
        "inference": value.get("inference"),
        "top_level_keys": sorted(value),
    }


def inspect_text(path: Path) -> dict[str, object]:
    """Strictly decode one private text snapshot as UTF-8."""

    value = path.read_bytes().decode("utf-8", errors="strict")
    return {
        "encoding": "utf-8",
        "character_count": len(value),
        "line_count": len(value.splitlines()),
    }


def inspect_video(path: Path) -> dict[str, object]:
    """Decode a video with ffmpeg, count its frames, and return stream metadata."""

    frame_count, counted_seconds = imageio_ffmpeg.count_frames_and_secs(str(path))
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        metadata = next(reader)
    finally:
        _close_reader(reader)

    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
        raise ValueError("video decoded zero frames")
    if (
        isinstance(counted_seconds, bool)
        or not isinstance(counted_seconds, (int, float))
        or counted_seconds <= 0
    ):
        raise ValueError("video duration is not positive")
    if not isinstance(metadata, Mapping):
        raise ValueError("ffmpeg did not return stream metadata")
    size = metadata.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise ValueError("video stream size is unavailable")
    fps = metadata.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        raise ValueError("video stream fps is unavailable")
    stream_duration = metadata.get("duration")
    if (
        isinstance(stream_duration, bool)
        or not isinstance(stream_duration, (int, float))
        or stream_duration <= 0
    ):
        raise ValueError("video stream duration is unavailable")
    codec = metadata.get("codec")
    if not isinstance(codec, str) or not codec:
        raise ValueError("video stream codec is unavailable")

    return {
        "frame_count": int(frame_count),
        "duration_seconds": float(counted_seconds),
        "size": [int(size[0]), int(size[1])],
        "fps": float(fps),
        "codec": codec,
        "pixel_format": metadata.get("pix_fmt"),
        "rotation": metadata.get("rotate"),
        "stream_duration_seconds": float(stream_duration),
        "ffmpeg_version": metadata.get("ffmpeg_version"),
    }


def _close_reader(reader: object) -> None:
    generator_frame = getattr(reader, "gi_frame", None)
    process = (
        generator_frame.f_locals.get("process")
        if generator_frame is not None
        else None
    )
    try:
        reader.close()  # type: ignore[attr-defined]
    finally:
        if process is not None:
            for pipe_name in ("stdin", "stdout"):
                pipe = getattr(process, pipe_name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _lookup_json_field(value: Mapping[str, object], dotted_field: str) -> object:
    current: object = value
    for part in dotted_field.split("."):
        if not part or not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _safe_workspace_path(workspace: Path, declared_path: object) -> Path:
    if not isinstance(declared_path, str) or not declared_path:
        raise ValueError("record path must be a non-empty string")
    relative = Path(declared_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("record path must stay below workspace")

    root = workspace.resolve()
    resolved = (root / relative).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("record path resolves outside workspace") from exc
    return resolved


def _snapshot_file(source: Path, snapshot_root: Path) -> Path:
    snapshot = snapshot_root / f"{uuid4().hex}{source.suffix}"
    shutil.copyfile(source, snapshot)
    return snapshot


def _inspect_record(
    record: object, workspace: Path, snapshot_root: Path
) -> dict[str, object]:
    if not isinstance(record, Mapping):
        return {
            "status": "failed",
            "required": True,
            "error": "invalid_record",
        }

    path_text = record.get("path")
    kind = record.get("kind")
    required = record.get("required", True)
    if type(required) is not bool:
        return {
            "path": path_text,
            "kind": kind,
            "required": True,
            "declared_required": required,
            "status": "failed",
            "error": "invalid_record",
            "detail": "required must be a JSON boolean",
        }
    result: dict[str, object] = {
        "path": path_text,
        "kind": kind,
        "required": required,
    }

    try:
        path = _safe_workspace_path(workspace, path_text)
    except ValueError as exc:
        result.update(status="failed", error="unsafe_path", detail=str(exc))
        return result

    result["exists"] = path.is_file()
    if not path.is_file():
        if required:
            result.update(status="failed", error="required_missing")
        else:
            result.update(status="skipped", error="optional_missing")
        return result

    if kind not in SUPPORTED_KINDS:
        result.update(status="failed", error="unsupported_kind")
        return result

    try:
        snapshot = _snapshot_file(path, snapshot_root)
        size = snapshot.stat().st_size
        actual_hash = sha256_file(snapshot)
        result.update(bytes=size, sha256=actual_hash)

        declared_hash = record.get("sha256")
        if declared_hash is not None:
            result["declared_sha256"] = declared_hash
            hash_match = (
                isinstance(declared_hash, str)
                and len(declared_hash) == 64
                and actual_hash == declared_hash.lower()
            )
            result["hash_match"] = hash_match
            if not hash_match:
                result.update(status="failed", error="sha256_mismatch")
                return result

        if kind == "json":
            inspection = inspect_json(snapshot)
        elif kind == "text":
            inspection = inspect_text(snapshot)
        else:
            inspection = inspect_video(snapshot)
        result["inspection"] = inspection

        bindings = record.get("bindings")
        if bindings is not None:
            if kind != "json" or not isinstance(bindings, Mapping):
                result.update(status="failed", error="invalid_bindings")
                return result
            with snapshot.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, dict):
                result.update(status="failed", error="invalid_bindings")
                return result

            binding_results: list[dict[str, object]] = []
            all_bindings_match = True
            for field in sorted(bindings):
                if not isinstance(field, str):
                    result.update(status="failed", error="invalid_bindings")
                    return result
                expected = bindings[field]
                actual = _lookup_json_field(document, field)
                match = actual is not _MISSING and actual == expected
                all_bindings_match = all_bindings_match and match
                binding_results.append(
                    {
                        "field": field,
                        "expected": expected,
                        "actual": None if actual is _MISSING else actual,
                        "match": match,
                    }
                )
            result["binding_results"] = binding_results
            if not all_bindings_match:
                result.update(status="failed", error="binding_mismatch")
                return result

        result["status"] = "passed"
        return result
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
        StopIteration,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        result.update(
            status="failed",
            error=f"{kind}_inspection_failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return result


def _load_manifest(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("manifest must contain one JSON object")
    if type(value.get("schema_version")) is not str or value.get("schema_version") != "0.1":
        raise ValueError("manifest schema_version must be the string '0.1'")
    if not isinstance(value.get("modules"), list):
        raise ValueError("manifest modules must be a list")
    return value


def inspect_manifest(
    manifest_path: Path,
    workspace: Path,
    module_ids: Collection[str] | None = None,
) -> dict[str, object]:
    """Inspect selected manifest modules against files beneath *workspace*."""

    manifest_source = manifest_path.resolve()
    if not manifest_source.is_file():
        raise ValueError(f"manifest is not a file: {manifest_source}")
    with tempfile.TemporaryDirectory(prefix="videoactagent-module-io-") as directory:
        snapshot_root = Path(directory)
        manifest_snapshot = _snapshot_file(manifest_source, snapshot_root)
        manifest_bytes = manifest_snapshot.stat().st_size
        manifest_sha256 = sha256_file(manifest_snapshot)
        manifest = _load_manifest(manifest_snapshot)
        requested = None if module_ids is None else list(dict.fromkeys(module_ids))
        modules_by_id: dict[str, Mapping[str, object]] = {}
        errors: list[str] = []

        for raw_module in manifest["modules"]:  # type: ignore[index]
            if not isinstance(raw_module, Mapping):
                errors.append("invalid module record")
                continue
            module_id = raw_module.get("module_id")
            if not isinstance(module_id, str) or not module_id:
                errors.append("module_id must be a non-empty string")
                continue
            if module_id in modules_by_id:
                errors.append(f"duplicate module: {module_id}")
                continue
            modules_by_id[module_id] = raw_module

        selected_ids = list(modules_by_id) if requested is None else requested
        for module_id in selected_ids:
            if module_id not in modules_by_id:
                errors.append(f"unknown module: {module_id}")

        module_reports: list[dict[str, object]] = []
        for module_id in selected_ids:
            module = modules_by_id.get(module_id)
            if module is None:
                continue

            inputs_raw = module.get("inputs", [])
            outputs_raw = module.get("outputs", [])
            if not isinstance(inputs_raw, list) or not isinstance(outputs_raw, list):
                module_reports.append(
                    {
                        "module_id": module_id,
                        "status": "failed",
                        "inputs": [],
                        "outputs": [],
                        "errors": ["inputs and outputs must be lists"],
                    }
                )
                continue

            inputs = [
                _inspect_record(record, workspace, snapshot_root)
                for record in inputs_raw
            ]
            outputs = [
                _inspect_record(record, workspace, snapshot_root)
                for record in outputs_raw
            ]
            records = inputs + outputs
            module_ok = all(
                item.get("status") in {"passed", "skipped"} for item in records
            )
            module_reports.append(
                {
                    "module_id": module_id,
                    "status": "passed" if module_ok else "failed",
                    "inputs": inputs,
                    "outputs": outputs,
                }
            )

        ok = not errors and all(
            item["status"] == "passed" for item in module_reports
        )
        return {
            "schema_version": "0.1",
            "manifest": {
                "path": str(manifest_source),
                "bytes": manifest_bytes,
                "sha256": manifest_sha256,
                "schema_version": manifest["schema_version"],
            },
            "manifest_schema_version": manifest.get("schema_version"),
            "manifest_path": str(manifest_source),
            "workspace": str(workspace.resolve()),
            "ok": ok,
            "errors": errors,
            "modules": module_reports,
        }


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_output_path(path: Path, workspace: Path) -> Path:
    root = workspace.resolve()
    resolved = (
        path.resolve(strict=False)
        if path.is_absolute()
        else (root / path).resolve(strict=False)
    )
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output must stay below workspace") from exc
    return resolved


def _same_file_target(left: Path, right: Path) -> bool:
    if left == right:
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _selected_evidence_paths(
    report: Mapping[str, object], workspace: Path
) -> list[Path]:
    evidence_paths: list[Path] = []
    modules = report.get("modules")
    if not isinstance(modules, list):
        return evidence_paths
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        for section in ("inputs", "outputs"):
            records = module.get(section)
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                try:
                    evidence_paths.append(_safe_workspace_path(workspace, record.get("path")))
                except ValueError:
                    continue
    return evidence_paths


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect",))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.all and args.module:
        print("MODULE_IO_FAILED: choose --all or --module, not both", file=sys.stderr)
        return 2
    if not args.all and not args.module:
        print("MODULE_IO_FAILED: choose --all or at least one --module", file=sys.stderr)
        return 2

    selected = None if args.all else args.module
    try:
        output_path = _resolve_output_path(args.output, args.workspace)
        report = inspect_manifest(args.manifest, args.workspace, selected)
        protected_paths = [
            args.manifest.resolve(),
            *_selected_evidence_paths(report, args.workspace),
        ]
        if any(_same_file_target(output_path, protected) for protected in protected_paths):
            raise ValueError("output collision with manifest or selected evidence")
        _atomic_write_json(output_path, report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"MODULE_IO_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if report["ok"]:
        print(f"MODULE_IO_OK {output_path}")
        return 0
    print(f"MODULE_IO_FAILED {output_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
