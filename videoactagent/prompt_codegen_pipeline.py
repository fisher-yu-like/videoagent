"""Prompt-first whole-story pipeline: DeepSeek ShotScript -> Blender code -> MP4."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from threading import Lock
from typing import Any
from urllib.request import urlopen

from videoactagent.codegen_contract import snapshot_codegen_inputs
from videoactagent.codegen_safety import validate_generated_code
from videoactagent.deepseek_blender_codegen import request_blender_code
from videoactagent.deepseek_planner import request_scene_plan
from videoactagent.scene_plan import ScenePlanDraft


MODEL = "deepseek-v4-pro"
SCHEMA_VERSION = "prompt-codegen-job-1.0"


class PromptPipelineError(ValueError):
    """Raised when a prompt-first job exhausts its bounded repair attempts."""

    def __init__(self, message: str, *, job_path: Path, document: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.job_path = str(job_path)
        self.document = dict(document or {})


@dataclass(frozen=True)
class PromptPipelineConfig:
    experiments_root: Path
    blender: Path
    protected_workspace: Path
    protected_url: str
    protection_probe: Callable[[], Mapping[str, Any]] | None = None
    duration_seconds: float = 5.0
    fps: int = 8
    resolution: tuple[int, int] = (640, 360)
    max_attempts: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiments_root", Path(self.experiments_root).resolve())
        object.__setattr__(self, "blender", Path(self.blender).resolve())
        object.__setattr__(self, "protected_workspace", Path(self.protected_workspace).resolve())
        if self.duration_seconds <= 0 or self.fps <= 0 or self.max_attempts < 1:
            raise ValueError("duration_seconds, fps and max_attempts must be positive")
        if len(self.resolution) != 2 or any(int(value) <= 0 for value in self.resolution):
            raise ValueError("resolution must contain positive width and height")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _tree_digest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"protected workspace is not a directory: {root}")
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": path.relative_to(root).as_posix(), "sha256": digest, "bytes": path.stat().st_size})
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"root": str(root), "tree_sha256": hashlib.sha256(canonical).hexdigest(), "files": files}


def _guard_get(url: str) -> dict[str, Any]:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("protected_url must be an HTTP(S) URL")
    with urlopen(url, timeout=20) as response:
        status = int(getattr(response, "status", response.getcode()))
        body = response.read()
    return {"status": status, "body_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def _default_blender_runner(**kwargs: Any) -> dict[str, Any]:
    from videoactagent.codegen_blender_runner import run_codegen_blender
    return run_codegen_blender(**kwargs)


def _default_verifier(**kwargs: Any) -> dict[str, Any]:
    from videoactagent.codegen_verify import verify_codegen_render
    return verify_codegen_render(**kwargs)


def _error_text(exc: BaseException, *, job_root: Path | None = None) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if job_root is not None:
        log = job_root / "render.log"
        if log.is_file():
            try:
                content = log.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                content = ""
            if content:
                message += "\nBlender log:\n" + content[-4000:]
    return message[-6000:]


def _probe_default(config: PromptPipelineConfig) -> dict[str, Any]:
    protected = _tree_digest(config.protected_workspace)
    guard = _guard_get(config.protected_url)
    return {"tree_sha256": protected["tree_sha256"], "guard": guard}


def _fingerprint(value: Mapping[str, Any]) -> tuple[Any, Any]:
    guard = value.get("guard")
    if isinstance(guard, Mapping):
        guard_value = guard.get("body_sha256", guard.get("guard_sha256", guard.get("status")))
    else:
        guard_value = value.get("guard_sha256", guard)
    return value.get("tree_sha256"), guard_value


class PromptCodegenRunner:
    """Run one prompt end-to-end, with at most three attempts per stage."""

    def __init__(
        self,
        config: PromptPipelineConfig,
        *,
        planner: Callable[..., dict[str, Any]] = request_scene_plan,
        codegen: Callable[..., dict[str, Any]] = request_blender_code,
        safety_validator: Callable[[str], dict[str, Any]] = validate_generated_code,
        blender_runner: Callable[..., dict[str, Any]] = _default_blender_runner,
        verifier: Callable[..., dict[str, Any]] = _default_verifier,
    ) -> None:
        self.config = config
        self.planner = planner
        self.codegen = codegen
        self.safety_validator = safety_validator
        self.blender_runner = blender_runner
        self.verifier = verifier
        self._allocation_lock = Lock()

    def _allocate(self) -> tuple[str, Path]:
        root = self.config.experiments_root
        root.mkdir(parents=True, exist_ok=True)
        with self._allocation_lock:
            ids = [int(path.name[2:]) for path in root.glob("PF*") if path.is_dir() and path.name[2:].isdigit()]
            job_id = f"PF{max(ids, default=0) + 1}"
            job_root = root / job_id
            job_root.mkdir()
        return job_id, job_root

    def _probe(self) -> dict[str, Any]:
        value = self.config.protection_probe() if self.config.protection_probe else _probe_default(self.config)
        if not isinstance(value, Mapping):
            raise PromptPipelineError("protection probe must return an object", job_path=Path(""))
        return dict(value)

    def _fail(self, job_path: Path, document: dict[str, Any], message: str) -> PromptPipelineError:
        document["status"] = "failed"
        document["error"] = message
        document["updated_at"] = _now()
        document.setdefault("history", []).append({"status": "failed", "at": document["updated_at"], "error": message})
        _write(job_path, document)
        return PromptPipelineError(message, job_path=job_path, document=document)

    def run(self, prompt: str) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > 20000:
            raise ValueError("prompt is too long")
        job_id, job_root = self._allocate()
        job_path = job_root / "job.json"
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "status": "queued",
            "model": MODEL,
            "created_at": _now(),
            "updated_at": _now(),
            "api_call_count": 0,
            "retry_count": 0,
            "planner_attempts": 0,
            "codegen_attempts": 0,
            "paths": {"job_root": str(job_root), "source": "source", "plan": "plan", "api": "api", "renders": "renders"},
            "history": [{"status": "queued", "at": _now(), "error": None}],
        }
        _write_text(job_root / "prompt.txt", prompt.strip() + "\n")
        _write(job_path, document)
        try:
            before = self._probe()
            document["protected_before"] = before
            _write(job_path, document)
        except Exception as exc:
            raise self._fail(job_path, document, _error_text(exc))

        feedback: str | None = None
        draft: dict[str, Any] | None = None
        planner_evidence: list[dict[str, Any]] = []
        document["status"] = "planning"
        _write(job_path, document)
        for attempt in range(1, self.config.max_attempts + 1):
            attempt_dir = job_root / "plan" / f"attempt-{attempt}"
            try:
                evidence = self.planner(
                    story_prompt=prompt.strip(), duration_seconds=self.config.duration_seconds,
                    output_dir=attempt_dir, feedback=feedback, previous_draft=draft,
                    model=MODEL,
                )
                planner_evidence.append(dict(evidence or {}))
                draft_path = attempt_dir / "draft.json"
                draft = json.loads(draft_path.read_text(encoding="utf-8"))
                parsed = ScenePlanDraft.from_dict(draft)
                shotscript = parsed.to_shotscript(prompt.strip())
                trajectory = parsed.to_trajectory()
                actor_ids = {actor["id"] for actor in shotscript["shots"][0]["actors"]}
                trajectory["tracks"] = [track for track in trajectory["tracks"] if track["target"]["type"] == "actor" and track["target"]["id"] in actor_ids]
                _write(attempt_dir / "shotscript.json", shotscript)
                _write(attempt_dir / "trajectory.json", trajectory)
                document.update({"planner_attempts": attempt, "planner_evidence": planner_evidence, "plan": f"plan/attempt-{attempt}", "status": "shot_validated", "updated_at": _now()})
                _write(job_path, document)
                break
            except Exception as exc:
                feedback = _error_text(exc)
                _write(attempt_dir / "error.json", {"error": feedback})
                document.update({"planner_attempts": attempt, "planner_evidence": planner_evidence, "updated_at": _now()})
                _write(job_path, document)
        else:
            raise self._fail(job_path, document, feedback or "planner failed")

        try:
            source_input = snapshot_codegen_inputs(
                prompt_path=job_root / "prompt.txt",
                shotscript_path=job_root / document["plan"] / "shotscript.json",
                trajectory_path=job_root / document["plan"] / "trajectory.json",
                destination=job_root / "source",
                fps=self.config.fps,
                resolution=self.config.resolution,
            )
            _write(job_path, document)
        except Exception as exc:
            raise self._fail(job_path, document, _error_text(exc))

        code_feedback: str | None = None
        codegen_evidence: list[dict[str, Any]] = []
        document["status"] = "codegen"
        _write(job_path, document)
        for attempt in range(1, self.config.max_attempts + 1):
            api_dir = job_root / "api" / f"attempt-{attempt}"
            render_dir = job_root / "renders" / f"attempt-{attempt}"
            try:
                codegen_input = dict(source_input)
                if code_feedback:
                    codegen_input["repair_feedback"] = code_feedback
                evidence = self.codegen(codegen_input=codegen_input, output_dir=api_dir)
                codegen_evidence.append(dict(evidence or {}))
                code_file = api_dir / "generated_scene.py"
                if not code_file.is_file():
                    raise ValueError("codegen succeeded without generated_scene.py")
                safety = self.safety_validator(code_file.read_text(encoding="utf-8"))
                api_file = job_root / "api" / "generated_scene.py"
                shutil.copyfile(code_file, api_file)
                render_result = self.blender_runner(
                    blender=self.config.blender, job_root=job_root,
                    input_path=job_root / "source" / "input.json", code_path=api_file,
                    output_dir=render_dir,
                )
                verification = self.verifier(job_root=job_root, input_path=job_root / "source" / "input.json", render_dir=render_dir)
                after = self._probe()
                if _fingerprint(before) != _fingerprint(after):
                    raise ValueError("protected workspace or guard changed during the job")
                smoke_dir = job_root / "renders" / "smoke"
                shutil.copytree(render_dir, smoke_dir)
                video = smoke_dir / "video.mp4"
                if not video.is_file() or video.stat().st_size <= 0:
                    raise ValueError("verified render did not contain video.mp4")
                document.update({
                    "status": "succeeded", "codegen_attempts": attempt,
                    "codegen_evidence": codegen_evidence, "code_safety": safety,
                    "code_sha256": _sha256(api_file), "renders": {"smoke": {"path": "renders/smoke", "verification": verification, "render": render_result}},
                    "video": str(video), "api_call_count": sum(int(item.get("api_call_count", 1)) for item in planner_evidence + codegen_evidence),
                    "retry_count": max(0, document["planner_attempts"] - 1) + max(0, attempt - 1), "updated_at": _now(),
                })
                document.setdefault("history", []).append({"status": "succeeded", "at": document["updated_at"], "error": None})
                _write(job_path, document)
                return {"status": "succeeded", "job": str(job_path), "video": str(video), "document": document}
            except Exception as exc:
                code_feedback = _error_text(exc, job_root=job_root)
                _write(api_dir / "error.json", {"error": code_feedback})
                document.update({"codegen_attempts": attempt, "codegen_evidence": codegen_evidence, "updated_at": _now()})
                _write(job_path, document)
        raise self._fail(job_path, document, code_feedback or "codegen/render failed")
