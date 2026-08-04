"""One-call, zero-retry DeepSeek adapter for multicamera planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from videoactagent.multicam_plan import MulticamPlanError, load_multicam_plan
from videoactagent.scene_plan import (
    ACTOR_ACTIONS,
    OBJECT_SEMANTICS,
    ScenePlanDraft,
    ScenePlanError,
)


MODEL = "deepseek-v4-flash"
SYSTEM_JSON_PROMPT = """You are a cinematography planning agent. Return one JSON object only.
Plan exactly three synchronized full-timeline cameras named camera_a, camera_b and camera_c.
Collectively their responsibility_segments must cover [0,1] exactly without gaps or overlaps.
Use only roles master/follow/reverse; sides north/south/east/west and diagonal combinations;
shot sizes extreme_wide/wide/medium/close/extreme_close; motions static/follow/arc/dolly/truck;
look_at_policy actors_midpoint/target_actor/fixed_world_point. Preserve the requested locked prefix.
The JSON fields must exactly match the schema example included in the user input."""
SCENE_JSON_PROMPT = f"""You are a constrained scene planning agent. Return one JSON object only.
Use schema_version scene-plan-1.0 and exactly the fields shown in the schema example.
Choose only the listed environment, camera, and object enum values. Use one to three actors.
Actor action is only a deterministic moving/static label and must be exactly one of:
{', '.join(ACTOR_ACTIONS)}. Do not imply articulated or complex action generation.
All actor/object start and end XY coordinates must stay inside world_bounds. IDs must be unique.
Object semantic must be one of: {', '.join(OBJECT_SEMANTICS)}. A static object must use
identical start and end coordinates.
Do not return Markdown, Blender code, extra fields, NaN, Infinity, or invented defaults."""


class DeepSeekPlannerError(ValueError):
    """Raised when a real planning call cannot produce validated evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, value: object) -> None:
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_bytes(path: Path, data: bytes) -> None:
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _save_raw_response(output: Path, raw_response: bytes, api_key: str) -> None:
    secret = api_key.encode("utf-8")
    redacted = raw_response.replace(secret, b"[REDACTED]") if secret else raw_response
    _write_bytes(output / "response.raw", redacted)


