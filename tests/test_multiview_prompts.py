from __future__ import annotations


def test_station_prompt_variants_are_four_distinct_auditable_prompts() -> None:
    from videoactagent.multiview_prompts import station_multiview_prompts

    prompts = station_multiview_prompts("Preserve the approved shared-world character and station layout.")

    assert len(prompts) == 4
    assert len(set(prompts)) == 4
    for prompt in prompts:
        assert "[Video1]" in prompt
        assert "[Video2]" in prompt
        assert "[Video3]" in prompt
        assert "shared Blender world" in prompt
