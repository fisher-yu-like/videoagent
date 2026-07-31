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
    build_seedance_reference_video,
    build_seedance_t2v,
)
from videoactagent.seedance_reference import (
    SeedanceCapabilityEvidence,
    load_seedance_capability_evidence,
    validate_remote_video_asset,
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


def prepare_backend(
    bundle: dict,
    backend: str,
    bindings: dict,
    capability_evidence: SeedanceCapabilityEvidence | dict | None = None,
) -> dict:
    capability = gateway_capability(backend)
    shot = bundle["shots"][0]
    shot_id = shot["shot_id"]
    shot_duration = float(shot["duration"])
    duration = int(round(shot_duration))
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
    reference_evidence = capability_evidence
    if isinstance(reference_evidence, dict):
        reference_evidence = load_seedance_capability_evidence(reference_evidence)
    if reference_evidence is not None and not isinstance(
        reference_evidence, SeedanceCapabilityEvidence
    ):
        raise ValueError("capability_evidence is not valid Seedance evidence")
    if backend != "seedance" and reference_evidence is not None:
        raise ValueError("Seedance capability evidence cannot be used for Kling")
    if reference_evidence is None:
        if capability.reference_video == "gateway_unverified":
            proxy_blockers.append("JD gateway reference_video capability is unverified")
        elif capability.reference_video == "unsupported":
            proxy_blockers.append("JD gateway reference_video is unsupported")
    elif reference_evidence.model_capability != "model_supported":
        proxy_blockers.append("Seedance model reference_video capability is unsupported")
    elif (
        reference_evidence.gateway_capability == "gateway_verified"
        and not reference_evidence._capture_verified
    ):
        proxy_blockers.append("gateway_verified evidence was not loaded from a captured response")
    if "proxy_video" not in shot_bindings:
        proxy_blockers.append("missing remote binding: proxy_video")
    else:
        if reference_evidence is None:
            validate_remote_asset(shot_bindings["proxy_video"])
        else:
            validate_remote_video_asset(shot_bindings["proxy_video"])
    if reference_evidence is not None and shot_duration != 5.0:
        proxy_blockers.append("Seedance reference_video requires exactly five seconds")
    if proxy_blockers:
        conditions["proxy_video"] = {
            "status": "blocked",
            "blockers": proxy_blockers,
        }
    else:
        assert reference_evidence is not None
        conditions["proxy_video"] = {
            "status": (
                "ready"
                if reference_evidence.gateway_capability == "gateway_verified"
                else "ready_for_single_combined_probe"
            ),
            "payload": build_seedance_reference_video(
                shot["prompts"]["cinematic"],
                shot_bindings["proxy_video"],
                model=reference_evidence.model,
                duration=duration,
            ),
        }

    reported_capability = asdict(capability)
    if reference_evidence is not None:
        def _provenance(record):
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

        reported_capability["provided_reference_video"] = {
            "model": reference_evidence.model,
            "model_capability": reference_evidence.model_capability,
            "gateway_capability": reference_evidence.gateway_capability,
            "content_type": "video_url",
            "url_field": "video_url",
            "role": "reference_video",
            "evidence": {
                "model": _provenance(reference_evidence.model_evidence),
                "gateway": _provenance(reference_evidence.gateway_evidence),
            },
        }
    return {
        "schema_version": "0.1",
        "backend": backend,
        "shot_id": shot_id,
        "network_called": False,
        "capability_evidence": reported_capability,
        "conditions": conditions,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--backend", choices=("seedance", "kling"), required=True)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--capability-evidence", type=Path)
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
    reference_evidence = (
        load_seedance_capability_evidence(args.capability_evidence)
        if args.capability_evidence
        else None
    )
    report = prepare_backend(
        bundle,
        args.backend,
        bindings,
        capability_evidence=reference_evidence,
    )
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
