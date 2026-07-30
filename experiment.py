"""Prepare and inspect the explicitly gated, offline full-chain experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any

from videoactagent.full_chain import ExperimentMatrix, compile_matrix, write_prepared_matrix
from videoactagent.vace_full_chain import export_vace_job
from videoactagent.whole_story_gateway import prepare_api_job


DEFAULT_OUTPUT = Path("runs/work/full_chain_24_v1")
_API_BACKENDS = {"kling", "seedance"}
_ACTIONS = ("prepare", "canary-status", "remainder-status")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _job_record(job: Any, job_root: Path) -> dict[str, Any]:
    prepared = job_root / "prepared"
    experiment_root = job_root.parents[1]
    return {
        "job_id": f"{job.story_id}__{job.backend}",
        "story_id": job.story_id,
        "backend": job.backend,
        "release": job.release,
        "release_parent": job_root.relative_to(experiment_root).as_posix(),
        "prepared_path": prepared.relative_to(experiment_root).as_posix(),
        "hashes": _tree_hashes(prepared),
    }


def _summary(matrix: ExperimentMatrix, matrix_snapshot: dict[str, Any], root: Path) -> dict[str, Any]:
    jobs_root = root / "jobs"
    records = [_job_record(job, jobs_root / f"{job.story_id}__{job.backend}") for job in matrix.jobs]
    return {
        "schema_version": "1.0",
        "status": "prepared",
        "source_config_sha256": matrix.config.config_sha256,
        "matrix": {
            "path": "matrix.json",
            "sha256": _sha256(root / "matrix.json"),
            "snapshot_sha256": matrix_snapshot["snapshot_sha256"],
        },
        "job_count": len(records),
        "api_job_count": sum(item["backend"] in _API_BACKENDS for item in records),
        "vace_job_count": sum(item["backend"] == "vace" for item in records),
        "canary_job_count": sum(item["release"] == "canary" for item in records),
        "canary_story_ids": list(matrix.canary_story_ids),
        "attempts": {
            "submissions": 0,
            "status_queries": 0,
            "downloads": 0,
            "vace_inferences": 0,
        },
        "offline": {"api_called": False, "gpu_called": False, "network_called": False},
        "jobs": records,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_experiment(config_path: Path | str, output_dir: Path | str = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Build all 24 offline job directories and publish them only when complete."""
    output = Path(output_dir).resolve()
    if output.exists():
        raise ValueError(f"prepared experiment output already exists: {output}")
    matrix = compile_matrix(config_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        matrix_snapshot = write_prepared_matrix(matrix, staging)
        jobs_root = staging / "jobs"
        for job in matrix.jobs:
            job_root = jobs_root / f"{job.story_id}__{job.backend}"
            prepared = job_root / "prepared"
            if job.backend in _API_BACKENDS:
                prepare_api_job(job, prepared)
            elif job.backend == "vace":
                export_vace_job(job, prepared)
            else:  # compile_matrix currently makes this unreachable; keep the gate explicit.
                raise ValueError(f"unsupported prepared backend: {job.backend}")
        summary = _summary(matrix, matrix_snapshot, staging)
        _write_json(staging / "summary.json", summary)
        os.replace(staging, output)
        staging = None
        return summary
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _offline_status(config_path: Path | str, release: str, output_dir: Path | str = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Read prepared evidence only; this function never invokes a backend."""
    matrix = compile_matrix(config_path)
    output = Path(output_dir).resolve()
    selected = [job for job in matrix.jobs if job.release == release]
    summary_path = output / "summary.json"
    if not summary_path.is_file():
        return {"status": "not_prepared", "release": release, "job_count": len(selected), "prepared_job_count": 0}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    jobs = summary.get("jobs") if isinstance(summary, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("prepared experiment summary is malformed")
    prepared_ids = {item.get("job_id") for item in jobs if isinstance(item, dict)}
    expected_ids = {f"{job.story_id}__{job.backend}" for job in selected}
    return {
        "status": "prepared" if expected_ids <= prepared_ids else "incomplete",
        "release": release,
        "job_count": len(selected),
        "prepared_job_count": len(expected_ids & prepared_ids),
        "offline": {"api_called": False, "gpu_called": False, "network_called": False},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("command", choices=_ACTIONS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_experiment(args.config)
        else:
            result = _offline_status(args.config, "canary" if args.command == "canary-status" else "remainder")
    except (OSError, ValueError) as exc:
        print(f"EXPERIMENT_ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
