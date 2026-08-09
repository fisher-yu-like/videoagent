"""Versioned pipeline boundary for the new VideoActAgent flow."""

from .state import SCHEMA_VERSION, WorldState, WorldStateError
from .proxy_verifier import FEEDBACK_CATEGORIES, ProxyVerifierError, verify_proxy
from .revision_manager import RevisionError, allocate_revision, build_handoff, record_approval, record_feedback, snapshot_bundle
from .vlm_feedback import VLMFeedbackError, build_vlm_payload, extract_vlm_feedback, request_vlm_feedback
from .appearance_prompt import (
    APPEARANCE_COMPILER_VERSION,
    APPEARANCE_PROMPT_SCHEMA_VERSION,
    APPEARANCE_PROFILE_SCHEMA_VERSION,
    AppearancePromptError,
    compile_appearance_prompt,
)
from .backend_adapter import (
    BACKEND_ADAPTER_SCHEMA_VERSION,
    BackendAdapterError,
    prepare_backend_adapter,
    write_backend_adapter_bundle,
)

__all__ = [
    "SCHEMA_VERSION", "WorldState", "WorldStateError",
    "FEEDBACK_CATEGORIES", "ProxyVerifierError", "verify_proxy",
    "RevisionError", "allocate_revision", "build_handoff", "record_approval", "record_feedback", "snapshot_bundle",
    "VLMFeedbackError", "build_vlm_payload", "extract_vlm_feedback", "request_vlm_feedback",
    "APPEARANCE_COMPILER_VERSION", "APPEARANCE_PROMPT_SCHEMA_VERSION", "APPEARANCE_PROFILE_SCHEMA_VERSION",
    "AppearancePromptError", "compile_appearance_prompt",
    "BACKEND_ADAPTER_SCHEMA_VERSION", "BackendAdapterError", "prepare_backend_adapter", "write_backend_adapter_bundle",
]
