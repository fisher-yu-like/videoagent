from __future__ import annotations

import argparse
from dataclasses import asdict
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.backends.capabilities import gateway_capability
from videoactagent.backends.jd import (
    build_kling_t2v,
    build_seedance_first_last,
    build_seedance_t2v,
)


PLACEHOLDER_HOSTS = {"example.com", "localhost"}
PLACEHOLDER_SUFFIXES = (".example", ".invalid", ".localhost", ".test")


def validate_remote_asset(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme == "asset":
        if not parsed.netloc:
            raise ValueError("asset URI must contain an asset identifier")
        return value
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("remote asset must use HTTPS or asset://")
    hostname = parsed.hostname.lower()
    if hostname in PLACEHOLDER_HOSTS or hostname.endswith(PLACEHOLDER_SUFFIXES):
        raise ValueError("placeholder and local hosts are not remote asset evidence")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
    ):
        raise ValueError("local or reserved IP addresses are not remote assets")
    return value


def _prompt_payload(backend: str, prompt: str, duration: int) -> dict:
    if backend == "seedance":
        return build_seedance_t2v(prompt, duration)
    if backend == "kling":
        return build_kling_t2v(prompt, duration)
    raise ValueError(f"unsupported backend: {backend}")


def prepare_backend(bundle: dict, backend: str, bindings: dict) -> dict:
    capability = gateway_capability(backend)
    shot = bundle["shots"][0]
    shot_id = shot["shot_id"]
    duration = int(round(shot["duration"]))
    shot_bindings = bindings.get(shot_id, {})
    conditions = {
        "plain": {
            "status": "ready",
            "payload": _prompt_payload(backend, shot["prompts"]["plain"], duration),
        },
        "cinematic": {
            "status": "ready",
            "payload": _prompt_payload(
                backend, shot["prompts"]["cinematic"], duration
            ),
        },
    }

    missing_frames = [
        name for name in ("first_frame", "last_frame") if name not in shot_bindings
    ]
    first_last_blockers = []
    if capability.first_last_frame != "client_declared":
        first_last_blockers.append("JD gateway first_last_frame is unsupported")
    if missing_frames:
        first_last_blockers.append(
            f"missing remote bindings: {', '.join(missing_frames)}"
        )
    if first_last_blockers:
        conditions["first_last"] = {
            "status": "blocked",
            "blockers": first_last_blockers,
        }
    else:
        first_url = validate_remote_asset(shot_bindings["first_frame"])
        last_url = validate_remote_asset(shot_bindings["last_frame"])
        conditions["first_last"] = {
            "status": "ready",
            "payload": build_seedance_first_last(
                shot["prompts"]["cinematic"],
                first_url,
                last_url,
                duration,
            ),
        }

    proxy_blockers = []
    if capability.reference_video == "gateway_unverified":
        proxy_blockers.append("JD gateway reference_video capability is unverified")
    elif capability.reference_video == "unsupported":
        proxy_blockers.append("JD gateway reference_video is unsupported")
    if "proxy_video" not in shot_bindings:
        proxy_blockers.append("missing remote binding: proxy_video")
    else:
        validate_remote_asset(shot_bindings["proxy_video"])
    conditions["proxy_video"] = {
        "status": "blocked",
        "blockers": proxy_blockers,
    }

    return {
        "schema_version": "0.1",
        "backend": backend,
        "shot_id": shot_id,
        "network_called": False,
        "capability_evidence": asdict(capability),
        "conditions": conditions,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--backend", choices=("seedance", "kling"), required=True)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    bindings = (
        json.loads(args.bindings.read_text(encoding="utf-8"))
        if args.bindings
        else {}
    )
    report = prepare_backend(bundle, args.backend, bindings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        "BACKEND_READINESS_OK",
        json.dumps(
            {"backend": args.backend, "output": str(args.output)},
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    main()
