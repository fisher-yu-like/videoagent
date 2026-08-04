"""Immutable CG jobs for the isolated DeepSeek Blender codegen experiment."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any
from urllib.request import urlopen
from uuid import uuid4

from videoactagent.codegen_contract import snapshot_codegen_inputs
from videoactagent.codegen_safety import CodegenSafetyError, validate_generated_code
from videoactagent.codegen_verify import CodegenVerifyError, verify_codegen_render
from videoactagent.codegen_blender_runner import CodegenBlenderRunnerError, run_codegen_blender
from videoactagent.deepseek_blender_codegen import DeepSeekCodegenError, request_blender_code


TERMINAL = {"api_failed", "code_rejected", "blender_failed", "output_invalid", "isolation_failed", "succeeded"}
SCHEMA_VERSION = "blender-codegen-job-1.0"


class CodegenJobError(ValueError):
    """Raised when a CG job cannot advance without violating evidence rules."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodegenJobError(f"cannot read job: {path}") from exc
    if not isinstance(value, dict):
        raise CodegenJobError("job document must be an object")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise CodegenJobError(f"protected workspace is not a directory: {root}")
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        files.append({"path": relative, "sha256": _sha(path), "bytes": path.stat().st_size})
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"root": str(root), "tree_sha256": hashlib.sha256(canonical).hexdigest(), "files": files}


