"""Conservative repair boundary for model JSON responses.

This intentionally does not guess missing fields, commas, quotes, or values.
It only unwraps common Markdown fences and parses one complete JSON value while
recording harmless trailing delimiters/commentary. The caller must still run
its strict schema validator before accepting the document.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Tuple


class JSONRepairError(ValueError):
    """Raised when a response cannot be safely reduced to one JSON value."""


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)(?:\n\s*```\s*)$", re.DOTALL)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metadata(raw: str, normalized: str, actions: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "json-repair-1.0",
        "repaired": bool(actions),
        "actions": list(actions),
        "raw_sha256": _digest(raw),
        "parsed_text_sha256": _digest(normalized),
    }


def parse_json_response(content: str, *, label: str = "model") -> Tuple[object, Mapping[str, Any]]:
    """Parse one model JSON response with auditable, conservative repairs."""

    if not isinstance(content, str) or not content.strip():
        raise JSONRepairError(f"{label} JSON content is empty")
    raw = content
    text = content.strip()
    actions: list[str] = []
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group("body").strip()
        actions.append("markdown_fence_removed")

    try:
        return json.loads(text), _metadata(raw, text, actions)
    except json.JSONDecodeError as original_error:
        # Detect a second complete top-level value before invoking an
        # auto-repair library.  Some versions of json-repair intentionally
        # salvage multiple values as a list; that is unsafe at this boundary
        # because the Director/CodeAgent contract requires exactly one doc.
        decoder = json.JSONDecoder()
        first_candidates = [index for index, char in enumerate(text) if char in "{["]
        if first_candidates:
            try:
                _first_value, first_end = decoder.raw_decode(text, first_candidates[0])
            except json.JSONDecodeError:
                pass
            else:
                top_level_trailing = text[first_end:].strip()
                if top_level_trailing and any(char in "{[" for char in top_level_trailing):
                    raise JSONRepairError(
                        f"{label} content contains a second top-level JSON value"
                    ) from original_error
        # Prefer the installed json-repair package when available (the host's
        # project venv provides it). The caller still applies the strict
        # Director/CodeAgent schema after this step.
        try:
            from json_repair import repair_json  # type: ignore
        except ImportError:
            repair_json = None
        if repair_json is not None:
            try:
                repaired_value = repair_json(text, return_objects=True)
            except Exception:
                repaired_value = None
            if repaired_value is not None:
                canonical = json.dumps(repaired_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                return repaired_value, _metadata(raw, canonical, actions + ["json_repair_library"])
        decoder = json.JSONDecoder()
        # Only inspect the first JSON-looking value. Trying later starts could
        # silently discard a second top-level document.
        starts = first_candidates[:1]
        for start in starts:
            try:
                value, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                continue
            trailing = text[end:].strip()
            if not trailing:
                repair_actions = actions + (["leading_text_removed"] if start else [])
                parsed_text = text[start:end]
                return value, _metadata(raw, parsed_text, repair_actions)
            # A single value followed by a stray closing delimiter is the
            # exact failure produced by some providers. Never accept a second
            # JSON object or arbitrary executable-looking suffix.
            if trailing and set(trailing) <= set("}]\r\n \t"):
                parsed_text = text[start:end]
                repair_actions = actions + (["leading_text_removed"] if start else []) + ["trailing_delimiter_removed"]
                return value, _metadata(raw, parsed_text, repair_actions)
            if trailing.startswith("```") and "{" not in trailing and "[" not in trailing:
                parsed_text = text[start:end]
                repair_actions = actions + (["leading_text_removed"] if start else []) + ["trailing_fence_removed"]
                return value, _metadata(raw, parsed_text, repair_actions)
            # Permit a short natural-language suffix, but not another JSON
            # value; this covers providers that append one sentence after JSON.
            if "{" not in trailing and "[" not in trailing and len(trailing) <= 400:
                parsed_text = text[start:end]
                repair_actions = actions + (["leading_text_removed"] if start else []) + ["trailing_text_removed"]
                return value, _metadata(raw, parsed_text, repair_actions)
        raise JSONRepairError(f"{label} content is not safely repairable JSON: {original_error}") from original_error
