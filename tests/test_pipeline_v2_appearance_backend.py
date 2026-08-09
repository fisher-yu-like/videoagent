import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

import pytest

from pipeline_v2.appearance_prompt import AppearancePromptError, compile_appearance_prompt
from pipeline_v2.backend_adapter import BackendAdapterError, prepare_backend_adapter
from pipeline_v2.state import WorldState


@pytest.fixture
def local_tmp_path():
    path = Path(tempfile.mkdtemp(prefix="pipeline-v2-appearance-", dir=str(Path.cwd())))
    try:
        yield path
    finally:
        shutil.rmtree(str(path), ignore_errors=True)


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
                {"id": "traveler", "kind": "character", "asset": "proxy_human"},
                {"id": "friend", "kind": "character", "asset": "proxy_human"},
                {"id": "luggage", "kind": "object", "asset": "proxy_object"},
            ],
        },
        "physical_state_plan": {"events": []},
        "character_trajectory_plan": {"tracks": [
            {"target_id": "traveler", "points": [
                {"frame": 0, "position": [0, 0, 0], "rotation": [0, 0, 0]},
                {"frame": 47, "position": [1, 0, 0], "rotation": [0, 0, 0]},
            ]},
            {"target_id": "friend", "points": [
                {"frame": 0, "position": [2, 0, 0], "rotation": [0, 0, 0]},
                {"frame": 47, "position": [2, 0, 0], "rotation": [0, 0, 0]},
            ]},
        ]},
        "object_trajectory_plan": {"tracks": [{
            "target_id": "luggage", "points": [
                {"frame": 0, "position": [-1, 0, 0], "rotation": [0, 0, 0]},
                {"frame": 47, "position": [0, 0, 0], "rotation": [0, 0, 0]},
            ],
        }]},
        "camera_trajectory_plan": {"cameras": [
            {
                "id": "camera_1", "role": "master", "target": {"object_id": "traveler"},
                "lens_mm": 35, "roll_deg": 0,
                "points": [
                    {"frame": 0, "position": [0, -5, 2], "rotation": [0, 0, 0]},
                    {"frame": 47, "position": [1, -5, 2], "rotation": [0, 0, 0]},
                ],
            },
            {
                "id": "camera_2", "role": "reverse", "target": {"object_id": "friend"},
                "lens_mm": 40, "roll_deg": 0,
                "points": [
                    {"frame": 0, "position": [2, -5, 2], "rotation": [0, 0, 0]},
                    {"frame": 47, "position": [2, -5, 2], "rotation": [0, 0, 0]},
                ],
            },
            {
                "id": "camera_3", "role": "detail", "target": {"object_id": "luggage"},
                "lens_mm": 50, "roll_deg": 0,
                "points": [
                    {"frame": 0, "position": [-1, -4, 1], "rotation": [0, 0, 0]},
                    {"frame": 47, "position": [0, -4, 1], "rotation": [0, 0, 0]},
                ],
            },
        ]},
    }


def five_second_world() -> dict:
    data = valid_world()
    data["scene_plan"]["duration_seconds"] = 5.0
    data["scene_plan"]["frame_count"] = 120
    for group_name in ("character_trajectory_plan", "object_trajectory_plan", "camera_trajectory_plan"):
        groups = data[group_name].get("tracks") or data[group_name].get("cameras")
        for track in groups:
            for point in track["points"]:
                if point["frame"] == 47:
                    point["frame"] = 119
    return data


def profile() -> dict:
    return {
        "schema_version": "appearance-profile-1.0",
        "scene_id": "station_001",
        "subjects": [
            {"entity_id": "traveler", "description": "adult traveler in a dark wool coat, natural skin and cloth"},
            {"entity_id": "friend", "description": "adult friend in a muted blue jacket, natural skin and cloth"},
            {"entity_id": "luggage", "description": "realistic small rolling suitcase with fabric texture and metal handle"},
        ],
        "environment": "real railway station platform with grounded architecture and natural depth",
        "lighting": "soft overcast daylight with coherent contact shadows and stable exposure",
        "quality": "photoreal live-action appearance, natural anatomy, realistic skin, cloth, and materials",
        "must_avoid": ["clay or low-poly geometry", "labels, guide lines, and visible CG overlays"],
    }


def manifest_for(world: WorldState, root: Path) -> dict:
    videos = []
    for camera_id in ("camera_1", "camera_2", "camera_3"):
        path = root / f"{camera_id}.mp4"
        path.write_bytes((camera_id + "-real-source-placeholder").encode("utf-8"))
        videos.append({
            "camera_id": camera_id,
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "frame_count": world.frame_count,
            "fps": world.fps,
            "resolution": [640, 360],
        })
    manifest = {
        "schema_version": "pipeline-v2-render-manifest-1.0",
        "world_state_hash": world.world_state_hash(),
        "frame_count": world.frame_count,
        "fps": world.fps,
        "resolution": [640, 360],
        "videos": videos,
    }
    return manifest


