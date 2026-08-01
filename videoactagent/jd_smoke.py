from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.backends.jd import (
    build_seedance_reference_video,
    build_kling_t2v,
    build_seedance_t2v,
    download_once,
    query_once,
    submit_once,
)
from videoactagent.run_record import RunDirectory
from videoactagent.trajectory_backend import (
    ALLOWED_REVISION_OPERATIONS,
    REQUIRED_REVISION_SOURCE_DIGESTS,
    REVISION_OPERATION_ORDER,
    REVISION_BUNDLE_KEYS,
    REVISION_BUNDLE_TYPE,
    REVISION_PROMPT_CONDITION,
    _ensure_snapshot_unchanged,
    _read_snapshot,
    _strict_object,
)


REQUIRED_TRAJECTORY_SOURCE_DIGESTS = (
    "trajectory",
    "shotscript",
    "compiled_control",
    "trajectory_prompt",
    "patched_shotscript",
    "proxy_manifest",
    "proxy_video",
)

REFERENCE_RECORD_NAMES = frozenset(
    {
        "approval", "clay", "restyle_prompt", "restyle_profile",
        "compiled_prompt", "trajectory_prompt", "annotation", "iteration_manifest",
    }
)


def _canonical_json_sha256(value: object) -> str:
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _reference_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"reference candidate {label} must be a lowercase SHA-256")
    return value


