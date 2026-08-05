"""One-call DeepSeek-v4-pro adapter for constrained Blender scene code."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


MODEL = "deepseek-v4-pro"
RESPONSE_SCHEMA = "blender-codegen-response-1.0"
MAX_CODE_BYTES = 40 * 1024


class DeepSeekCodegenError(ValueError):
    """Raised when the single code generation request cannot be validated."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    # Write bytes explicitly so Windows newline translation cannot change the
    # hash that is recorded for the model's raw Python response.
    temporary.write_bytes(value.encode("utf-8"))
    os.replace(temporary, path)


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _endpoint(base: str) -> tuple[str, str]:
    parsed = urlsplit(base.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeepSeekCodegenError("DeepSeek environment base URL is invalid")
    path = parsed.path.rstrip("/")
    redacted = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, path + "/chat/completions", "", ""))
    return endpoint, redacted


def _artifact(output: Path) -> str | None:
    for name in ("response.json", "response.raw"):
        if (output / name).is_file():
            return name
    return None


def _evidence(*, status: str, started_at: str, started: float, api_calls: int,
              base_url: str | None, error: str | None, output: Path,
              response: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": status,
        "started_at": started_at,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "api_call_count": api_calls,
        "retry_count": 0,
        "base_url": base_url,
        "model": response.get("model", MODEL) if isinstance(response, Mapping) else MODEL,
        "response_artifact": _artifact(output),
        "response_id": response.get("id") if isinstance(response, Mapping) else None,
        "usage": response.get("usage") if isinstance(response, Mapping) else None,
        "error": error,
    }


def request_blender_code(
    *,
    codegen_input: Mapping[str, Any],
    output_dir: Path | str,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Make exactly one non-streaming request and persist redacted evidence."""

    if not isinstance(codegen_input, Mapping):
        raise DeepSeekCodegenError("codegen_input must be an object")
    output = Path(output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    started_at = _now()
    started = time.monotonic()
    environment = os.environ if environ is None else environ
    key = str(environment.get("DEEPSEEK_API_KEY", "")).strip()
    raw_base = str(environment.get("DEEPSEEK_BASE_URL", "")).strip()
    if not key or not raw_base:
        evidence = _evidence(status="environment_blocked", started_at=started_at, started=started, api_calls=0, base_url=None, error="DeepSeek environment variables are unavailable", output=output)
        _write(output / "evidence.json", evidence)
        raise DeepSeekCodegenError(evidence["error"])

    redacted_base: str | None = None
    try:
        endpoint, redacted_base = _endpoint(raw_base)
        prompt = codegen_input.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DeepSeekCodegenError("codegen input prompt is missing")
        system_prompt = (
            "You are a constrained Blender Python scene builder. Return one JSON object only "
            "with exactly schema_version, summary, python_code. Set schema_version to "
            f"{RESPONSE_SCHEMA}. python_code must define exactly one build_scene(context) "
            "function. The context is a dict; use context['scene'], "
            "context['input'], context['world_xy'] and context['frame_for_time'], never "
            "context.scene. Use only bpy, math and mathutils; every used module must have "
            "an explicit import (especially import math). Do not import files, render, "
            "network, processes or environment APIs. The trusted runner owns all rendering "
            "and actor trajectory keyframes. The scene must remain visibly renderable: "
            "include a light, a non-black world, visible materials, and a camera aimed "
            "above the ground."
        )
        user_payload = {
            "task": "Build one complete unbroken station scene from the immutable input.",
            "input": dict(codegen_input),
            "requirements": [
                "Create one Blender object per actor using the exact actor IDs.",
                "Create a camera named DirectorCamera and a visible environment.",
                "Create at least one light, set a non-black world background, use visible "
                "node-based materials, keep actor geometry above the floor, and aim the "
                "camera at actor chest/head height rather than the ground.",
                "Do not insert actor location keyframes; the trusted runner applies the "
                "immutable normalized trajectory after build_scene returns.",
                "Do not read or write files and do not call render operators.",
            ],
        }
        request_payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
            "max_tokens": 8192,
            "stream": False,
        }
        _write(output / "request.json", {"endpoint": endpoint, "payload": _redact(request_payload, key), "authorization_saved": False})
        request = Request(endpoint, data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        try:
            with transport(request, timeout=120) as response_stream:
                raw_response = response_stream.read()
        except Exception as exc:
            raise DeepSeekCodegenError(f"DeepSeek request failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(raw_response, bytes):
            raw_response = bytes(raw_response)
        redacted_raw = raw_response.replace(key.encode("utf-8"), b"[REDACTED]") if key else raw_response
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            (output / "response.raw").write_bytes(redacted_raw)
            raise DeepSeekCodegenError(f"DeepSeek response is not JSON: {exc}") from exc
        if not isinstance(response, Mapping):
            _write(output / "response.json", _redact(response, key))
            raise DeepSeekCodegenError("DeepSeek response must be a JSON object")
        _write(output / "response.json", _redact(response, key))
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise DeepSeekCodegenError("DeepSeek response choices are invalid")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise DeepSeekCodegenError("DeepSeek response was truncated at max_tokens=8192")
        if finish_reason != "stop":
            raise DeepSeekCodegenError("DeepSeek response did not finish normally")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekCodegenError("DeepSeek response content is empty")
        try:
            envelope = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekCodegenError(f"DeepSeek code envelope is not JSON: {exc}") from exc
        if not isinstance(envelope, Mapping):
            raise DeepSeekCodegenError("DeepSeek code envelope must be an object")
        if set(envelope) != {"schema_version", "summary", "python_code"}:
            raise DeepSeekCodegenError("DeepSeek code envelope fields are invalid")
        if envelope["schema_version"] != RESPONSE_SCHEMA:
            raise DeepSeekCodegenError("DeepSeek code envelope schema_version is invalid")
        if not isinstance(envelope["summary"], str) or not envelope["summary"].strip():
            raise DeepSeekCodegenError("DeepSeek code summary is empty")
        code = envelope["python_code"]
        if not isinstance(code, str) or not code.strip():
            raise DeepSeekCodegenError("DeepSeek python_code is empty")
        code_bytes = len(code.encode("utf-8"))
        if code_bytes > MAX_CODE_BYTES:
            raise DeepSeekCodegenError("DeepSeek python_code exceeds 40 KiB")
        _write_text(output / "generated_scene.py", code)
        evidence = _evidence(status="succeeded", started_at=started_at, started=started, api_calls=1, base_url=redacted_base, error=None, output=output, response=response)
        evidence.update({"code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(), "code_bytes": code_bytes, "summary": envelope["summary"]})
        _write(output / "evidence.json", evidence)
        return evidence
    except DeepSeekCodegenError as exc:
        evidence = _evidence(status="failed", started_at=started_at, started=started, api_calls=1, base_url=redacted_base, error=str(exc), output=output)
        _write(output / "evidence.json", evidence)
        raise
    except Exception as exc:
        evidence = _evidence(status="failed", started_at=started_at, started=started, api_calls=1, base_url=redacted_base, error=f"{type(exc).__name__}: {exc}", output=output)
        _write(output / "evidence.json", evidence)
        raise DeepSeekCodegenError(evidence["error"]) from exc
