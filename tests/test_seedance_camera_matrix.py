from pathlib import Path


def test_camera_matrix_has_one_explicit_job_per_camera():
    from scripts.run_seedance_camera_matrix import CAMERA_SPECS, build_camera_prompt

    assert len(CAMERA_SPECS) == 8
    assert len({spec["camera_id"] for spec in CAMERA_SPECS}) == 8
    for spec in CAMERA_SPECS:
        prompt = build_camera_prompt(spec["camera_id"], spec["role"])
        assert spec["camera_id"] in prompt
        assert "only" in prompt.lower()
        assert "one continuous five-second" in prompt.lower()


def test_identity_anchor_prompt_freezes_people_and_removes_proxy_environment():
    from scripts.run_seedance_camera_matrix import build_camera_prompt

    prompt = build_camera_prompt("master_front_tracking", "master front tracking", identity_anchor=True)
    assert "canonical identity anchor" in prompt.lower()
    assert "navy knee-length overcoat" in prompt.lower()
    assert "white low-poly posts" in prompt.lower()


def test_text_identity_lock_does_not_require_a_second_video():
    from scripts.run_seedance_camera_matrix import build_camera_prompt

    prompt = build_camera_prompt("master_front_tracking", "master front tracking", identity_lock=True)
    assert "fixed identity specification" in prompt.lower()
    assert "canonical identity anchor is supplied first" not in prompt.lower()


def test_camera_matrix_requires_existing_proxy_and_never_reuses_output_root():
    from scripts.run_seedance_camera_matrix import validate_matrix_inputs

    proxy_root = Path("proxy-root")
    output_root = Path("output-root")
    assert validate_matrix_inputs(proxy_root, output_root, existing_names=set()) is None


def test_camera_matrix_can_run_a_named_smoke_subset():
    from scripts.run_seedance_camera_matrix import select_camera_specs

    selected = select_camera_specs(["master_front_tracking"])
    assert [item["camera_id"] for item in selected] == ["master_front_tracking"]


def test_reverse_camera_revision_clears_the_fixed_wall():
    from scripts.run_reverse_camera_proxy_revision import reverse_camera_positions

    positions = reverse_camera_positions()
    assert len(positions) == 2
    assert all(position[2] > 4.4 for position in positions)
