"""Optional VLM review adapter for dataset-scale Proxy feedback.

This module is not used for the human-reviewed station run. When called, it
makes exactly one OpenAI-compatible request and persists redacted evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.request import Request, urlopen

from .code_agent import _endpoint, _provider_config, _redact, _write_json
from .proxy_verifier import FEEDBACK_CATEGORIES, sha256_file


VLM_SCHEMA_VERSION = "proxy-vlm-feedback-1.0"


class VLMFeedbackError(ValueError):
    """Raised when a VLM review request or response violates the contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _image_data_url(path: Path) -> str:
    if not path.is_file() or path.stat().st_size <= 0:
        raise VLMFeedbackError(f"frame is missing or empty: {path}")
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(suffix)
    if mime is None:
        raise VLMFeedbackError(f"unsupported frame image type: {path.suffix}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_vlm_payload(
    proxy_report: Mapping[str, Any],
    frame_paths: Sequence[Path | str],
    *,
    story_context: str = "",
    model: str = "gpt-5.6-luna",
    review_stage: str = "proxy",
) -> dict[str, Any]:
    if not isinstance(proxy_report, Mapping):
        raise VLMFeedbackError("proxy_report must be an object")
    paths = [Path(path).resolve() for path in frame_paths]
    if not paths:
        raise VLMFeedbackError("at least one real frame is required")
    if review_stage not in {"proxy", "final"}:
        raise VLMFeedbackError("review_stage must be proxy or final")
    subject = "final generated video" if review_stage == "final" else "real Proxy frames"
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"Review the supplied {subject} as a dataset annotation judge. "
            "Use the deterministic report as metadata evidence, but judge only visible "
            "creative intent, visual consistency, action order, identity consistency, and camera coverage. Return exactly one JSON object with "
            f"schema_version {VLM_SCHEMA_VERSION}. Set verdict to approve or revision_requested. "
            "For revision_requested, feedback must contain one or more categories from "
            f"{list(FEEDBACK_CATEGORIES)!r}; scene/trajectory/physical/camera issues must route "
            "back to Director and Blender Proxy, while appearance_only is the only category "
            "that may route to an appearance edit prompt. Include frame numbers when visible. "
            "The JSON object MUST have exactly these top-level fields: "
            "schema_version, verdict, feedback, summary. Each feedback item MUST have exactly "
            "category, message, evidence_frames; do not use issue, frames, route, or any other "
            "field names. evidence_frames MUST be a JSON array of non-negative integer frame "
            "indices from the supplied video (use [] when no exact frame index is certain; do "
            "not write phrases such as sampled views or ranges). For approve, feedback must be "
            "[]; for revision_requested, feedback must be non-empty.\n\n"
            f"Story context: {story_context.strip()}\n"
            f"Deterministic ProxyVerifier report:\n{json.dumps(dict(proxy_report), ensure_ascii=False, sort_keys=True)}"
        ),
    }]
    for path in paths:
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(path)}})
        camera_label = path.parent.name
        frame_label = path.stem
        content.append({
            "type": "text",
            "text": f"Evidence label: camera={camera_label}; frame={frame_label}. Preserve this order when judging the action sequence.",
        })
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict visual Proxy reviewer. Return JSON only."},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 4000,
        "stream": False,
    }


