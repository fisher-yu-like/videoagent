"""Prepare the fixed 24-call trajectory experiment matrix without submitting it."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import sys

from videoactagent.trajectory_closed_loop import (
    SourceSnapshot,
    _exact_keys,
    _guard_snapshots,
    _require_sha,
    _safe_workspace_path,
    _same_file,
    _snapshot,
    _verify_session_frames,
    json_bytes,
    load_strict_bytes,
    write_immutable,
)
from videoactagent.backends.jd import extract_status, extract_task_id, extract_video_urls
from videoactagent.stage3_audit import _validate_gateway_evidence
from videoactagent.trajectory_eval import evaluate_files


SCHEMA_VERSION = "0.1"
SCENES = (
    ("station_platform", "s01"),
    ("city_crosswalk", "s01"),
    ("forest_path", "s01"),
    ("studio_room", "s01"),
)
BACKENDS = ("kling", "seedance")
CONDITIONS = (
    "manual_text_baseline",
    "trajectory_compiled",
    "trajectory_compiled_feedback_revision",
)
_PROMPT_BY_CONDITION = {
    "manual_text_baseline": "plain",
    "trajectory_compiled": "trajectory_compiled",
    "trajectory_compiled_feedback_revision": "trajectory_compiled_feedback_revision",
}
_PILOT_KEYS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "backend",
        "manual_reviewed",
        "source_artifacts",
    }
)
_ARTIFACT_NAMES = frozenset(
    {
        "result_video",
        "evaluation_report",
        "api_audit",
        "trajectory",
        "session_manifest",
        "annotation",
    }
)
_ARTIFACT_KEYS = frozenset({"path", "sha256"})
_AUDIT_BASE_KEYS = frozenset(
    {
        "schema_version",
        "audited_at_utc",
        "evidence_source",
        "network_called",
        "backend",
        "task_id",
        "query_count",
        "status_sequence",
        "evidence_files",
        "checks",
        "result",
        "billing_interpreted",
        "quality_evaluated",
    }
)
_AUDIT_CHECK_KEYS = frozenset(
    {
        "task_ids_match",
        "terminal_status_success",
        "request_endpoint_model_valid",
        "state_base_url_official_host",
        "terminal_video_urls_match_state",
        "download_record_matches_file",
        "blender_media_readable",
    }
)
_AUDIT_RESULT_KEYS = frozenset({"path", "bytes", "sha256", "blender_media"})
_AUDIT_MEDIA_KEYS = frozenset(
    {"blender_version", "duration_seconds", "fps", "frames", "height", "width"}
)
_EVIDENCE_RECORD_KEYS = frozenset({"path", "bytes", "sha256"})


def _cost(value: object, backend: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{backend} cost must be a finite non-negative number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{backend} cost must be a finite non-negative number") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1_000_000.0:
        raise ValueError(f"{backend} cost must be a finite non-negative number")
    return result


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{label} must be a finite positive number")
    return result


def _bounded_count(value: object, label: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 10**9
    ):
        raise ValueError(f"{label} must be a bounded integer")
    return value


def _validate_api_audit(
    backend: str,
    audit_snapshot: SourceSnapshot,
    video_snapshot: SourceSnapshot,
    workspace: Path,
) -> list[SourceSnapshot]:
    audit = load_strict_bytes(audit_snapshot.contents, f"{backend} API audit")
    expected_keys = _AUDIT_BASE_KEYS | ({"seedance"} if backend == "kling" else set())
    _exact_keys(audit, frozenset(expected_keys), f"{backend} API audit")
    if audit["schema_version"] != 1 or audit["backend"] != backend:
        raise ValueError(f"{backend} API audit identity mismatch")
    if (
        not isinstance(audit["audited_at_utc"], str)
        or not audit["audited_at_utc"]
        or audit["evidence_source"]
        != "persisted_gateway_records_internal_consistency"
        or audit["network_called"] is not False
        or audit["billing_interpreted"] is not False
        or audit["quality_evaluated"] is not False
    ):
        raise ValueError(f"{backend} API audit boundary fields are invalid")
    task_id = audit["task_id"]
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(f"{backend} API audit task_id is invalid")
    query_count = _bounded_count(audit["query_count"], f"{backend} query_count", minimum=1)
    statuses = audit["status_sequence"]
    if (
        not isinstance(statuses, list)
        or len(statuses) != query_count
        or not all(isinstance(status, str) and status for status in statuses)
        or statuses[-1] != "success"
    ):
        raise ValueError(f"{backend} API audit status sequence is invalid")
    checks = audit["checks"]
    if not isinstance(checks, Mapping):
        raise ValueError(f"{backend} API audit checks must be an object")
    _exact_keys(checks, _AUDIT_CHECK_KEYS, f"{backend} API audit checks")
    if any(value is not True for value in checks.values()):
        if checks.get("terminal_status_success") is not True:
            raise ValueError(f"{backend} API audit does not prove terminal success")
        raise ValueError(f"{backend} API audit contains a failed or unknown check")
    if backend == "kling":
        seedance = audit["seedance"]
        if not isinstance(seedance, Mapping):
            raise ValueError("kling audit seedance boundary must be an object")
        _exact_keys(seedance, frozenset({"outcome", "source"}), "kling seedance boundary")
        if seedance != {
            "outcome": "unknown",
            "source": "project_context_not_part_of_run",
        }:
            raise ValueError("kling audit seedance boundary is invalid")

    result = audit["result"]
    if not isinstance(result, Mapping):
        raise ValueError(f"{backend} API audit result must be an object")
    _exact_keys(result, _AUDIT_RESULT_KEYS, f"{backend} API audit result")
    result_name = result["path"]
    if not isinstance(result_name, str) or Path(result_name).name != result_name:
        raise ValueError(f"{backend} API audit result path is invalid")
    result_path = (audit_snapshot.path.parent / result_name).resolve()
    if not _same_file(result_path, video_snapshot.path):
        raise ValueError(f"{backend} API audit result is not the reviewed video")
    if (
        _bounded_count(result["bytes"], f"{backend} result bytes", minimum=1)
        != len(video_snapshot.contents)
        or _require_sha(result["sha256"], f"{backend} result sha256")
        != video_snapshot.sha256
    ):
        raise ValueError(f"{backend} API audit/result hash binding mismatch")
    media = result["blender_media"]
    if not isinstance(media, Mapping):
        raise ValueError(f"{backend} blender_media must be an object")
    _exact_keys(media, _AUDIT_MEDIA_KEYS, f"{backend} blender_media")
    if not isinstance(media["blender_version"], str) or not media["blender_version"]:
        raise ValueError(f"{backend} blender_version is invalid")
    for field in ("duration_seconds", "fps"):
        _positive_number(media[field], f"{backend} media {field}")
    for field in ("frames", "height", "width"):
        _bounded_count(media[field], f"{backend} media {field}", minimum=1)

    evidence = audit["evidence_files"]
    if not isinstance(evidence, Mapping):
        raise ValueError(f"{backend} evidence_files must be an object")
    fixed = {"metadata.json", "request.json", "response.json", "state.json", "download.json"}
    query_names = sorted(name for name in evidence if isinstance(name, str) and name.startswith("query_") and name.endswith(".json"))
    if set(evidence) != fixed | set(query_names) or len(query_names) != query_count:
        raise ValueError(f"{backend} API audit evidence_files set is invalid")
    evidence_snapshots: list[SourceSnapshot] = []
    documents: dict[str, dict[str, object]] = {}
    for name in sorted(evidence):
        record = evidence[name]
        if not isinstance(name, str) or Path(name).name != name or not isinstance(record, Mapping):
            raise ValueError(f"{backend} API evidence record is invalid")
        _exact_keys(record, _EVIDENCE_RECORD_KEYS, f"{backend} evidence {name}")
        if record["path"] != name:
            raise ValueError(f"{backend} evidence path/name mismatch")
        path = _safe_workspace_path(
            audit_snapshot.path.parent / name, workspace, f"{backend} evidence {name}"
        )
        snapshot = _snapshot(path, f"{backend} evidence {name}")
        if (
            _bounded_count(record["bytes"], f"{backend} evidence bytes", minimum=1)
            != len(snapshot.contents)
            or _require_sha(record["sha256"], f"{backend} evidence sha256")
            != snapshot.sha256
        ):
            raise ValueError(f"{backend} evidence {name} hash/byte mismatch")
        documents[name] = load_strict_bytes(snapshot.contents, f"{backend} evidence {name}")
        evidence_snapshots.append(snapshot)

    metadata = documents["metadata.json"]
    request = documents["request.json"]
    response = documents["response.json"]
    state = documents["state.json"]
    download = documents["download.json"]
    if metadata.get("backend") != backend or request.get("method") != "POST":
        raise ValueError(f"{backend} persisted metadata/request mismatch")
    _validate_gateway_evidence(request, state, backend)
    if extract_task_id(response) != task_id or state.get("task_id") != task_id:
        raise ValueError(f"{backend} persisted task IDs mismatch")
    queries = [documents[name] for name in query_names]
    if any(query.get("task_id") != task_id for query in queries):
        raise ValueError(f"{backend} query task IDs mismatch")
    actual_statuses = [extract_status(query) for query in queries]
    if actual_statuses != statuses or state.get("status") != "success":
        raise ValueError(f"{backend} persisted terminal/status sequence mismatch")
    terminal_urls = [
        {"id": item_id, "url": url} for item_id, url in extract_video_urls(queries[-1])
    ]
    if state.get("video_urls") != terminal_urls:
        raise ValueError(f"{backend} persisted terminal video URLs mismatch")
    if (
        download.get("path") != result_name
        or download.get("bytes") != len(video_snapshot.contents)
        or download.get("sha256") != video_snapshot.sha256
    ):
        raise ValueError(f"{backend} download/video binding mismatch")
    return evidence_snapshots


def _validate_pilot_report(
    backend: str, report_path: Path, workspace: Path
) -> dict[str, object]:
    path = _safe_workspace_path(report_path, workspace, f"{backend} pilot report")
    review_snapshot = _snapshot(path, f"{backend} pilot report")
    report = load_strict_bytes(review_snapshot.contents, f"{backend} pilot report")
    _exact_keys(report, _PILOT_KEYS, f"{backend} pilot report")
    if report["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{backend} pilot report schema mismatch")
    if report["evidence_type"] != "real_api_trajectory_pilot_manual_review":
        raise ValueError(f"{backend} pilot report evidence_type mismatch")
    if report["backend"] != backend:
        raise ValueError(f"{backend} pilot report backend mismatch")
    if type(report["manual_reviewed"]) is not bool:
        raise ValueError(f"{backend} pilot manual_reviewed must be a JSON boolean")
    artifacts = report["source_artifacts"]
    if not isinstance(artifacts, Mapping):
        raise ValueError(f"{backend} source_artifacts must be an object")
    _exact_keys(artifacts, _ARTIFACT_NAMES, f"{backend} source_artifacts")
    snapshots: dict[str, SourceSnapshot] = {}
    for name in sorted(_ARTIFACT_NAMES):
        record = artifacts[name]
        if not isinstance(record, Mapping):
            raise ValueError(f"{backend} {name} must be an object")
        _exact_keys(record, _ARTIFACT_KEYS, f"{backend} {name}")
        declared = record["path"]
        if not isinstance(declared, str) or not declared or Path(declared).is_absolute():
            raise ValueError(f"{backend} {name} path must be workspace-relative")
        artifact = _safe_workspace_path(Path(declared), workspace, f"{backend} {name}")
        snapshot = _snapshot(artifact, f"{backend} {name}")
        expected = _require_sha(record["sha256"], f"{backend} {name} sha256")
        if snapshot.sha256 != expected:
            raise ValueError(f"{backend} {name} hash mismatch")
        snapshots[name] = snapshot
    if snapshots["result_video"].path.suffix.lower() != ".mp4":
        raise ValueError(f"{backend} result_video must be an MP4")
    evaluation = load_strict_bytes(
        snapshots["evaluation_report"].contents, f"{backend} evaluation report"
    )
    provenance = evaluation.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError(f"{backend} evaluation provenance is missing")
    if provenance.get("video_sha256") != snapshots["result_video"].sha256:
        raise ValueError(f"{backend} evaluation/video hash binding mismatch")
    frame_snapshots = _verify_session_frames(
        snapshots["session_manifest"], provenance.get("frame_sha256"), workspace
    )
    recomputed = evaluate_files(
        trajectory_path=snapshots["trajectory"].path,
        video_path=snapshots["result_video"].path,
        session_manifest_path=snapshots["session_manifest"].path,
        annotation_path=snapshots["annotation"].path,
    )
    if recomputed != evaluation:
        raise ValueError(
            f"{backend} evaluation does not exactly match fresh Task 7 recomputation"
        )
    evidence_snapshots = _validate_api_audit(
        backend,
        snapshots["api_audit"],
        snapshots["result_video"],
        workspace,
    )
    _guard_snapshots(
        [review_snapshot, *snapshots.values(), *frame_snapshots, *evidence_snapshots]
    )
    return {
        "report_path": path.relative_to(Path(workspace).resolve()).as_posix(),
        "report_sha256": review_snapshot.sha256,
        "manual_reviewed": report["manual_reviewed"],
        "source_sha256": {
            name: snapshot.sha256 for name, snapshot in snapshots.items()
        },
        "_source_snapshots": (
            review_snapshot,
            *snapshots.values(),
            *frame_snapshots,
            *evidence_snapshots,
        ),
    }


def _job_command_if_ready(
    *,
    root: Path,
    bundle_relative: str,
    backend: str,
    scene_id: str,
    shot_id: str,
    condition: str,
    run_root: str,
) -> tuple[str, list[str] | None, str | None, SourceSnapshot | None]:
    bundle_path = _safe_workspace_path(
        Path(bundle_relative), root, f"{condition} bundle"
    )
    if not bundle_path.is_file():
        return "preparation_required", None, "bundle_missing", None
    bundle_snapshot = _snapshot(bundle_path, f"{condition} bundle")
    bundle = load_strict_bytes(bundle_snapshot.contents, f"{condition} bundle")
    if bundle.get("backend") != backend:
        raise ValueError(f"{condition} bundle backend mismatch")
    if bundle.get("scene_id") != scene_id:
        raise ValueError(f"{condition} bundle scene mismatch")
    from videoactagent import jd_smoke

    choice = _PROMPT_BY_CONDITION[condition]
    if choice == "trajectory_compiled":
        shot, _, _ = jd_smoke.validate_trajectory_bundle(bundle, backend, shot_id)
    elif choice == "trajectory_compiled_feedback_revision":
        validator = getattr(jd_smoke, "validate_revision_trajectory_bundle", None)
        if validator is None:
            return (
                "preparation_required",
                None,
                "feedback_bundle_validator_unavailable",
                bundle_snapshot,
            )
        validated = validator(bundle, backend, shot_id)
        if not isinstance(validated, tuple) or not validated:
            raise ValueError("feedback bundle validator returned no shot")
        shot = validated[0]
    else:
        shot = jd_smoke.find_shot(bundle, shot_id)
    prompts = shot.get("prompts")
    if not isinstance(prompts, Mapping) or not isinstance(prompts.get(choice), str) or not prompts[choice].strip():
        raise ValueError(f"{condition} bundle does not contain prompt {choice!r}")
    duration = shot.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError(f"{condition} bundle duration is invalid")
    _positive_number(duration, f"{condition} bundle duration")
    argv = [
        sys.executable,
        "-m",
        "videoactagent.jd_smoke",
        f"submit-{backend}",
        "--bundle",
        bundle_relative,
        "--shot",
        shot_id,
        "--prompt",
        choice,
        "--run-root",
        run_root,
    ]
    parsed = jd_smoke.parse_args(argv[3:])
    if parsed.command != f"submit-{backend}" or parsed.prompt != choice:
        raise ValueError(f"{condition} submission command parser mismatch")
    _guard_snapshots([bundle_snapshot])
    return "ready", argv, None, bundle_snapshot


def build_experiment_manifest(
    pilot_reports: Mapping[str, Path],
    *,
    workspace: Path,
    per_call_cost: Mapping[str, float | None] | None = None,
    currency: str = "CNY",
) -> dict[str, object]:
    """Build a deterministic plan; this function has no submission code path."""

    root = Path(workspace).resolve()
    unknown_reports = set(pilot_reports) - set(BACKENDS)
    if unknown_reports:
        raise ValueError(f"unknown pilot report backend: {sorted(unknown_reports)}")
    if not isinstance(currency, str) or not currency or len(currency) > 16:
        raise ValueError("currency must be a short non-empty string")
    costs_input = dict(per_call_cost or {})
    unknown_costs = set(costs_input) - set(BACKENDS)
    if unknown_costs:
        raise ValueError(f"unknown cost backend: {sorted(unknown_costs)}")
    costs = {backend: _cost(costs_input.get(backend), backend) for backend in BACKENDS}

    reviewed: dict[str, dict[str, object]] = {}
    review_snapshots: list[SourceSnapshot] = []
    for backend in BACKENDS:
        if backend in pilot_reports:
            validated = _validate_pilot_report(
                backend, Path(pilot_reports[backend]), root
            )
            private_snapshots = validated.pop("_source_snapshots")
            assert isinstance(private_snapshots, tuple)
            review_snapshots.extend(private_snapshots)
            reviewed[backend] = validated
    reviews_verified = set(reviewed) == set(BACKENDS) and all(
        item["manual_reviewed"] is True for item in reviewed.values()
    )
    reasons: list[str] = []
    for backend in BACKENDS:
        if backend not in reviewed:
            reasons.append(f"missing_{backend}_pilot_report")
        elif reviewed[backend]["manual_reviewed"] is not True:
            reasons.append(f"{backend}_pilot_not_manually_reviewed")

    jobs: list[dict[str, object]] = []
    bundle_snapshots: list[SourceSnapshot] = []
    for scene_id, shot_id in SCENES:
        for backend in BACKENDS:
            for condition in CONDITIONS:
                job_id = f"{scene_id}__{backend}__{condition}"
                bundle = (
                    Path("runs")
                    / "trajectory_experiment"
                    / "prepared"
                    / scene_id
                    / backend
                    / condition
                    / "bundle.json"
                ).as_posix()
                run_root = (
                    Path("runs") / "trajectory_experiment" / "real" / scene_id / condition
                ).as_posix()
                status, argv, blocked_reason, bundle_snapshot = _job_command_if_ready(
                    root=root,
                    bundle_relative=bundle,
                    backend=backend,
                    scene_id=scene_id,
                    shot_id=shot_id,
                    condition=condition,
                    run_root=run_root,
                )
                if bundle_snapshot is not None:
                    bundle_snapshots.append(bundle_snapshot)
                jobs.append(
                    {
                        "job_id": job_id,
                        "scene_id": scene_id,
                        "shot_id": shot_id,
                        "backend": backend,
                        "condition": condition,
                        "planned_calls": 1,
                        "estimated_cost": costs[backend],
                        "status": status,
                        "blocked_reason": blocked_reason,
                        "bundle_path": bundle,
                        "submission_command_argv": argv,
                        "requires_separate_manual_execution": True,
                    }
                )
    planned = len(SCENES) * len(BACKENDS) * len(CONDITIONS)
    assert len(jobs) == planned == 24
    ready_job_count = sum(job["status"] == "ready" for job in jobs)
    submission_allowed = reviews_verified and ready_job_count == planned
    total = (
        sum(
            float(job["estimated_cost"])
            for job in jobs
            if job["estimated_cost"] is not None
        )
        if all(costs[backend] is not None for backend in BACKENDS)
        else None
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "offline_trajectory_experiment_plan",
        "planning_only": True,
        "network_called": False,
        "submitted": False,
        "submission_allowed": submission_allowed,
        "planned_call_count": planned,
        "fixed_dimensions": {
            "scenes": [scene for scene, _ in SCENES],
            "backends": list(BACKENDS),
            "conditions": list(CONDITIONS),
        },
        "pilot_gate": {
            "required_backends": list(BACKENDS),
            "manual_review_required": True,
            "reviews_verified": reviews_verified,
            "reasons": reasons,
            "report_sha256": {
                backend: item["report_sha256"] for backend, item in reviewed.items()
            },
            "verified_reports": reviewed,
        },
        "bundle_gate": {
            "required_job_count": planned,
            "ready_job_count": ready_job_count,
            "all_bundles_ready": ready_job_count == planned,
            "blocked_job_ids": [
                job["job_id"] for job in jobs if job["status"] != "ready"
            ],
        },
        "cost_estimate": {
            "currency": currency,
            "per_call": costs,
            "total": total,
            "source": "caller_supplied" if any(value is not None for value in costs.values()) else "not_provided",
        },
        "jobs": jobs,
    }
    _guard_snapshots([*review_snapshots, *bundle_snapshots])
    return manifest


def _pilot_argument(value: str) -> tuple[str, Path]:
    backend, separator, raw_path = value.partition("=")
    if not separator or backend not in BACKENDS or not raw_path:
        raise argparse.ArgumentTypeError("pilot report must be kling=PATH or seedance=PATH")
    return backend, Path(raw_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--pilot-report", action="append", type=_pilot_argument, default=[])
    prepare.add_argument("--kling-cost-per-call")
    prepare.add_argument("--seedance-cost-per-call")
    prepare.add_argument("--currency", default="CNY")
    prepare.add_argument("--workspace", type=Path, default=Path("."))
    prepare.add_argument("--output", type=Path, required=True)
    return parser


def _parse_cost(value: str | None, backend: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{backend} cost must be numeric") from exc


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reports: dict[str, Path] = {}
        for backend, path in args.pilot_report:
            if backend in reports:
                raise ValueError(f"duplicate pilot report backend: {backend}")
            reports[backend] = path
        output = _safe_workspace_path(args.output, args.workspace, "output")
        protected = [
            _safe_workspace_path(path, args.workspace, f"{backend} pilot report")
            for backend, path in reports.items()
        ]
        if any(_same_file(output, source) for source in protected):
            raise ValueError("output collides with an input")
        manifest = build_experiment_manifest(
            reports,
            workspace=args.workspace,
            per_call_cost={
                "kling": _parse_cost(args.kling_cost_per_call, "kling"),
                "seedance": _parse_cost(args.seedance_cost_per_call, "seedance"),
            },
            currency=args.currency,
        )
        contents = json_bytes(manifest)
        write_immutable(output, contents)
    except (FileExistsError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"TRAJECTORY_EXPERIMENT_PREPARE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        "TRAJECTORY_EXPERIMENT_PREPARE_OK "
        f"output={output} calls=24 submission_allowed={str(manifest['submission_allowed']).lower()} "
        f"network_called=false sha256={hashlib.sha256(contents).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
