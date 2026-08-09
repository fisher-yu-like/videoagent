from pathlib import Path

import pytest

from pipeline_v2.blender_sandbox import SandboxError, build_blender_command


def test_build_blender_command_is_self_contained_and_uses_factory_startup() -> None:
    command = build_blender_command(
        Path(r"D:\blender\blender.exe"),
        Path("generated_blender.py"),
        Path("input_world_state.json"),
        Path("output"),
    )

    assert "--background" in command
    assert "--factory-startup" in command
    assert "--world-state" in command
    assert "--output-dir" in command
    assert "--render-style" in command
    assert "--resolution" in command


def test_build_blender_command_rejects_invalid_resolution() -> None:
    with pytest.raises(SandboxError, match="resolution"):
        build_blender_command("blender.exe", "script.py", "state.json", "out", resolution=(0, 360))


def test_build_blender_command_passes_optional_motion_bvh_inputs():
    command = build_blender_command(
        Path(r"D:\blender\blender.exe"),
        Path("generated_blender.py"),
        Path("input_world_state.json"),
        Path("output"),
        motion_bvh=Path("left_side_step.bvh"),
        motion_bvh_alt=Path("right_side_step.bvh"),
    )
    assert command[-4:] == ["--motion-bvh", "left_side_step.bvh", "--motion-bvh-alt", "right_side_step.bvh"]
