"""Strict, zero-network Seedance reference-video request preparation.

Official model documentation and captured gateway behavior are intentionally
separate evidence.  This module only validates and builds candidates; it has no
submission path.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

from videoactagent.backends.jd import (
    SUPPORTED_SEEDANCE_MODELS,
    build_seedance_reference_video,
)


SCHEMA_VERSION = "seedance-reference-capability/1"
CAPTURE_FIELDS = frozenset({"path", "sha256"})
COMMON_EVIDENCE_FIELDS = frozenset(
    {"observed_at", "model", "content_type", "url_field", "role"}
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ITERATION_RE = re.compile(r"D[1-9][0-9]*")
PLACEHOLDER_HOSTS = frozenset(
    {"example.com", "example.org", "example.net", "localhost"}
)
PLACEHOLDER_SUFFIXES = (
    ".example.com",
    ".example.org",
    ".example.net",
    ".example",
    ".invalid",
    ".localhost",
    ".test",
    ".local",
)
_TRUST_TOKEN = object()


class _UntrustedEvidence(ValueError):
    pass


@dataclass(frozen=True)
class CapturedGatewayResponse:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _safe_relative_path(self.path, "captured gateway response")
        _sha256(self.sha256, "captured gateway response sha256")


@dataclass(frozen=True)
class SeedanceEvidenceRecord:
    observed_at: str
    model: str
    content_type: Literal["video_url"]
    url_field: Literal["video_url"]
    role: Literal["reference_video"]
    source_url: str | None = None
    response_record: CapturedGatewayResponse | None = None

    def __post_init__(self) -> None:
        _validate_evidence_record_fields(self, "evidence record")


@dataclass(frozen=True)
class SeedanceCapabilityEvidence:
    schema_version: Literal["seedance-reference-capability/1"]
    model_capability: Literal["model_supported", "unsupported"]
    gateway_capability: Literal["gateway_unverified", "gateway_verified"]
    model_evidence: SeedanceEvidenceRecord
    gateway_evidence: SeedanceEvidenceRecord | None
    _capture_verified: bool = field(default=False, init=False, repr=False, compare=False)
    _validation_token: object = field(default=None, init=False, repr=False, compare=False)
    _captured_task_id: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("Seedance capability evidence schema_version is invalid")
        if self.model_capability not in ("model_supported", "unsupported"):
            raise ValueError("model_capability state is invalid")
        if self.gateway_capability not in ("gateway_unverified", "gateway_verified"):
            raise ValueError("gateway_capability state is invalid")
        if not isinstance(self.model_evidence, SeedanceEvidenceRecord):
            raise ValueError("model_evidence must be a SeedanceEvidenceRecord")
        if self.gateway_evidence is not None and not isinstance(
            self.gateway_evidence, SeedanceEvidenceRecord
        ):
            raise ValueError("gateway_evidence must be a SeedanceEvidenceRecord")
        if self.gateway_evidence is not None and self.gateway_evidence.model != self.model:
            raise ValueError("model and gateway evidence exact models differ")
        if self.gateway_capability == "gateway_verified" and (
            self.gateway_evidence is None
            or self.gateway_evidence.response_record is None
        ):
            raise ValueError("gateway_verified requires a captured local gateway response record")

    @property
    def model(self) -> str:
        return self.model_evidence.model


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a nonempty trimmed string")
    return value


def _validate_evidence_record_fields(
    record: SeedanceEvidenceRecord, label: str
) -> None:
    if record.model not in SUPPORTED_SEEDANCE_MODELS:
        raise ValueError(f"{label} model is not an allowlisted exact model identifier")
    if record.content_type != "video_url":
        raise ValueError(f"{label} content_type must be exactly video_url")
    if record.url_field != "video_url":
        raise ValueError(f"{label} url_field must be exactly video_url")
    if record.role != "reference_video":
        raise ValueError(f"{label} role must be exactly reference_video")
    _utc_timestamp(record.observed_at)
    if (record.source_url is None) == (record.response_record is None):
        raise ValueError(f"{label} must contain exactly one source URL or captured response record")
    if record.source_url is not None:
        _public_https_url(record.source_url, f"{label} source URL")


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_relative_path(value: object, label: str) -> str:
    path = _nonempty(value, f"{label} path")
    candidate = PurePosixPath(path)
    if (
        "\\" in path
        or candidate.is_absolute()
        or path != candidate.as_posix()
        or any(part in ("", ".", "..") for part in candidate.parts)
        or ":" in candidate.parts[0]
    ):
        raise ValueError(f"{label} must use a safe relative path")
    return path


def _public_https_url(value: object, label: str) -> str:
    url = _nonempty(value, label)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{label} must use a public HTTPS hostname")
    hostname = parsed.hostname.lower().rstrip(".")
    if (
        "." not in hostname
        or hostname in PLACEHOLDER_HOSTS
        or hostname.endswith(PLACEHOLDER_SUFFIXES)
    ):
        raise ValueError(f"{label} uses a local or placeholder hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        raise ValueError(f"{label} must use a public hostname, not an IP address")
    return url


def validate_remote_video_asset(value: str) -> str:
    """Validate a real Seedance video source, never an asset/local placeholder."""
    return _public_https_url(value, "Seedance reference video URL")


def _utc_timestamp(value: object) -> str:
    text = _nonempty(value, "observed_at")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("observed_at must be an ISO-8601 UTC timestamp")
    if "T" not in text:
        raise ValueError("observed_at must include UTC date and time")
    return text


def _capture(value: object) -> CapturedGatewayResponse:
    if not isinstance(value, Mapping) or set(value) != CAPTURE_FIELDS:
        raise ValueError("captured gateway response record is invalid")
    return CapturedGatewayResponse(
        path=_safe_relative_path(value.get("path"), "captured gateway response"),
        sha256=_sha256(value.get("sha256"), "captured gateway response sha256"),
    )


def _evidence_record(value: object, label: str) -> SeedanceEvidenceRecord:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    fields = set(value)
    provenance = fields - COMMON_EVIDENCE_FIELDS
    if provenance not in ({"source_url"}, {"response_record"}):
        raise ValueError(
            f"{label} must contain exactly one source URL or captured response record"
        )
    model = _nonempty(value.get("model"), f"{label} model")
    if model not in SUPPORTED_SEEDANCE_MODELS:
        raise ValueError(f"{label} model is not an allowlisted exact model identifier")
    if value.get("content_type") != "video_url":
        raise ValueError(f"{label} content_type must be exactly video_url")
    if value.get("url_field") != "video_url":
        raise ValueError(f"{label} url_field must be exactly video_url")
    if value.get("role") != "reference_video":
        raise ValueError(f"{label} role must be exactly reference_video")
    source_url = None
    response_record = None
    if "source_url" in provenance:
        source_url = _public_https_url(value.get("source_url"), f"{label} source URL")
    else:
        response_record = _capture(value.get("response_record"))
    return SeedanceEvidenceRecord(
        observed_at=_utc_timestamp(value.get("observed_at")),
        model=model,
        content_type="video_url",
        url_field="video_url",
        role="reference_video",
        source_url=source_url,
        response_record=response_record,
    )


def _read_json_strict(path: Path) -> object:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read capability evidence: {exc}") from exc


def _verify_response_capture(record: CapturedGatewayResponse, base_dir: Path | None) -> str:
    if base_dir is None:
        raise ValueError(
            "gateway_verified requires a captured local gateway response and base_dir"
        )
    try:
        root = base_dir.resolve(strict=True)
        target = (root / Path(record.path)).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve captured gateway response: {exc}") from exc
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("captured gateway response must use a safe relative path") from exc
    if not target.is_file():
        raise ValueError("captured gateway response is not a file")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read captured gateway response: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != record.sha256:
        raise ValueError("captured gateway response hash mismatch")
    try:
        response = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("captured gateway response must be a JSON object") from exc
    if not isinstance(response, dict) or not response:
        raise ValueError("captured gateway response must be a nonempty JSON object")
    task_id = response.get("task_id")
    result = response.get("result")
    if not task_id and isinstance(result, Mapping):
        task_id = result.get("task_id")
    if response.get("error") or not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("captured gateway response must show a successful task response")
    return task_id


def load_seedance_capability_evidence(
    source: Mapping[str, object] | Path,
    *,
    base_dir: Path | None = None,
) -> SeedanceCapabilityEvidence:
    """Load strict evidence; only a locally hash-verified capture can verify gateway."""
    if isinstance(source, Path):
        value = _read_json_strict(source)
        if base_dir is not None:
            raise ValueError("base_dir is implicit when loading evidence from a path")
        base_dir = source.resolve(strict=True).parent
    else:
        value = source
    required = {
        "schema_version",
        "model_capability",
        "gateway_capability",
        "model_evidence",
        "gateway_evidence",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("Seedance capability evidence fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Seedance capability evidence schema_version is invalid")
    model_capability = value.get("model_capability")
    if model_capability not in ("model_supported", "unsupported"):
        raise ValueError("model_capability state is invalid")
    gateway_capability = value.get("gateway_capability")
    if gateway_capability not in ("gateway_unverified", "gateway_verified"):
        raise ValueError("gateway_capability state is invalid")
    model_evidence = _evidence_record(value.get("model_evidence"), "model evidence")
    gateway_value = value.get("gateway_evidence")
    gateway_evidence = (
        None if gateway_value is None else _evidence_record(gateway_value, "gateway evidence")
    )
    if gateway_evidence is not None and gateway_evidence.model != model_evidence.model:
        raise ValueError("model and gateway evidence exact models differ")
    if gateway_capability == "gateway_verified":
        if gateway_evidence is None or gateway_evidence.response_record is None:
            raise ValueError(
                "gateway_verified requires a captured local gateway response record"
            )
    elif gateway_evidence is not None and gateway_evidence.response_record is not None:
        raise ValueError("captured gateway response must not be marked gateway_unverified")
    captured_task_id = None
    if gateway_capability == "gateway_verified":
        captured_task_id = _verify_response_capture(
            gateway_evidence.response_record, base_dir
        )
    loaded = SeedanceCapabilityEvidence(
        schema_version=SCHEMA_VERSION,
        model_capability=model_capability,
        gateway_capability=gateway_capability,
        model_evidence=model_evidence,
        gateway_evidence=gateway_evidence,
    )
    object.__setattr__(loaded, "_capture_verified", gateway_capability == "gateway_verified")
    object.__setattr__(loaded, "_validation_token", _TRUST_TOKEN)
    object.__setattr__(loaded, "_captured_task_id", captured_task_id)
    return loaded


def validate_capability_evidence(
    capability: SeedanceCapabilityEvidence,
) -> SeedanceCapabilityEvidence:
    """Canonical trust boundary for immutable capability evidence objects."""
    if not isinstance(capability, SeedanceCapabilityEvidence):
        raise ValueError("capability evidence object has an invalid type")
    if capability._validation_token is not _TRUST_TOKEN:
        raise _UntrustedEvidence("capability evidence was not loaded by the strict loader")
    _validate_evidence_record_fields(capability.model_evidence, "model evidence")
    if capability.gateway_evidence is not None:
        _validate_evidence_record_fields(capability.gateway_evidence, "gateway evidence")
    if capability.gateway_capability == "gateway_verified" and (
        not capability._capture_verified
        or not capability._captured_task_id
        or capability.gateway_evidence is None
        or capability.gateway_evidence.response_record is None
    ):
        raise _UntrustedEvidence("gateway_verified evidence lacks trusted capture provenance")
    return capability


def _record_shape(
    value: object,
    label: str,
    *,
    allowed_extra: frozenset[str] = frozenset(),
) -> dict[str, object] | None:
    """Return None for absent hash fields (a blocker); raise for malformed values."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} record must be an object")
    required = {"path", "sha256", "bytes"}
    if not required.issubset(value):
        raise ValueError(f"{label} record is missing path/hash/size")
    if set(value) - required - allowed_extra:
        raise ValueError(f"{label} record contains unknown fields")
    path = _safe_relative_path(value.get("path"), label)
    digest = _sha256(value.get("sha256"), f"{label} sha256")
    size = value.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"{label} bytes must be a positive integer")
    return {**dict(value), "path": path, "sha256": digest, "bytes": size}


