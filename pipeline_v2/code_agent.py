"""Provider-neutral LLM Blender Code Agent boundary.

The generated scene implementation is intentionally not imported from the old
renderer. Only the request/response contract and static safety checks live here.
"""

from __future__ import annotations

from ast import Call, Import, ImportFrom, Name, NodeVisitor, parse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .json_repair import JSONRepairError, parse_json_response


CODE_AGENT_SCHEMA_VERSION = "blender-code-agent-1.0"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_ALLOWED_IMPORT_ROOTS = {"argparse", "bpy", "json", "math", "mathutils", "pathlib", "re", "sys", "hashlib"}
_BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
_REQUIRED_MARKERS = {
    "world_state_arg": "--world-state",
    "output_dir_arg": "--output-dir",
    "render_manifest": "render_manifest.json",
    "state_log": "state_log.json",
    "camera_log": "camera_log.json",
    "applied_state_log": "applied_state_log.json",
    "render_call": "bpy.ops.render.render",
    "camera_look_at": "to_track_quat",
    "world_datablock": "bpy.data.worlds.new",
    "manifest_schema": "pipeline-v2-render-manifest-1.0",
    "manifest_world_hash": "world_state_hash",
    "manifest_video_list": "videos",
    "success_marker": "PIPELINE_V2_BLENDER_OK",
    "render_style_arg": "--render-style",
    "resolution_arg": "--resolution",
    "media_type_video": "media_type",
    "argv_separator": "sys.argv",
    "visible_lighting": "bpy.data.lights.new",
    "camera_authored_log": "authored",
    "camera_applied_log": "applied",
}


