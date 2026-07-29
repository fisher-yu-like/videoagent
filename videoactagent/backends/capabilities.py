from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


EvidenceState = Literal[
    "client_declared",
    "model_supported",
    "gateway_unverified",
    "unsupported",
]


@dataclass(frozen=True)
class GatewayCapability:
    backend: str
    text_to_video: EvidenceState
    image_to_video: EvidenceState
    first_last_frame: EvidenceState
    reference_image: EvidenceState
    reference_video: EvidenceState
    reference_audio: EvidenceState


_CAPABILITIES = {
    "seedance": GatewayCapability(
        backend="seedance",
        text_to_video="client_declared",
        image_to_video="client_declared",
        first_last_frame="client_declared",
        reference_image="client_declared",
        reference_video="gateway_unverified",
        reference_audio="client_declared",
    ),
    "kling": GatewayCapability(
        backend="kling",
        text_to_video="client_declared",
        image_to_video="client_declared",
        first_last_frame="unsupported",
        reference_image="unsupported",
        reference_video="gateway_unverified",
        reference_audio="unsupported",
    ),
}


def gateway_capability(backend: str) -> GatewayCapability:
    try:
        return _CAPABILITIES[backend]
    except KeyError as exc:
        raise ValueError(f"unsupported JD backend: {backend}") from exc