def test_appearance_prompt_is_hash_bound_and_does_not_plan_motion(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(valid_world())
    manifest = manifest_for(world, tmp_path)

    bundle = compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=profile())

    assert bundle["schema_version"] == "appearance-only-prompt-1.0"
    assert bundle["source_world_state_hash"] == world.world_state_hash()
    assert bundle["prompt_sha256"] == hashlib.sha256(bundle["prompt"].encode()).hexdigest()
    assert "camera moves" not in bundle["prompt"].lower()
    assert "trajectory plan" not in bundle["prompt"].lower()
    assert "approved shared-world clay proxy as the sole source of blocking, timing, occlusion" in bundle["prompt"]
    assert "camera movement" in bundle["prompt"]


def test_appearance_prompt_rejects_motion_instruction_and_manifest_mismatch(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(valid_world())
    manifest = manifest_for(world, tmp_path)
    bad_profile = profile()
    bad_profile["subjects"] = copy.deepcopy(bad_profile["subjects"])
    bad_profile["subjects"][0]["description"] += "; camera moves left"
    with pytest.raises(AppearancePromptError, match="appearance-only"):
        compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=bad_profile)

    mismatched = copy.deepcopy(manifest)
    mismatched["world_state_hash"] = "0" * 64
    with pytest.raises(AppearancePromptError, match="WorldState hash"):
        compile_appearance_prompt(world=world, proxy_manifest=mismatched, profile=profile())


def test_backend_adapter_blocks_vace_proxy_submission(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(valid_world())
    manifest = manifest_for(world, tmp_path)
    appearance = compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=profile())

    bundle = prepare_backend_adapter(
        backend="vace",
        world=world,
        proxy_manifest=manifest,
        proxy_root=tmp_path,
        appearance_prompt=appearance,
    )

    assert bundle["status"] == "blocked"
    assert bundle["conditioning_mode"] == "source_video_edit"
    assert bundle["network_called"] is False
    assert bundle["automatic_retry_limit"] == 0
    assert bundle["jobs"] == []
    assert "does not currently accept Proxy" in " ".join(bundle["blockers"])


def test_backend_adapter_does_not_fallback_seedance_reference_to_prompt_only(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(valid_world())
    manifest = manifest_for(world, tmp_path)
    appearance = compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=profile())

    bundle = prepare_backend_adapter(
        backend="seedance_reference",
        world=world,
        proxy_manifest=manifest,
        proxy_root=tmp_path,
        appearance_prompt=appearance,
    )

    assert bundle["status"] == "blocked"
    assert "public HTTPS proxy URL" in " ".join(bundle["blockers"])
    assert bundle["fallbacks"] == {"prompt_only": False}


def test_seedance_and_kling_reference_candidates_are_the_only_proxy_backends(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(five_second_world())
    manifest = manifest_for(world, tmp_path)
    appearance = compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=profile())
    urls = {
        camera_id: f"https://cdn.video-example.net/{camera_id}.mp4"
        for camera_id in ("camera_1", "camera_2", "camera_3")
    }

    seedance = prepare_backend_adapter(
        backend="seedance_reference", model="Doubao-Seedance-2.5", world=world,
        proxy_manifest=manifest, proxy_root=tmp_path, appearance_prompt=appearance,
        proxy_urls=urls,
    )
    kling = prepare_backend_adapter(
        backend="kling_reference", model="Kling-V3-omni", world=world,
        proxy_manifest=manifest, proxy_root=tmp_path, appearance_prompt=appearance,
        proxy_urls=urls,
    )
    for bundle in (seedance, kling):
        assert bundle["status"] == "ready_for_single_probe"
        assert bundle["conditioning_mode"] == "reference_video"
        assert len(bundle["jobs"]) == 3
        assert bundle["gateway_capability"] == "unverified"


def test_backend_adapter_rejects_unknown_backend(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(valid_world())
    manifest = manifest_for(world, tmp_path)
    appearance = compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=profile())
    with pytest.raises(BackendAdapterError, match="unsupported backend"):
        prepare_backend_adapter(
            backend="unknown",
            world=world,
            proxy_manifest=manifest,
            proxy_root=tmp_path,
            appearance_prompt=appearance,
        )


def test_backend_adapter_rejects_appearance_prompt_bound_to_another_proxy(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    world = WorldState.from_dict(valid_world())
    manifest = manifest_for(world, tmp_path)
    appearance = compile_appearance_prompt(world=world, proxy_manifest=manifest, profile=profile())
    appearance["source_proxy_manifest_hash"] = "0" * 64
    with pytest.raises(BackendAdapterError, match="Proxy manifest hash"):
        prepare_backend_adapter(
            backend="vace",
            world=world,
            proxy_manifest=manifest,
            proxy_root=tmp_path,
            appearance_prompt=appearance,
        )
