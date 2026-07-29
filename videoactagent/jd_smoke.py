from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.backends.jd import (
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

    query = subparsers.add_parser("query")
    query.add_argument("--run-dir", type=Path, required=True)

    download = subparsers.add_parser("download")
    download.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
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
        query_once(require_key(), run)
        return
    if args.command == "download":
        download_once(run)
        return
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
