"""Prepare one hash-bound, offline trajectory prompt revision.

This module contains no API client.  It consumes the Task 7 evaluator report,
re-verifies its source artifacts, and emits at most one allow-listed revision.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from uuid import uuid4

from videoactagent.trajectory import TrajectoryInstruction, TrajectoryTrack
from videoactagent.trajectory_eval import evaluate_files


SCHEMA_VERSION = "0.1"
ALLOWED_OPERATIONS = (
    "strengthen_direction",
    "split_time_segments",
    "reduce_amplitude",
    "strengthen_screen_direction",
    "simplify_orbit_to_truck",
    "preserve_matched_control",
)
_VERDICT_OPERATION = {
    "direction_mismatch": "strengthen_direction",
    "timing_mismatch": "split_time_segments",
    "amplitude_excess": "reduce_amplitude",
    "screen_direction_mismatch": "strengthen_screen_direction",
    "orbit_mismatch": "simplify_orbit_to_truck",
    "matched": "preserve_matched_control",
}
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "evidence_type",
        "coordinate_space",
        "scene_id",
        "shot_id",
        "track_id",
        "target",
        "provenance",
        "metrics",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "video_sha256",
        "trajectory_file_sha256",
        "trajectory_canonical_sha256",
        "session_manifest_sha256",
        "annotation_sha256",
        "frame_sha256",
    }
)
_METRICS_KEYS = frozenset(
    {"time_base", "occlusion_policy", "samples", "dtw", "aggregate"}
)
_AGGREGATE_KEYS = frozenset(
    {
        "requested_sample_count",
        "compared_sample_count",
        "occluded_sample_count",
        "mean_distance",
        "mean_normalized_distance",
        "max_distance",
        "endpoint_t",
        "endpoint_status",
        "endpoint_reason",
        "endpoint_error",
        "normalized_endpoint_error",
        "direction_cosine",
        "direction_match",
        "requested_arrival_t",
        "observed_arrival_t",
        "arrival_error",
        "arrival_tolerance",
        "dtw_normalized_cost",
    }
)
_THRESHOLDS = {
    "arrival_error_max": 0.15,
    "normalized_endpoint_error_max": 0.20,
    "dtw_normalized_cost_max": 0.20,
}
_PROMPT_CLAUSES = {
    "strengthen_direction": "Keep the requested motion direction unambiguous throughout the shot.",
    "split_time_segments": "Execute the trajectory in explicit beginning, middle, and ending time segments.",
    "reduce_amplitude": "Reduce the motion amplitude while preserving the requested path direction.",
    "strengthen_screen_direction": "Maintain the requested left-to-right or right-to-left screen direction without reversal.",
    "simplify_orbit_to_truck": "Replace the difficult orbit with a simple lateral truck in the same screen direction.",
}


@dataclass(frozen=True)
class OperationSelection(Sequence[str]):
    """Stable, deduplicated operations plus per-control audit decisions."""

    operations: tuple[str, ...]
    decisions: tuple[dict[str, object], ...]

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        return self.operations[index]

    def __len__(self) -> int:
        return len(self.operations)

    def __iter__(self) -> Iterator[str]:
        return iter(self.operations)


@dataclass(frozen=True)
class SourceSnapshot:
    path: Path
    contents: bytes
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_strict_bytes(contents: bytes, label: str) -> dict[str, object]:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    value = json.loads(
        text,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    _reject_unsafe_numbers(value, label)
    return value


def load_strict_object(path: Path, label: str) -> dict[str, object]:
    return load_strict_bytes(Path(path).read_bytes(), label)


def _snapshot(path: Path, label: str) -> SourceSnapshot:
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    contents = path.read_bytes()
    return SourceSnapshot(path, contents, hashlib.sha256(contents).hexdigest())


def _guard_snapshots(snapshots: Iterable[SourceSnapshot]) -> None:
    for snapshot in snapshots:
        if not snapshot.path.is_file() or snapshot.path.read_bytes() != snapshot.contents:
            raise ValueError(f"source changed during preparation: {snapshot.path}")


def _reject_unsafe_numbers(value: object, label: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except OverflowError as exc:
            raise ValueError(f"{label} contains a non-finite or oversized number") from exc
        if not math.isfinite(numeric):
            raise ValueError(f"{label} contains a non-finite or oversized number")
        return
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_unsafe_numbers(child, label)
        return
    if isinstance(value, list):
        for child in value:
            _reject_unsafe_numbers(child, label)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _require_sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _finite_number(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    nullable: bool = False,
) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} is below its allowed range")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} is above its allowed range")
    return number


def _count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**9:
        raise ValueError(f"{label} must be a bounded non-negative integer")
    return value


def _safe_workspace_path(path: Path, workspace: Path, label: str) -> Path:
    root = Path(workspace).resolve()
    candidate = Path(path)
    resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside workspace") from exc
    return resolved


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _verify_session_frames(
    session_manifest: SourceSnapshot,
    expected_hashes: object,
    workspace: Path,
) -> list[SourceSnapshot]:
    session = load_strict_bytes(session_manifest.contents, "session manifest")
    frames = session.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("session manifest frames must be a non-empty list")
    expected = expected_hashes
    if not isinstance(expected, list) or len(expected) != len(frames):
        raise ValueError("session frame provenance count mismatch")
    session_root = session_manifest.path.parent.resolve()
    verified: list[SourceSnapshot] = []
    for index, (record, report_sha) in enumerate(zip(frames, expected)):
        if not isinstance(record, Mapping):
            raise ValueError(f"session frame {index} must be an object")
        declared_path = record.get("path")
        if (
            not isinstance(declared_path, str)
            or not declared_path
            or Path(declared_path).is_absolute()
        ):
            raise ValueError(f"session frame {index} path must be relative")
        frame = _safe_workspace_path(
            session_root / declared_path, workspace, f"session frame {index}"
        )
        try:
            frame.relative_to(session_root)
        except ValueError as exc:
            raise ValueError(f"session frame {index} escapes its session directory") from exc
        recorded_sha = _require_sha(record.get("sha256"), f"session frame {index} sha256")
        report_digest = _require_sha(report_sha, f"evaluation frame {index} sha256")
        if recorded_sha != report_digest:
            raise ValueError(f"session frame {index} provenance hash mismatch")
        frame_snapshot = _snapshot(frame, f"session frame {index}")
        if frame_snapshot.sha256 != recorded_sha:
            raise ValueError(f"session frame {index} hash mismatch")
        verified.append(frame_snapshot)
    return verified


def operations_for_control_verdicts(
    controls: Iterable[Mapping[str, object]],
) -> OperationSelection:
    """Map known verdicts to the exact allow-list in deterministic order."""

    by_id: dict[str, dict[str, object]] = {}
    for control in controls:
        if not isinstance(control, Mapping) or set(control) != {"control_id", "verdict"}:
            raise ValueError("each control verdict must contain only control_id and verdict")
        control_id = control["control_id"]
        verdict = control["verdict"]
        if not isinstance(control_id, str) or not control_id:
            raise ValueError("control_id must be a non-empty string")
        if verdict not in _VERDICT_OPERATION:
            raise ValueError(f"unknown verdict: {verdict!r}")
        prior = by_id.get(control_id)
        if prior is not None:
            if prior["verdict"] != verdict:
                raise ValueError(f"conflicting verdicts for control {control_id!r}")
            continue
        operation = _VERDICT_OPERATION[str(verdict)]
        by_id[control_id] = {
            "control_id": control_id,
            "verdict": verdict,
            "operations": [operation],
        }
    if not by_id:
        raise ValueError("at least one known control verdict is required")
    decisions = tuple(by_id[key] for key in sorted(by_id))
    requested = {
        operation
        for decision in decisions
        for operation in decision["operations"]  # type: ignore[union-attr]
    }
    ordered = tuple(operation for operation in ALLOWED_OPERATIONS if operation in requested)
    return OperationSelection(ordered, decisions)


def _validate_report(report: Mapping[str, object]) -> dict[str, object]:
    _exact_keys(report, _REPORT_KEYS, "evaluation report")
    if report["schema_version"] != SCHEMA_VERSION:
        raise ValueError("evaluation report schema_version mismatch")
    if report["evidence_type"] != "manual_visual_trajectory_evaluation":
        raise ValueError("evaluation report is not manual visual evidence")
    if report["coordinate_space"] != "normalized_0_1_top_left":
        raise ValueError("evaluation report coordinate_space mismatch")
    for identity in ("scene_id", "shot_id", "track_id"):
        if not isinstance(report[identity], str) or not report[identity]:
            raise ValueError(f"evaluation report {identity} must be non-empty")
    target = report["target"]
    if not isinstance(target, Mapping):
        raise ValueError("evaluation report target must be an object")
    _exact_keys(target, frozenset({"type", "id"}), "evaluation target")
    if target["type"] not in {"camera", "actor", "anchor"}:
        raise ValueError("evaluation target type is unknown")
    if not isinstance(target["id"], str) or not target["id"]:
        raise ValueError("evaluation target id must be non-empty")

    provenance = report["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError("evaluation provenance must be an object")
    _exact_keys(provenance, _PROVENANCE_KEYS, "evaluation provenance")
    for field in _PROVENANCE_KEYS - {"frame_sha256"}:
        _require_sha(provenance[field], f"evaluation provenance {field}")
    frames = provenance["frame_sha256"]
    if not isinstance(frames, list) or not frames:
        raise ValueError("evaluation provenance frame_sha256 must be non-empty")
    for index, digest in enumerate(frames):
        _require_sha(digest, f"evaluation frame_sha256[{index}]")

    metrics = report["metrics"]
    if not isinstance(metrics, Mapping):
        raise ValueError("evaluation metrics must be an object")
    _exact_keys(metrics, _METRICS_KEYS, "evaluation metrics")
    aggregate = metrics["aggregate"]
    if not isinstance(aggregate, Mapping):
        raise ValueError("evaluation aggregate metrics must be an object")
    _exact_keys(aggregate, _AGGREGATE_KEYS, "evaluation aggregate metrics")
    requested = _count(aggregate["requested_sample_count"], "requested_sample_count")
    compared = _count(aggregate["compared_sample_count"], "compared_sample_count")
    occluded = _count(aggregate["occluded_sample_count"], "occluded_sample_count")
    if requested < 2 or compared < 2 or compared > requested or occluded > requested:
        raise ValueError("evaluation sample counts are inconsistent")
    time_base = metrics["time_base"]
    if not isinstance(time_base, Mapping):
        raise ValueError("evaluation time_base must be an object")
    _exact_keys(
        time_base, frozenset({"type", "sample_count"}), "evaluation time_base"
    )
    if (
        time_base["type"] != "normalized_uniform"
        or _count(time_base["sample_count"], "time_base sample_count") != requested
    ):
        raise ValueError("evaluation time_base contradicts aggregate sample count")
    for field in (
        "mean_distance",
        "mean_normalized_distance",
        "max_distance",
        "requested_arrival_t",
        "observed_arrival_t",
        "dtw_normalized_cost",
    ):
        _finite_number(aggregate[field], field, minimum=0.0, nullable=True)
    endpoint_t = _finite_number(
        aggregate["endpoint_t"], "endpoint_t", minimum=0.0, maximum=1.0, nullable=True
    )
    endpoint_status = aggregate["endpoint_status"]
    endpoint_reasons = {
        "occluded": "observed_endpoint_occluded",
        "occluded_gap": "observed_endpoint_occluded_gap",
        "outside_track": "observed_endpoint_outside_track",
        "unavailable": "requested_track_has_no_visible_endpoint",
    }
    if endpoint_status == "compared":
        if aggregate["endpoint_reason"] is not None:
            raise ValueError("compared endpoint_reason must be null")
        endpoint_error = _finite_number(
            aggregate["endpoint_error"], "endpoint_error", minimum=0.0
        )
        normalized_endpoint = _finite_number(
            aggregate["normalized_endpoint_error"],
            "normalized_endpoint_error",
            minimum=0.0,
        )
        assert endpoint_error is not None and normalized_endpoint is not None
        if endpoint_t is None:
            raise ValueError("compared endpoint_t must be present")
        if not math.isclose(
            normalized_endpoint,
            endpoint_error / math.sqrt(2.0),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("normalized_endpoint_error contradicts endpoint_error")
    elif endpoint_status in endpoint_reasons:
        if aggregate["endpoint_reason"] != endpoint_reasons[endpoint_status]:
            raise ValueError("endpoint_reason contradicts endpoint_status")
        if aggregate["endpoint_error"] is not None or aggregate["normalized_endpoint_error"] is not None:
            raise ValueError("non-compared endpoint_error values must be null")
        if endpoint_status == "unavailable" and endpoint_t is not None:
            raise ValueError("unavailable endpoint_t must be null")
        if endpoint_status != "unavailable" and endpoint_t is None:
            raise ValueError("observed endpoint status requires endpoint_t")
    else:
        raise ValueError(f"unknown endpoint_status: {endpoint_status!r}")

    requested_arrival = _finite_number(
        aggregate["requested_arrival_t"],
        "requested_arrival_t",
        minimum=0.0,
        maximum=1.0,
        nullable=True,
    )
    observed_arrival = _finite_number(
        aggregate["observed_arrival_t"],
        "observed_arrival_t",
        minimum=0.0,
        maximum=1.0,
        nullable=True,
    )
    arrival_error = _finite_number(
        aggregate["arrival_error"],
        "arrival_error",
        minimum=0.0,
        maximum=1.0,
        nullable=True,
    )
    if requested_arrival is not None and observed_arrival is not None:
        if arrival_error is None or not math.isclose(
            arrival_error,
            abs(observed_arrival - requested_arrival),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("arrival_error contradicts requested/observed arrival")
    elif arrival_error is not None:
        raise ValueError("arrival_error requires requested and observed arrival")
    if endpoint_status == "unavailable" and (
        requested_arrival is not None or observed_arrival is not None
    ):
        raise ValueError("unavailable endpoint cannot have arrival metrics")
    _finite_number(aggregate["arrival_tolerance"], "arrival_tolerance", minimum=0.0, maximum=1.0)
    direction_cosine = _finite_number(
        aggregate["direction_cosine"],
        "direction_cosine",
        minimum=-1.0,
        maximum=1.0,
        nullable=True,
    )
    if type(aggregate["direction_match"]) is not bool:
        raise ValueError("direction_match must be a known JSON boolean verdict")
    if direction_cosine is None or aggregate["direction_match"] is not (direction_cosine > 0.0):
        raise ValueError("direction metrics do not produce a consistent known verdict")
    dtw = metrics["dtw"]
    if not isinstance(dtw, Mapping):
        raise ValueError("evaluation DTW metrics must be an object")
    dtw_value = _finite_number(
        dtw.get("normalized_cost"), "DTW normalized_cost", minimum=0.0
    )
    aggregate_dtw = _finite_number(
        aggregate["dtw_normalized_cost"], "dtw_normalized_cost", minimum=0.0
    )
    assert dtw_value is not None and aggregate_dtw is not None
    if not math.isclose(dtw_value, aggregate_dtw, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("DTW metrics contradict aggregate dtw_normalized_cost")
    return dict(aggregate)


def _metric_verdicts(
    report: Mapping[str, object],
    aggregate: Mapping[str, object],
    track: TrajectoryTrack,
) -> list[dict[str, str]]:
    direction_match = bool(aggregate["direction_match"])
    endpoint = _finite_number(
        aggregate["normalized_endpoint_error"], "normalized_endpoint_error", minimum=0.0
    )
    dtw = _finite_number(
        aggregate["dtw_normalized_cost"], "dtw_normalized_cost", minimum=0.0
    )
    arrival = aggregate["arrival_error"]
    if arrival is None:
        requested_arrival = aggregate["requested_arrival_t"]
        observed_arrival = aggregate["observed_arrival_t"]
        if requested_arrival is not None and observed_arrival is None:
            timing_matches = False
        elif requested_arrival is None and observed_arrival is None:
            timing_matches = True
        else:
            raise ValueError("arrival metrics do not produce a known verdict")
    else:
        arrival_value = _finite_number(arrival, "arrival_error", minimum=0.0)
        assert arrival_value is not None
        timing_matches = arrival_value <= _THRESHOLDS["arrival_error_max"]
    assert endpoint is not None and dtw is not None
    path_failure = (
        "orbit_mismatch"
        if (
            track.target_type == "camera"
            and track.primitive == "circle"
            and track.semantic in {"orbit_clockwise", "orbit_counterclockwise"}
        )
        else "timing_mismatch"
    )
    return [
        {
            "control_id": "direction",
            "verdict": "matched" if direction_match else "direction_mismatch",
        },
        {
            "control_id": "arrival_timing",
            "verdict": "matched" if timing_matches else "timing_mismatch",
        },
        {
            "control_id": "amplitude",
            "verdict": (
                "matched"
                if endpoint <= _THRESHOLDS["normalized_endpoint_error_max"]
                else "amplitude_excess"
            ),
        },
        {
            "control_id": "screen_direction",
            "verdict": "matched" if direction_match else "screen_direction_mismatch",
        },
        {
            "control_id": "orbit_or_path",
            "verdict": (
                "matched" if dtw <= _THRESHOLDS["dtw_normalized_cost_max"] else path_failure
            ),
        },
    ]


def prepare_revision(
    *,
    evaluation_path: Path,
    prompt_path: Path,
    video_path: Path,
    trajectory_path: Path,
    session_manifest_path: Path,
    annotation_path: Path,
    revision_generation: int,
    workspace: Path,
) -> dict[str, object]:
    """Build one deterministic revision plan after re-hashing every source."""

    if type(revision_generation) is not int or revision_generation < 0:
        raise ValueError("revision_generation must be a non-negative integer")
    if revision_generation >= 1:
        raise ValueError("maximum revision generation is 1")
    paths = {
        "evaluation_report": _safe_workspace_path(evaluation_path, workspace, "evaluation report"),
        "original_prompt": _safe_workspace_path(prompt_path, workspace, "prompt"),
        "video": _safe_workspace_path(video_path, workspace, "video"),
        "trajectory": _safe_workspace_path(trajectory_path, workspace, "trajectory"),
        "session_manifest": _safe_workspace_path(session_manifest_path, workspace, "session manifest"),
        "annotation": _safe_workspace_path(annotation_path, workspace, "annotation"),
    }
    if any(not path.is_file() for path in paths.values()):
        raise ValueError("all revision sources must be existing files")
    path_values = list(paths.values())
    if any(
        _same_file(left, right)
        for index, left in enumerate(path_values)
        for right in path_values[index + 1 :]
    ):
        raise ValueError("revision input paths must be distinct files")
    snapshots = {
        name: _snapshot(path, name.replace("_", " ")) for name, path in paths.items()
    }
    report = load_strict_bytes(
        snapshots["evaluation_report"].contents, "evaluation report"
    )
    aggregate = _validate_report(report)
    provenance = report["provenance"]
    assert isinstance(provenance, Mapping)
    source_sha = {
        "evaluation_report": snapshots["evaluation_report"].sha256,
        "original_prompt": snapshots["original_prompt"].sha256,
        "video": snapshots["video"].sha256,
        "trajectory": snapshots["trajectory"].sha256,
        "session_manifest": snapshots["session_manifest"].sha256,
        "annotation": snapshots["annotation"].sha256,
    }
    for name, provenance_name in (
        ("video", "video_sha256"),
        ("trajectory", "trajectory_file_sha256"),
        ("session_manifest", "session_manifest_sha256"),
        ("annotation", "annotation_sha256"),
    ):
        if source_sha[name] != _require_sha(
            provenance[provenance_name], f"{name} provenance"
        ):
            raise ValueError(f"{name.replace('_', ' ')} hash mismatch")
    frame_snapshots = _verify_session_frames(
        snapshots["session_manifest"], provenance["frame_sha256"], workspace
    )

    instruction = TrajectoryInstruction.from_json_bytes(
        snapshots["trajectory"].contents
    )
    if instruction.scene_id != report["scene_id"] or instruction.shot_id != report["shot_id"]:
        raise ValueError("evaluation scene/shot does not match trajectory")
    tracks = [track for track in instruction.tracks if track.track_id == report["track_id"]]
    if len(tracks) != 1:
        raise ValueError("evaluation track does not uniquely match trajectory")
    track = tracks[0]
    if track.target.to_dict() != report["target"]:
        raise ValueError("evaluation target does not match trajectory track")
    visible_points = [point for point in track.points if point.visible]
    endpoint_t = aggregate["endpoint_t"]
    if visible_points:
        if not isinstance(endpoint_t, (int, float)) or isinstance(endpoint_t, bool) or not math.isclose(
            float(endpoint_t), visible_points[-1].t, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("endpoint_t does not equal the requested last visible point")
    elif endpoint_t is not None:
        raise ValueError("endpoint_t must be null when the requested track has no visible point")

    recomputed = evaluate_files(
        trajectory_path=paths["trajectory"],
        video_path=paths["video"],
        session_manifest_path=paths["session_manifest"],
        annotation_path=paths["annotation"],
    )
    if recomputed != report:
        raise ValueError(
            "persisted evaluation does not exactly match fresh Task 7 recomputation"
        )
    _guard_snapshots([*snapshots.values(), *frame_snapshots])
    try:
        original_prompt = snapshots["original_prompt"].contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("prompt must be UTF-8") from exc
    if not original_prompt.strip() or len(original_prompt.encode("utf-8")) > 1024 * 1024:
        raise ValueError("prompt must be non-empty and at most 1 MiB")
    controls = _metric_verdicts(report, aggregate, track)
    selection = operations_for_control_verdicts(controls)
    changed = [operation for operation in selection if operation != "preserve_matched_control"]
    if changed:
        suffix = " ".join(_PROMPT_CLAUSES[operation] for operation in changed)
        revised_prompt = original_prompt.rstrip() + "\nTrajectory feedback revision (generation 1): " + suffix + "\n"
    else:
        revised_prompt = original_prompt
    result = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "offline_bounded_trajectory_revision",
        "scene_id": report["scene_id"],
        "shot_id": report["shot_id"],
        "track_id": report["track_id"],
        "revision_generation": revision_generation + 1,
        "max_revision_generation": 1,
        "submission_allowed": False,
        "network_called": False,
        "source_sha256": source_sha,
        "evaluator_provenance": dict(provenance),
        "metric_thresholds": dict(_THRESHOLDS),
        "decisions": list(selection.decisions),
        "operations": list(selection.operations),
        "original_prompt_sha256": source_sha["original_prompt"],
        "original_prompt": original_prompt,
        "revised_prompt": revised_prompt,
        "revised_prompt_sha256": hashlib.sha256(revised_prompt.encode("utf-8")).hexdigest(),
    }
    _guard_snapshots([*snapshots.values(), *frame_snapshots])
    return result


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def write_immutable(path: Path, contents: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"immutable output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--evaluation", type=Path, required=True)
    prepare.add_argument("--prompt", type=Path, required=True)
    prepare.add_argument("--video", type=Path, required=True)
    prepare.add_argument("--trajectory", type=Path, required=True)
    prepare.add_argument("--session-manifest", type=Path, required=True)
    prepare.add_argument("--annotation", type=Path, required=True)
    prepare.add_argument("--revision-generation", type=int, required=True)
    prepare.add_argument("--workspace", type=Path, default=Path("."))
    prepare.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = [
            _safe_workspace_path(path, args.workspace, "input")
            for path in (
                args.evaluation,
                args.prompt,
                args.video,
                args.trajectory,
                args.session_manifest,
                args.annotation,
            )
        ]
        output = _safe_workspace_path(args.output, args.workspace, "output")
        if any(_same_file(output, source) for source in inputs):
            raise ValueError("output collides with an input")
        result = prepare_revision(
            evaluation_path=args.evaluation,
            prompt_path=args.prompt,
            video_path=args.video,
            trajectory_path=args.trajectory,
            session_manifest_path=args.session_manifest,
            annotation_path=args.annotation,
            revision_generation=args.revision_generation,
            workspace=args.workspace,
        )
        contents = json_bytes(result)
        write_immutable(output, contents)
    except (FileExistsError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"TRAJECTORY_REVISION_PREPARE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        "TRAJECTORY_REVISION_PREPARE_OK "
        f"output={output} sha256={hashlib.sha256(contents).hexdigest()} network_called=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