def extract_vlm_feedback(response: object) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise VLMFeedbackError("VLM response must be an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise VLMFeedbackError("VLM response choices are invalid")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise VLMFeedbackError(f"VLM response did not finish normally: {choice.get('finish_reason')}")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise VLMFeedbackError("VLM response content is empty")
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise VLMFeedbackError(f"VLM response content is not JSON: {exc}") from exc
    if not isinstance(document, Mapping) or set(document) != {"schema_version", "verdict", "feedback", "summary"}:
        raise VLMFeedbackError("VLM feedback fields are invalid")
    if document["schema_version"] != VLM_SCHEMA_VERSION:
        raise VLMFeedbackError(f"schema_version must be {VLM_SCHEMA_VERSION!r}")
    if document["verdict"] not in {"approve", "revision_requested"}:
        raise VLMFeedbackError("VLM verdict must be approve or revision_requested")
    if not isinstance(document["summary"], str):
        raise VLMFeedbackError("VLM summary must be text")
    feedback = document["feedback"]
    if not isinstance(feedback, list):
        raise VLMFeedbackError("VLM feedback must be a list")
    normalized: list[dict[str, Any]] = []
    for item in feedback:
        if not isinstance(item, Mapping) or set(item) != {"category", "message", "evidence_frames"}:
            raise VLMFeedbackError("VLM feedback item fields are invalid")
        if item["category"] not in FEEDBACK_CATEGORIES:
            raise VLMFeedbackError(f"VLM feedback category is invalid: {item['category']!r}")
        if not isinstance(item["message"], str) or not item["message"].strip():
            raise VLMFeedbackError("VLM feedback message must be non-empty")
        if not isinstance(item["evidence_frames"], list) or any(type(frame) is not int or frame < 0 for frame in item["evidence_frames"]):
            raise VLMFeedbackError("VLM evidence_frames must be non-negative integers")
        normalized.append({"category": item["category"], "message": item["message"].strip(), "evidence_frames": list(item["evidence_frames"])})
    if document["verdict"] == "approve" and normalized:
        raise VLMFeedbackError("approved VLM review cannot contain revision feedback")
    if document["verdict"] == "revision_requested" and not normalized:
        raise VLMFeedbackError("revision_requested VLM review requires feedback")
    return {"schema_version": VLM_SCHEMA_VERSION, "verdict": document["verdict"], "feedback": normalized, "summary": document["summary"].strip()}


def request_vlm_feedback(
    *,
    proxy_report: Mapping[str, Any],
    frame_paths: Sequence[Path | str],
    output_dir: Path | str,
    story_context: str = "",
    provider: str = "openai",
    model: str | None = None,
    review_stage: str = "proxy",
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Make exactly one VLM request and persist all evidence."""

    if provider != "openai":
        raise VLMFeedbackError("VLM adapter currently supports the openai-compatible provider only")
    output = Path(output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    environment = os.environ if environ is None else environ
    started = time.monotonic()
    started_at = _now()
    api_calls = 0
    api_key = ""
    selected_model = model or "gpt-5.6-luna"
    redacted_base = None
    try:
        api_key, raw_base, selected_model = _provider_config(environment, "openai", model)
        endpoint, redacted_base = _endpoint(raw_base)
        paths = [Path(path).resolve() for path in frame_paths]
        payload = build_vlm_payload(proxy_report, paths, story_context=story_context, model=selected_model, review_stage=review_stage)
        _write_json(output / "request.json", {"endpoint": endpoint, "payload": payload, "authorization_saved": False})
        request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        api_calls = 1
        with transport(request, timeout=180) as response_stream:
            raw_response = response_stream.read()
        response = json.loads(raw_response.decode("utf-8"))
        _write_json(output / "response.json", _redact(response, api_key))
        document = extract_vlm_feedback(response)
        _write_json(output / "feedback.json", document)
        _write_json(output / "evidence.json", {
            "schema_version": "1.0",
            "status": "succeeded",
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "provider": provider,
            "model": response.get("model", selected_model),
            "base_url": redacted_base,
            "api_call_count": api_calls,
            "retry_count": 0,
            "response_id": response.get("id"),
            "usage": response.get("usage"),
            "frame_hashes": {str(path): sha256_file(path) for path in paths},
            "error": None,
        })
        return document
    except Exception as exc:
        _write_json(output / "evidence.json", {
            "schema_version": "1.0",
            "status": "failed",
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "provider": provider,
            "model": selected_model,
            "base_url": redacted_base,
            "api_call_count": api_calls,
            "retry_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        })
        if isinstance(exc, VLMFeedbackError):
            raise
        raise VLMFeedbackError(f"VLM request failed: {exc}") from exc
