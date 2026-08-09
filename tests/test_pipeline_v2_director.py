import copy

import pytest

from pipeline_v2.director import (
    DirectorError,
    build_request_payload,
    compile_director_plan,
    normalize_director_plan,
)


def director_plan() -> dict:
    return {
        "schema_version": "director-plan-1.0",
        "scene_plan": {
            "scene_id": "station_001",
            "environment_preset": "station",
            "duration_seconds": 2.0,
            "fps": 24,
            "entities": [
                {"id": "person_A", "kind": "character", "asset": "proxy_human"},
                {"id": "bench_A", "kind": "object", "asset": "bench"},
            ],
        },
        "physical_state_plan": {
            "events": [
                {
                    "id": "contact_001",
                    "t": 0.5,
                    "type": "contact",
                    "participants": ["person_A", "bench_A"],
                }
            ]
        },
        "character_trajectory_plan": {
            "tracks": [
                {
                    "target_id": "person_A",
                    "points": [
                        {"t": 0.0, "position": [0, 0, 0], "rotation": [0, 0, 0]},
                        {"t": 1.0, "position": [2, 0, 0], "rotation": [0, 0, 0]},
                    ],
                }
            ]
        },
        "object_trajectory_plan": {
            "tracks": [
                {
                    "target_id": "bench_A",
                    "points": [
                        {"t": 0.0, "position": [3, 0, 0], "rotation": [0, 0, 0]},
                        {"t": 1.0, "position": [3, 0, 0], "rotation": [0, 0, 0]},
                    ],
                }
            ]
        },
        "camera_trajectory_plan": {
            "cameras": [
                {
                    "id": "cam_a",
                    "role": "master",
                    "target": {"object_id": "person_A"},
                    "lens_mm": 50,
                    "roll_deg": 0,
                    "points": [
                        {"t": 0.0, "position": [0, -6, 3]},
                        {"t": 1.0, "position": [2, -6, 3]},
                    ],
                }
            ]
        },
    }


def test_compile_director_plan_converts_normalized_time_to_shared_frames() -> None:
    world = compile_director_plan(director_plan())

    assert world.frame_count == 48
    assert world.frame_indices == (0, 47)
    assert world.to_dict()["physical_state_plan"]["events"][0]["frame"] == 24
    camera_point = world.to_dict()["camera_trajectory_plan"]["cameras"][0]["points"][0]
    assert camera_point["frame"] == 0
    assert camera_point["rotation"] != [0.0, 0.0, 0.0]


def test_compile_director_plan_rejects_unknown_camera_target() -> None:
    data = copy.deepcopy(director_plan())
    data["camera_trajectory_plan"]["cameras"][0]["target"] = {"object_id": "missing"}

    with pytest.raises(DirectorError, match="camera target"):
        compile_director_plan(data)


def test_compile_director_plan_rejects_non_monotonic_normalized_time() -> None:
    data = copy.deepcopy(director_plan())
    data["object_trajectory_plan"]["tracks"][0]["points"][1]["t"] = 0.0

    with pytest.raises(DirectorError, match="strictly increasing"):
        compile_director_plan(data)


def test_compile_director_plan_resamples_variable_keyframes_to_shared_frames() -> None:
    data = copy.deepcopy(director_plan())
    data["character_trajectory_plan"]["tracks"][0]["points"].insert(
        1, {"t": 0.35, "position": [0.7, 0, 0], "rotation": [0, 0, 0]}
    )
    world = compile_director_plan(data)

    assert world.frame_indices == (0, 16, 47)
    for group in ("character_trajectory_plan", "object_trajectory_plan"):
        assert tuple(item["frame"] for item in world.to_dict()[group]["tracks"][0]["points"]) == (0, 16, 47)
    assert tuple(item["frame"] for item in world.to_dict()["camera_trajectory_plan"]["cameras"][0]["points"]) == (0, 16, 47)


def test_request_payload_is_semantic_and_does_not_request_blender_code() -> None:
    payload = build_request_payload("A person waits at a station.", duration_seconds=2.0, fps=24, camera_count=3)

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["stream"] is False
    text = payload["messages"][0]["content"] + payload["messages"][1]["content"]
    assert "director-plan-1.0" in text
    assert "Blender code" not in text


def test_normalize_provider_plan_keeps_original_semantics_inside_strict_contract() -> None:
    provider = {
        "schema_version": "director-plan-1.0",
        "scene_plan": {"description": "station", "duration_seconds": 2.0, "fps": 24},
        "physical_state_plan": {
            "gravity": [0, -9.8, 0],
            "wind": [0, 0, 0],
            "collision_events": [],
        },
        "character_trajectory_plan": {
            "traveler": {"trajectory": [
                {"t": 0.0, "position": [0, 0, 0], "rotation": [0, 0, 0]},
                {"t": 1.0, "position": [2, 0, 0], "rotation": [0, 0, 0]},
            ]}
        },
        "object_trajectory_plan": {
            "luggage": {"trajectory": [
                {"t": 0.0, "position": [1, 0, 0], "rotation": [0, 0, 0]},
                {"t": 1.0, "position": [1, 0, 0], "rotation": [0, 0, 0]},
            ]}
        },
        "camera_trajectory_plan": {
            "camera_1": {
                "target": "character",
                "target_character": "traveler",
                "trajectory": [
                    {"t": 0.0, "position": [0, -4, 2]},
                    {"t": 1.0, "position": [2, -4, 2]},
                ],
                "type": "static",
            }
        },
    }

    normalized = normalize_director_plan(
        provider,
        fallback_scene_id="station_001",
        fallback_environment_preset="station",
    )
    world = compile_director_plan(normalized)

    assert normalized["scene_plan"]["entities"][0]["id"] == "traveler"
    assert world.to_dict()["physical_state_plan"]["parameters"]["gravity"] == [0, -9.8, 0]
    assert world.camera_count == 1


def test_normalize_provider_plan_preserves_entity_camera_targets_and_event_intervals() -> None:
    provider = {
        "schema_version": "director-plan-1.0",
        "scene_plan": {"duration_seconds": 2.0, "fps": 24},
        "physical_state_plan": {
            "events": [
                {"event": "traveler moves beside luggage.", "t_start": 0.25, "t_end": 0.5}
            ]
        },
        "character_trajectory_plan": {
            "traveler": {"trajectory": [
                {"t": 0.0, "position": [0, 0, 0], "rotation": [0, 0, 0]},
                {"t": 1.0, "position": [1, 0, 0], "rotation": [0, 0, 0]},
            ]}
        },
        "object_trajectory_plan": {
            "luggage": {"trajectory": [
                {"t": 0.0, "position": [0, 1, 0], "rotation": [0, 0, 0]},
                {"t": 1.0, "position": [1, 1, 0], "rotation": [0, 0, 0]},
            ]}
        },
        "camera_trajectory_plan": {
            "camera_1": {
                "target_entity": "traveler",
                "trajectory": [
                    {"t": 0.0, "position": [0, -4, 2]},
                    {"t": 1.0, "position": [1, -4, 2]},
                ]
            }
        },
    }
    normalized = normalize_director_plan(provider, fallback_scene_id="station_001", fallback_environment_preset="station")
    world = compile_director_plan(normalized)

    assert normalized["camera_trajectory_plan"]["cameras"][0]["target"] == {"object_id": "traveler"}
    assert normalized["physical_state_plan"]["events"][0]["t"] == 0.25
    assert normalized["physical_state_plan"]["events"][0]["participants"] == ["traveler", "luggage"]
    assert world.to_dict()["physical_state_plan"]["parameters"]["provider_events"][0]["t_end"] == 0.5