def _guard_get(url: str, transport: Callable[..., Any] = urlopen) -> dict[str, Any]:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise CodegenJobError("protected_url must be an HTTP(S) URL")
    try:
        with transport(url, timeout=20) as response:
            raw_status = getattr(response, "status", None)
            if raw_status is None and hasattr(response, "getcode"):
                raw_status = response.getcode()
            status = int(raw_status)
            try:
                body = response.read(1024 * 1024)
            except TypeError:
                body = response.read()
    except Exception as exc:
        raise CodegenJobError(f"protected URL guard failed: {type(exc).__name__}: {exc}") from exc
    if status != 200:
        raise CodegenJobError(f"protected URL guard returned HTTP {status}")
    if not isinstance(body, bytes):
        body = bytes(body)
    return {"status": status, "body_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


@contextmanager
def _job_lock(job_root: Path):
    lock = job_root / "job.lock"
    acquired = False
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(_now())
        acquired = True
        yield
    except FileExistsError as exc:
        raise CodegenJobError("job already has an active execution lock") from exc
    finally:
        if acquired:
            lock.unlink(missing_ok=True)


def _job_path(value: object) -> Path:
    path = Path(value) if isinstance(value, (str, Path)) else None
    if path is None or not path.is_file() or path.name != "job.json":
        raise CodegenJobError("--job must point to an existing job.json")
    return path.resolve()


def _set_state(job_path: Path, job: dict[str, Any], status: str, *, error: str | None = None) -> None:
    job["status"] = status
    job["updated_at"] = _now()
    if error is not None:
        job["error"] = error
    job.setdefault("history", []).append({"status": status, "at": job["updated_at"], "error": error})
    _write(job_path, job)


def prepare_codegen_job(
    *,
    experiments_root: Path,
    protected_workspace: Path,
    protected_url: str,
    prompt_path: Path,
    shotscript_path: Path,
    trajectory_path: Path,
    fps: int = 8,
    resolution: tuple[int, int] = (640, 360),
    guard_transport: Callable[..., Any] | None = None,
) -> Path:
    """Create a new prepared CG job with no API or Blender call."""

    root = Path(experiments_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing_ids = []
    for child in root.glob("CG*"):
        if child.is_dir() and child.name[2:].isdigit():
            existing_ids.append(int(child.name[2:]))
    job_id = f"CG{max(existing_ids, default=0) + 1}"
    job_root = root / job_id
    job_root.mkdir()
    try:
        protected = _tree_digest(Path(protected_workspace))
        guard = _guard_get(protected_url, urlopen if guard_transport is None else guard_transport)
        source = job_root / "source"
        value = snapshot_codegen_inputs(prompt_path=Path(prompt_path), shotscript_path=Path(shotscript_path), trajectory_path=Path(trajectory_path), destination=source, fps=fps, resolution=resolution)
        job = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "status": "prepared",
            "created_at": _now(),
            "updated_at": _now(),
            "api_call_count": 0,
            "retry_count": 0,
            "paths": {"job_root": str(job_root), "input": "source/input.json", "api": "api", "renders": "renders"},
            "protected": {"workspace": str(Path(protected_workspace).resolve()), "url": protected_url, **protected, "guard": guard},
            "render_contract": value["render_contract"],
            "history": [{"status": "prepared", "at": _now(), "error": None}],
        }
        _write(job_root / "job.json", job)
        return job_root / "job.json"
    except Exception:
        shutil.rmtree(job_root, ignore_errors=True)
        raise


def generate_codegen_job(
    job_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
    requester: Callable[..., dict[str, Any]] = request_blender_code,
    safety_validator: Callable[[str], dict[str, Any]] = validate_generated_code,
) -> dict[str, Any]:
    """Make the job's one API call and validate the returned Python."""

    job_file = _job_path(job_path)
    job = _load(job_file)
    job_root = job_file.parent
    if job.get("status") != "prepared":
        raise CodegenJobError(f"job cannot generate from status {job.get('status')!r}")
    with _job_lock(job_root):
        _set_state(job_file, job, "generating")
        input_file = job_root / job["paths"]["input"]
        api_dir = job_root / job["paths"]["api"]
        if api_dir.exists():
            _set_state(job_file, job, "api_failed", error="API output directory already exists; retry is forbidden")
            raise CodegenJobError(job["error"])
        try:
            document = json.loads(input_file.read_text(encoding="utf-8"))
            evidence = requester(codegen_input=document, output_dir=api_dir, environ=environ, transport=transport)
            job["api_call_count"] = int(evidence.get("api_call_count", 1))
            job["retry_count"] = int(evidence.get("retry_count", 0))
            code_file = api_dir / "generated_scene.py"
            if not code_file.is_file():
                raise CodegenJobError("API succeeded without generated_scene.py")
            safety = safety_validator(code_file.read_text(encoding="utf-8"))
            job["code_sha256"] = _sha(code_file)
            job["code_safety"] = safety
            job["api_evidence"] = evidence
            _set_state(job_file, job, "code_validated")
            return job
        except CodegenSafetyError as exc:
            job["api_evidence"] = _load(api_dir / "evidence.json") if (api_dir / "evidence.json").is_file() else None
            _set_state(job_file, job, "code_rejected", error=str(exc))
            raise CodegenJobError(str(exc)) from exc
        except Exception as exc:
            evidence_path = api_dir / "evidence.json"
            job["api_evidence"] = _load(evidence_path) if evidence_path.is_file() else None
            _set_state(job_file, job, "api_failed", error=f"{type(exc).__name__}: {exc}")
            raise CodegenJobError(job["error"]) from exc


def _profile_input(job_root: Path, source_input: Path, profile: str) -> Path:
    if profile == "smoke":
        return source_input
    document = json.loads(source_input.read_text(encoding="utf-8"))
    contract = dict(document["render_contract"])
    contract["fps"] = 24
    contract["resolution"] = [960, 540]
    contract["frame_end"] = int(round(float(contract["duration_seconds"]) * 24))
    document["render_contract"] = contract
    target = job_root / "source" / "render_full_input.json"
    _write(target, document)
    return target


def _isolation_ok(job: Mapping[str, Any], guard_transport: Callable[..., Any]) -> tuple[bool, str | None]:
    protected = job["protected"]
    current = _tree_digest(Path(protected["workspace"]))
    if current["tree_sha256"] != protected["tree_sha256"]:
        return False, "protected workspace digest changed"
    current_guard = _guard_get(protected["url"], guard_transport)
    if current_guard["status"] != protected["guard"]["status"] or current_guard["body_sha256"] != protected["guard"]["body_sha256"]:
        return False, "protected URL guard changed"
    return True, None


def render_codegen_job(
    job_path: Path,
    *,
    blender: Path,
    profile: str,
    runner: Callable[..., dict[str, Any]] = run_codegen_blender,
    verifier: Callable[..., dict[str, Any]] = verify_codegen_render,
    guard_transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Render validated code once under the requested smoke/full profile."""

    if profile not in {"smoke", "full"}:
        raise CodegenJobError("profile must be smoke or full")
    job_file = _job_path(job_path)
    job = _load(job_file)
    if job.get("status") != "code_validated":
        raise CodegenJobError(f"job cannot render from status {job.get('status')!r}")
    with _job_lock(job_file.parent):
        _set_state(job_file, job, "rendering")
        job_root = job_file.parent
        source_input = job_root / job["paths"]["input"]
        input_file = _profile_input(job_root, source_input, profile)
        code_file = job_root / job["paths"]["api"] / "generated_scene.py"
        output_dir = job_root / job["paths"]["renders"] / profile
        if output_dir.exists():
            _set_state(job_file, job, "blender_failed", error="render output already exists; rerender is forbidden")
            raise CodegenJobError(job["error"])
        try:
            render_result = runner(blender=Path(blender), job_root=job_root, input_path=input_file, code_path=code_file, output_dir=output_dir)
        except Exception as exc:
            _set_state(job_file, job, "blender_failed", error=f"{type(exc).__name__}: {exc}")
            raise CodegenJobError(job["error"]) from exc
        try:
            verification = verifier(job_root=job_root, input_path=input_file, render_dir=output_dir)
        except Exception as exc:
            _set_state(job_file, job, "output_invalid", error=f"{type(exc).__name__}: {exc}")
            raise CodegenJobError(job["error"]) from exc
        try:
            isolation, reason = _isolation_ok(job, urlopen if guard_transport is None else guard_transport)
        except Exception as exc:
            isolation, reason = False, f"isolation guard failed: {type(exc).__name__}: {exc}"
        if not isolation:
            _set_state(job_file, job, "isolation_failed", error=reason)
            raise CodegenJobError(reason or "isolation failed")
        job.setdefault("renders", {})[profile] = {"render": render_result, "verification": verification, "input": str(input_file.relative_to(job_root)).replace("\\", "/")}
        _set_state(job_file, job, "succeeded")
        return {"status": "succeeded", "profile": profile, "render": render_result, "verification": verification}


def _resolution(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().split("x", 1)
        result = (int(width), int(height))
    except (ValueError, AttributeError) as exc:
        raise CodegenJobError("resolution must be WIDTHxHEIGHT") from exc
    if result[0] <= 0 or result[1] <= 0:
        raise CodegenJobError("resolution must be positive")
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--experiments-root", type=Path, required=True)
    prepare.add_argument("--protected-workspace", type=Path, required=True)
    prepare.add_argument("--protected-url", required=True)
    prepare.add_argument("--prompt", type=Path, required=True)
    prepare.add_argument("--shotscript", type=Path, required=True)
    prepare.add_argument("--trajectory", type=Path, required=True)
    prepare.add_argument("--fps", type=int, default=8)
    prepare.add_argument("--resolution", default="640x360")
    generate = sub.add_parser("generate"); generate.add_argument("--job", type=Path, required=True)
    for name in ("render-smoke", "render-full"):
        item = sub.add_parser(name); item.add_argument("--job", type=Path, required=True); item.add_argument("--blender", type=Path, required=True)
    status = sub.add_parser("status"); status.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_codegen_job(experiments_root=args.experiments_root, protected_workspace=args.protected_workspace, protected_url=args.protected_url, prompt_path=args.prompt, shotscript_path=args.shotscript, trajectory_path=args.trajectory, fps=args.fps, resolution=_resolution(args.resolution))
            print(result); return 0
        if args.command == "generate":
            print(json.dumps(generate_codegen_job(args.job), ensure_ascii=False, indent=2)); return 0
        if args.command.startswith("render-"):
            profile = "smoke" if args.command == "render-smoke" else "full"
            print(json.dumps(render_codegen_job(args.job, blender=args.blender, profile=profile), ensure_ascii=False, indent=2)); return 0
        print(json.dumps(_load(_job_path(args.job)), ensure_ascii=False, indent=2)); return 0
    except (CodegenJobError, OSError, ValueError) as exc:
        print(f"CODEGEN_JOB_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
