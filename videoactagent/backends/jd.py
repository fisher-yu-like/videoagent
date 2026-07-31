from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid

from videoactagent.run_record import RunDirectory


SUPPORTED_SEEDANCE_MODELS = frozenset(
    {"Doubao-Seedance-2.5", "Doubao-Seedance-2.0"}
)


def build_kling_t2v(prompt: str, duration: int = 5) -> dict:
    return {
        "model": "Kling-V2-5-Turbo",
        "content": [{"type": "text", "text": prompt}],
        "parameters": {
            "duration": duration,
            "mode": "std",
            "aspect_ratio": "16:9",
        },
    }


def build_seedance_t2v(prompt: str, duration: int = 5) -> dict:
    return {
        "model": "Doubao-Seedance-2.0",
        "content": [{"type": "text", "text": prompt}],
        "parameters": {
            "ratio": "16:9",
            "resolution": "720p",
            "duration": duration,
            "watermark": False,
        },
    }


def build_seedance_first_last(
    prompt: str,
    first_url: str,
    last_url: str,
    duration: int = 5,
) -> dict:
    return {
        "model": "Doubao-Seedance-2.0",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": first_url},
                "role": "first_frame",
            },
            {
                "type": "image_url",
                "image_url": {"url": last_url},
                "role": "last_frame",
            },
        ],
        "parameters": {
            "ratio": "adaptive",
            "resolution": "720p",
            "duration": duration,
            "watermark": False,
        },
    }


def build_seedance_reference_video(
    prompt: str,
    proxy_url: str,
    *,
    model: str,
    duration: int = 5,
) -> dict:
    """Build, but never submit, the one supported reference-video request shape."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Seedance reference prompt must be a nonempty string")
    if model not in SUPPORTED_SEEDANCE_MODELS:
        raise ValueError("Seedance model is not an allowlisted exact model identifier")
    if type(duration) is not int or duration != 5:
        raise ValueError("Seedance reference-video duration must be exactly 5 seconds")
    # Imported lazily to keep the URL contract in one place without a module cycle.
    from videoactagent.seedance_reference import validate_remote_video_asset

    url = validate_remote_video_asset(proxy_url)
    return {
        "model": model,
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "video_url",
                "video_url": {"url": url},
                "role": "reference_video",
            },
        ],
        "parameters": {
            "ratio": "16:9",
            "resolution": "720p",
            "duration": 5,
            "watermark": False,
        },
    }


def extract_task_id(response: dict) -> str:
    task_id = response.get("task_id") or (response.get("result") or {}).get(
        "task_id"
    )
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("gateway response does not contain task_id")
    return task_id


def extract_status(response: dict) -> str | None:
    return (
        response.get("task_status")
        or (response.get("result") or {}).get("task_status")
        or response.get("status")
    )


def extract_video_urls(response: dict) -> list[tuple[str, str]]:
    content = response.get("content")
    if content is None:
        content = (response.get("result") or {}).get("content")
    urls = []
    for item in content or []:
        url = (item.get("video_url") or {}).get("url")
        if url:
            urls.append((item.get("id") or "video", url))
    return urls


def _headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise RuntimeError("JD_KLING_KEY is required")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Trace-id": uuid.uuid4().hex,
    }


def _request_once(
    url: str,
    api_key: str,
    method: str,
    payload: dict | None = None,
) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers=_headers(api_key),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"JD gateway HTTP {exc.code}: {body}") from exc
    return json.loads(body)


def submit_once(
    payload: dict,
    api_key: str,
    base_url: str,
    run: RunDirectory,
) -> str:
    endpoint = f"{base_url.rstrip('/')}/v1/task/submit"
    run.write_json(
        "request.json",
        {"method": "POST", "endpoint": endpoint, "payload": payload},
    )
    try:
        response = _request_once(endpoint, api_key, "POST", payload)
        run.write_json("response.json", response)
        if response.get("error"):
            raise RuntimeError(f"JD gateway submit error: {response['error']}")
        task_id = extract_task_id(response)
        run.write_json(
            "state.json",
            {
                "base_url": base_url.rstrip("/"),
                "task_id": task_id,
                "status": "submitted",
                "video_urls": [],
            },
        )
    except Exception as exc:
        run.write_json(
            "failure.json",
            {"type": type(exc).__name__, "message": str(exc)},
        )
        raise
    print(f"SUBMITTED task_id={task_id}", flush=True)
    return task_id


def query_once(api_key: str, run: RunDirectory) -> dict:
    state_path = run.path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    endpoint = f"{state['base_url']}/v1/task/{state['task_id']}"
    response = _request_once(endpoint, api_key, "GET")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run.write_json(f"query_{timestamp}.json", response)
    status = extract_status(response)
    urls = extract_video_urls(response)
    state["status"] = status
    state["video_urls"] = [
        {"id": item_id, "url": url} for item_id, url in urls
    ]
    run.write_json("state.json", state)
    print(
        f"QUERY status={status} videos={len(urls)}",
        flush=True,
    )
    return response


def download_once(run: RunDirectory) -> Path:
    state = json.loads((run.path / "state.json").read_text(encoding="utf-8"))
    videos = state.get("video_urls") or []
    if not videos:
        raise RuntimeError("state does not contain a downloadable video URL")
    target = run.path / "result.mp4"
    temporary = run.path / ".result.mp4.tmp"
    request = urllib.request.Request(videos[0]["url"], method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            contents = response.read()
        temporary.write_bytes(contents)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    record = {
        "path": target.name,
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }
    run.write_json("download.json", record)
    print(
        f"DOWNLOADED bytes={record['bytes']} sha256={record['sha256']}",
        flush=True,
    )
    return target
