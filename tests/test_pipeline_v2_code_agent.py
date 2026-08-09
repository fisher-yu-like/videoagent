import json
from pathlib import Path
import shutil

import pytest

from pipeline_v2.code_agent import (
    CodeAgentError,
    build_code_agent_payload,
    extract_code_agent_document,
    validate_generated_script,
    request_code_agent,
)


def world_document() -> dict:
    return {
        "schema_version": "world-state-1.0",
        "scene_plan": {
            "scene_id": "station_001",
            "environment_preset": "station",
            "duration_seconds": 2.0,
            "fps": 24,
            "frame_count": 48,
            "entities": [
                {"id": "traveler", "kind": "character", "asset": "proxy_human"},
                {"id": "luggage", "kind": "object", "asset": "proxy_object"},
            ],
        },
        "physical_state_plan": {"events": [], "parameters": {}},
        "character_trajectory_plan": {"tracks": []},
        "object_trajectory_plan": {"tracks": []},
        "camera_trajectory_plan": {
            "cameras": [
                {
                    "id": "camera_1",
                    "role": "master",
                    "target": {"point": [0, 0, 0]},
                    "lens_mm": 50,
                    "roll_deg": 0,
                    "points": [
                        {"frame": 0, "position": [0, -4, 2], "rotation": [0, 0, 0]},
                        {"frame": 47, "position": [1, -4, 2], "rotation": [0, 0, 0]},
                    ],
                }
            ]
        },
    }


def code_document(script: str) -> dict:
    return {
        "schema_version": "blender-code-agent-1.0",
        "script": script,
        "scene_description": "A neutral proxy scene.",
        "asset_manifest": [],
        "camera_manifest": ["camera_1"],
        "expected_outputs": ["render_manifest.json", "state_log.json", "camera_log.json"],
        "assumptions": [],
        "known_limitations": [],
    }


SAFE_SCRIPT = """
import argparse
import json
from pathlib import Path
import bpy
from mathutils import Vector

def write_state_log(output_dir, state):
    (output_dir / 'state_log.json').write_text(json.dumps(state), encoding='utf-8')

def write_camera_log(output_dir, state):
    (output_dir / 'camera_log.json').write_text(json.dumps(state), encoding='utf-8')

def write_applied_state_log(output_dir, state):
    (output_dir / 'applied_state_log.json').write_text(json.dumps(state), encoding='utf-8')

def render_outputs(output_dir):
    bpy.ops.render.render(animation=True)
    (output_dir / 'render_manifest.json').write_text('{}', encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--world-state')
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    write_state_log(output_dir, {})
    write_camera_log(output_dir, {})
    render_outputs(output_dir)

if __name__ == '__main__':
    main()
"""


def test_payload_contains_world_contract_without_legacy_renderer_imports() -> None:
    payload = build_code_agent_payload(
        "A traveler meets a friend at a station.",
        world=world_document(),
        physical_contract={"schema_version": "physical-render-contract-1.0"},
        provider="openai",
        model="gpt-5.6-luna",
    )

    assert payload["model"] == "gpt-5.6-luna"
    assert payload["response_format"] == {"type": "json_object"}
    assert "do not import videoactagent" in payload["messages"][0]["content"]
    assert "shared world" in payload["messages"][0]["content"]
    assert "station_001" in payload["messages"][1]["content"]
    assert "procedural_skeleton_v1" in payload["messages"][0]["content"]
    assert "IK target" in payload["messages"][0]["content"]
    assert "thinking" not in payload


