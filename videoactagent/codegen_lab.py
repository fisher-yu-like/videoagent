"""Independent local browser service for the DeepSeek Blender codegen experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any, Callable, Mapping
from uuid import uuid4

from videoactagent.codegen_job import generate_codegen_job, prepare_codegen_job, render_codegen_job


class CodegenLabError(ValueError):
    """A user-visible Codegen Lab validation or state error."""


@dataclass(frozen=True)
class CodegenLabConfig:
    workspace: Path
    blender: Path
    port: int = 8781

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        object.__setattr__(self, "blender", Path(self.blender).resolve())

    @property
    def experiments_root(self) -> Path:
        return self.workspace / "runs" / "work" / "codegen_blender_v1"

    @property
    def static_root(self) -> Path:
        return Path(__file__).resolve().parent.parent / "static"


@dataclass
class Operation:
    operation_id: str
    job_path: str
    kind: str
    state: str = "queued"
    error: str | None = None
    result: dict[str, Any] | None = None
    _condition: Condition = field(default_factory=Condition, repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "job": self.job_path,
            "kind": self.kind,
            "state": self.state,
            "error": self.error,
            "result": self.result,
        }


def _load_job(job_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodegenLabError(f"job.json is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise CodegenLabError("job.json must be a JSON object")
    return value


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _artifact_urls(job_path: Path, root: Path) -> list[dict[str, str]]:
    allowed = {".mp4", ".png", ".json", ".log", ".txt", ".py"}
    result: list[dict[str, str]] = []
    for path in sorted(job_path.parent.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        relative = path.relative_to(job_path.parent).as_posix()
        if relative == "job.json":
            continue
        result.append({"path": relative, "url": f"/api/artifact?job={job_path.as_posix()}&path={relative}"})
    return result


def status_document(job_path: Path, operation: Operation | None = None) -> dict[str, Any]:
    """Return a deliberately small status view without secrets/file inventories."""

    job_file = Path(job_path).resolve()
    job = _load_job(job_file)
    allowed = {
        "schema_version", "job_id", "status", "created_at", "updated_at", "api_call_count",
        "retry_count", "error", "code_sha256", "code_safety", "render_contract", "renders", "history",
    }
    result = {key: job[key] for key in allowed if key in job}
    protected = job.get("protected")
    if isinstance(protected, Mapping):
        result["protected"] = {
            key: protected[key]
            for key in ("workspace", "tree_sha256", "guard")
            if key in protected
        }
    evidence = job.get("api_evidence")
    if isinstance(evidence, Mapping):
        result["api_evidence"] = {
            key: evidence[key]
            for key in ("status", "model", "elapsed_seconds", "api_call_count", "retry_count", "response_sha256", "code_sha256")
            if key in evidence and not isinstance(evidence[key], (Mapping, list))
        }
    result["artifacts"] = _artifact_urls(job_file, job_file.parent)
    if operation is not None:
        result["operation"] = operation.document()
    return result


class CodegenLabApplication:
    """Application service shared by the HTTP adapter and direct tests."""

    def __init__(
        self,
        config: CodegenLabConfig,
        *,
        prepare_fn: Callable[..., Path] = prepare_codegen_job,
        generate_fn: Callable[..., dict[str, Any]] = generate_codegen_job,
        render_fn: Callable[..., dict[str, Any]] = render_codegen_job,
    ) -> None:
        self.config = config
        self.prepare_fn = prepare_fn
        self.generate_fn = generate_fn
        self.render_fn = render_fn
        self._lock = Lock()
        self._operations: dict[str, Operation] = {}
        self._active_jobs: dict[Path, str] = {}

    def resolve_job(self, value: object) -> Path:
        if not isinstance(value, (str, Path)) or not str(value):
            raise CodegenLabError("job is required")
        path = Path(value).resolve()
        root = self.config.experiments_root.resolve()
        if path.name != "job.json" or not path.is_file() or not _inside(root, path.parent):
            raise CodegenLabError("job must be an existing CG job.json below experiments_root")
        return path

    def resolve_artifact(self, job_path: Path, value: object) -> Path:
        job_file = self.resolve_job(job_path)
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise CodegenLabError("artifact path must be relative")
        candidate = (job_file.parent / value).resolve()
        if not _inside(job_file.parent, candidate) or candidate == job_file:
            raise CodegenLabError("artifact path escapes the job")
        if candidate.suffix.lower() not in {".mp4", ".png", ".json", ".log", ".txt", ".py"} or not candidate.is_file():
            raise CodegenLabError("artifact is unavailable")
        return candidate

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = ("protected_workspace", "protected_url", "prompt", "shotscript", "trajectory")
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise CodegenLabError(f"missing fields: {', '.join(missing)}")
        try:
            job_path = self.prepare_fn(
                experiments_root=Path(payload.get("experiments_root") or self.config.experiments_root),
                protected_workspace=Path(str(payload["protected_workspace"])),
                protected_url=str(payload["protected_url"]),
                prompt_path=Path(str(payload["prompt"])),
                shotscript_path=Path(str(payload["shotscript"])),
                trajectory_path=Path(str(payload["trajectory"])),
                fps=int(payload.get("fps", 8)),
                resolution=_resolution(payload.get("resolution", "640x360")),
            )
        except CodegenLabError:
            raise
        except Exception as exc:
            raise CodegenLabError(f"prepare failed: {type(exc).__name__}: {exc}") from exc
        return status_document(Path(job_path))

    def _reserve(self, job_path: Path, kind: str) -> Operation:
        with self._lock:
            if job_path in self._active_jobs:
                raise CodegenLabError("job already has a running operation")
            operation = Operation(uuid4().hex, str(job_path), kind)
            self._operations[operation.operation_id] = operation
            self._active_jobs[job_path] = operation.operation_id
            return operation

    def _run(self, operation: Operation, job_path: Path, function: Callable[..., dict[str, Any]], kwargs: dict[str, Any]) -> None:
        with operation._condition:
            operation.state = "running"
            operation._condition.notify_all()
        try:
            operation.result = function(job_path, **kwargs)
            operation.state = "done"
        except Exception as exc:
            operation.error = f"{type(exc).__name__}: {exc}"
            operation.state = "error"
        finally:
            with self._lock:
                self._active_jobs.pop(job_path, None)
            with operation._condition:
                operation._condition.notify_all()

    def start_generate(self, value: object) -> dict[str, Any]:
        job_path = self.resolve_job(value)
        operation = self._reserve(job_path, "generate")
        thread = Thread(target=self._run, args=(operation, job_path, self.generate_fn, {}), daemon=True)
        thread.start()
        return operation.document()

    def start_render(self, value: object, profile: str, blender: object | None = None) -> dict[str, Any]:
        if profile not in {"smoke", "full"}:
            raise CodegenLabError("profile must be smoke or full")
        job_path = self.resolve_job(value)
        operation = self._reserve(job_path, "render")
        kwargs = {"blender": Path(blender) if blender else self.config.blender, "profile": profile}
        thread = Thread(target=self._run, args=(operation, job_path, self.render_fn, kwargs), daemon=True)
        thread.start()
        return operation.document()

    def get_operation(self, operation_id: str) -> Operation:
        with self._lock:
            try:
                return self._operations[operation_id]
            except KeyError as exc:
                raise CodegenLabError("operation not found") from exc

    def wait_operation(self, operation_id: str, timeout: float | None = None) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        with operation._condition:
            if operation.state in {"queued", "running"}:
                operation._condition.wait(timeout)
        return operation.document()

    def status(self, value: object, operation_id: str | None = None) -> dict[str, Any]:
        job_path = self.resolve_job(value)
        operation = self.get_operation(operation_id) if operation_id else None
        return status_document(job_path, operation)


def _resolution(value: object) -> tuple[int, int]:
    if not isinstance(value, str) or "x" not in value.lower():
        raise CodegenLabError("resolution must be WIDTHxHEIGHT")
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except ValueError as exc:
        raise CodegenLabError("resolution must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0:
        raise CodegenLabError("resolution must be positive")
    return width, height
