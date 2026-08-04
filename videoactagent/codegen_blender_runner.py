"""Host-side process boundary for trusted Blender codegen rendering."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from videoactagent.codegen_safety import CodegenSafetyError, validate_generated_code


class CodegenBlenderRunnerError(ValueError):
    """Raised when Blender cannot produce hash-bound evidence."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, path: Path, label: str) -> Path:
    root = root.resolve()
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CodegenBlenderRunnerError(f"{label} must stay below job root") from exc
    return resolved


def _nonempty(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise CodegenBlenderRunnerError(f"{label} is missing or empty: {path}")


def run_codegen_blender(
    *,
    blender: Path,
    job_root: Path,
    input_path: Path,
    code_path: Path,
    output_dir: Path,
    timeout: int = 330,
) -> dict[str, object]:
    """Run one isolated Blender process and verify its output envelope."""

    root = Path(job_root).resolve()
    if not root.is_dir():
        raise CodegenBlenderRunnerError(f"job root is not a directory: {root}")
    input_file = _inside(root, Path(input_path), "input")
    code_file = _inside(root, Path(code_path), "code")
    if not input_file.is_file() or not code_file.is_file():
        raise CodegenBlenderRunnerError("input and code files must exist below job root")
    output = _inside(root, Path(output_dir), "output")
    if output == root or output.exists():
        raise CodegenBlenderRunnerError("output directory must be a new child of job root")
    if type(timeout) is not int or timeout <= 0:
        raise CodegenBlenderRunnerError("timeout must be a positive integer")
    blender_path = Path(blender).resolve(strict=False)
    input_hash = _sha256(input_file)
    code_hash = _sha256(code_file)
    try:
        validate_generated_code(code_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, CodegenSafetyError) as exc:
        raise CodegenBlenderRunnerError(f"generated code rejected before Blender: {exc}") from exc
    entrypoint = Path(__file__).with_name("codegen_blender_entry.py").resolve()
    command = [
        str(blender_path), "--background", "--factory-startup", "-F", "FFMPEG", "--python", str(entrypoint), "--",
        "--input", str(input_file), "--code", str(code_file), "--output-dir", str(output),
        "--expected-input-sha256", input_hash, "--expected-code-sha256", code_hash,
    ]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DEEPSEEK_")}
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(command, cwd=str(root), env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        (root / "render.log").write_text(stdout + stderr, encoding="utf-8")
        raise CodegenBlenderRunnerError(f"Blender render timeout after {timeout}s") from exc
    finally:
        (root / "render.log").write_text(stdout + stderr, encoding="utf-8")
    evidence = stdout + stderr
    if completed.returncode != 0:
        raise CodegenBlenderRunnerError(f"Blender exited with code {completed.returncode}")
    if "BLENDER_CODEGEN_OK=" not in evidence:
        raise CodegenBlenderRunnerError("Blender exited without BLENDER_CODEGEN_OK marker")
    manifest_path = output / "codegen_manifest.json"
    _nonempty(manifest_path, "codegen manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodegenBlenderRunnerError("codegen manifest is not valid JSON") from exc
    if manifest.get("input_sha256") != input_hash or manifest.get("code_sha256") != code_hash:
        raise CodegenBlenderRunnerError("manifest input/code hash mismatch")
    video_record = manifest.get("video")
    blend_record = manifest.get("blend")
    if not isinstance(video_record, dict) or not isinstance(blend_record, dict):
        raise CodegenBlenderRunnerError("manifest artifact records are missing")
    for record, label in ((video_record, "video"), (blend_record, "blend")):
        relative = record.get("path")
        if not isinstance(relative, str):
            raise CodegenBlenderRunnerError(f"manifest {label} path is invalid")
        artifact = _inside(output, output / relative, f"manifest {label}")
        _nonempty(artifact, label)
        if record.get("sha256") != _sha256(artifact):
            raise CodegenBlenderRunnerError(f"manifest {label} hash mismatch")
    for name in ("first", "middle", "last"):
        _nonempty(output / "frames" / f"{name}.png", f"{name} frame")
    return {
        "status": "succeeded",
        "output_dir": str(output),
        "input_sha256": input_hash,
        "code_sha256": code_hash,
        "manifest": manifest,
        "log_path": str(root / "render.log"),
        "stdout": stdout,
        "stderr": stderr,
    }
