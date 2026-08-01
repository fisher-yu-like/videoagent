"""One-call, zero-retry DeepSeek adapter for multicamera planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from videoactagent.multicam_plan import MulticamPlanError, load_multicam_plan


SYSTEM_JSON_PROMPT = """You are a cinematography planning agent. Return one JSON object only.
Plan exactly three synchronized full-timeline cameras named camera_a, camera_b and camera_c.
Collectively their responsibility_segments must cover [0,1] exactly without gaps or overlaps.
Use only roles master/follow/reverse; sides north/south/east/west and diagonal combinations;
shot sizes extreme_wide/wide/medium/close/extreme_close; motions static/follow/arc/dolly/truck;
look_at_policy actors_midpoint/target_actor/fixed_world_point. Preserve the requested locked prefix.
The JSON fields must exactly match the schema example included in the user input."""


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
        "error": error,
    }


def request_multicam_plan(
    *, scene_context: Mapping[str, Any], output_dir: Path | str,
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
    model = environment.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
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
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_JSON_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
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
            raise DeepSeekPlannerError(f"DeepSeek response is not JSON: {exc}") from exc
        _write(output / "response.json", response)
        choices = response.get("choices") if isinstance(response, Mapping) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise DeepSeekPlannerError("DeepSeek response choices are invalid")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
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
            "response_id": response.get("id"),
            "usage": response.get("usage"),
            "validated_scene_id": plan.scene_id,
            "error": None,
        }
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
        )
        _write(output / "evidence.json", evidence)
        raise DeepSeekPlannerError(evidence["error"]) from exc