def _redact_json(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return {
            _redact_json(key, secret) if isinstance(key, str) else key:
            _redact_json(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_json(item, secret) for item in value)
    return value


def _response_artifact(output: Path) -> str | None:
    if (output / "response.json").is_file():
        return "response.json"
    if (output / "response.raw").is_file():
        return "response.raw"
    return None


def _endpoint(value: str) -> tuple[str, str]:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeepSeekPlannerError("DeepSeek environment base URL is invalid")
    base_path = parsed.path.rstrip("/")
    redacted = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
    endpoint = urlunsplit((
        parsed.scheme, parsed.netloc, base_path + "/chat/completions", "", ""
    ))
    return endpoint, redacted


def _failure_evidence(
    *, status: str, started_at: str, elapsed: float, api_calls: int,
    base_url: str | None, model: str | None, error: str,
    response_artifact: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": status,
        "started_at": started_at,
        "elapsed_seconds": round(elapsed, 6),
        "api_call_count": api_calls,
        "retry_count": 0,
        "base_url": base_url,
        "model": model,
        "response_artifact": response_artifact,
        "error": error,
    }


def request_multicam_plan(
    *, scene_context: Mapping[str, Any], output_dir: Path | str,
    previous_plan: Mapping[str, Any] | None = None,
    previous_camera_bundle: Mapping[str, Any] | None = None,
    revision_scope: str | None = None,
    feedback: str | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    output = Path(output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    started_at = _now()
    started = time.monotonic()
    environment = os.environ if environ is None else environ
    key = environment.get("DEEPSEEK_API_KEY", "").strip()
    raw_base = environment.get("DEEPSEEK_BASE_URL", "").strip()
    model = MODEL
    if not key or not raw_base:
        evidence = _failure_evidence(
            status="environment_blocked", started_at=started_at,
            elapsed=time.monotonic() - started, api_calls=0, base_url=None,
            model=model or None, error="DeepSeek environment variables are unavailable",
        )
        _write(output / "evidence.json", evidence)
        raise DeepSeekPlannerError(evidence["error"])
    try:
        endpoint, redacted_base = _endpoint(raw_base)
        revision_values = (
            previous_plan, previous_camera_bundle, revision_scope, feedback,
        )
        revision_requested = any(value is not None for value in revision_values)
        if revision_requested and not all(value is not None for value in revision_values):
            raise DeepSeekPlannerError(
                "previous_plan, previous_camera_bundle, revision_scope and feedback "
                "must be supplied together"
            )
        if revision_requested:
            if not isinstance(previous_plan, Mapping):
                raise DeepSeekPlannerError("previous_plan must be an object")
            if not isinstance(previous_camera_bundle, Mapping):
                raise DeepSeekPlannerError("previous_camera_bundle must be an object")
            if revision_scope not in {"all", "camera_a", "camera_b", "camera_c"}:
                raise DeepSeekPlannerError("revision_scope is invalid")
            if not isinstance(feedback, str) or not feedback.strip():
                raise DeepSeekPlannerError("feedback must be a non-empty string")
            feedback = feedback.strip()
            if len(feedback) > 2000:
                raise DeepSeekPlannerError("feedback must be at most 2000 characters")
        scene_id = scene_context.get("scene_id")
        actors = scene_context.get("actors")
        targets = scene_context.get("controllable_targets", actors)
        locked = scene_context.get("locked_through_keyframe")
        if (
            not isinstance(scene_id, str) or not isinstance(actors, list)
            or not isinstance(targets, list) or not targets
            or not all(isinstance(target, str) for target in targets)
        ):
            raise DeepSeekPlannerError("scene context identity is invalid")
        schema_example = {
            "schema_version": "1.0",
            "scene_id": scene_id,
            "director_intent": "short intent",
            "actor_staging": ["one concrete staging instruction"],
            "locked_through_keyframe": locked,
            "cameras": [
                {
                    "camera_id": "camera_a", "role": "master", "target": "all_actors",
                    "side": "south", "shot_size": "wide", "motion": "static",
                    "look_at_policy": "actors_midpoint",
                    "responsibility_segments": [[0.0, 0.34]],
                    "constraints": ["keep all actors visible"],
                    "rationale": "establish spatial relationships",
                },
                {
                    "camera_id": "camera_b", "role": "follow", "target": targets[0],
                    "side": "south_west", "shot_size": "medium", "motion": "follow",
                    "look_at_policy": "target_actor",
                    "responsibility_segments": [[0.34, 0.67]],
                    "constraints": ["preserve headroom"],
                    "rationale": "cover the moving subject",
                },
                {
                    "camera_id": "camera_c", "role": "reverse", "target": targets[-1],
                    "side": "north_east", "shot_size": "medium", "motion": "arc",
                    "look_at_policy": "target_actor",
                    "responsibility_segments": [[0.67, 1.0]],
                    "constraints": ["avoid crossing actor paths"],
                    "rationale": "cover the response angle",
                },
            ],
        }
        user_payload = dict(scene_context)
        user_payload["required_json_schema_example"] = schema_example
        if revision_requested:
            user_payload["revision"] = {
                "scope": revision_scope,
                "feedback": feedback,
                "previous_plan": dict(previous_plan),
                "previous_camera_bundle": dict(previous_camera_bundle),
            }
        system_prompt = SYSTEM_JSON_PROMPT
        if revision_scope in {"camera_a", "camera_b", "camera_c"}:
            system_prompt += (
                " For a single-camera revision, reproduce every unselected camera "
                "assignment exactly."
            )
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            "max_tokens": 4096,
            "stream": False,
        }
        _write(output / "request.json", _redact_json({
            "endpoint": endpoint,
            "payload": request_payload,
            "authorization_saved": False,
        }, key))
        request = Request(
            endpoint,
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with transport(request, timeout=60) as response_stream:
            raw_response = response_stream.read()
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            _save_raw_response(output, raw_response, key)
            raise DeepSeekPlannerError(f"DeepSeek response is not JSON: {exc}") from exc
        _write(output / "response.json", _redact_json(response, key))
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise DeepSeekPlannerError("DeepSeek response choices are invalid")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise DeepSeekPlannerError(
                "DeepSeek response was truncated at max_tokens=4096"
            )
        if finish_reason != "stop":
            raise DeepSeekPlannerError("DeepSeek response did not finish normally")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekPlannerError("DeepSeek response content is empty")
        try:
            plan_value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekPlannerError(f"DeepSeek plan content is not JSON: {exc}") from exc
        if not isinstance(plan_value, Mapping):
            raise DeepSeekPlannerError("DeepSeek plan must be one JSON object")
        try:
            plan = load_multicam_plan(
                plan_value,
                scene_id=scene_id,
                actors=targets,
                locked_through_keyframe=locked,
            )
        except MulticamPlanError as exc:
            raise DeepSeekPlannerError(f"DeepSeek plan schema is invalid: {exc}") from exc
        _write(output / "plan.json", plan.to_dict())
        evidence = {
            "schema_version": "1.0",
            "status": "succeeded",
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "api_call_count": 1,
            "retry_count": 0,
            "base_url": redacted_base,
            "model": response.get("model", model),
            "response_artifact": "response.json",
            "response_id": response.get("id"),
            "usage": response.get("usage"),
            "validated_scene_id": plan.scene_id,
            "error": None,
        }
        evidence = _redact_json(evidence, key)
        _write(output / "evidence.json", evidence)
        return evidence
    except DeepSeekPlannerError as exc:
        base_url = None
        try:
            _unused, base_url = _endpoint(raw_base)
        except DeepSeekPlannerError:
            pass
        evidence = _failure_evidence(
            status="failed", started_at=started_at, elapsed=time.monotonic() - started,
            api_calls=1 if (output / "request.json").is_file() else 0,
            base_url=base_url, model=model or None, error=str(exc),
            response_artifact=_response_artifact(output),
        )
        _write(output / "evidence.json", evidence)
        raise
    except Exception as exc:
        try:
            _unused, redacted_base = _endpoint(raw_base)
        except DeepSeekPlannerError:
            redacted_base = None
        evidence = _failure_evidence(
            status="failed", started_at=started_at, elapsed=time.monotonic() - started,
            api_calls=1 if (output / "request.json").is_file() else 0,
            base_url=redacted_base, model=model or None,
            error=f"{type(exc).__name__}: {exc}",
            response_artifact=_response_artifact(output),
        )
        _write(output / "evidence.json", evidence)
        raise DeepSeekPlannerError(evidence["error"]) from exc


def request_scene_plan(
    *, story_prompt: str, duration_seconds: float, output_dir: Path | str,
    feedback: str | None = None, previous_draft: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Request exactly one scene draft and persist its complete redacted evidence."""

    output = Path(output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=False)
    started_at = _now()
    started = time.monotonic()
    environment = os.environ if environ is None else environ
    key = environment.get("DEEPSEEK_API_KEY", "").strip()
    raw_base = environment.get("DEEPSEEK_BASE_URL", "").strip()
    if not key or not raw_base:
        evidence = _failure_evidence(
            status="environment_blocked", started_at=started_at,
            elapsed=time.monotonic() - started, api_calls=0, base_url=None,
            model=MODEL, error="DeepSeek environment variables are unavailable",
        )
        _write(output / "evidence.json", evidence)
        raise DeepSeekPlannerError(evidence["error"])

    try:
        endpoint, redacted_base = _endpoint(raw_base)
        if not isinstance(story_prompt, str) or not story_prompt.strip():
            raise DeepSeekPlannerError("story_prompt must be a non-empty string")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(float(duration_seconds))
            or float(duration_seconds) <= 0.0
        ):
            raise DeepSeekPlannerError("duration_seconds must be a finite positive number")
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip()):
            raise DeepSeekPlannerError("feedback must be a non-empty string when supplied")
        if previous_draft is not None and not isinstance(previous_draft, Mapping):
            raise DeepSeekPlannerError("previous_draft must be an object when supplied")

        schema_example = {
            "schema_version": "scene-plan-1.0",
            "scene_id": "safe_scene_id",
            "environment_preset": "station",
            "duration_seconds": float(duration_seconds),
            "fps": 3,
            "world_bounds": [-5.0, 5.0, -4.0, 4.0],
            "actors": [
                {
                    "id": "actor_a", "color": "#F28E2B", "action": "walk",
                    "start": [-3.0, 0.0, 0.0], "end": [-0.5, 0.0, 0.0],
                    "facing": "actor_b",
                },
                {
                    "id": "actor_b", "color": "#4E79A7", "action": "wait",
                    "start": [1.5, 0.0, 0.0], "end": [1.5, 0.0, 0.0],
                    "facing": "actor_a",
                },
            ],
            "objects": [{
                "id": "prop", "primitive": "cube", "semantic": "move",
                "start": [-2.8, 0.2], "end": [-0.3, 0.2],
            }],
            "initial_camera": {
                "shot_size": "wide", "focal_length_mm": 35.0,
                "motion": "static", "start": [0.0, -10.0, 6.0],
                "end": [0.0, -10.0, 6.0], "look_at": "actors_midpoint",
            },
            "explanation": "A short concrete staging explanation.",
        }
        user_payload: dict[str, Any] = {
            "story_prompt": story_prompt,
            "duration_seconds": float(duration_seconds),
            "allowed_environment_presets": [
                "station", "city_crosswalk", "forest_path", "studio_room",
                "cafe", "warehouse", "generic",
            ],
            "allowed_shot_sizes": [
                "extreme_wide", "wide", "medium", "close", "extreme_close",
            ],
            "allowed_camera_motions": [
                "static", "follow", "arc", "dolly", "truck", "dolly_in",
                "dolly_out", "truck_left", "truck_right", "pan_left", "pan_right",
            ],
            "allowed_object_primitives": ["cube", "sphere", "cylinder"],
            "allowed_object_semantics": list(OBJECT_SEMANTICS),
            "allowed_actor_actions": list(ACTOR_ACTIONS),
            "required_json_schema_example": schema_example,
        }
        if feedback is not None:
            user_payload["feedback"] = feedback
        if previous_draft is not None:
            user_payload["previous_draft"] = dict(previous_draft)
        request_payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SCENE_JSON_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            "max_tokens": 4096,
            "stream": False,
        }
        _write(output / "request.json", {
            "endpoint": endpoint,
            "payload": request_payload,
            "authorization_saved": False,
        })
        request = Request(
            endpoint,
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with transport(request, timeout=60) as response_stream:
            raw_response = response_stream.read()
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            _save_raw_response(output, raw_response, key)
            raise DeepSeekPlannerError(f"DeepSeek response is not JSON: {exc}") from exc
        _write(output / "response.json", _redact_json(response, key))
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise DeepSeekPlannerError("DeepSeek response choices are invalid")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise DeepSeekPlannerError("DeepSeek response was truncated at max_tokens=4096")
        if finish_reason != "stop":
            raise DeepSeekPlannerError("DeepSeek response did not finish normally")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekPlannerError("DeepSeek response content is empty")
        try:
            draft_value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekPlannerError(f"DeepSeek scene content is not JSON: {exc}") from exc
        if not isinstance(draft_value, Mapping):
            raise DeepSeekPlannerError("DeepSeek scene plan must be one JSON object")
        try:
            draft = ScenePlanDraft.from_dict(draft_value)
        except ScenePlanError as exc:
            raise DeepSeekPlannerError(f"DeepSeek scene plan schema is invalid: {exc}") from exc
        if not math.isclose(
            draft.duration_seconds, float(duration_seconds), rel_tol=0.0, abs_tol=1e-9
        ):
            raise DeepSeekPlannerError("DeepSeek scene plan duration differs from the request")
        _write(output / "draft.json", draft.to_dict())
        evidence = {
            "schema_version": "1.0",
            "status": "succeeded",
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "api_call_count": 1,
            "retry_count": 0,
            "base_url": redacted_base,
            "model": MODEL,
            "response_artifact": "response.json",
            "response_id": response.get("id"),
            "usage": response.get("usage"),
            "validated_scene_id": draft.scene_id,
            "error": None,
        }
        evidence = _redact_json(evidence, key)
        _write(output / "evidence.json", evidence)
        return evidence
    except DeepSeekPlannerError as exc:
        base_url = None
        try:
            _unused, base_url = _endpoint(raw_base)
        except DeepSeekPlannerError:
            pass
        evidence = _failure_evidence(
            status="failed", started_at=started_at, elapsed=time.monotonic() - started,
            api_calls=1 if (output / "request.json").is_file() else 0,
            base_url=base_url, model=MODEL, error=str(exc),
            response_artifact=_response_artifact(output),
        )
        _write(output / "evidence.json", evidence)
        raise
    except Exception as exc:
        try:
            _unused, redacted_base = _endpoint(raw_base)
        except DeepSeekPlannerError:
            redacted_base = None
        evidence = _failure_evidence(
            status="failed", started_at=started_at, elapsed=time.monotonic() - started,
            api_calls=1 if (output / "request.json").is_file() else 0,
            base_url=redacted_base, model=MODEL,
            error=f"{type(exc).__name__}: {exc}",
            response_artifact=_response_artifact(output),
        )
        _write(output / "evidence.json", evidence)
        raise DeepSeekPlannerError(evidence["error"]) from exc