class CodeAgentError(ValueError):
    """Raised when a code-agent response is unsafe or incomplete."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _endpoint(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CodeAgentError("provider base URL is invalid")
    path = parsed.path.rstrip("/")
    redacted = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/chat/completions", "", "")), redacted


def build_code_agent_payload(
    story_prompt: str,
    *,
    world: Mapping[str, Any],
    physical_contract: Mapping[str, Any],
    provider: str,
    model: str,
) -> dict[str, Any]:
    if not isinstance(story_prompt, str) or not story_prompt.strip():
        raise CodeAgentError("story_prompt must be non-empty text")
    if provider not in {"openai", "deepseek"}:
        raise CodeAgentError("provider must be openai or deepseek")
    system = (
        "You are a Blender Code Agent in a VideoCoCo-style draft-before-generation pipeline. "
        "Return exactly one JSON object with schema_version blender-code-agent-1.0. "
        "Generate a complete standalone Blender Python script for this specific prompt; "
        "do not import videoactagent, pipeline_v2, VideoCoCo, or any project renderer. "
        "The script must create one shared world, all entities, and every camera from the "
        "Create and assign a real bpy.data.worlds.new datablock before accessing scene.world.color or nodes; "
        "WorldState; it must not create separate inconsistent scenes per camera. "
        "The canonical pipeline coordinate system is right-handed Z-up: ground is z=0, X is longitudinal, Y is "
        "lateral/depth, and Z is vertical. Treat all supplied WorldState positions in that canonical system; do not "
        "reinterpret the axes from prose. "
        "Use neutral clay/grayscale materials only unless the supplied asset registry explicitly provides a "
        "material profile. Read the optional asset_registry.json and scene_layout.json sidecars as the "
        "authoritative asset/layout metadata. If an entity has source_kind canonical_procedural_v3 or a "
        "canonical GLB entry, materialize that asset exactly once in the shared World and never replace it "
        "with a single sphere, cylinder, or cube. Humanoids must have a readable full-body articulated "
        "silhouette with head, torso, pelvis, separated upper/lower arms, hands, upper/lower legs, and feet; "
        "preserve asset_id, dimensions_m, front_axis, up_axis, and origin. Primitive fallback is allowed only "
        "when the registry says primitive_fallback or the asset is explicitly unavailable. Preserve all authored entity and camera "
        "trajectory frames exactly, and write state_log.json, camera_log.json, and "
        "If the registry source_kind is procedural_skeleton_v1, create one armature per character with explicit "
        "shoulder/elbow/wrist and hip/knee/ankle bones, hand/foot IK target empties, pole targets, joint limits, "
        "and a frame-major skeleton_pose_log.json; never drive a limb by translating its mesh center into another body part. "
        "respect the coordinate conventions in the physical contract: entity rotations are radians. "
        "Camera trajectory points contain authored semantic look-at orientation metadata, but the "
        "Blender camera must be oriented from its per-frame target using direction.to_track_quat('-Z','Y') "
        "and to_euler('XYZ'), with roll applied around the camera local Z axis; do not assign the authored "
        "camera Euler triple directly. Use a character target height near 1.1m and a luggage target height "
        "near 0.5m so subjects are actually framed. Preserve authored position and target responsibilities, "
        "set camera_log.target_id exactly to the supplied CameraTrajectoryPlan target object_id (use null only "
        "for an explicit fixed point), and never replace an entity target with a fixed origin or another entity. "
        "and log both authored and applied camera orientation. "
        "render_manifest.json with SHA-256 hashes. The script must accept --world-state and "
        "--output-dir, run headlessly, save a .blend, and render one MP4 per camera. "
        "Only import argparse, bpy, json, math, mathutils, pathlib, re, sys, or hashlib. "
        "Do not use network, subprocess, shell commands, eval, exec, or environment secrets. "
        "The render_manifest.json must have schema_version pipeline-v2-render-manifest-1.0, "
        "contain world_state_hash, frame_count, fps, resolution, and a videos list; each video "
        "item must contain a relative path (never an absolute path), camera_id, bytes, sha256, "
        "frame_count, fps, and resolution. Hash the canonical WorldState as UTF-8 compact sorted "
        "JSON followed by exactly one newline. The script must write actual sampled transforms to "
        "state_log.json, camera_log.json, and applied_state_log.json, "
        "set image_settings.media_type to VIDEO before FFMPEG, accept --render-style and "
        "--resolution, and print PIPELINE_V2_BLENDER_OK only after all outputs are verified. "
        "Do not use the obsolete Action.fcurves API and do not set BLENDER_EEVEE_NEXT; on this "
        "Blender use BLENDER_EEVEE, BLENDER_WORKBENCH, or CYCLES as available. "
        "Do not access animation_data.action, fcurves, or keyframe_points at all; default keyframe interpolation "
        "is acceptable for this proxy and is safer across Blender versions. "
        "Target the host's Python runtime conservatively: write Python 3.7-compatible syntax, "
        "with no walrus operator, pattern matching, type-alias syntax, or other newer constructs. "
        "Do not return Markdown or a code fence. "
        "Use this minimal launch/render contract as a literal pattern (adapt names only): "
        "`parser=argparse.ArgumentParser(); parser.add_argument('--world-state', required=True); "
        "parser.add_argument('--output-dir', required=True); argv=sys.argv; "
        "script_args=argv[argv.index('--')+1:] if '--' in argv else []; "
        "args=parser.parse_args(script_args); "
        "scene.render.image_settings.media_type='VIDEO'; "
        "scene.render.image_settings.file_format='FFMPEG'; "
        "for frame in range(frame_count): scene.frame_set(frame); "
        "camera_log=[{'authored':[{'frame':0,'position':[0,0,0],'rotation':[0,0,0]}], "
        "'camera_id':'camera_1','target_id':'traveler'}]` . "
        "The first assignment must precede the second; do not call parse_args() on Blender's full argv. "
        "Use this exact camera-log shape: one object per camera with camera_id, target_id, an authored "
        "list of {frame, position, rotation}, and an applied list of {frame, position, rotation}; "
        "write applied_state_log as one object per frame with an entities mapping. "
        "Write state_log.json in the same frame-major shape: exactly frame_count objects, each with integer frame "
        "and an entities mapping containing every authored entity's position and rotation. "
        "For a visible clay render, also create at least two real lights, for example: "
        "`data=bpy.data.lights.new('key','AREA'); data.energy=800; data.size=5.0; "
        "obj=bpy.data.objects.new('key',data); bpy.context.collection.objects.link(obj); "
        "obj.location=(0,-2,6)`; add a fill light from a different direction and keep positive energy. "
        "Do not rely on an unlit World background alone."
    )
    implementation_checklist = {
        "source_of_truth": "Preserve the supplied WorldState trajectories and physical render contract; do not invent a different story.",
        "proxy_style": "neutral clay/grayscale, readable silhouettes, station geometry and all target entities visible",
        "asset_materialization": [
            "read asset_registry.json and scene_layout.json when present",
            "reuse each canonical asset once in the shared World",
            "never collapse a canonical humanoid into one primitive",
            "preserve asset dimensions, axes, origin, and asset_id",
        ],
        "visibility": [
            "at least two real lights with positive energy",
            "no wall-only, ceiling-only, or nearly black camera output",
            "camera target height: characters about 1.1m, luggage about 0.5m",
        ],
        "causal_audit": [
            "every visible entity state must trace to a WorldState trajectory",
            "do not show a physical event before its authored frame",
            "write authored and applied camera rows for every frame",
        ],
        "python_runtime": "Python 3.7-compatible syntax; run AST-valid code before returning",
        "launch_arguments": "read arguments only after the Blender '--' separator",
        "video_settings_order": ["image_settings.media_type='VIDEO'", "image_settings.file_format='FFMPEG'"],
    }
    user = {
        "story_prompt": story_prompt.strip(),
        "world_state": world,
        "physical_render_contract": physical_contract,
        "implementation_checklist": implementation_checklist,
        "videococo_preview_contract": {
            "semantic_keyframes": "honor any supplied K0-K4 states",
            "transitions": "do not reveal a caused state before its transition",
            "must_show": "keep required entities and intermediate states visible",
            "must_avoid": "do not solve a missing process by making only the final frame correct",
            "preview_outputs": ["MP4", ".blend", "state_log.json", "camera_log.json", "applied_state_log.json"],
        },
        "required_output_schema": {
            "schema_version": CODE_AGENT_SCHEMA_VERSION,
            "fields": [
                "schema_version", "script", "scene_description", "asset_manifest",
                "camera_manifest", "expected_outputs", "assumptions", "known_limitations",
            ],
        },
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.15,
        "max_tokens": 24000,
        "stream": False,
    }
    if provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    return payload


def _document_exact(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise CodeAgentError("code-agent document must be an object")
    required = {
        "schema_version", "script", "scene_description", "asset_manifest",
        "camera_manifest", "expected_outputs", "assumptions", "known_limitations",
    }
    missing = required - set(document)
    unknown = set(document) - required
    if missing or unknown:
        raise CodeAgentError(f"code-agent document fields invalid: missing={sorted(missing)}, unknown={sorted(unknown)}")
    if document["schema_version"] != CODE_AGENT_SCHEMA_VERSION:
        raise CodeAgentError(f"schema_version must be {CODE_AGENT_SCHEMA_VERSION!r}")
    if not isinstance(document["script"], str) or not document["script"].strip():
        raise CodeAgentError("code-agent script must be non-empty text")
    for field in ("asset_manifest", "camera_manifest", "expected_outputs", "assumptions", "known_limitations"):
        if not isinstance(document[field], list):
            raise CodeAgentError(f"code-agent {field} must be a list")
    return dict(document)


def _extract_code_agent_document_with_meta(response: object) -> tuple[dict[str, Any], Mapping[str, Any]]:
    if not isinstance(response, Mapping):
        raise CodeAgentError("provider response must be an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise CodeAgentError("provider response choices are invalid")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise CodeAgentError(f"provider response did not finish normally: {choice.get('finish_reason')}")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise CodeAgentError("code-agent response content is empty")
    try:
        document, repair_meta = parse_json_response(content, label="code-agent")
    except JSONRepairError as exc:
        raise CodeAgentError(str(exc)) from exc
    return _document_exact(document), repair_meta


def extract_code_agent_document(response: object) -> dict[str, Any]:
    """Extract and strictly validate a Code Agent document."""

    document, _repair_meta = _extract_code_agent_document_with_meta(response)
    return document


class _SafetyVisitor(NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Import(self, node: Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root not in _ALLOWED_IMPORT_ROOTS:
                self.errors.append(f"import is not allowed: {alias.name}")

    def visit_ImportFrom(self, node: ImportFrom) -> None:
        root = (node.module or "").split(".", 1)[0]
        if root not in _ALLOWED_IMPORT_ROOTS:
            self.errors.append(f"import is not allowed: {node.module}")

    def visit_Call(self, node: Call) -> None:
        if isinstance(node.func, Name) and node.func.id in _BANNED_CALLS:
            self.errors.append(f"call is not allowed: {node.func.id}")


def validate_generated_script(document: object) -> dict[str, Any]:
    normalized = _document_exact(document)
    script = normalized["script"]
    if len(script) > 200_000:
        raise CodeAgentError("generated script exceeds 200000 characters")
    if "videoactagent" in script or "pipeline_v2" in script or "VideoCoCo" in script:
        raise CodeAgentError("project import or reference is not allowed in generated script")
    if "action.fcurves" in script:
        raise CodeAgentError("obsolete Blender Action.fcurves API is not allowed")
    if ":=" in script:
        raise CodeAgentError("Python 3.7 compatibility forbids the walrus operator")
    if "BLENDER_EEVEE_NEXT" in script:
        raise CodeAgentError("BLENDER_EEVEE_NEXT is not available in the target Blender build")
    media_video = script.find("media_type = 'VIDEO'")
    if media_video < 0:
        media_video = script.find('media_type = "VIDEO"')
    ffmpeg_format = script.find("file_format = 'FFMPEG'")
    if ffmpeg_format < 0:
        ffmpeg_format = script.find('file_format = "FFMPEG"')
    if ffmpeg_format >= 0 and (media_video < 0 or media_video > ffmpeg_format):
        raise CodeAgentError("Blender 5.1 requires image_settings.media_type=VIDEO before file_format=FFMPEG")
    try:
        tree = parse(script, filename="generated_blender.py", mode="exec")
    except SyntaxError as exc:
        raise CodeAgentError(f"generated Blender script is not valid Python: {exc}") from exc
    visitor = _SafetyVisitor()
    visitor.visit(tree)
    if visitor.errors:
        raise CodeAgentError("; ".join(visitor.errors))
    markers = {name: marker in script for name, marker in _REQUIRED_MARKERS.items()}
    missing_markers = [name for name, present in markers.items() if not present]
    if missing_markers:
        raise CodeAgentError(f"generated Blender script is missing required markers: {missing_markers}")
    return {
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "script_bytes": len(script.encode("utf-8")),
        "required_markers": markers,
    }


def _provider_config(environment: Mapping[str, str], provider: str, model: str | None) -> tuple[str, str, str]:
    configs = {
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
    }
    if provider not in configs:
        raise CodeAgentError(f"unsupported provider: {provider!r}")
    key_name, base_name, model_name, default_model = configs[provider]
    api_key = environment.get(key_name, "").strip()
    base_url = environment.get(base_name, "").strip()
    selected_model = (model if model is not None else environment.get(model_name, default_model)).strip() or default_model
    if not api_key or not base_url:
        raise CodeAgentError(f"{provider} environment variables are unavailable")
    return api_key, base_url, selected_model


def request_code_agent(
    *,
    story_prompt: str,
    world: Mapping[str, Any],
    physical_contract: Mapping[str, Any],
    output_dir: Path | str,
    provider: str = "openai",
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Make exactly one code-agent call and persist all redacted evidence."""
    output = Path(output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    environment = os.environ if environ is None else environ
    started_at = _now()
    started = time.monotonic()
    api_calls = 0
    api_key = ""
    selected_model = model or ""
    redacted_base = None
    try:
        api_key, raw_base, selected_model = _provider_config(environment, provider, model)
        endpoint, redacted_base = _endpoint(raw_base)
        payload = build_code_agent_payload(
            story_prompt,
            world=world,
            physical_contract=physical_contract,
            provider=provider,
            model=selected_model,
        )
        _write_json(output / "request.json", {"endpoint": endpoint, "payload": payload, "authorization_saved": False})
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        api_calls = 1
        with transport(request, timeout=180) as response_stream:
            raw_response = response_stream.read()
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            (output / "response.raw").write_bytes(raw_response.replace(api_key.encode(), b"[REDACTED]"))
            raise CodeAgentError(f"{provider} response is not JSON: {exc}") from exc
        _write_json(output / "response.json", _redact(response, api_key))
        document, repair_meta = _extract_code_agent_document_with_meta(response)
        _write_json(output / "json_repair.json", repair_meta)
        validation = validate_generated_script(document)
        _write_json(output / "code_agent_document.json", _redact(document, api_key))
        # Preserve the exact UTF-8 bytes that were hashed by validate_generated_script.
        # Text-mode writes on Windows translate LF to CRLF and break provenance.
        (output / "generated_blender.py").write_bytes(document["script"].encode("utf-8"))
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
            "json_repair": repair_meta,
            "script_validation": validation,
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
        if isinstance(exc, CodeAgentError):
            raise
        raise CodeAgentError(f"code-agent request failed: {exc}") from exc
