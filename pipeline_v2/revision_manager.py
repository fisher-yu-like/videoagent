"""Immutable revision directories and feedback routing for Proxy review."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .proxy_verifier import FEEDBACK_CATEGORIES


class RevisionError(ValueError):
    """Raised when a revision or feedback would overwrite evidence."""


_REVISION_RE = re.compile(r"^revision_(\d{3})$")


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise RevisionError(f"evidence already exists: {path}")
    # ASCII escapes keep JSON portable even when the caller runs under a
    # legacy Windows console encoding; json.load restores the original text.
    path.write_text(json.dumps(dict(value), ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _validate_feedback(feedback: Mapping[str, Any]) -> dict[str, Any]:
    category = feedback.get("category")
    if category not in FEEDBACK_CATEGORIES:
        raise RevisionError(f"feedback category is invalid: {category!r}")
    message = feedback.get("message")
    if not isinstance(message, str) or not message.strip():
        raise RevisionError("feedback message must be non-empty")
    return {"category": category, "message": message.strip()}


def build_handoff(feedback: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _validate_feedback(feedback)
    category = normalized["category"]
    return {
        **normalized,
        "route": "appearance_only_edit_prompt" if category == "appearance_only" else "director_revision",
        "requires_proxy_rerender": category != "appearance_only",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def allocate_revision(root: Path | str, *, revision_id: str | None = None, parent_revision: str | None = None) -> Path:
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if revision_id is None:
        indexes = []
        for item in base.iterdir():
            match = _REVISION_RE.match(item.name)
            if match and item.is_dir():
                indexes.append(int(match.group(1)))
        revision_id = f"revision_{max(indexes, default=-1) + 1:03d}"
    if _REVISION_RE.fullmatch(revision_id) is None:
        raise RevisionError("revision_id must match revision_NNN")
    target = base / revision_id
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise RevisionError(f"revision already exists: {target}") from exc
    _write_json_exclusive(target / "revision.json", {
        "schema_version": "proxy-revision-1.0",
        "revision_id": revision_id,
        "parent_revision": parent_revision,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    return target


def record_feedback(revision_dir: Path | str, feedback: Mapping[str, Any]) -> dict[str, Any]:
    target = Path(revision_dir).resolve()
    if not target.is_dir():
        raise RevisionError(f"revision directory does not exist: {target}")
    handoff = build_handoff(feedback)
    _write_json_exclusive(target / "feedback.json", dict(feedback))
    _write_json_exclusive(target / "handoff.json", handoff)
    return handoff


def record_approval(revision_dir: Path | str, *, reviewer: str, notes: str = "") -> dict[str, Any]:
    """Persist one human approval decision; a revision can only be approved once."""

    target = Path(revision_dir).resolve()
    if not target.is_dir():
        raise RevisionError(f"revision directory does not exist: {target}")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise RevisionError("reviewer must be non-empty")
    approval = {
        "schema_version": "proxy-approval-1.0",
        "status": "approved",
        "reviewer": reviewer.strip(),
        "notes": notes.strip() if isinstance(notes, str) else "",
        "approved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _write_json_exclusive(target / "approval.json", approval)
    return approval


def snapshot_bundle(root: Path | str, source_dir: Path | str, *, parent_revision: str | None = None) -> Path:
    """Copy a real Proxy bundle into a new immutable revision directory."""

    source = Path(source_dir).resolve()
    if not source.is_dir():
        raise RevisionError(f"source bundle does not exist: {source}")
    revision = allocate_revision(root, parent_revision=parent_revision)
    files: dict[str, dict[str, Any]] = {}
    for source_path in source.rglob("*"):
        if not source_path.is_file():
            continue
        relative = source_path.relative_to(source)
        target = revision / relative
        if target.exists():
            raise RevisionError(f"snapshot target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        files[relative.as_posix()] = {"bytes": target.stat().st_size, "sha256": digest}
    _write_json_exclusive(revision / "source_manifest.json", {
        "schema_version": "proxy-revision-source-manifest-1.0",
        "source_dir": str(source),
        "files": files,
    })
    return revision
