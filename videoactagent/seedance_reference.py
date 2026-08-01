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
import weakref

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
_TRUSTED_EVIDENCE: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
ACCEPTED_GATEWAY_STATUSES = frozenset(
    {"submitted", "queued", "processing", "succeeded"}
)


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
    model: str
    model_capability: Literal["model_supported", "unsupported"]
    gateway_capability: Literal["gateway_unverified", "gateway_verified"]
    model_evidence: SeedanceEvidenceRecord
    gateway_evidence: SeedanceEvidenceRecord | None
    _capture_verified: bool = field(default=False, init=False, repr=False, compare=False)
    _validation_token: object = field(default=None, init=False, repr=False, compare=False)
    _captured_task_id: str | None = field(default=None, init=False, repr=False, compare=False)
    _capture_root: Path | None = field(default=None, init=False, repr=False, compare=False)
    _capture_bytes: bytes | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("Seedance capability evidence schema_version is invalid")
        if self.model not in SUPPORTED_SEEDANCE_MODELS:
            raise ValueError("top-level model is not an allowlisted exact model identifier")
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
        if self.model_evidence.model != self.model:
            raise ValueError("top-level and model evidence exact models differ")
        if self.model_evidence.source_url is None:
            raise ValueError("model capability requires source URL provenance")
        if self.gateway_evidence is not None and self.gateway_evidence.model != self.model:
            raise ValueError("model and gateway evidence exact models differ")
        if self.gateway_capability == "gateway_verified" and (
            self.gateway_evidence is None
            or self.gateway_evidence.response_record is None
        ):
            raise ValueError("gateway_verified requires a captured local gateway response record")
        if self.gateway_capability == "gateway_unverified" and self.gateway_evidence is not None:
            raise ValueError("gateway_unverified must not claim gateway evidence")

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


def _parse_json_object(data: bytes, label: str) -> dict[str, object]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field in {label}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_captured_request(value: object, model: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"model", "content", "parameters"}:
        raise ValueError("captured gateway request fields are invalid")
    if value.get("model") != model or model not in SUPPORTED_SEEDANCE_MODELS:
        raise ValueError("captured gateway request exact model is invalid")
    content = value.get("content")
    if not isinstance(content, list) or len(content) != 2:
        raise ValueError("captured gateway request must contain one text and one video")
    text_item, video_item = content
    if (
        not isinstance(text_item, Mapping)
        or set(text_item) != {"type", "text"}
        or text_item.get("type") != "text"
    ):
        raise ValueError("captured gateway request text item is invalid")
    captured_text = text_item.get("text")
    if not isinstance(captured_text, str) or not captured_text.strip():
        raise ValueError("captured gateway request text must be a nonempty string")
    if (
        not isinstance(video_item, Mapping)
        or set(video_item) != {"type", "video_url", "role"}
        or video_item.get("type") != "video_url"
        or video_item.get("role") != "reference_video"
    ):
        raise ValueError("captured gateway request reference_video item is invalid")
    nested = video_item.get("video_url")
    if not isinstance(nested, Mapping) or set(nested) != {"url"}:
        raise ValueError("captured gateway request nested video_url field is invalid")
    validate_remote_video_asset(nested.get("url"))
    parameters = value.get("parameters")
    expected = {
        "ratio": "16:9",
        "resolution": "720p",
        "duration": 5,
        "watermark": False,
    }
    if not isinstance(parameters, Mapping) or dict(parameters) != expected:
        raise ValueError("captured gateway request parameters contract is invalid")
    if type(parameters.get("duration")) is not int or type(parameters.get("watermark")) is not bool:
        raise ValueError("captured gateway request parameter types are invalid")


def _meaningful_error(value: object) -> bool:
    return value not in (None, False, 0, "", [], {})


