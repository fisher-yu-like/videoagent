"""Run only the pinned VACE source preprocessor and a real CUDA transfer.

The command deliberately has no checkpoint argument and never constructs a
model.  A passing report proves source tensor compatibility only.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
from types import ModuleType
from typing import Any
import uuid

from videoactagent.vace_inputs import (
    SCHEMA_VERSION,
    VACE_COMMIT,
    VACE_PROCESSOR_BLOB_ID,
    ProvenanceError,
    sha256_file,
    verify_prepared_job,
)


PROCESSOR_KWARGS = {
    "downsample": (4, 16, 16),
    "min_area": 480 * 832,
    "max_area": 480 * 832,
    "min_fps": 16,
    "max_fps": 16,
    "zero_start": True,
    "seq_len": 32760,
    "keep_last": True,
}
SOURCE_VALIDATION_EVIDENCE = "pinned_vace_preprocessor_real_cuda"
EXPECTED_SOURCE_SHAPE = [3, 13, 480, 832]
EXPECTED_MASK_SHAPE = [1, 13, 480, 832]
EXPECTED_FRAME_IDS = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14]


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_parent(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _copy_verified_snapshot(
    source: Path, target: Path, expected_sha256: str
) -> str:
    source = Path(source).resolve()
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    snapshot_sha256 = sha256_file(target)
    if snapshot_sha256 != expected_sha256:
        target.unlink(missing_ok=True)
        raise ProvenanceError(
            "snapshot sha256 mismatch: "
            f"expected {expected_sha256}, got {snapshot_sha256}"
        )
    return snapshot_sha256


def build_processor(processor_class: type) -> Any:
    """Instantiate the upstream processor with the exact VACE 1.3B values."""

    return processor_class(**PROCESSOR_KWARGS)


def normalize_and_binarize_mask(mask: Any, torch_module: ModuleType) -> tuple[Any, Any]:
    """Reproduce the upstream normalization and its downstream binary gate."""

    normalized = torch_module.clamp((mask[:1] + 1) / 2, min=0, max=1)
    binary = torch_module.where(normalized > 0.5, 1.0, 0.0)
    return normalized, binary


def _best_effort_cuda_cleanup(torch_module: Any) -> None:
    try:
        torch_module.cuda.empty_cache()
    except Exception:
        # Cleanup happens after evidence publication. A driver cleanup error
        # must not turn a fully published success into a failed CLI result.
        pass


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"cannot read VACE job {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceError("VACE job must be a JSON object")
    return value


def _git_head(vace_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=vace_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    head = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not head:
        detail = (completed.stderr or completed.stdout).strip()
        raise ProvenanceError(f"cannot read VACE Git HEAD: {detail}")
    return head[0]


def _verify_processor_blob(vace_root: Path, expected_blob_id: str) -> str:
    processor_path = "vace/models/utils/preprocessor.py"
    committed = subprocess.run(
        [
            "git",
            "rev-parse",
            f"HEAD:{processor_path}",
        ],
        cwd=vace_root,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=15,
    )
    committed_output = committed.stdout.strip().splitlines()
    if committed.returncode != 0 or not committed_output:
        detail = (committed.stderr or committed.stdout).strip()
        raise ProvenanceError(f"cannot read pinned VACE processor blob: {detail}")
    committed_blob_id = committed_output[0]
    if committed_blob_id != expected_blob_id:
        raise ProvenanceError(
            "pinned VACE processor blob mismatch: "
            f"expected {expected_blob_id}, got {committed_blob_id}"
        )

    worktree = subprocess.run(
        [
            "git",
            "hash-object",
            f"--path={processor_path}",
            processor_path,
        ],
        cwd=vace_root,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=15,
    )
    worktree_output = worktree.stdout.strip().splitlines()
    if worktree.returncode != 0 or not worktree_output:
        detail = (worktree.stderr or worktree.stdout).strip()
        raise ProvenanceError(f"cannot hash VACE processor worktree blob: {detail}")
    worktree_blob_id = worktree_output[0]
    if worktree_blob_id != expected_blob_id:
        raise ProvenanceError(
            "VACE processor worktree blob mismatch: "
            f"expected {expected_blob_id}, got {worktree_blob_id}"
        )
    return committed_blob_id


def _load_processor_class(vace_root: Path) -> tuple[type, Path, str]:
    module_path = vace_root / "vace" / "models" / "utils" / "preprocessor.py"
    if not module_path.is_file():
        raise ProvenanceError(f"missing pinned VACE preprocessor: {module_path}")
    blob_id = _verify_processor_blob(vace_root, VACE_PROCESSOR_BLOB_ID)
    spec = importlib.util.spec_from_file_location(
        "videoactagent_pinned_vace_preprocessor", module_path
    )
    if spec is None or spec.loader is None:
        raise ProvenanceError(f"cannot import pinned VACE preprocessor: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    processor_class = getattr(module, "VaceVideoProcessor", None)
    if processor_class is None:
        raise ProvenanceError("pinned checkout has no VaceVideoProcessor")
    return processor_class, module_path, blob_id


def _verified_file(path: Path, expected_sha256: str, label: str) -> Path:
    path = path.resolve()
    if not path.is_file():
        raise ProvenanceError(f"missing {label}: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ProvenanceError(f"{label} sha256 mismatch")
    return path


def _relative_path(root: Path, recorded: str, label: str) -> Path:
    relative = Path(recorded)
    if relative.is_absolute():
        raise ProvenanceError(f"{label} path must be relative")
    root = root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ProvenanceError(f"{label} path escapes its root")
    return resolved


def _find_source_bundle(job_path: Path, job: dict[str, Any]) -> Path:
    source_bundle = job.get("source_bundle")
    if not isinstance(source_bundle, dict):
        raise ProvenanceError("job source_bundle is missing")
    recorded = source_bundle.get("path")
    expected_sha = source_bundle.get("sha256")
    if not isinstance(recorded, str) or not isinstance(expected_sha, str):
        raise ProvenanceError("job source_bundle path/hash is missing")

    candidates = [_relative_path(job_path.parent, recorded, "source bundle")]
    roots = [Path.cwd().resolve(), *job_path.resolve().parents]
    for root in roots:
        candidates.append(root / "runs" / "stage2_control_bridge" / "control_bundle.json")
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and sha256_file(candidate) == expected_sha:
            return candidate
    raise ProvenanceError("cannot locate the hash-matching real Stage 2 source bundle")


def _resolve_inputs(job_path: Path, job: dict[str, Any]) -> tuple[Path, Path, Path]:
    mapping = job.get("mapping")
    source = job.get("source")
    mask_record = job.get("mask")
    if not isinstance(mapping, dict) or not isinstance(source, dict):
        raise ProvenanceError("job source mapping is missing")
    if not isinstance(mask_record, dict):
        raise ProvenanceError("job mask record is missing")
    controls = source.get("controls")
    if not isinstance(controls, dict):
        raise ProvenanceError("job source controls are missing")
    proxy_record = controls.get("proxy_video")
    if not isinstance(proxy_record, dict):
        raise ProvenanceError("job proxy_video record is missing")

    bundle_path = _find_source_bundle(job_path, job)
    verified_job = verify_prepared_job(job_path, bundle_path)
    if verified_job != job:
        raise ProvenanceError("in-memory job differs from the verified job file")
    proxy_text = mapping.get("src_video")
    if not isinstance(proxy_text, str):
        raise ProvenanceError("job src_video mapping is missing")
    parts = PurePosixPath(proxy_text).parts
    if not parts or parts[0] != "source":
        raise ProvenanceError("job src_video must use the source/ path base")
    proxy_path = _relative_path(
        bundle_path.parent, str(Path(*parts[1:])), "src_video"
    )
    proxy_path = _verified_file(
        proxy_path, str(proxy_record.get("sha256")), "src_video"
    )

    mask_text = mapping.get("src_mask")
    if not isinstance(mask_text, str) or mask_text != mask_record.get("path"):
        raise ProvenanceError("job src_mask mapping/record mismatch")
    mask_path = _relative_path(job_path.parent, mask_text, "src_mask")
    mask_path = _verified_file(mask_path, str(mask_record.get("sha256")), "src_mask")
    return bundle_path, proxy_path, mask_path


def _resolve_source_control(
    bundle_path: Path, job: dict[str, Any], control_name: str
) -> Path:
    record = job.get("source", {}).get("controls", {}).get(control_name)
    if not isinstance(record, dict):
        raise ProvenanceError(f"job {control_name} record is missing")
    recorded = record.get("path")
    expected_sha256 = record.get("sha256")
    if not isinstance(recorded, str) or not isinstance(expected_sha256, str):
        raise ProvenanceError(f"job {control_name} path/hash is missing")
    parts = PurePosixPath(recorded).parts
    if not parts or parts[0] != "source":
        raise ProvenanceError(f"job {control_name} must use the source/ path base")
    path = _relative_path(
        bundle_path.parent, str(Path(*parts[1:])), control_name
    )
    return _verified_file(path, expected_sha256, control_name)


def _tensor_record(tensor: Any) -> dict[str, Any]:
    return {
        "shape": [int(value) for value in tensor.shape],
        "dtype": str(tensor.dtype),
        "device": str(tensor.device),
        "finite": bool(tensor.isfinite().all().item()),
        "value_range": [float(tensor.min().item()), float(tensor.max().item())],
    }


def _nvidia_driver() -> str | None:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader",
                "--id=0",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().splitlines()
    return value[0].strip() if completed.returncode == 0 and value else None


def _validate_exact_frame_ids(actual: Any) -> None:
    if actual != EXPECTED_FRAME_IDS:
        raise ProvenanceError(
            "frame_ids mismatch: "
            f"expected {EXPECTED_FRAME_IDS}, actual {actual}"
        )


def _paths_alias(left: Path, right: Path) -> bool:
    left = Path(left).resolve()
    right = Path(right).resolve()
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _validate_output_paths(
    job_path: Path,
    output_path: Path,
    validated_job_output: Path,
    inputs: dict[str, Path],
) -> None:
    job_path = Path(job_path).resolve()
    job_root = job_path.parent
    outputs = {
        "report": Path(output_path).resolve(),
        "validated": Path(validated_job_output).resolve(),
    }
    for label, path in outputs.items():
        if path == job_root or not path.is_relative_to(job_root):
            raise ProvenanceError(
                f"{label} output path escapes the VACE job directory"
            )
    if _paths_alias(outputs["report"], outputs["validated"]):
        raise ProvenanceError("report and validated output paths must be distinct")
    for output_label, output in outputs.items():
        for input_label, input_path in inputs.items():
            if _paths_alias(output, input_path):
                raise ProvenanceError(
                    f"{output_label} output aliases {input_label} input"
                )


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": report.get("status"),
        "evidence_source": report.get("evidence_source"),
    }
    if report.get("status") == "passed":
        summary.update(
            {
                "claim_boundary": report.get("claim_boundary"),
                "job_sha256": report.get("job", {}).get("sha256"),
                "vace_commit": report.get("vace", {}).get("git_head"),
                "processor_blob_id": report.get("processor", {}).get(
                    "module_blob_id"
                ),
                "source_shape": report.get("tensors", {})
                .get("source", {})
                .get("shape"),
                "mask_shape": report.get("tensors", {})
                .get("mask", {})
                .get("shape"),
                "frame_ids": report.get("sampling", {}).get("frame_ids"),
                "cuda_available": report.get("cuda", {}).get("available"),
                "inference": report.get("inference"),
            }
        )
    else:
        summary.update(
            {
                "error_type": report.get("error_type"),
                "error": report.get("error"),
                "inference": report.get("inference"),
            }
        )
    return summary


def _report_binding(
    report_path: Path, job_root: Path, report: dict[str, Any]
) -> dict[str, Any]:
    report_path = Path(report_path).resolve()
    job_root = Path(job_root).resolve()
    return {
        "path": report_path.relative_to(job_root).as_posix(),
        "bytes": report_path.stat().st_size,
        "sha256": sha256_file(report_path),
        "summary": _report_summary(report),
    }


def _validated_job_with_report(
    job: dict[str, Any], report_binding: dict[str, Any], passed: bool
) -> dict[str, Any]:
    validated = copy.deepcopy(job)
    validated["evidence"] = {
        "source_validation_passed": passed,
        "source_validation_report": report_binding,
        "inference_success": False,
    }
    return validated


def _failure_report(exc: Exception, elapsed_seconds: float) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "evidence_source": SOURCE_VALIDATION_EVIDENCE,
        "command": "vace-source-preprocess-probe",
        "claim_boundary": "source_preprocessing_and_cuda_transfer_only",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "timing": {"elapsed_seconds": elapsed_seconds},
        "inference": {"model_constructed": False, "checkpoint_loaded": False},
    }


def _publish_failed_validation(
    job: dict[str, Any],
    report_path: Path,
    validated_job_output: Path,
    failure: dict[str, Any],
    job_root: Path,
) -> None:
    atomic_write_json(report_path, failure)
    binding = _report_binding(report_path, job_root, failure)
    atomic_write_json(
        validated_job_output,
        _validated_job_with_report(job, binding, passed=False),
    )


def _mark_validation_started(
    job: dict[str, Any],
    report_path: Path,
    validated_job_output: Path,
    job_root: Path,
) -> None:
    pending = copy.deepcopy(job)
    pending["evidence"] = {
        "source_validation_passed": False,
        "source_validation_attempt": {
            "status": "running",
            "report_path": Path(report_path)
            .resolve()
            .relative_to(Path(job_root).resolve())
            .as_posix(),
        },
        "inference_success": False,
    }
    try:
        atomic_write_json(validated_job_output, pending)
    except Exception as exc:
        validated_job_output = Path(validated_job_output).resolve()
        if validated_job_output.is_file():
            archived_bytes = validated_job_output.stat().st_size
            archived_sha256 = sha256_file(validated_job_output)
            stale_path = validated_job_output.with_name(
                f"{validated_job_output.name}.{uuid.uuid4().hex}.stale"
            )
            validated_job_output.replace(stale_path)
            _fsync_parent(stale_path)
            atomic_write_json(
                stale_path.with_name(stale_path.name + ".failure.json"),
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "stale",
                    "reason": str(exc),
                    "canonical_path": validated_job_output.name,
                    "archived_path": stale_path.name,
                    "archived_bytes": archived_bytes,
                    "archived_sha256": archived_sha256,
                    "source_validation_passed": False,
                },
            )
        raise


def _validated_job_from_live_report(
    job: dict[str, Any],
    report: dict[str, Any],
    report_binding: dict[str, Any],
) -> dict[str, Any]:
    expected_video = job["source"]["controls"]["proxy_video"]["sha256"]
    expected_mask = job["mask"]["sha256"]
    checks = {
        "status": report.get("status") == "passed",
        "commit": report["vace"]["git_head"] == VACE_COMMIT,
        "processor": report["processor"]["module_blob_id"]
        == VACE_PROCESSOR_BLOB_ID,
        "video_snapshot": report["inputs"]["src_video"]["snapshot_sha256"]
        == expected_video,
        "mask_snapshot": report["inputs"]["src_mask"]["snapshot_sha256"]
        == expected_mask,
        "source_shape": report["tensors"]["source"]["shape"]
        == EXPECTED_SOURCE_SHAPE,
        "mask_shape": report["tensors"]["mask"]["shape"]
        == EXPECTED_SOURCE_SHAPE,
        "binary_shape": report["tensors"]["normalized_binary_mask"]["shape"]
        == EXPECTED_MASK_SHAPE,
        "source_finite": report["tensors"]["source"]["finite"] is True,
        "mask_finite": report["tensors"]["mask"]["finite"] is True,
        "binary_finite": report["tensors"]["normalized_binary_mask"]["finite"]
        is True,
        "binary_values": report["tensors"]["normalized_binary_mask"][
            "unique_values"
        ]
        == [1.0],
        "spatial": report["sampling"]["spatial_size"] == [480, 832],
        "cuda": report["cuda"]["available"] is True,
        "inference": report["inference"]
        == {"model_constructed": False, "checkpoint_loaded": False},
    }
    _validate_exact_frame_ids(report["sampling"]["frame_ids"])
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ProvenanceError("live source validation failed: " + ", ".join(failed))
    return _validated_job_with_report(job, report_binding, passed=True)


def run_probe(
    job_path: Path,
    vace_root: Path,
    output_path: Path,
    validated_job_output: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    job_path = Path(job_path).resolve()
    vace_root = Path(vace_root).resolve()
    output_path = Path(output_path).resolve()
    validated_job_output = Path(validated_job_output).resolve()
    job = _load_json_object(job_path)
    if job.get("schema_version") != SCHEMA_VERSION:
        raise ProvenanceError("VACE job schema mismatch")
    if job.get("vace", {}).get("commit") != VACE_COMMIT:
        raise ProvenanceError("VACE job commit mismatch")
    if job.get("evidence") != {
        "source_validation_passed": False,
        "inference_success": False,
    }:
        raise ProvenanceError("VACE job evidence flags must be false before probing")
    bundle_path, proxy_path, mask_path = _resolve_inputs(job_path, job)
    first_frame_path = _resolve_source_control(bundle_path, job, "first_frame")
    last_frame_path = _resolve_source_control(bundle_path, job, "last_frame")
    _validate_output_paths(
        job_path,
        output_path,
        validated_job_output,
        {
            "job": job_path,
            "source_bundle": bundle_path,
            "src_video": proxy_path,
            "src_mask": mask_path,
            "first_frame": first_frame_path,
            "last_frame": last_frame_path,
        },
    )
    _mark_validation_started(
        job, output_path, validated_job_output, job_path.parent
    )

    try:
        git_head = _git_head(vace_root)
        if git_head != VACE_COMMIT:
            raise ProvenanceError(
                f"VACE checkout mismatch: expected {VACE_COMMIT}, got {git_head}"
            )
        processor_class, module_path, processor_blob_id = _load_processor_class(
            vace_root
        )
        processor = build_processor(processor_class)

        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; source validation cannot pass")
        device = torch.device("cuda:0")
        torch.cuda.set_device(device)
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(device)
        source_cpu = mask_cpu = source_cuda = mask_cuda = normalized_mask = binary_mask = None
        with tempfile.TemporaryDirectory(prefix="videoactagent-vace-probe-") as tmp:
            snapshot_root = Path(tmp)
            video_snapshot = snapshot_root / "src_video.mp4"
            mask_snapshot = snapshot_root / "src_mask.mp4"
            video_sha = _copy_verified_snapshot(
                proxy_path,
                video_snapshot,
                job["source"]["controls"]["proxy_video"]["sha256"],
            )
            mask_sha = _copy_verified_snapshot(
                mask_path, mask_snapshot, job["mask"]["sha256"]
            )
            source_cpu, mask_cpu, frame_ids, spatial_size, returned_fps = (
                processor.load_video_pair(str(video_snapshot), str(mask_snapshot))
            )
            source_cuda = source_cpu.to(device)
            mask_cuda = mask_cpu.to(device)
            normalized_mask, binary_mask = normalize_and_binarize_mask(
                mask_cuda, torch
            )
            torch.cuda.synchronize(device)
            source_record = _tensor_record(source_cuda)
            mask_record = _tensor_record(mask_cuda)
            binary_record = _tensor_record(binary_mask)
            binary_record["unique_values"] = [
                float(value)
                for value in torch.unique(binary_mask).detach().cpu().tolist()
            ]
            driver_version = _nvidia_driver()
            if not driver_version:
                raise RuntimeError("cannot record the NVIDIA driver version")
            properties = torch.cuda.get_device_properties(device)
            report = {
                "schema_version": SCHEMA_VERSION,
                "status": "passed",
                "evidence_source": SOURCE_VALIDATION_EVIDENCE,
                "command": "vace-source-preprocess-probe",
                "claim_boundary": "source_preprocessing_and_cuda_transfer_only",
                "job": {"path": str(job_path), "sha256": sha256_file(job_path)},
                "vace": {
                    "root": str(vace_root),
                    "expected_commit": VACE_COMMIT,
                    "git_head": git_head,
                },
                "inputs": {
                    "source_bundle": {
                        "original_path": str(bundle_path),
                        "sha256": sha256_file(bundle_path),
                    },
                    "src_video": {
                        "original_path": str(proxy_path),
                        "snapshot_sha256": video_sha,
                    },
                    "src_mask": {
                        "original_path": str(mask_path),
                        "snapshot_sha256": mask_sha,
                    },
                },
                "processor": {
                    "class": "VaceVideoProcessor",
                    "module_path": str(module_path),
                    "module_blob_id": processor_blob_id,
                    "module_file_sha256": sha256_file(module_path),
                    "kwargs": {
                        key: list(value) if isinstance(value, tuple) else value
                        for key, value in PROCESSOR_KWARGS.items()
                    },
                },
                "sampling": {
                    "frame_ids": [int(value) for value in frame_ids],
                    "spatial_size": [int(value) for value in spatial_size],
                    "fps": float(returned_fps),
                },
                "tensors": {
                    "source": source_record,
                    "mask": mask_record,
                    "normalized_binary_mask": binary_record,
                },
                "cuda": {
                    "available": True,
                    "device": str(device),
                    "device_name": torch.cuda.get_device_name(device),
                    "device_total_memory_bytes": int(properties.total_memory),
                    "driver_version": driver_version,
                    "torch_version": torch.__version__,
                    "torch_cuda_version": torch.version.cuda,
                    "source_transfer_count": 1,
                    "mask_transfer_count": 1,
                    "peak_allocated_bytes": int(
                        torch.cuda.max_memory_allocated(device)
                    ),
                    "peak_reserved_bytes": int(torch.cuda.max_memory_reserved(device)),
                },
                "timing": {"elapsed_seconds": time.perf_counter() - started},
                "inference": {
                    "model_constructed": False,
                    "checkpoint_loaded": False,
                },
            }
            atomic_write_json(output_path, report)
            report_binding = _report_binding(output_path, job_path.parent, report)
            validated_job = _validated_job_from_live_report(
                job, report, report_binding
            )
            atomic_write_json(validated_job_output, validated_job)
            return report
    except Exception as exc:
        failure = _failure_report(exc, time.perf_counter() - started)
        try:
            _publish_failed_validation(
                job,
                output_path,
                validated_job_output,
                failure,
                job_path.parent,
            )
        except Exception as publish_exc:
            raise RuntimeError(
                f"{exc}; failed to publish source-validation failure: {publish_exc}"
            ) from exc
        raise
    finally:
        if "torch" in locals():
            source_cpu = mask_cpu = source_cuda = mask_cuda = None
            normalized_mask = binary_mask = None
            _best_effort_cuda_cleanup(torch)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate real Stage 6 media with pinned VACE preprocessing and CUDA; "
            "this command cannot load checkpoints or run inference"
        )
    )
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--vace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validated-job-output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.perf_counter()
    try:
        run_probe(
            args.job,
            args.vace_root,
            args.output,
            args.validated_job_output,
        )
    except Exception as exc:
        failure = _failure_report(exc, time.perf_counter() - started)
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        "VACE_SOURCE_VALIDATION_OK",
        json.dumps(
            {
                "output": str(Path(args.output).resolve()),
                "sha256": sha256_file(Path(args.output).resolve()),
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