def materialize_approved_prompt(
    approved_export: Mapping[str, object], workspace_root: Path
) -> dict[str, object]:
    """Copy an export and safely inline its exact hash-bound UTF-8 prompt bytes."""
    if not isinstance(approved_export, Mapping):
        raise ValueError("approved_export must be an object")
    value = _record_shape(
        approved_export.get("restyle_prompt"),
        "restyle_prompt",
        allowed_extra=frozenset({"text"}),
    )
    if value is None:
        raise ValueError("restyle_prompt record is missing path/hash/size")
    root = Path(workspace_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace_root must be a directory")
    target = (root / Path(str(value["path"]))).resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("restyle_prompt must use a safe relative path") from exc
    if not target.is_file():
        raise ValueError("restyle_prompt path is not a file")
    data = target.read_bytes()
    if len(data) != value["bytes"] or hashlib.sha256(data).hexdigest() != value["sha256"]:
        raise ValueError("restyle_prompt hash/size mismatch")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("restyle_prompt is not valid UTF-8") from exc
    if not text.strip():
        raise ValueError("restyle_prompt text must be nonempty")
    result = deepcopy(dict(approved_export))
    result["restyle_prompt"]["text"] = text
    return result


def _blocked_candidate(blockers: list[str], source_hashes: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": "seedance-reference-candidate/1",
        "state": "blocked",
        "blockers": blockers,
        "network_called": False,
        "generation_submit_limit": 1,
        "automatic_retry_limit": 0,
        "fallbacks": {"reference_image": False, "prompt_only": False},
        "source_hashes": source_hashes,
    }


def prepare_reference_candidate(
    *,
    approved_export: Mapping[str, object],
    proxy_url: str,
    capability: object,
) -> dict[str, object]:
    """Prepare one hash-bound candidate without making any network call."""
    if not isinstance(approved_export, Mapping):
        raise ValueError("approved_export must be an object")
    blockers: list[str] = []
    source_hashes: dict[str, str] = {}

    iteration = approved_export.get("iteration_id")
    approval_present = approved_export.get("approval") is not None
    if not approval_present or iteration is None:
        blockers.append("missing approved human iteration evidence")
    elif not isinstance(iteration, str):
        raise ValueError("approved iteration_id must be a string")
    elif ITERATION_RE.fullmatch(iteration) is None:
        blockers.append("approved iteration is not a human iteration")

    record_specs = (
        ("approval", frozenset({"provenance"})),
        ("clay", frozenset({"media", "provenance"})),
        ("restyle_prompt", frozenset({"text", "provenance"})),
        ("restyle_profile", frozenset({"provenance"})),
    )
    records: dict[str, dict[str, object]] = {}
    for name, extras in record_specs:
        raw = approved_export.get(name)
        if raw is None:
            if name != "approval" or approval_present:
                blockers.append(f"missing approved {name} record")
            continue
        checked = _record_shape(raw, name, allowed_extra=extras)
        if checked is None:
            blockers.append(f"missing approved {name} hash/size/path")
            continue
        records[name] = checked
        source_hashes[name] = str(checked["sha256"])

    # Bind compiler artifacts exported by Task 4 when they are available.  They
    # are optional for compatibility with earlier exports, but never ignored if
    # supplied.
    for name in (
        "iteration_manifest",
        "annotation",
        "compiled_prompt",
        "trajectory_prompt",
    ):
        if name not in approved_export:
            continue
        checked = _record_shape(approved_export[name], name)
        if checked is None:
            blockers.append(f"missing approved {name} hash/size/path")
        else:
            source_hashes[name] = str(checked["sha256"])

    compiler_versions: dict[str, str] = {}
    for name in ("trajectory_compiler_version", "restyle_compiler_version"):
        raw = approved_export.get(name)
        if raw is None:
            blockers.append(f"missing {name}")
        else:
            compiler_versions[name] = _nonempty(raw, name)
    compiler_hashes: dict[str, str] = {}
    for name in ("trajectory_compiler_sha256", "restyle_compiler_sha256"):
        if name in approved_export:
            compiler_hashes[name] = _sha256(approved_export[name], name)
    for name in ("compiled_prompt", "trajectory_prompt", "restyle_prompt"):
        if name in source_hashes:
            compiler_hashes[name] = source_hashes[name]

    clay = records.get("clay")
    if clay is not None:
        media = clay.get("media")
        if not isinstance(media, Mapping) or "duration_seconds" not in media:
            blockers.append("approved clay proxy intended duration is missing")
        else:
            duration = media.get("duration_seconds")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise ValueError("approved clay proxy duration must be numeric")
            if float(duration) != 5.0:
                blockers.append("approved clay proxy intended duration is not five seconds")

    prompt_text: str | None = None
    prompt = records.get("restyle_prompt")
    if prompt is not None:
        text = prompt.get("text")
        if text is None:
            blockers.append("restyle prompt text is not materialized")
        elif not isinstance(text, str) or not text.strip():
            raise ValueError("restyle_prompt text must be a nonempty string")
        else:
            data = text.encode("utf-8")
            if len(data) != prompt["bytes"] or hashlib.sha256(data).hexdigest() != prompt["sha256"]:
                raise ValueError("restyle_prompt hash/size mismatch")
            prompt_text = text

    valid_url: str | None = None
    if proxy_url is None or proxy_url == "":
        blockers.append("missing public Seedance proxy video URL")
    elif not isinstance(proxy_url, str):
        raise ValueError("proxy_url must be a string")
    else:
        valid_url = validate_remote_video_asset(proxy_url)

    loaded_capability: SeedanceCapabilityEvidence | None
    if isinstance(capability, SeedanceCapabilityEvidence):
        try:
            loaded_capability = validate_capability_evidence(capability)
        except _UntrustedEvidence as exc:
            blockers.append(str(exc))
            loaded_capability = None
    elif isinstance(capability, Mapping):
        loaded_capability = load_seedance_capability_evidence(capability)
    else:
        loaded_capability = None
    if loaded_capability is None:
        blockers.append("missing verified Seedance model capability evidence")
    elif loaded_capability.model_capability != "model_supported":
        blockers.append("Seedance model reference-video capability is unsupported")

    if blockers:
        return _blocked_candidate(blockers, source_hashes)

    assert loaded_capability is not None and prompt_text is not None and valid_url is not None
    state = (
        "ready"
        if loaded_capability.gateway_capability == "gateway_verified"
        else "ready_for_single_combined_probe"
    )
    payload = build_seedance_reference_video(
        prompt_text, valid_url, model=loaded_capability.model, duration=5
    )
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    def provenance(record: SeedanceEvidenceRecord | None) -> dict[str, object] | None:
        if record is None:
            return None
        if record.response_record is not None:
            return {
                "capture": {
                    "path": record.response_record.path,
                    "sha256": record.response_record.sha256,
                }
            }
        return {"source_url": record.source_url}

    return {
        "schema_version": "seedance-reference-candidate/1",
        "state": state,
        "blockers": [],
        "network_called": False,
        "generation_submit_limit": 1,
        "automatic_retry_limit": 0,
        "fallbacks": {"reference_image": False, "prompt_only": False},
        "capability": {
            "model": loaded_capability.model,
            "model_capability": loaded_capability.model_capability,
            "gateway_capability": loaded_capability.gateway_capability,
            "content_type": "video_url",
            "url_field": "video_url",
            "role": "reference_video",
        },
        "capability_evidence": {
            "model": provenance(loaded_capability.model_evidence),
            "gateway": provenance(loaded_capability.gateway_evidence),
        },
        "approved_iteration": iteration,
        "compiler_versions": compiler_versions,
        "compiler_hashes": compiler_hashes,
        "source_hashes": source_hashes,
        "proxy": {
            "url": valid_url,
            "path": str(records["clay"]["path"]),
            "sha256": str(records["clay"]["sha256"]),
            "bytes": int(records["clay"]["bytes"]),
        },
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