def validate_reference_candidate(
    candidate: dict, *, base_dir: Path | None = None
) -> dict[str, object]:
    """Reconstruct and validate the complete Task-5 submission contract."""

    expected_keys = {
        "schema_version", "state", "blockers", "network_called",
        "generation_submit_limit", "automatic_retry_limit", "fallbacks",
        "capability", "capability_evidence", "approved_iteration",
        "compiler_versions", "compiler_hashes", "source_hashes",
        "source_records", "proxy", "payload", "payload_sha256",
    }
    if set(candidate) != expected_keys:
        raise ValueError("reference candidate fields mismatch")
    state = candidate.get("state")
    if candidate.get("schema_version") != "seedance-reference-candidate/1" or state not in {
        "ready", "ready_for_single_combined_probe"
    }:
        raise ValueError("reference candidate is not ready")
    if (
        candidate.get("blockers") != []
        or candidate.get("network_called") is not False
        or type(candidate.get("generation_submit_limit")) is not int
        or candidate.get("generation_submit_limit") != 1
        or type(candidate.get("automatic_retry_limit")) is not int
        or candidate.get("automatic_retry_limit") != 0
        or candidate.get("fallbacks") != {"reference_image": False, "prompt_only": False}
    ):
        raise ValueError("reference candidate safety limits mismatch")

    capability = candidate.get("capability")
    if not isinstance(capability, dict) or set(capability) != {
        "model", "model_capability", "gateway_capability", "content_type",
        "url_field", "role",
    }:
        raise ValueError("reference candidate capability fields mismatch")
    expected_gateway = "gateway_verified" if state == "ready" else "gateway_unverified"
    if (
        capability.get("model") not in {"Doubao-Seedance-2.0", "Doubao-Seedance-2.5"}
        or capability.get("model_capability") != "model_supported"
        or capability.get("gateway_capability") != expected_gateway
        or capability.get("content_type") != "video_url"
        or capability.get("url_field") != "video_url"
        or capability.get("role") != "reference_video"
    ):
        raise ValueError("reference candidate capability binding mismatch")
    evidence = candidate.get("capability_evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"model", "gateway"}:
        raise ValueError("reference candidate capability evidence fields mismatch")
    model_evidence = evidence.get("model")
    if not isinstance(model_evidence, dict) or set(model_evidence) != {"source_url"}:
        raise ValueError("reference candidate model evidence mismatch")
    source_url = model_evidence.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise ValueError("reference candidate model evidence URL mismatch")
    gateway_evidence = evidence.get("gateway")
    if state == "ready_for_single_combined_probe":
        if gateway_evidence is not None:
            raise ValueError("combined probe must not claim gateway evidence")
    else:
        if not isinstance(gateway_evidence, dict) or set(gateway_evidence) != {"capture"}:
            raise ValueError("reference candidate gateway evidence mismatch")
        capture = gateway_evidence.get("capture")
        if not isinstance(capture, dict) or set(capture) != {"path", "sha256"}:
            raise ValueError("reference candidate gateway capture mismatch")
        if not isinstance(capture.get("path"), str) or not capture["path"]:
            raise ValueError("reference candidate gateway capture path mismatch")
        capture_sha = _reference_digest(capture.get("sha256"), "gateway capture hash")
        if base_dir is not None:
            relative_capture = Path(capture["path"])
            if relative_capture.is_absolute() or ".." in relative_capture.parts:
                raise ValueError("reference candidate gateway capture path is unsafe")
            root = base_dir.resolve()
            capture_path = (root / relative_capture).resolve(strict=False)
            try:
                capture_path.relative_to(root)
            except ValueError as exc:
                raise ValueError("reference candidate gateway capture path is unsafe") from exc
            if not capture_path.is_file() or hashlib.sha256(
                capture_path.read_bytes()
            ).hexdigest() != capture_sha:
                raise ValueError("reference candidate gateway capture snapshot mismatch")

    iteration = candidate.get("approved_iteration")
    if not isinstance(iteration, str) or re.fullmatch(r"D[1-9][0-9]*", iteration) is None:
        raise ValueError("reference candidate approved iteration mismatch")
    versions = candidate.get("compiler_versions")
    if not isinstance(versions, dict) or set(versions) != {
        "trajectory_compiler_version", "restyle_compiler_version"
    } or any(not isinstance(item, str) or not item for item in versions.values()):
        raise ValueError("reference candidate compiler version binding mismatch")
    records = candidate.get("source_records")
    hashes = candidate.get("source_hashes")
    if (
        not isinstance(records, dict) or set(records) != REFERENCE_RECORD_NAMES
        or not isinstance(hashes, dict) or set(hashes) != REFERENCE_RECORD_NAMES
    ):
        raise ValueError("reference candidate source bindings mismatch")
    for name in REFERENCE_RECORD_NAMES:
        record = records[name]
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
            raise ValueError(f"reference candidate {name} record mismatch")
        if (
            not isinstance(record.get("path"), str) or not record["path"]
            or type(record.get("bytes")) is not int or record["bytes"] <= 0
        ):
            raise ValueError(f"reference candidate {name} record mismatch")
        digest = _reference_digest(record.get("sha256"), f"{name} hash")
        if hashes[name] != digest:
            raise ValueError(f"reference candidate {name} source hash mismatch")
    proxy = candidate.get("proxy")
    clay = records["clay"]
    if not isinstance(proxy, dict) or set(proxy) != {"url", "path", "sha256", "bytes"}:
        raise ValueError("reference candidate proxy fields mismatch")
    if {key: proxy.get(key) for key in ("path", "sha256", "bytes")} != clay:
        raise ValueError("reference candidate proxy/source binding mismatch")

    compiler_hashes = candidate.get("compiler_hashes")
    required_compiler_hashes = {"compiled_prompt", "trajectory_prompt", "restyle_prompt"}
    optional_compiler_hashes = {
        "trajectory_compiler_sha256", "restyle_compiler_sha256"
    }
    if (
        not isinstance(compiler_hashes, dict)
        or not required_compiler_hashes.issubset(compiler_hashes)
        or not set(compiler_hashes).issubset(
            required_compiler_hashes | optional_compiler_hashes
        )
    ):
        raise ValueError("reference candidate compiler hashes mismatch")
    for name in required_compiler_hashes:
        if compiler_hashes[name] != hashes[name]:
            raise ValueError("reference candidate compiler/source hash mismatch")
    for name in optional_compiler_hashes & set(compiler_hashes):
        _reference_digest(compiler_hashes[name], f"{name} hash")
    payload = candidate.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("reference candidate payload must be an object")
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 2 or not isinstance(content[0], dict):
        raise ValueError("reference candidate request shape mismatch")
    prompt = content[0].get("text")
    if not isinstance(prompt, str):
        raise ValueError("reference candidate prompt is missing")
    prompt_bytes = prompt.encode("utf-8")
    if (
        len(prompt_bytes) != records["restyle_prompt"]["bytes"]
        or hashlib.sha256(prompt_bytes).hexdigest() != hashes["restyle_prompt"]
    ):
        raise ValueError("reference candidate restyle prompt binding mismatch")
    rebuilt = build_seedance_reference_video(
        prompt, proxy.get("url"), model=capability["model"], duration=5
    )
    if payload != rebuilt:
        raise ValueError("reference candidate request shape mismatch")
    payload_sha = _canonical_json_sha256(payload)
    if candidate.get("payload_sha256") != payload_sha:
        raise ValueError("reference candidate payload SHA-256 mismatch")
    return {
        "state": state,
        "payload": payload,
        "payload_sha256": payload_sha,
        "proxy_sha256": proxy["sha256"],
        "restyle_prompt_sha256": hashes["restyle_prompt"],
        "approval_sha256": hashes["approval"],
        "capability_sha256": _canonical_json_sha256(
            {"capability": capability, "capability_evidence": evidence}
        ),
        "source_hashes": dict(hashes),
        "model": capability["model"],
    }


def _write_immutable_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class _ImmutableSubmissionRun:
    """RunDirectory-compatible writer with append-only submission artifacts."""

    def __init__(self, run: RunDirectory):
        self.path = run.path

    def write_json(self, name: str, value: dict) -> Path:
        path = self.path / name
        if name == "request.json" and path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != value:
                raise RuntimeError("immutable request conflict")
            return path
        if name in {"request.json", "response.json", "failure.json", "state.json"}:
            _write_immutable_json(path, value)
            return path
        raise RuntimeError(f"unexpected submission artifact: {name}")


def _claim_reference_attempt(run_root: Path, candidate_sha: str) -> None:
    claim = run_root / ".seedance_reference_claims" / f"{candidate_sha}.json"
    try:
        _write_immutable_json(
            claim,
            {
                "candidate_sha256": candidate_sha,
                "attempted_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "generation_submit_count": 1,
            },
        )
    except FileExistsError as exc:
        raise RuntimeError("reference candidate was already attempted in this run root") from exc


def _safe_source_path(source_root: Path, declared: object, label: str) -> Path:
    if not isinstance(declared, str) or not declared:
        raise ValueError(f"reference source snapshot {label} path is invalid")
    relative = Path(declared)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"reference source snapshot {label} path is unsafe")
    target = (source_root / relative).resolve(strict=False)
    try:
        target.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(f"reference source snapshot {label} path is unsafe") from exc
    return target


