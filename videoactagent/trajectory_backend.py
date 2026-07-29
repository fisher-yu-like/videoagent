"""Prepare immutable, hash-bound API pilot bundles from trajectory artifacts.

Preparation is deliberately offline.  Submission, query, and download remain
owned by :mod:`videoactagent.jd_smoke` and the existing JD transport.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from uuid import uuid4


SCHEMA_VERSION = "0.1"
BACKENDS = ("kling", "seedance")
PROMPT_CONDITION = "trajectory_compiled"
T2V_CAPABILITY = "prompt_approximation"
REVISION_PROMPT_CONDITION = "trajectory_compiled_feedback_revision"
REVISION_BUNDLE_TYPE = "trajectory_api_pilot_feedback_revision"
REQUIRED_SOURCE_DIGESTS = frozenset(
    {
        "trajectory",
        "shotscript",
        "compiled_control",
        "trajectory_prompt",
        "patched_shotscript",
        "proxy_manifest",
        "proxy_video",
    }
)
REQUIRED_REVISION_SOURCE_DIGESTS = frozenset(
    {
        "evaluation_report",
        "video",
        "trajectory",
        "session_manifest",
        "annotation",
        "original_prompt",
    }
)
REVISION_OPERATION_ORDER = (
    "strengthen_direction",
    "split_time_segments",
    "reduce_amplitude",
    "strengthen_screen_direction",
    "simplify_orbit_to_truck",
    "preserve_matched_control",
)
ALLOWED_REVISION_OPERATIONS = frozenset(REVISION_OPERATION_ORDER)
BASE_BUNDLE_KEYS = frozenset(
    {
        "schema_version", "bundle_type", "backend", "scene_id", "shot_id",
        "prompt_condition", "backend_capability", "submission_ready", "unsupported",
        "submit_retry_limit", "trajectory_sha256", "submitted_prompt_sha256",
        "source_sha256", "shots",
    }
)
REVISION_ARTIFACT_KEYS = frozenset(
    {
        "schema_version", "artifact_type", "scene_id", "shot_id", "track_id",
        "revision_generation", "max_revision_generation", "submission_allowed",
        "network_called", "source_sha256", "evaluator_provenance", "metric_thresholds",
        "decisions", "operations", "original_prompt", "original_prompt_sha256",
        "revised_prompt", "revised_prompt_sha256",
    }
)
REVISION_BUNDLE_KEYS = frozenset(
    {
        "schema_version", "bundle_type", "backend", "scene_id", "shot_id",
        "prompt_condition", "backend_capability", "submission_ready", "unsupported",
        "submit_retry_limit", "trajectory_sha256", "submitted_prompt_sha256",
        "source_sha256", "base_bundle_sha256", "revision_artifact_sha256",
        "revision_source_sha256", "revision_prompt_binding", "revision_generation",
        "operations", "shots",
    }
)
EVALUATOR_PROVENANCE_KEYS = frozenset(
    {
        "video_sha256", "trajectory_file_sha256", "trajectory_canonical_sha256",
        "session_manifest_sha256", "annotation_sha256", "frame_sha256",
    }
)
REVISION_THRESHOLD_KEYS = frozenset(
    {"arrival_error_max", "normalized_endpoint_error_max", "dtw_normalized_cost_max"}
)
REVISION_VERDICTS = frozenset(
    {
        "direction_mismatch", "timing_mismatch", "amplitude_excess",
        "screen_direction_mismatch", "orbit_mismatch", "matched",
    }
)
REVISION_VERDICT_OPERATION = {
    "direction_mismatch": "strengthen_direction",
    "timing_mismatch": "split_time_segments",
    "amplitude_excess": "reduce_amplitude",
    "screen_direction_mismatch": "strengthen_screen_direction",
    "orbit_mismatch": "simplify_orbit_to_truck",
    "matched": "preserve_matched_control",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path, label: str) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_unsafe_numbers(value: object, label: str) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(f"{label} contains an oversized number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_unsafe_numbers(child, label)
        return
    if isinstance(value, list):
        for child in value:
            _reject_unsafe_numbers(child, label)


def _strict_object(contents: bytes, label: str) -> dict:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    value = json.loads(
        text,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    _reject_unsafe_numbers(value, label)
    return value


def _stat_identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _read_snapshot(path: Path, label: str) -> tuple[bytes, tuple[int, int, int, int]]:
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError(f"{label} is not a file: {candidate}")
    with candidate.open("rb") as handle:
        before = _stat_identity(os.fstat(handle.fileno()))
        contents = handle.read()
        after = _stat_identity(os.fstat(handle.fileno()))
    if before != after:
        raise ValueError(f"{label} changed while it was read")
    return contents, after


def _ensure_snapshot_unchanged(
    path: Path, expected_identity: tuple[int, int, int, int], expected_contents: bytes, label: str
) -> None:
    try:
        actual_identity = _stat_identity(Path(path).stat())
    except OSError as exc:
        raise ValueError(f"{label} disappeared after validation") from exc
    if actual_identity != expected_identity or sha256_file(Path(path)) != hashlib.sha256(expected_contents).hexdigest():
        raise ValueError(f"{label} changed after validation")


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {sorted(unknown)}")


def _require_sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    if value == "0" * 64:
        raise ValueError(f"{label} must not be an all-zero placeholder")
    return value


def _verify_file_hash(path: Path, expected: object, label: str) -> str:
    expected_sha = _require_sha(expected, label)
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"{label} hash mismatch: expected {expected_sha}, got {actual_sha}"
        )
    return actual_sha


def _shot_duration(patched_shotscript: dict, shot_id: str) -> float:
    shots = patched_shotscript.get("shots")
    if not isinstance(shots, list):
        raise ValueError("patched shotscript does not contain shots")
    matches = [
        shot
        for shot in shots
        if isinstance(shot, dict) and shot.get("shot_id") == shot_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"patched shotscript must contain exactly one shot {shot_id!r}"
        )
    duration = matches[0].get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise ValueError("shot duration must be numeric")
    if duration <= 0:
        raise ValueError("shot duration must be positive")
    return float(duration)


def prepare_api_bundle(
    compiled_control_path: Path,
    proxy_manifest_path: Path,
    backend: str,
) -> dict[str, object]:
    """Build one deterministic API-ready bundle without making a network call."""

    if backend not in BACKENDS:
        raise ValueError(f"unsupported backend: {backend}")
    compiled_path = Path(compiled_control_path)
    proxy_path = Path(proxy_manifest_path)
    if not compiled_path.is_file():
        raise ValueError(f"compiled control is not a file: {compiled_path}")
    if not proxy_path.is_file():
        raise ValueError(f"proxy manifest is not a file: {proxy_path}")

    compiled = _read_object(compiled_path, "compiled control")
    unsupported = compiled.get("unsupported")
    if not isinstance(unsupported, list):
        raise ValueError("compiled unsupported field must be a list")
    if unsupported or compiled.get("submission_ready") is not True:
        raise ValueError("compiled trajectory contains unsupported controls")
    capability = compiled.get("backend_capability")
    if capability != {"t2v": T2V_CAPABILITY}:
        raise ValueError("compiled trajectory is not a T2V prompt approximation")

    trajectory_sha = _require_sha(
        compiled.get("trajectory_sha256"), "compiled trajectory_sha256"
    )
    source_artifacts = compiled.get("source_artifact_sha256")
    if not isinstance(source_artifacts, dict):
        raise ValueError("compiled source_artifact_sha256 must be an object")
    source_trajectory_sha = _require_sha(
        source_artifacts.get("trajectory"), "source trajectory"
    )
    source_shotscript_sha = _require_sha(
        source_artifacts.get("shotscript"), "source shotscript"
    )
    if source_trajectory_sha != trajectory_sha:
        raise ValueError("compiled source trajectory hash mismatch")

    compiled_artifacts = compiled.get("artifact_sha256")
    if not isinstance(compiled_artifacts, dict):
        raise ValueError("compiled artifact_sha256 must be an object")
    prompt_path = compiled_path.parent / "trajectory_prompt.txt"
    patched_path = compiled_path.parent / "patched_shotscript.json"
    prompt_sha = _verify_file_hash(
        prompt_path,
        compiled_artifacts.get("trajectory_prompt.txt"),
        "trajectory prompt",
    )
    patched_sha = _verify_file_hash(
        patched_path,
        compiled_artifacts.get("patched_shotscript.json"),
        "patched shotscript",
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    patched = _read_object(patched_path, "patched shotscript")
    shot_id = compiled.get("shot_id")
    if not isinstance(shot_id, str) or not shot_id:
        raise ValueError("compiled shot_id must be a non-empty string")
    duration = _shot_duration(patched, shot_id)

    proxy = _read_object(proxy_path, "proxy manifest")
    if proxy.get("trajectory_sha256") != trajectory_sha:
        raise ValueError("proxy trajectory hash mismatch")
    if proxy.get("shotscript_sha256") != source_shotscript_sha:
        raise ValueError("proxy shotscript hash mismatch")
    controlled_shot = proxy.get("controlled_shot")
    if not isinstance(controlled_shot, dict) or controlled_shot.get("shot_id") != shot_id:
        raise ValueError("proxy controlled shot mismatch")
    video = proxy.get("video")
    if not isinstance(video, dict):
        raise ValueError("proxy manifest video field must be an object")
    video_relative = video.get("path")
    if (
        not isinstance(video_relative, str)
        or not video_relative
        or Path(video_relative).is_absolute()
        or ".." in Path(video_relative).parts
    ):
        raise ValueError("proxy video path must be a safe relative path")
    proxy_video_path = proxy_path.parent / video_relative
    proxy_video_sha = _verify_file_hash(
        proxy_video_path, video.get("sha256"), "proxy video"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": "trajectory_api_pilot",
        "backend": backend,
        "scene_id": compiled.get("scene_id"),
        "shot_id": shot_id,
        "prompt_condition": PROMPT_CONDITION,
        "backend_capability": {"t2v": T2V_CAPABILITY},
        "submission_ready": True,
        "unsupported": [],
        "submit_retry_limit": 0,
        "trajectory_sha256": trajectory_sha,
        "submitted_prompt_sha256": prompt_sha,
        "source_sha256": {
            "trajectory": source_trajectory_sha,
            "shotscript": source_shotscript_sha,
            "compiled_control": sha256_file(compiled_path),
            "trajectory_prompt": prompt_sha,
            "patched_shotscript": patched_sha,
            "proxy_manifest": sha256_file(proxy_path),
            "proxy_video": proxy_video_sha,
        },
        "shots": [
            {
                "shot_id": shot_id,
                "duration": duration,
                "prompts": {PROMPT_CONDITION: prompt},
            }
        ],
    }


def _validate_base_bundle(base: Mapping[str, object], backend: str) -> tuple[dict, str, dict[str, str]]:
    _exact_keys(base, BASE_BUNDLE_KEYS, "base trajectory bundle")
    if base.get("schema_version") != SCHEMA_VERSION or base.get("bundle_type") != "trajectory_api_pilot":
        raise ValueError("base trajectory bundle schema/type mismatch")
    if base.get("backend") != backend:
        raise ValueError("base trajectory bundle backend mismatch")
    if base.get("prompt_condition") != PROMPT_CONDITION:
        raise ValueError("base trajectory bundle prompt condition mismatch")
    if base.get("submission_ready") is not True or base.get("unsupported", object()) != []:
        raise ValueError("base trajectory bundle is not approved for submission")
    if type(base.get("submit_retry_limit")) is not int or base.get("submit_retry_limit") != 0:
        raise ValueError("base trajectory bundle retry limit must be zero")
    if base.get("backend_capability") != {"t2v": T2V_CAPABILITY}:
        raise ValueError("base trajectory bundle capability mismatch")

    source_value = base.get("source_sha256")
    if not isinstance(source_value, Mapping):
        raise ValueError("base trajectory bundle source_sha256 must be an object")
    _exact_keys(source_value, REQUIRED_SOURCE_DIGESTS, "base source_sha256")
    source = {name: _require_sha(source_value[name], f"base source_sha256.{name}") for name in REQUIRED_SOURCE_DIGESTS}
    trajectory_sha = _require_sha(base.get("trajectory_sha256"), "base trajectory_sha256")
    if trajectory_sha != source["trajectory"]:
        raise ValueError("base trajectory SHA-256 mismatch")

    shots = base.get("shots")
    if not isinstance(shots, list) or len(shots) != 1 or not isinstance(shots[0], Mapping):
        raise ValueError("base trajectory bundle must contain exactly one shot")
    shot = dict(shots[0])
    if shot.get("shot_id") != base.get("shot_id"):
        raise ValueError("base trajectory shot identity mismatch")
    prompts = shot.get("prompts")
    if not isinstance(prompts, Mapping) or set(prompts) != {PROMPT_CONDITION}:
        raise ValueError("base trajectory prompt map mismatch")
    original_prompt = prompts[PROMPT_CONDITION]
    if not isinstance(original_prompt, str) or not original_prompt:
        raise ValueError("base trajectory prompt must be non-empty text")
    prompt_sha = _require_sha(base.get("submitted_prompt_sha256"), "base submitted_prompt_sha256")
    actual_prompt_sha = hashlib.sha256(original_prompt.encode("utf-8")).hexdigest()
    if prompt_sha != source["trajectory_prompt"] or prompt_sha != actual_prompt_sha:
        raise ValueError("base trajectory prompt hash mismatch")
    duration = shot.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("base trajectory duration must be numeric")
    if (
        not math.isfinite(float(duration))
        or not 0 < float(duration) <= 60
        or int(round(float(duration))) <= 0
    ):
        raise ValueError("base trajectory duration is out of range")
    return shot, trajectory_sha, source


def _validate_revision(
    revision: Mapping[str, object], base: Mapping[str, object], trajectory_sha: str
) -> tuple[str, list[str], dict[str, str]]:
    _exact_keys(revision, REVISION_ARTIFACT_KEYS, "revision")
    if revision.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("revision schema_version mismatch")
    if revision.get("artifact_type") != "offline_bounded_trajectory_revision":
        raise ValueError("revision artifact_type mismatch")
    if type(revision.get("revision_generation")) is not int or revision.get("revision_generation") != 1:
        raise ValueError("revision_generation must equal integer 1")
    if type(revision.get("max_revision_generation")) is not int or revision.get("max_revision_generation") != 1:
        raise ValueError("max_revision_generation must equal integer 1")
    if revision.get("submission_allowed") is not False or revision.get("network_called") is not False:
        raise ValueError("revision must be an offline, non-submittable artifact")
    if revision.get("scene_id") != base.get("scene_id") or revision.get("shot_id") != base.get("shot_id"):
        raise ValueError("revision/base scene or shot identity mismatch")
    if not isinstance(revision.get("track_id"), str) or not revision.get("track_id"):
        raise ValueError("revision track_id must be non-empty text")

    operations_value = revision.get("operations")
    if not isinstance(operations_value, list) or not operations_value:
        raise ValueError("revision operations must be a non-empty list")
    if any(not isinstance(item, str) or item not in ALLOWED_REVISION_OPERATIONS for item in operations_value):
        raise ValueError("revision contains an operation outside the allowlist")
    if len(set(operations_value)) != len(operations_value):
        raise ValueError("revision operations must not contain duplicates")
    operations = list(operations_value)
    if operations != [item for item in REVISION_OPERATION_ORDER if item in operations]:
        raise ValueError("revision operations are not in canonical order")

    decisions = revision.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("revision decisions must be a non-empty list")
    decision_ids: set[str] = set()
    decision_operations: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, Mapping) or set(decision) != {"control_id", "verdict", "operations"}:
            raise ValueError(f"revision decision {index} fields mismatch")
        control_id = decision["control_id"]
        verdict = decision["verdict"]
        selected = decision["operations"]
        if not isinstance(control_id, str) or not control_id or control_id in decision_ids:
            raise ValueError("revision decision control_id is invalid or duplicated")
        if not isinstance(verdict, str) or verdict not in REVISION_VERDICTS:
            raise ValueError("revision decision verdict is unknown")
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(item, str) or item not in ALLOWED_REVISION_OPERATIONS for item in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ValueError("revision decision operations are invalid")
        if selected != [REVISION_VERDICT_OPERATION[verdict]]:
            raise ValueError("revision decision operation does not match its verdict")
        decision_ids.add(control_id)
        decision_operations.update(selected)
    if decision_operations != set(operations):
        raise ValueError("revision decision operations do not match top-level operations")

    thresholds = revision.get("metric_thresholds")
    if not isinstance(thresholds, Mapping):
        raise ValueError("revision metric_thresholds must be an object")
    _exact_keys(thresholds, REVISION_THRESHOLD_KEYS, "revision metric_thresholds")
    for name, value in thresholds.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ValueError(f"revision metric_thresholds.{name} is invalid")

    source_value = revision.get("source_sha256")
    if not isinstance(source_value, Mapping):
        raise ValueError("revision source_sha256 must be an object")
    _exact_keys(source_value, REQUIRED_REVISION_SOURCE_DIGESTS, "revision source_sha256")
    source = {
        name: _require_sha(source_value[name], f"revision source_sha256.{name}")
        for name in REQUIRED_REVISION_SOURCE_DIGESTS
    }
    evaluator = revision.get("evaluator_provenance")
    if not isinstance(evaluator, Mapping):
        raise ValueError("revision evaluator_provenance must be an object")
    _exact_keys(evaluator, EVALUATOR_PROVENANCE_KEYS, "revision evaluator_provenance")
    for name in (
        "video_sha256",
        "trajectory_file_sha256",
        "trajectory_canonical_sha256",
        "session_manifest_sha256",
        "annotation_sha256",
    ):
        _require_sha(evaluator.get(name), f"revision evaluator_provenance.{name}")
    frames = evaluator.get("frame_sha256")
    if not isinstance(frames, list) or not frames:
        raise ValueError("revision evaluator frame provenance must be non-empty")
    for index, digest in enumerate(frames):
        _require_sha(digest, f"revision evaluator frame_sha256[{index}]")
    if source["video"] != evaluator["video_sha256"]:
        raise ValueError("revision video provenance mismatch")
    if source["trajectory"] not in (
        evaluator["trajectory_file_sha256"], evaluator["trajectory_canonical_sha256"]
    ) or evaluator["trajectory_file_sha256"] != evaluator["trajectory_canonical_sha256"]:
        raise ValueError("revision trajectory provenance mismatch")
    if source["trajectory"] != trajectory_sha:
        raise ValueError("revision/base trajectory hash mismatch")
    if source["session_manifest"] != evaluator["session_manifest_sha256"]:
        raise ValueError("revision session manifest provenance mismatch")
    if source["annotation"] != evaluator["annotation_sha256"]:
        raise ValueError("revision annotation provenance mismatch")

    original_prompt = revision.get("original_prompt")
    revised_prompt = revision.get("revised_prompt")
    if not isinstance(original_prompt, str) or not original_prompt:
        raise ValueError("revision original_prompt must be non-empty text")
    if not isinstance(revised_prompt, str) or not revised_prompt:
        raise ValueError("revision revised_prompt must be non-empty text")
    original_sha = _require_sha(revision.get("original_prompt_sha256"), "revision original_prompt_sha256")
    revised_sha = _require_sha(revision.get("revised_prompt_sha256"), "revision revised_prompt_sha256")
    if original_sha != hashlib.sha256(original_prompt.encode("utf-8")).hexdigest():
        raise ValueError("revision original prompt hash mismatch")
    if revised_sha != hashlib.sha256(revised_prompt.encode("utf-8")).hexdigest():
        raise ValueError("revision revised prompt hash mismatch")
    if source["original_prompt"] != original_sha:
        raise ValueError("revision original prompt source mismatch")
    base_prompt = base["shots"][0]["prompts"][PROMPT_CONDITION]  # type: ignore[index]
    if original_prompt != base_prompt or original_sha != base["submitted_prompt_sha256"]:
        raise ValueError("revision original prompt does not bind to base bundle")
    return revised_prompt, operations, source


def prepare_revision_api_bundle(
    revision_path: Path, base_bundle_path: Path, backend: str
) -> dict[str, object]:
    """Build one immutable-ready feedback-revision bundle without network access."""

    if backend not in BACKENDS:
        raise ValueError(f"unsupported backend: {backend}")
    revision_path = Path(revision_path)
    base_bundle_path = Path(base_bundle_path)
    revision_bytes, revision_identity = _read_snapshot(revision_path, "revision")
    base_bytes, base_identity = _read_snapshot(base_bundle_path, "base bundle")
    revision = _strict_object(revision_bytes, "revision")
    base = _strict_object(base_bytes, "base bundle")
    base_shot, trajectory_sha, base_sources = _validate_base_bundle(base, backend)
    revised_prompt, operations, revision_sources = _validate_revision(
        revision, base, trajectory_sha
    )
    _ensure_snapshot_unchanged(revision_path, revision_identity, revision_bytes, "revision")
    _ensure_snapshot_unchanged(base_bundle_path, base_identity, base_bytes, "base bundle")
    revised_prompt_sha = hashlib.sha256(revised_prompt.encode("utf-8")).hexdigest()
    revision_artifact_sha = hashlib.sha256(revision_bytes).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": REVISION_BUNDLE_TYPE,
        "backend": backend,
        "scene_id": base["scene_id"],
        "shot_id": base["shot_id"],
        "prompt_condition": REVISION_PROMPT_CONDITION,
        "backend_capability": {"t2v": T2V_CAPABILITY},
        "submission_ready": True,
        "unsupported": [],
        "submit_retry_limit": 0,
        "trajectory_sha256": trajectory_sha,
        "submitted_prompt_sha256": revised_prompt_sha,
        "source_sha256": base_sources,
        "base_bundle_sha256": hashlib.sha256(base_bytes).hexdigest(),
        "revision_artifact_sha256": revision_artifact_sha,
        "revision_source_sha256": revision_sources,
        "revision_prompt_binding": {
            "revision_artifact_sha256": revision_artifact_sha,
            "revised_prompt_sha256": revised_prompt_sha,
        },
        "revision_generation": 1,
        "operations": operations,
        "shots": [
            {
                "shot_id": base_shot["shot_id"],
                "duration": base_shot["duration"],
                "prompts": {REVISION_PROMPT_CONDITION: revised_prompt},
            }
        ],
    }


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_immutable(path: Path, contents: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"immutable API bundle already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-directory hard link publishes the fully fsynced inode without
        # ever replacing an existing name.  Unlike exists()+replace(), this is
        # an atomic no-clobber operation when another process races us.
        os.link(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--compiled", type=Path, required=True)
    prepare.add_argument("--proxy-manifest", type=Path, required=True)
    prepare.add_argument("--backend", choices=BACKENDS, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    revision = subparsers.add_parser("prepare-revision")
    revision.add_argument("--revision", type=Path, required=True)
    revision.add_argument("--base-bundle", type=Path, required=True)
    revision.add_argument("--backend", choices=BACKENDS, required=True)
    revision.add_argument("--workspace", type=Path, default=Path("."))
    revision.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "prepare-revision":
            revision = _safe_workspace_path(args.revision, args.workspace, "revision")
            base_bundle = _safe_workspace_path(args.base_bundle, args.workspace, "base bundle")
            output = _safe_workspace_path(args.output, args.workspace, "output")
            if _same_file(output, revision) or _same_file(output, base_bundle):
                raise ValueError("output collides with an input")
            bundle = prepare_revision_api_bundle(revision, base_bundle, args.backend)
        else:
            output = args.output
            bundle = prepare_api_bundle(
                args.compiled, args.proxy_manifest, args.backend
            )
        contents = _json_bytes(bundle)
        _write_immutable(output, contents)
    except (FileExistsError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"TRAJECTORY_BACKEND_PREPARE_FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(
        "TRAJECTORY_BACKEND_PREPARE_OK "
        f"backend={args.backend} output={output} "
        f"sha256={hashlib.sha256(contents).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