def test_generated_script_static_contract_accepts_safe_script() -> None:
    safe = SAFE_SCRIPT + "\n# world_state_hash videos PIPELINE_V2_BLENDER_OK --render-style --resolution media_type applied_state_log.json pipeline-v2-render-manifest-1.0 to_track_quat bpy.data.worlds.new sys.argv bpy.data.lights.new authored applied\n"
    result = validate_generated_script(code_document(safe))
    assert result["script_sha256"]
    assert result["required_markers"] == {
        "world_state_arg": True,
        "output_dir_arg": True,
        "render_manifest": True,
        "state_log": True,
        "camera_log": True,
        "applied_state_log": True,
        "render_call": True,
        "camera_look_at": True,
        "world_datablock": True,
        "manifest_schema": True,
        "manifest_world_hash": True,
        "manifest_video_list": True,
        "success_marker": True,
        "render_style_arg": True,
        "resolution_arg": True,
        "media_type_video": True,
        "argv_separator": True,
        "visible_lighting": True,
        "camera_authored_log": True,
        "camera_applied_log": True,
    }


def test_generated_script_rejects_legacy_project_import() -> None:
    document = code_document(SAFE_SCRIPT.replace("import bpy", "import videoactagent\nimport bpy"))
    with pytest.raises(CodeAgentError, match="project import"):
        validate_generated_script(document)


def test_generated_script_rejects_process_or_network_imports() -> None:
    document = code_document(SAFE_SCRIPT.replace("import bpy", "import subprocess\nimport bpy"))
    with pytest.raises(CodeAgentError, match="import is not allowed"):
        validate_generated_script(document)


def test_generated_script_rejects_obsolete_blender_action_api() -> None:
    document = code_document(SAFE_SCRIPT.replace("bpy.ops.render.render", "obj.animation_data.action.fcurves"))
    with pytest.raises(CodeAgentError, match="Action.fcurves"):
        validate_generated_script(document)


def test_generated_script_rejects_unavailable_eevee_engine_enum() -> None:
    document = code_document(SAFE_SCRIPT.replace("bpy.ops.render.render", "scene.render.engine = 'BLENDER_EEVEE_NEXT'"))
    with pytest.raises(CodeAgentError, match="BLENDER_EEVEE_NEXT"):
        validate_generated_script(document)


def test_generated_script_requires_video_media_type_before_ffmpeg_format() -> None:
    script = SAFE_SCRIPT + "\n# world_state_hash videos PIPELINE_V2_BLENDER_OK --render-style --resolution media_type applied_state_log.json pipeline-v2-render-manifest-1.0 to_track_quat bpy.data.worlds.new sys.argv bpy.data.lights.new authored applied\n"
    script += "scene.render.image_settings.file_format = 'FFMPEG'\nscene.render.image_settings.media_type = 'VIDEO'\n"
    with pytest.raises(CodeAgentError, match="media_type=VIDEO before"):
        validate_generated_script(code_document(script))


def test_extract_code_agent_document_requires_normal_json_response() -> None:
    response = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps(code_document(SAFE_SCRIPT))},
        }]
    }
    assert extract_code_agent_document(response)["schema_version"] == "blender-code-agent-1.0"


def test_request_code_agent_preserves_script_bytes_for_hash_binding() -> None:
    script = SAFE_SCRIPT + "\n# world_state_hash videos PIPELINE_V2_BLENDER_OK --render-style --resolution media_type applied_state_log.json pipeline-v2-render-manifest-1.0 to_track_quat bpy.data.worlds.new sys.argv bpy.data.lights.new authored applied\n"
    response = {"id": "resp-test", "model": "gpt-5.6-luna", "usage": {}, "choices": [{
        "finish_reason": "stop",
        "message": {"content": json.dumps(code_document(script))},
    }]}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(response).encode("utf-8")

    def transport(request, timeout):
        return FakeResponse()

    output_dir = Path(__file__).resolve().parents[1] / "pipeline_v2/runs/_test_code_agent_bytes"
    shutil.rmtree(output_dir, ignore_errors=True)
    try:
        request_code_agent(
            story_prompt="station",
            world=world_document(),
            physical_contract={"schema_version": "physical-render-contract-1.0"},
            output_dir=output_dir,
            environ={"OPENAI_API_KEY": "test", "OPENAI_BASE_URL": "https://example.test/v1"},
            transport=transport,
        )
        assert (output_dir / "generated_blender.py").read_bytes() == script.encode("utf-8")
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