def _snapshot_reference_sources(
    candidate: dict, source_root_value: Path
) -> dict[str, tuple[Path, bytes, str]]:
    source_root = source_root_value.resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError("reference source root must be a directory")
    records = candidate["source_records"]
    snapshots: dict[str, tuple[Path, bytes, str]] = {}
    for name in sorted(REFERENCE_RECORD_NAMES):
        record = records[name]
        path = _safe_source_path(source_root, record["path"], name)
        if not path.is_file():
            raise ValueError(f"reference source snapshot {name} is missing")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != record["bytes"] or digest != record["sha256"]:
            raise ValueError(f"reference source snapshot {name} hash/size mismatch")
        snapshots[name] = (path, data, digest)
    prompt = snapshots["restyle_prompt"][1]
    try:
        prompt_text = prompt.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("reference source snapshot restyle_prompt is not UTF-8") from exc
    if prompt_text != candidate["payload"]["content"][0]["text"]:
        raise ValueError("reference source snapshot materialized prompt mismatch")
    gateway = candidate["capability_evidence"]["gateway"]
    if gateway is not None:
        capture = gateway["capture"]
        path = _safe_source_path(source_root, capture["path"], "capability_capture")
        if not path.is_file():
            raise ValueError("reference source snapshot capability capture is missing")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != capture["sha256"]:
            raise ValueError("reference source snapshot capability capture hash mismatch")
        # Re-run Task 5's exact captured request/accepted response validator;
        # the editable candidate digest alone cannot establish capability.
        from videoactagent.seedance_reference import _validate_capture_document

        _validate_capture_document(data, candidate["capability"]["model"])
        snapshots["capability_capture"] = (path, data, digest)
    return snapshots


def _ensure_reference_sources_unchanged(
    snapshots: dict[str, tuple[Path, bytes, str]]
) -> None:
    for name, (path, expected_bytes, expected_sha) in snapshots.items():
        if not path.is_file():
            raise ValueError(f"reference source snapshot {name} disappeared")
        current = path.read_bytes()
        if current != expected_bytes or hashlib.sha256(current).hexdigest() != expected_sha:
            raise ValueError(f"reference source snapshot {name} changed")


