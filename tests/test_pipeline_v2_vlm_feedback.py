import json
from pathlib import Path
import shutil

import pytest

from pipeline_v2.vlm_feedback import (
    VLMFeedbackError,
    build_vlm_payload,
    extract_vlm_feedback,
    request_vlm_feedback,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_FRAME = ROOT / "pipeline_v2/runs/module3_station_20260808_019/revision_000/camera_1_02.bmp"


def report() -> dict:
    return {
        "schema_version": "proxy-verifier-1.0",
        "verdict": "pending_review",
        "world_state_hash": "b28c06a4c3067dd863a7a69c46b0bccade93c5d66950548864b405852625856f",
        "checks": [{"check_id": "camera.camera_1", "status": "passed"}],
    }


def vlm_document(verdict="revision_requested") -> dict:
    return {
        "schema_version": "proxy-vlm-feedback-1.0",
        "verdict": verdict,
        "feedback": [] if verdict == "approve" else [{
            "category": "camera_trajectory",
            "message": "camera_1 should keep both characters in frame",
            "evidence_frames": [60],
        }],
        "summary": "visual review",
    }


def test_build_vlm_payload_uses_real_frame_data_and_feedback_schema() -> None:
    payload = build_vlm_payload(report(), [REAL_FRAME], story_context="station", model="gpt-5.6-luna")
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][1]["content"][0]["type"] == "text"
    assert "non-negative integer frame" in payload["messages"][1]["content"][0]["text"]
    assert payload["messages"][1]["content"][1]["type"] == "image_url"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/bmp;base64,")
    assert payload["messages"][1]["content"][2]["type"] == "text"
    assert "camera_1_02" in payload["messages"][1]["content"][2]["text"]


def test_extract_vlm_feedback_rejects_invalid_category_and_accepts_approval() -> None:
    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(vlm_document("approve"))}}]}
    result = extract_vlm_feedback(response)
    assert result["verdict"] == "approve"
    assert result["feedback"] == []

    invalid = vlm_document()
    invalid["feedback"][0]["category"] = "scene_unknown"
    bad_response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(invalid)}}]}
    with pytest.raises(VLMFeedbackError, match="category"):
        extract_vlm_feedback(bad_response)


def test_request_vlm_feedback_persists_one_call_and_redacted_evidence() -> None:
    document = vlm_document()
    response = {"id": "vlm-test", "model": "gpt-5.6-luna", "usage": {"total_tokens": 12}, "choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps(document)},
    }]}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(response).encode("utf-8")

    root = ROOT / "pipeline_v2/runs/_test_vlm_feedback"
    shutil.rmtree(root, ignore_errors=True)
    try:
        result = request_vlm_feedback(
            proxy_report=report(),
            frame_paths=[REAL_FRAME],
            output_dir=root,
            story_context="station",
            environ={"OPENAI_API_KEY": "test", "OPENAI_BASE_URL": "https://example.test/v1"},
            transport=lambda request, timeout: FakeResponse(),
        )
        assert result["verdict"] == "revision_requested"
        evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
        assert evidence["api_call_count"] == 1
        assert evidence["status"] == "succeeded"
        assert "Bearer test" not in (root / "request.json").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