def _reject_nested_gateway_errors(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"http_code", "http_status", "status_code"}:
                try:
                    http_code = int(item)
                except (TypeError, ValueError) as exc:
                    raise ValueError("captured gateway response HTTP code is invalid") from exc
                if not 200 <= http_code < 300:
                    raise ValueError("captured gateway response has an error HTTP code")
            elif lowered == "code" or lowered.endswith("_code"):
                if item not in (0, "0", None):
                    raise ValueError("captured gateway response has a nonzero application code")
            elif "error" in lowered and _meaningful_error(item):
                raise ValueError("captured gateway response contains a nested error")
            if lowered in {"status", "task_status"} and isinstance(item, str):
                if item.lower() in {"failed", "failure", "canceled", "cancelled", "error"}:
                    raise ValueError("captured gateway response has a failed status")
            _reject_nested_gateway_errors(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nested_gateway_errors(item)


def _validate_captured_response(value: object) -> str:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("captured gateway response must be a nonempty object")
    _reject_nested_gateway_errors(value)
    result = value.get("result")
    result_mapping = result if isinstance(result, Mapping) else {}
    task_id = value.get("task_id") or result_mapping.get("task_id")
    status = (
        value.get("task_status")
        or value.get("status")
        or result_mapping.get("task_status")
        or result_mapping.get("status")
    )
    if not isinstance(task_id, str) or not task_id.strip() or task_id != task_id.strip():
        raise ValueError("captured gateway response must contain a task_id")
    if not isinstance(status, str) or status.lower() not in ACCEPTED_GATEWAY_STATUSES:
        raise ValueError("captured gateway response status is not accepted")
    return task_id


def _validate_capture_document(data: bytes, model: str) -> str:
    document = _parse_json_object(data, "captured gateway document")
    if set(document) != {"request", "response"}:
        raise ValueError("captured gateway document must contain exact request and response")
    _validate_captured_request(document["request"], model)
    return _validate_captured_response(document["response"])


def _materialize_gateway_capture(
    record: CapturedGatewayResponse,
    base_dir: Path | None,
    model: str,
    *,
    expected_bytes: bytes | None = None,
) -> tuple[Path, bytes, str]:
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
    if expected_bytes is not None and data != expected_bytes:
        raise ValueError("captured gateway document changed after evidence loading")
    return root, data, _validate_capture_document(data, model)


def _record_fingerprint(record: SeedanceEvidenceRecord | None) -> tuple[object, ...] | None:
    if record is None:
        return None
    capture = record.response_record
    return (
        record.observed_at,
        record.model,
        record.content_type,
        record.url_field,
        record.role,
        record.source_url,
        None if capture is None else (capture.path, capture.sha256),
    )


def _capability_fingerprint(capability: SeedanceCapabilityEvidence) -> tuple[object, ...]:
    return (
        capability.schema_version,
        capability.model,
        capability.model_capability,
        capability.gateway_capability,
        _record_fingerprint(capability.model_evidence),
        _record_fingerprint(capability.gateway_evidence),
        capability._capture_verified,
        capability._captured_task_id,
        capability._capture_root,
        capability._capture_bytes,
    )


def _register_capability(capability: SeedanceCapabilityEvidence) -> None:
    identifier = id(capability)
    reference = weakref.ref(
        capability,
        lambda _reference, key=identifier: _TRUSTED_EVIDENCE.pop(key, None),
    )
    _TRUSTED_EVIDENCE[identifier] = (reference, _capability_fingerprint(capability))


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
        "model",
        "model_capability",
        "gateway_capability",
        "model_evidence",
        "gateway_evidence",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("Seedance capability evidence fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Seedance capability evidence schema_version is invalid")
    model = value.get("model")
    if model not in SUPPORTED_SEEDANCE_MODELS:
        raise ValueError("top-level model is not an allowlisted exact model identifier")
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
    if model_evidence.model != model:
        raise ValueError("top-level and model evidence exact models differ")
    if gateway_evidence is not None and gateway_evidence.model != model:
        raise ValueError("model and gateway evidence exact models differ")
    if gateway_capability == "gateway_verified":
        if gateway_evidence is None or gateway_evidence.response_record is None:
            raise ValueError(
                "gateway_verified requires a captured local gateway response record"
            )
    elif gateway_evidence is not None and gateway_evidence.response_record is not None:
        raise ValueError("captured gateway response must not be marked gateway_unverified")
    captured_task_id = None
    capture_root = None
    capture_bytes = None
    if gateway_capability == "gateway_verified":
        capture_root, capture_bytes, captured_task_id = _materialize_gateway_capture(
            gateway_evidence.response_record, base_dir, model
        )
    loaded = SeedanceCapabilityEvidence(
        schema_version=SCHEMA_VERSION,
        model=model,
        model_capability=model_capability,
        gateway_capability=gateway_capability,
        model_evidence=model_evidence,
        gateway_evidence=gateway_evidence,
    )
    object.__setattr__(loaded, "_capture_verified", gateway_capability == "gateway_verified")
    object.__setattr__(loaded, "_validation_token", _TRUST_TOKEN)
    object.__setattr__(loaded, "_captured_task_id", captured_task_id)
    object.__setattr__(loaded, "_capture_root", capture_root)
    object.__setattr__(loaded, "_capture_bytes", capture_bytes)
    _register_capability(loaded)
    return loaded


def validate_capability_evidence(
    capability: SeedanceCapabilityEvidence,
) -> SeedanceCapabilityEvidence:
    """Canonical trust boundary for immutable capability evidence objects."""
    if not isinstance(capability, SeedanceCapabilityEvidence):
        raise ValueError("capability evidence object has an invalid type")
    if capability.schema_version != SCHEMA_VERSION:
        raise ValueError("Seedance capability evidence schema_version is invalid")
    if capability.model not in SUPPORTED_SEEDANCE_MODELS:
        raise ValueError("top-level model is not an allowlisted exact model identifier")
    if capability.model_capability not in ("model_supported", "unsupported"):
        raise ValueError("model_capability state is invalid")
    if capability.gateway_capability not in ("gateway_unverified", "gateway_verified"):
        raise ValueError("gateway_capability state is invalid")
    if not isinstance(capability.model_evidence, SeedanceEvidenceRecord):
        raise ValueError("model_evidence must be a SeedanceEvidenceRecord")
    _validate_evidence_record_fields(capability.model_evidence, "model evidence")
    if capability.model_evidence.model != capability.model:
        raise ValueError("top-level and model evidence exact models differ")
    if capability.model_evidence.source_url is None:
        raise ValueError("model capability requires source URL provenance")
    if capability.gateway_evidence is not None:
        if not isinstance(capability.gateway_evidence, SeedanceEvidenceRecord):
            raise ValueError("gateway_evidence must be a SeedanceEvidenceRecord")
        _validate_evidence_record_fields(capability.gateway_evidence, "gateway evidence")
        if capability.gateway_evidence.model != capability.model:
            raise ValueError("model and gateway evidence exact models differ")
    if capability.gateway_capability == "gateway_unverified" and capability.gateway_evidence is not None:
        raise ValueError("gateway_unverified must not claim gateway evidence")
    registered = _TRUSTED_EVIDENCE.get(id(capability))
    if (
        registered is None
        or registered[0]() is not capability
        or registered[1] != _capability_fingerprint(capability)
    ):
        raise _UntrustedEvidence("capability evidence was not loaded by the strict loader")
    if capability.gateway_capability == "gateway_verified" and (
        not capability._capture_verified
        or not capability._captured_task_id
        or capability.gateway_evidence is None
        or capability.gateway_evidence.response_record is None
        or capability._capture_root is None
        or capability._capture_bytes is None
    ):
        raise _UntrustedEvidence("gateway_verified evidence lacks trusted capture provenance")
    if capability.gateway_capability == "gateway_verified":
        _root, _data, task_id = _materialize_gateway_capture(
            capability.gateway_evidence.response_record,
            capability._capture_root,
            capability.model,
            expected_bytes=capability._capture_bytes,
        )
        if task_id != capability._captured_task_id:
            raise ValueError("captured gateway task_id changed after evidence loading")
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
        ("compiled_prompt", frozenset({"provenance"})),
        ("trajectory_prompt", frozenset({"provenance"})),
        ("annotation", frozenset({"provenance"})),
        ("iteration_manifest", frozenset({"provenance"})),
    )
    records: dict[str, dict[str, object]] = {}
    for name, extras in record_specs:
        raw = approved_export.get(name)
        if raw is None:
            blockers.append(f"missing approved {name} record")
            continue
        checked = _record_shape(raw, name, allowed_extra=extras)
        if checked is None:
            blockers.append(f"missing approved {name} hash/size/path")
            continue
        records[name] = checked
        source_hashes[name] = str(checked["sha256"])

    compiled = records.get("compiled_prompt")
    trajectory = records.get("trajectory_prompt")
    if compiled is not None and trajectory is not None and (
        compiled["sha256"] != trajectory["sha256"]
        or compiled["bytes"] != trajectory["bytes"]
    ):
        raise ValueError("compiled_prompt and trajectory_prompt hash/size differ")

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
        loaded_capability = validate_capability_evidence(capability)
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
        "source_records": {
            name: {
                "path": str(record["path"]),
                "sha256": str(record["sha256"]),
                "bytes": int(record["bytes"]),
            }
            for name, record in sorted(records.items())
        },
        "proxy": {
            "url": valid_url,
            "path": str(records["clay"]["path"]),
            "sha256": str(records["clay"]["sha256"]),
            "bytes": int(records["clay"]["bytes"]),
        },
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