def require_key() -> str:
    api_key = os.environ.get("JD_KLING_KEY", "").strip()
    if not api_key:
        raise RuntimeError("JD_KLING_KEY is required")
    return api_key


def find_shot(bundle: dict, shot_id: str) -> dict:
    for shot in bundle["shots"]:
        if shot["shot_id"] == shot_id:
            return shot
    raise ValueError(f"shot not found in bundle: {shot_id}")


def _require_trajectory_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            f"trajectory bundle {label} must be a lowercase SHA-256 digest"
        )
    if value == "0" * 64:
        raise ValueError(f"trajectory bundle {label} must not be an all-zero placeholder")
    return value


def validate_trajectory_bundle(
    bundle: dict, backend: str, shot_id: str
) -> tuple[dict, str, dict[str, str]]:
    """Fail closed on trajectory provenance before run or transport creation."""

    if bundle.get("unsupported", object()) != []:
        raise ValueError("trajectory bundle unsupported must exist and equal []")
    if bundle.get("submission_ready") is not True:
        raise ValueError("trajectory bundle contains unsupported controls")
    if bundle.get("backend") != backend:
        raise ValueError(f"trajectory bundle backend mismatch: expected {backend}")
    if bundle.get("prompt_condition") != "trajectory_compiled":
        raise ValueError("trajectory bundle prompt condition mismatch")
    if bundle.get("submit_retry_limit") != 0:
        raise ValueError("trajectory bundle submit retry limit must be zero")
    if bundle.get("backend_capability") != {"t2v": "prompt_approximation"}:
        raise ValueError("trajectory bundle T2V capability mismatch")

    source_value = bundle.get("source_sha256")
    if not isinstance(source_value, dict):
        raise ValueError("trajectory bundle source_sha256 must be an object")
    source_sha256 = {
        field: _require_trajectory_sha256(
            source_value.get(field), f"source_sha256.{field}"
        )
        for field in REQUIRED_TRAJECTORY_SOURCE_DIGESTS
    }
    trajectory_sha256 = _require_trajectory_sha256(
        bundle.get("trajectory_sha256"), "trajectory_sha256"
    )
    if trajectory_sha256 != source_sha256["trajectory"]:
        raise ValueError("trajectory bundle trajectory SHA-256 mismatch")

    submitted_prompt_sha256 = _require_trajectory_sha256(
        bundle.get("submitted_prompt_sha256"), "submitted_prompt_sha256"
    )
    if submitted_prompt_sha256 != source_sha256["trajectory_prompt"]:
        raise ValueError("trajectory bundle submitted prompt SHA-256 mismatch")

    shot = find_shot(bundle, shot_id)
    prompts = shot.get("prompts")
    if not isinstance(prompts, dict):
        raise ValueError("trajectory bundle shot prompts must be an object")
    prompt = prompts.get("trajectory_compiled")
    if not isinstance(prompt, str):
        raise ValueError("trajectory bundle submitted prompt must be text")
    actual_prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if actual_prompt_sha256 != submitted_prompt_sha256:
        raise ValueError("trajectory bundle canonical submitted prompt mismatch")
    return shot, trajectory_sha256, source_sha256


