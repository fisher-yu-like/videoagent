from pipeline_v2.render_contract import build_physical_render_contract
from pipeline_v2.director import compile_director_plan


def world_plan() -> dict:
    return {
        "schema_version": "director-plan-1.0",
        "scene_plan": {
            "scene_id": "station_001",
            "environment_preset": "station",
            "duration_seconds": 2.0,
            "fps": 24,
            "entities": [{"id": "traveler", "kind": "character", "asset": "proxy_human"}],
        },
        "physical_state_plan": {"events": [], "parameters": {"motion_constraints": ["stay grounded"]}},
        "character_trajectory_plan": {"tracks": [{
            "target_id": "traveler",
            "points": [
                {"t": 0.0, "position": [0, 0, 0], "rotation": [0, 0, 0]},
                {"t": 1.0, "position": [1, 0, 0], "rotation": [0, 0, 0]},
            ],
        }]},
        "object_trajectory_plan": {"tracks": []},
        "camera_trajectory_plan": {"cameras": [{
            "id": "camera_1",
            "role": "master",
            "target": {"object_id": "traveler"},
            "lens_mm": 50,
            "roll_deg": 0,
            "points": [
                {"t": 0.0, "position": [0, -4, 2]},
                {"t": 1.0, "position": [1, -4, 2]},
            ],
        }]},
    }


def test_physical_render_contract_records_missing_video_causality_as_unresolved() -> None:
    world = compile_director_plan(world_plan())
    contract = build_physical_render_contract(world, world_plan())

    assert contract["source_world_state_hash"] == world.world_state_hash()
    assert contract["coordinate_conventions"]["camera_rotation"].startswith("degrees")
    assert "must_show" in contract["requirements"]
    assert "must_show" in contract["unresolved_fields"]
    assert contract["physical_state_plan"]["parameters"]["motion_constraints"] == ["stay grounded"]
