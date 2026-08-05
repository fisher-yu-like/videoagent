"""Independent local browser service for the DeepSeek Blender codegen experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit
import argparse
import sys
from uuid import uuid4

from videoactagent.codegen_job import generate_codegen_job, prepare_codegen_job, render_codegen_job
from videoactagent.prompt_codegen_pipeline import PromptCodegenRunner, PromptPipelineConfig


class CodegenLabError(ValueError):
    """A user-visible Codegen Lab validation or state error."""


@dataclass(frozen=True)
class CodegenLabConfig:
    workspace: Path
    blender: Path
    port: int = 8781
    static_root_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        object.__setattr__(self, "blender", Path(self.blender).resolve())
        if self.static_root_path is not None:
            object.__setattr__(self, "static_root_path", Path(self.static_root_path).resolve())

    @property
    def experiments_root(self) -> Path:
        return self.workspace / "runs" / "work" / "codegen_blender_v1"

    @property
    def static_root(self) -> Path:
        return self.static_root_path or Path(__file__).resolve().parent.parent / "static"

    @property
    def protected_workspace(self) -> Path:
        candidates = (
            self.workspace / "runs" / "work" / "my_story",
            self.workspace.parent.parent / "runs" / "work" / "my_story",
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()
        return (self.workspace / "runs" / "work" / "my_story").resolve()

    @property
    def protected_url(self) -> str:
        return "http://127.0.0.1:8770/api/session"

    @property
    def prompt_pipeline_config(self) -> PromptPipelineConfig:
        return PromptPipelineConfig(
            experiments_root=self.experiments_root,
            blender=self.blender,
            protected_workspace=self.protected_workspace,
            protected_url=self.protected_url,
        )


@dataclass
class Operation:
    operation_id: str
    job_path: str
    kind: str
    state: str = "queued"
    error: str | None = None
    result: dict[str, Any] | None = None
    stage: str = "queued"
    _condition: Condition = field(default_factory=Condition, repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "job": self.job_path,
            "kind": self.kind,
            "state": self.state,
            "error": self.error,
            "stage": self.stage,
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
        prompt_runner: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.prepare_fn = prepare_fn
        self.generate_fn = generate_fn
        self.render_fn = render_fn
        self.prompt_runner = prompt_runner or PromptCodegenRunner(config.prompt_pipeline_config)
        self._lock = Lock()
        self._operations: dict[str, Operation] = {}
        self._active_jobs: dict[Path, str] = {}
        self._active_prompt: str | None = None

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
        result = status_document(Path(job_path))
        result["job"] = str(Path(job_path).resolve())
        return result

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
            operation.stage = operation.kind
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

    def start_prompt_run(self, prompt: object) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodegenLabError("prompt is required")
        with self._lock:
            if self._active_prompt is not None:
                raise CodegenLabError("a prompt run is already active")
            operation = Operation(uuid4().hex, "", "prompt")
            self._operations[operation.operation_id] = operation
            self._active_prompt = operation.operation_id
        thread = Thread(target=self._run_prompt, args=(operation, prompt.strip()), daemon=True)
        thread.start()
        return operation.document()

    def _run_prompt(self, operation: Operation, prompt: str) -> None:
        with operation._condition:
            operation.state = "running"
            operation.stage = "prompt_pipeline"
            operation._condition.notify_all()
        try:
            operation.result = self.prompt_runner(prompt)
            if isinstance(operation.result, Mapping) and isinstance(operation.result.get("job"), str):
                operation.job_path = operation.result["job"]
            operation.state = "done"
            operation.stage = "succeeded"
        except Exception as exc:
            operation.error = f"{type(exc).__name__}: {exc}"
            operation.state = "error"
            operation.stage = "failed"
        finally:
            with self._lock:
                self._active_prompt = None
            with operation._condition:
                operation._condition.notify_all()

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

    def prompt_status(self, operation_id: str) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        result = operation.result if isinstance(operation.result, Mapping) else {}
        status: dict[str, Any] = {
            "operation_id": operation.operation_id,
            "state": operation.state,
            "stage": operation.stage,
            "status": "running" if operation.state in {"queued", "running"} else ("failed" if operation.state == "error" else result.get("status", "succeeded")),
            "error": operation.error,
        }
        document = result.get("document") if isinstance(result, Mapping) else None
        if isinstance(document, Mapping):
            for key in ("job_id", "api_call_count", "retry_count", "planner_attempts", "codegen_attempts"):
                if key in document:
                    status[key] = document[key]
        job_value = result.get("job") if isinstance(result, Mapping) else None
        video_value = result.get("video") if isinstance(result, Mapping) else None
        if operation.state == "done" and isinstance(job_value, str) and isinstance(video_value, str):
            job_path = Path(job_value).resolve()
            try:
                relative = Path(video_value).resolve().relative_to(job_path.parent).as_posix()
            except ValueError:
                relative = ""
            if relative:
                status["job"] = str(job_path)
                status["video_url"] = f"/api/artifact?job={quote(str(job_path))}&path={quote(relative)}"
        return status


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


class _CodegenLabHandler(BaseHTTPRequestHandler):
    server: "_CodegenLabServer"

    def log_message(self, format: str, *args: object) -> None:
        return

    @property
    def application(self) -> CodegenLabApplication:
        return self.server.application

    @property
    def config(self) -> CodegenLabConfig:
        return self.server.config

    def _send_json(self, status: int, value: object) -> None:
        body = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, error_type: str = "CodegenLabError") -> None:
        self._send_json(status, {"ok": False, "error": {"type": error_type, "message": message}})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2 * 1024 * 1024:
                raise CodegenLabError("request body is too large")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise CodegenLabError("request body must be JSON") from exc
        if not isinstance(value, dict):
            raise CodegenLabError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/":
                path = self.config.static_root / "codegen_lab.html"
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/health":
                self._send_json(200, {"ok": True, "service": "codegen-lab", "port": self.config.port, "workspace": str(self.config.workspace), "experiments_root": str(self.config.experiments_root), "blender": str(self.config.blender), "blender_exists": self.config.blender.is_file()})
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/api/prompt-status":
                operation = query.get("operation", [""])[0]
                self._send_json(200, {"ok": True, **self.application.prompt_status(operation)})
                return
            if parsed.path == "/api/status":
                job = query.get("job", [""])[0]
                operation = query.get("operation", [None])[0]
                self._send_json(200, {"ok": True, **self.application.status(job, operation)})
                return
            if parsed.path == "/api/artifact":
                job = self.application.resolve_job(query.get("job", [""])[0])
                artifact = self.application.resolve_artifact(job, query.get("path", [""])[0])
                self._send_file(artifact)
                return
            self._error(404, "route not found", "NotFound")
        except CodegenLabError as exc:
            self._error(400, str(exc))
        except FileNotFoundError:
            self._error(404, "artifact or page not found", "NotFound")
        except Exception as exc:
            self._error(500, f"server error: {type(exc).__name__}", "ServerError")

    def do_POST(self) -> None:
        try:
            payload = self._body()
            if self.path == "/api/prompt-run":
                operation = self.application.start_prompt_run(payload.get("prompt"))
                self._send_json(202, {"ok": True, **operation})
                return
            if self.path == "/api/prepare":
                self._send_json(200, {"ok": True, **self.application.prepare(payload)})
                return
            if self.path == "/api/generate":
                operation = self.application.start_generate(payload.get("job"))
                self._send_json(202, {"ok": True, **operation})
                return
            if self.path == "/api/render":
                operation = self.application.start_render(payload.get("job"), str(payload.get("profile", "")), payload.get("blender"))
                self._send_json(202, {"ok": True, **operation})
                return
            self._error(404, "route not found", "NotFound")
        except CodegenLabError as exc:
            self._error(400, str(exc))
        except FileNotFoundError:
            self._error(404, "file not found", "NotFound")
        except Exception as exc:
            self._error(500, f"server error: {type(exc).__name__}", "ServerError")

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        start, end = 0, len(data) - 1
        range_header = self.headers.get("Range")
        if range_header:
            try:
                value = (range_header[len("bytes="):] if range_header.startswith("bytes=") else range_header).split(",", 1)[0]
                left, right = value.split("-", 1)
                start = int(left) if left else max(0, len(data) - int(right))
                end = int(right) if right else len(data) - 1
                if start < 0 or end < start or end >= len(data):
                    raise ValueError
            except ValueError:
                self._error(416, "invalid byte range", "RangeNotSatisfiable")
                return
        body = data[start : end + 1]
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class _CodegenLabServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], config: CodegenLabConfig, application: CodegenLabApplication):
        super().__init__(address, _CodegenLabHandler)
        self.config = config
        self.application = application


def create_server(config: CodegenLabConfig, *, application: CodegenLabApplication | None = None) -> ThreadingHTTPServer:
    """Create (but do not start) the independent Codegen Lab HTTP server."""

    return _CodegenLabServer(("127.0.0.1", config.port), config, application or CodegenLabApplication(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent Codegen Lab browser service")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve the Codegen Lab page")
    serve.add_argument("--workspace", type=Path, required=True)
    serve.add_argument("--blender", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8781)
    args = parser.parse_args(argv)
    if args.command != "serve":
        return 2
    config = CodegenLabConfig(workspace=args.workspace, blender=args.blender, port=args.port)
    server = create_server(config)
    print(f"Codegen Lab: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