def validate_revision_trajectory_bundle(
    bundle: dict, backend: str, shot_id: str
) -> tuple[dict, dict[str, object]]:
    """Validate every offline revision provenance field before credentials."""

    if set(bundle) != set(REVISION_BUNDLE_KEYS):
        raise ValueError("trajectory revision bundle fields mismatch")
    if bundle.get("schema_version") != "0.1" or bundle.get("bundle_type") != REVISION_BUNDLE_TYPE:
        raise ValueError("trajectory revision bundle schema/type mismatch")
    if bundle.get("backend") != backend:
        raise ValueError(f"trajectory revision bundle backend mismatch: expected {backend}")
    if bundle.get("prompt_condition") != REVISION_PROMPT_CONDITION:
        raise ValueError("trajectory revision bundle prompt condition mismatch")
    if bundle.get("unsupported", object()) != [] or bundle.get("submission_ready") is not True:
        raise ValueError("trajectory revision bundle contains unsupported controls")
    if type(bundle.get("submit_retry_limit")) is not int or bundle.get("submit_retry_limit") != 0:
        raise ValueError("trajectory revision bundle retry limit must be integer zero")
    if bundle.get("backend_capability") != {"t2v": "prompt_approximation"}:
        raise ValueError("trajectory revision bundle T2V capability mismatch")
    if type(bundle.get("revision_generation")) is not int or bundle.get("revision_generation") != 1:
        raise ValueError("trajectory revision bundle generation must be integer one")

    source_value = bundle.get("source_sha256")
    if not isinstance(source_value, dict) or set(source_value) != set(REQUIRED_TRAJECTORY_SOURCE_DIGESTS):
        raise ValueError("trajectory revision bundle source_sha256 fields mismatch")
    source_sha256 = {
        field: _require_trajectory_sha256(source_value[field], f"source_sha256.{field}")
        for field in REQUIRED_TRAJECTORY_SOURCE_DIGESTS
    }
    trajectory_sha256 = _require_trajectory_sha256(bundle.get("trajectory_sha256"), "trajectory_sha256")
    if trajectory_sha256 != source_sha256["trajectory"]:
        raise ValueError("trajectory revision bundle trajectory SHA-256 mismatch")

    revision_source_value = bundle.get("revision_source_sha256")
    if not isinstance(revision_source_value, dict) or set(revision_source_value) != set(REQUIRED_REVISION_SOURCE_DIGESTS):
        raise ValueError("trajectory revision bundle revision source fields mismatch")
    revision_sources = {
        field: _require_trajectory_sha256(
            revision_source_value[field], f"revision_source_sha256.{field}"
        )
        for field in REQUIRED_REVISION_SOURCE_DIGESTS
    }
    if revision_sources["trajectory"] != trajectory_sha256:
        raise ValueError("trajectory revision bundle revision/base trajectory mismatch")
    if revision_sources["original_prompt"] != source_sha256["trajectory_prompt"]:
        raise ValueError("trajectory revision bundle original/base prompt mismatch")
    base_bundle_sha = _require_trajectory_sha256(bundle.get("base_bundle_sha256"), "base_bundle_sha256")
    revision_artifact_sha = _require_trajectory_sha256(
        bundle.get("revision_artifact_sha256"), "revision_artifact_sha256"
    )
    binding = bundle.get("revision_prompt_binding")
    if not isinstance(binding, dict) or set(binding) != {
        "revision_artifact_sha256", "revised_prompt_sha256"
    }:
        raise ValueError("trajectory revision bundle prompt binding fields mismatch")
    if _require_trajectory_sha256(
        binding.get("revision_artifact_sha256"),
        "revision_prompt_binding.revision_artifact_sha256",
    ) != revision_artifact_sha:
        raise ValueError("trajectory revision bundle artifact binding mismatch")
    operations = bundle.get("operations")
    if (
        not isinstance(operations, list)
        or not operations
        or any(not isinstance(item, str) or item not in ALLOWED_REVISION_OPERATIONS for item in operations)
        or len(set(operations)) != len(operations)
        or operations != [item for item in REVISION_OPERATION_ORDER if item in operations]
    ):
        raise ValueError("trajectory revision bundle operations are invalid")

    shots = bundle.get("shots")
    if not isinstance(shots, list) or len(shots) != 1 or not isinstance(shots[0], dict):
        raise ValueError("trajectory revision bundle must contain exactly one shot")
    shot = shots[0]
    if shot.get("shot_id") != shot_id or bundle.get("shot_id") != shot_id:
        raise ValueError("trajectory revision bundle shot mismatch")
    prompts = shot.get("prompts")
    if not isinstance(prompts, dict) or set(prompts) != {REVISION_PROMPT_CONDITION}:
        raise ValueError("trajectory revision bundle prompt map mismatch")
    prompt = prompts[REVISION_PROMPT_CONDITION]
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("trajectory revision bundle submitted prompt must be text")
    submitted_prompt_sha = _require_trajectory_sha256(
        bundle.get("submitted_prompt_sha256"), "submitted_prompt_sha256"
    )
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != submitted_prompt_sha:
        raise ValueError("trajectory revision bundle canonical submitted prompt mismatch")
    if _require_trajectory_sha256(
        binding.get("revised_prompt_sha256"),
        "revision_prompt_binding.revised_prompt_sha256",
    ) != submitted_prompt_sha:
        raise ValueError("trajectory revision bundle prompt/artifact binding mismatch")
    duration = shot.get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not 0 < float(duration) <= 60
        or int(round(float(duration))) <= 0
    ):
        raise ValueError("trajectory revision bundle duration is invalid")
    return shot, {
        "trajectory_sha256": trajectory_sha256,
        "source_sha256": source_sha256,
        "revision_source_sha256": revision_sources,
        "base_bundle_sha256": base_bundle_sha,
        "revision_artifact_sha256": revision_artifact_sha,
        "revision_generation": 1,
        "revision_operations": list(operations),
        "compiled_prompt": prompt,
        "backend_capability": bundle["backend_capability"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit-kling")
    submit.add_argument("--bundle", type=Path, required=True)
    submit.add_argument("--shot", required=True)
    submit.add_argument(
        "--prompt",
        choices=("plain", "cinematic", "trajectory_compiled", REVISION_PROMPT_CONDITION),
        required=True,
    )
    submit.add_argument("--run-root", type=Path, required=True)

    submit_seedance = subparsers.add_parser("submit-seedance")
    submit_seedance.add_argument("--bundle", type=Path, required=True)
    submit_seedance.add_argument("--shot", required=True)
    submit_seedance.add_argument(
        "--prompt",
        choices=("plain", "cinematic", "trajectory_compiled", REVISION_PROMPT_CONDITION),
        required=True,
    )
    submit_seedance.add_argument("--run-root", type=Path, required=True)

    submit_reference = subparsers.add_parser("submit-seedance-reference")
    submit_reference.add_argument("--candidate", type=Path, required=True)
    submit_reference.add_argument("--source-root", type=Path, required=True)
    submit_reference.add_argument("--run-root", type=Path, required=True)

    query = subparsers.add_parser("query")
    query.add_argument("--run-dir", type=Path, required=True)

    download = subparsers.add_parser("download")
    download.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "submit-seedance-reference":
        candidate_bytes, candidate_identity = _read_snapshot(
            args.candidate, "Seedance reference candidate"
        )
        candidate = _strict_object(candidate_bytes, "Seedance reference candidate")
        validated = validate_reference_candidate(candidate, base_dir=args.source_root)
        candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
        source_snapshots = _snapshot_reference_sources(candidate, args.source_root)
        # Credentials and all filesystem effects follow complete validation and
        # an immediate source snapshot recheck.
        _ensure_snapshot_unchanged(
            args.candidate,
            candidate_identity,
            candidate_bytes,
            "Seedance reference candidate",
        )
        api_key = require_key()
        _ensure_snapshot_unchanged(
            args.candidate,
            candidate_identity,
            candidate_bytes,
            "Seedance reference candidate",
        )
        _ensure_reference_sources_unchanged(source_snapshots)
        _claim_reference_attempt(args.run_root.resolve(), candidate_sha)
        run = RunDirectory.create(args.run_root, "seedance-reference")
        state = validated["state"]
        metadata = {
            "backend": "seedance",
            "input_mode": "reference_video",
            "status": "combined_probe" if state == "ready_for_single_combined_probe" else "submitted",
            "model": validated["model"],
            "generation_submit_limit": 1,
            "generation_submit_count": 1,
            "automatic_retry_limit": 0,
            "candidate_sha256": candidate_sha,
            "payload_sha256": validated["payload_sha256"],
            "proxy_sha256": validated["proxy_sha256"],
            "restyle_prompt_sha256": validated["restyle_prompt_sha256"],
            "approval_sha256": validated["approval_sha256"],
            "capability_sha256": validated["capability_sha256"],
            "source_hashes": validated["source_hashes"],
            "source_snapshot_sha256": {
                name: snapshot[2] for name, snapshot in sorted(source_snapshots.items())
            },
        }
        _write_immutable_json(run.path / "metadata.json", metadata)
        base_url = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com")
        _write_immutable_json(
            run.path / "request.json",
            {
                "method": "POST",
                "endpoint": f"{base_url.rstrip('/')}/v1/task/submit",
                "payload": validated["payload"],
            },
        )
        # Rehash at the last possible boundary. A failure still leaves the
        # immutable claim and therefore consumes the sole permitted attempt.
        _ensure_snapshot_unchanged(
            args.candidate,
            candidate_identity,
            candidate_bytes,
            "Seedance reference candidate",
        )
        if hashlib.sha256(args.candidate.read_bytes()).hexdigest() != candidate_sha:
            raise ValueError("Seedance reference candidate changed before transport")
        _ensure_reference_sources_unchanged(source_snapshots)
        print(f"RUN_DIR={run.path.resolve()}", flush=True)
        submit_once(
            validated["payload"], api_key, base_url, _ImmutableSubmissionRun(run)
        )
        return
    if args.command in ("submit-kling", "submit-seedance"):
        backend = "kling" if args.command == "submit-kling" else "seedance"
        bundle_bytes, bundle_identity = _read_snapshot(args.bundle, "trajectory bundle")
        bundle = _strict_object(bundle_bytes, "trajectory bundle")
        trajectory_metadata = {}
        if args.prompt == "trajectory_compiled":
            shot, trajectory_sha256, source_sha256 = validate_trajectory_bundle(
                bundle, backend, args.shot
            )
        elif args.prompt == REVISION_PROMPT_CONDITION:
            shot, trajectory_metadata = validate_revision_trajectory_bundle(
                bundle, backend, args.shot
            )
            trajectory_metadata["revision_bundle_sha256"] = hashlib.sha256(bundle_bytes).hexdigest()
        else:
            shot = find_shot(bundle, args.shot)
        prompt = shot["prompts"][args.prompt]
        duration = int(round(shot["duration"]))
        if args.command == "submit-kling":
            payload = build_kling_t2v(prompt, duration)
        else:
            payload = build_seedance_t2v(prompt, duration)
        if args.prompt == "trajectory_compiled":
            trajectory_metadata = {
                "trajectory_sha256": trajectory_sha256,
                "source_sha256": source_sha256,
                "compiled_prompt": prompt,
                "backend_capability": bundle["backend_capability"],
            }
        # Credential access is deliberately after complete provenance and
        # payload validation.  Recheck the source snapshot immediately before
        # creating the run directory or invoking transport.
        api_key = require_key()
        _ensure_snapshot_unchanged(
            args.bundle, bundle_identity, bundle_bytes, "trajectory bundle"
        )
        run = RunDirectory.create(args.run_root, backend)
        run.write_json(
            "metadata.json",
            {
                "backend": backend,
                "shot_id": args.shot,
                "prompt_condition": args.prompt,
                "submit_retry_limit": 0,
                **trajectory_metadata,
            },
        )
        print(f"RUN_DIR={run.path.resolve()}", flush=True)
        submit_once(
            payload,
            api_key,
            os.environ.get(
                "JD_KLING_BASE", "https://modelservice.jdcloud.com"
            ),
            run,
        )
        return

    run = RunDirectory(args.run_dir.resolve())
    if args.command == "query":
        api_key = require_key()
        attempted_at = datetime.now(timezone.utc)
        timestamp = attempted_at.strftime("%Y%m%dT%H%M%S%fZ")
        state_path = run.path / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        previous_count = state.get("query_count", 0)
        if type(previous_count) is not int or previous_count < 0:
            raise ValueError("state query_count is invalid")
        query_count = previous_count + 1
        state["query_count"] = query_count
        run.write_json("state.json", state)
        try:
            response = query_once(api_key, run)
        except Exception as exc:
            _write_immutable_json(
                run.path / f"query_attempt_{timestamp}.json",
                {
                    "attempted_at_utc": attempted_at.isoformat().replace("+00:00", "Z"),
                    "query_count": query_count,
                    "failure": {"type": type(exc).__name__, "message": str(exc)},
                },
            )
            raise
        current_state = json.loads(state_path.read_text(encoding="utf-8"))
        current_state["query_count"] = query_count
        run.write_json("state.json", current_state)
        _write_immutable_json(
            run.path / f"query_attempt_{timestamp}.json",
            {
                "attempted_at_utc": attempted_at.isoformat().replace("+00:00", "Z"),
                "query_count": query_count,
                "response_sha256": _canonical_json_sha256(response),
            },
        )
        return
    if args.command == "download":
        download_once(run)
        return
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
