import copy

import pytest

from pipeline_v2.state import WorldState, WorldStateError


def valid_world() -> dict:
    return {
        "schema_version": "world-state-1.0",
        "scene_plan": {
            "scene_id": "station_001",
            "environment_preset": "station",
            "duration_seconds": 2.0,
            "fps": 24,
            "frame_count": 48,
            "entities": [
                {"id": "person_A", "kind": "character", "asset": "proxy_human"},
                {"id": "bench_A", "kind": "object", "asset": "bench"},
            ],
        },
        "physical_state_plan": {
            "events": [
                {
                    "id": "sit_001",
                    "frame": 30,
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
                        {"frame": 0, "position": [0, 0, 0], "rotation": [0, 0, 0]},
                        {"frame": 47, "position": [2, 0, 0], "rotation": [0, 0, 0]},
                    ],
                }
            ]
        },
        "object_trajectory_plan": {
            "tracks": [
                {
                    "target_id": "bench_A",
                    "points": [
                        {"frame": 0, "position": [3, 0, 0], "rotation": [0, 0, 0]},
                        {"frame": 47, "position": [3, 0, 0], "rotation": [0, 0, 0]},
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
                        {"frame": 0, "position": [0, -6, 3], "rotation": [0, 0, 0]},
                        {"frame": 47, "position": [2, -6, 3], "rotation": [0, 0, 0]},
                    ],
                },
                {
                    "id": "cam_b",
                    "role": "follow",
                    "target": {"object_id": "person_A"},
                    "lens_mm": 70,
                    "roll_deg": 0,
                    "points": [
                        {"frame": 0, "position": [0, 6, 2], "rotation": [0, 0, 0]},
                        {"frame": 47, "position": [2, 6, 2], "rotation": [0, 0, 0]},
                    ],
                },
                {
                    "id": "cam_c",
                    "role": "reverse",
                    "target": {"object_id": "person_A"},
                    "lens_mm": 85,
                    "roll_deg": 0,
                    "points": [
                        {"frame": 0, "position": [-4, -2, 2], "rotation": [0, 0, 0]},
                        {"frame": 47, "position": [-2, -2, 2], "rotation": [0, 0, 0]},
                    ],
                },
            ]
        },
    }


def test_world_state_validates_shared_ids_and_canonical_hash() -> None:
    world = WorldState.from_dict(valid_world())

    assert world.scene_id == "station_001"
    assert world.camera_count == 3
    assert world.frame_indices == (0, 47)
    assert len(world.world_state_hash()) == 64
    assert len(world.object_state_hash()) == 64

    reordered = copy.deepcopy(valid_world())
    reordered["scene_plan"] = dict(reversed(list(reordered["scene_plan"].items())))
    assert WorldState.from_dict(reordered).world_state_hash() == world.world_state_hash()


def test_camera_only_edit_preserves_object_state_hash() -> None:
    world = WorldState.from_dict(valid_world())
    changed = copy.deepcopy(valid_world())
    changed["camera_trajectory_plan"]["cameras"][0]["lens_mm"] = 35

    edited = WorldState.from_dict(changed)
    assert edited.object_state_hash() == world.object_state_hash()
    assert edited.world_state_hash() != world.world_state_hash()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda data: data["character_trajectory_plan"]["tracks"][0].update({"target_id": "missing"}), "unknown target"),
        (lambda data: data["camera_trajectory_plan"]["cameras"][0].update({"target": {}}), "camera target"),
        (lambda data: data["object_trajectory_plan"]["tracks"][0]["points"].__setitem__(1, {"frame": 46, "position": [3, 0, 0], "rotation": [0, 0, 0]}), "frame indices"),
    ],
)
def test_world_state_rejects_inconsistent_references(mutate, message) -> None:
    data = valid_world()
    mutate(data)

    with pytest.raises(WorldStateError, match=message):
        WorldState.from_dict(data)


def test_world_state_rejects_more_than_eight_cameras() -> None:
    data = valid_world()
    template = copy.deepcopy(data["camera_trajectory_plan"]["cameras"][0])
    data["camera_trajectory_plan"]["cameras"] = [
        dict(template, id=f"cam_{index}") for index in range(9)
    ]

    with pytest.raises(WorldStateError, match="at most 8 cameras"):
        WorldState.from_dict(data)


def test_world_state_rejects_duration_frame_count_mismatch() -> None:
    data = valid_world()
    data["scene_plan"]["frame_count"] = 49

    with pytest.raises(WorldStateError, match="duration_seconds.*fps.*frame_count"):
        WorldState.from_dict(data)


def test_world_state_preserves_optional_physical_parameters() -> None:
    data = valid_world()
    data["physical_state_plan"]["parameters"] = {
        "gravity": [0, -9.8, 0],
        "wind": [0, 0, 0],
    }

    world = WorldState.from_dict(data)

    assert world.to_dict()["physical_state_plan"]["parameters"]["gravity"] == [0, -9.8, 0]


def test_world_state_public_document_round_trips_without_internal_track_fields() -> None:
    world = WorldState.from_dict(valid_world())

    exported = world.to_dict()
    assert all("kind" not in track for track in exported["character_trajectory_plan"]["tracks"])
    assert all("kind" not in track for track in exported["object_trajectory_plan"]["tracks"])
    restored = WorldState.from_dict(exported)
    assert restored.world_state_hash() == world.world_state_hash()
