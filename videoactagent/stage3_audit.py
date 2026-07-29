from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse
import uuid

from videoactagent.backends.jd import extract_status, extract_task_id, extract_video_urls


MEDIA_PREFIX = "STAGE3_MEDIA_META="
OFFICIAL_GATEWAY_HOST = "modelservice.jdcloud.com"


def _read_json(path: Path) -> tuple[dict, dict]:
    if not path.is_file():
        raise FileNotFoundError(f"required evidence file missing: {path}")
    contents = path.read_bytes()
    return json.loads(contents.decode("utf-8")), {
        "path": path.name,
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_gateway_evidence(request: dict, state: dict, backend: str) -> None:
    base_url = state.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError("state base_url is missing")
    parsed_base = urlparse(base_url)
    if (
        parsed_base.scheme != "https"
        or parsed_base.hostname != OFFICIAL_GATEWAY_HOST
        or parsed_base.port not in (None, 443)
        or parsed_base.path not in ("", "/")
        or parsed_base.params
        or parsed_base.query
        or parsed_base.fragment
        or parsed_base.username is not None
    ):
        raise ValueError("state base_url is not the official gateway host")

    endpoint = request.get("endpoint")
    expected_endpoint = f"{base_url.rstrip('/')}/v1/task/submit"
    if endpoint != expected_endpoint:
        raise ValueError("request endpoint does not match the official gateway")

    model = (request.get("payload") or {}).get("model")
    expected_model_prefix = {
        "kling": "Kling-",
        "seedance": "Doubao-Seedance-",
    }.get(backend)
    if expected_model_prefix is None:
        raise ValueError(f"unsupported metadata backend: {backend!r}")
    if not isinstance(model, str) or not model.startswith(expected_model_prefix):
        raise ValueError(f"request model does not match {backend} backend")


def _validate_output_path(run_dir: Path, output: Path) -> Path:
    output = output.resolve()
    reserved = run_dir / "audit.json"
    if output != reserved and output.is_relative_to(run_dir):
        raise ValueError(
            "output inside run directory must be the reserved audit.json"
        )
    return output


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _probe_with_blender(video: Path, blender: Path) -> dict:
    if not blender.is_file():
        raise FileNotFoundError(f"Blender executable missing: {blender}")
    video_literal = repr(str(video.resolve()))
    expression = (
        "import bpy,json;"
        f"c=bpy.data.movieclips.load({video_literal});"
        "print('STAGE3_MEDIA_META='+json.dumps({"
        "'blender_version':bpy.app.version_string,"
        "'width':c.size[0],'height':c.size[1],"
        "'frames':c.frame_duration,'fps':c.fps,"
        "'duration_seconds':c.frame_duration/c.fps"
        "},sort_keys=True))"
    )
    completed = subprocess.run(
        [
            str(blender.resolve()),
            "--background",
            "--factory-startup",
            "--python-expr",
            expression,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Blender media probe failed with exit code {completed.returncode}"
        )
    for line in completed.stdout.splitlines():
        if line.startswith(MEDIA_PREFIX):
            return json.loads(line[len(MEDIA_PREFIX) :])
    raise RuntimeError("Blender media probe did not emit metadata")


def audit_existing_run(run_dir: Path, blender: Path) -> dict:
    run_dir = run_dir.resolve()
    evidence_files = {}
    metadata, evidence_files["metadata.json"] = _read_json(
        run_dir / "metadata.json"
    )
    request, evidence_files["request.json"] = _read_json(run_dir / "request.json")
    response, evidence_files["response.json"] = _read_json(
        run_dir / "response.json"
    )
    state, evidence_files["state.json"] = _read_json(run_dir / "state.json")
    download, evidence_files["download.json"] = _read_json(
        run_dir / "download.json"
    )

    backend = metadata.get("backend")
    if backend not in {"kling", "seedance"}:
        raise ValueError(f"unsupported metadata backend: {backend!r}")
    if request.get("method") != "POST":
        raise ValueError("persisted submission request is not POST")
    _validate_gateway_evidence(request, state, backend)
    task_id = extract_task_id(response)
    query_paths = sorted(run_dir.glob("query_*.json"))
    if not query_paths:
        raise ValueError("no persisted query evidence found")
    queries = []
    for path in query_paths:
        query, record = _read_json(path)
        queries.append(query)
        evidence_files[path.name] = record
    query_task_ids = [query.get("task_id") for query in queries]
    task_ids_match = (
        state.get("task_id") == task_id
        and all(query_id == task_id for query_id in query_task_ids)
    )
    if not task_ids_match:
        raise ValueError("task IDs do not match across persisted evidence")
    statuses = [extract_status(query) for query in queries]
    terminal_status_success = statuses[-1] == "success"
    if not terminal_status_success or state.get("status") != "success":
        raise ValueError("persisted task does not have terminal success evidence")
    terminal_video_urls = [
        {"id": item_id, "url": url}
        for item_id, url in extract_video_urls(queries[-1])
    ]
    if state.get("video_urls") != terminal_video_urls:
        raise ValueError("terminal video URLs do not agree with state")

    result_name = download.get("path")
    if result_name != Path(str(result_name)).name:
        raise ValueError("download path must name a file within the run directory")
    result_path = run_dir / result_name
    if not result_path.is_file():
        raise FileNotFoundError(f"downloaded result missing: {result_path}")
    actual_bytes = result_path.stat().st_size
    actual_sha256 = _sha256(result_path)
    if download.get("bytes") != actual_bytes:
        raise ValueError("download byte count mismatch")
    if download.get("sha256") != actual_sha256:
        raise ValueError("download sha256 mismatch")
    media = _probe_with_blender(result_path, blender.resolve())

    return {
        "schema_version": 1,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "evidence_source": "persisted_gateway_records_internal_consistency",
        "network_called": False,
        "backend": backend,
        "task_id": task_id,
        "query_count": len(queries),
        "status_sequence": statuses,
        "evidence_files": evidence_files,
        "checks": {
            "task_ids_match": task_ids_match,
            "terminal_status_success": terminal_status_success,
            "request_endpoint_model_valid": True,
            "state_base_url_official_host": True,
            "terminal_video_urls_match_state": True,
            "download_record_matches_file": True,
            "blender_media_readable": True,
        },
        "result": {
            "path": result_path.name,
            "bytes": actual_bytes,
            "sha256": actual_sha256,
            "blender_media": media,
        },
        **(
            {
                "seedance": {
                    "outcome": "unknown",
                    "source": "project_context_not_part_of_run",
                }
            }
            if backend == "kling"
            else {}
        ),
        "billing_interpreted": False,
        "quality_evaluated": False,
    }


def audit_existing_kling_run(run_dir: Path, blender: Path) -> dict:
    """Backward-compatible Kling-only entry point."""

    audit = audit_existing_run(run_dir, blender)
    if audit["backend"] != "kling":
        raise ValueError("metadata backend is not kling")
    return audit


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit an existing persisted JD video run offline."
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    output = _validate_output_path(run_dir, args.output)
    audit = audit_existing_run(run_dir, args.blender)
    contents = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    payload = contents.encode("utf-8")
    _atomic_write(output, payload)
    print(
        "STAGE3_AUDIT_OK "
        f"output={output} "
        f"sha256={hashlib.sha256(payload).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
