import json
import math
import tempfile
from pathlib import Path


def test_three_scenes_have_explicit_tracks_and_granular_actions():
    from videoactagent.complex_scene_prompts_v2 import SCENES

    assert len(SCENES) == 3
    for scene in SCENES:
        assert len(scene["entities"]) >= 5
        assert len(scene["cameras"]) == 4
        assert len(scene["action_phases"]) >= 4
        assert scene["gesture_tracks"]
        for track in scene["tracks"]:
            points = track["points"]
            assert len(points) >= 4
            frames = [point["frame"] for point in points]
            assert frames == sorted(set(frames))
            assert frames[-1] < 120


def test_seedance_prompt_forbids_proxy_geometry_and_preserves_action_order():
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    prompt = scene_spec("plaza_dance_circle")["appearance_prompt"]
    assert "one continuous five-second" in prompt.lower()
    assert "clay" in prompt.lower()
    assert "wave" in prompt.lower() and "dance" in prompt.lower()
    assert "no cuts" in prompt.lower()


def test_suite_budget_is_three_real_seedance_submissions():
    from scripts.run_complex_scene_suite import SEEDANCE_SUBMISSION_BUDGET

    assert SEEDANCE_SUBMISSION_BUDGET == 3


def test_compile_scene_produces_pipeline_worldstate_with_four_cameras():
    from scripts.run_complex_scene_suite import compile_scene_world
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    world = compile_scene_world(scene_spec("plaza_dance_circle"))
    assert world.camera_count == 4
    assert world.frame_count == 120
    ids = {entity["id"] for entity in world.to_dict()["scene_plan"]["entities"]}
    assert {"person_a", "person_b"}.issubset(ids)


def test_output_root_is_isolated():
    import tempfile
    from scripts.run_complex_scene_suite import output_dir_for

    with tempfile.TemporaryDirectory(dir=".") as temp_root:
        first = output_dir_for(temp_root, "plaza_dance_circle", "20260809_000001")
        second = output_dir_for(temp_root, "plaza_dance_circle", "20260809_000002")
        assert first != second
        assert str(first).endswith("plaza_dance_circle_20260809_000001")


def test_seedance_stage_selects_one_camera_per_scene():
    from scripts.run_complex_scene_suite import select_seedance_camera
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    assert select_seedance_camera(scene_spec("plaza_dance_circle"))["camera_id"] == "master"


def test_seedance_stage_submits_every_manifest_camera_independently():
    from scripts.run_complex_scene_suite import select_seedance_cameras
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    cameras = select_seedance_cameras(scene_spec("plaza_dance_circle"))
    assert [camera["camera_id"] for camera in cameras] == ["master", "lateral", "reverse", "elevated"]


def test_badminton_net_proxy_keeps_players_visible():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert 'eid + "__top"' in script
    assert 'eid + "__post_l"' in script


def test_director_plan_adapter_overrides_world_schema_version():
    from scripts.run_complex_scene_suite import director_plan_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec
    from scripts.run_complex_scene_suite import compile_scene_world

    plan = director_plan_for(compile_scene_world(scene_spec("plaza_dance_circle")))
    assert plan["schema_version"] == "director-plan-1.0"


def test_canonical_registry_preserves_entities_without_changing_worldstate():
    from scripts.run_complex_scene_suite import asset_registry_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    spec = scene_spec("plaza_dance_circle")
    registry = asset_registry_for(spec, "canonical")
    assert registry["shared_world"] is True
    person = next(item for item in registry["assets"] if item["asset_id"] == "person_a")
    assert person["source_kind"] == "canonical_procedural_v3"
    assert "upper_arm.L" in person["parts"]
    assert len(person["asset_sha256"]) == 64


def test_generated_blender_script_has_canonical_branch_and_preserves_camera_log():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert 'args.render_style == "canonical"' in script
    assert "asset_registry.json" in script
    assert "gesture_tracks.json" in script
    assert 'add_cube("backdrop", (0, 12.0, 3.0)' in script
    assert 'camera_logs.append' in script


def test_canonical_motion_profile_contains_leg_tracks_and_foot_contacts():
    from scripts.run_complex_scene_suite import canonical_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    profile = canonical_motion_profile_for(scene_spec("plaza_dance_circle"))
    assert profile["schema_version"] == "canonical-motion-profile-1.0"
    dancer = next(item for item in profile["characters"] if item["target_id"] == "person_a")
    assert set(dancer["limb_tracks"]) >= {"left_leg", "right_leg"}
    assert len(dancer["limb_tracks"]["left_leg"]) == 5
    assert len(dancer["foot_contacts"]) == 5
    assert {"left", "right"} <= set(dancer["foot_contacts"][0])


def test_generated_blender_script_consumes_canonical_motion_profile():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert 'motion_tracks.json' in script
    assert 'canonical_motion_tracks' in script
    assert 'foot_contacts' in script
    assert 'legs[(eid, "left_leg" if side == "L" else "right_leg")]' in script


def test_storyblender_revision_preserves_tracks_and_widens_staging():
    from scripts.run_complex_scene_suite import storyblender_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    original = scene_spec("plaza_dance_circle")
    revised = storyblender_revision(original)
    assert revised["revision"]["id"] == "revision_006"
    assert revised["revision"]["parent_revision"] == "revision_005"
    assert revised["tracks"] == original["tracks"]
    assert revised["gesture_tracks"] != original["gesture_tracks"]
    assert revised["cameras"][0]["lens_mm"] < original["cameras"][0]["lens_mm"]
    assert revised["cameras"][0]["points"][0]["position"][1] < original["cameras"][0]["points"][0]["position"][1]


def test_motion_materialization_verifier_requires_each_character_foot_contacts():
    from scripts.run_complex_scene_suite import verify_motion_materialization

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        registry = root / "motion_tracks.json"
        log = root / "motion_log.json"
        registry.write_text(json.dumps({"characters": [{"target_id": "person_a", "foot_contacts": [{"frame": 0, "left": True, "right": True}]}]}), encoding="utf-8")
        log.write_text(json.dumps([{"target_id": "person_a", "foot_contacts": [{"frame": 0, "left": True, "right": True}]}]), encoding="utf-8")
        result = verify_motion_materialization(motion_path=registry, motion_log_path=log, proxy_style="canonical")
        assert result["status"] == "passed"


def test_safe_arm_angle_limits_inward_swing_without_removing_outward_gesture():
    from scripts.run_complex_scene_suite import safe_arm_angle

    assert safe_arm_angle("left_arm", 1.5) < 1.0
    assert safe_arm_angle("right_arm", -1.5) > -1.0
    assert safe_arm_angle("left_arm", -1.2) == -1.2
    assert safe_arm_angle("right_arm", 1.2) == 1.2


def test_generated_blender_script_has_head_collision_guard_for_arms():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "safe_arm_angle" in script
    assert "arm.location.z = 2.02 + 0.55 * strength" not in script


def test_arm_collision_verifier_checks_real_applied_pose_log():
    from scripts.run_complex_scene_suite import verify_arm_collision_constraints

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        gesture = root / "gesture_tracks.json"
        pose = root / "arm_pose_log.json"
        gesture.write_text(json.dumps({"gesture_tracks": [{"target_id": "person_a", "limb": "left_arm"}]}), encoding="utf-8")
        pose.write_text(json.dumps([{"target_id": "person_a", "limb": "left_arm", "min_angle": -1.2, "max_angle": 0.7, "min_elbow_clearance": 0.18}]), encoding="utf-8")
        result = verify_arm_collision_constraints(gesture_path=gesture, arm_pose_path=pose, proxy_style="canonical")
        assert result["status"] == "passed"


def test_arm_clearance_revision_is_new_and_preserves_storyblender_revision():
    from scripts.run_complex_scene_suite import arm_clearance_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = arm_clearance_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_007"
    assert revised["revision"]["parent_revision"] == "revision_006"
    assert revised["tracks"] != []


def test_skeleton_motion_profile_contains_explicit_bone_tracks_and_constraints():
    from scripts.run_complex_scene_suite import skeleton_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    profile = skeleton_motion_profile_for(scene_spec("plaza_dance_circle"))
    assert profile["schema_version"] == "skeleton-motion-profile-1.0"
    character = next(item for item in profile["characters"] if item["target_id"] == "person_a")
    required = {"root", "pelvis", "spine", "head", "upper_arm.L", "forearm.L", "hand.L", "upper_leg.L", "lower_leg.L", "foot.L"}
    assert required <= set(character["bones"])
    assert len(character["bone_tracks"]["hand.L"]) == 5
    assert len(character["bone_tracks"]["foot.L"]) == 5
    assert character["foot_contacts"]
    assert character["joint_limits"]["elbow"]


def test_generated_blender_script_contains_isolated_skeleton_branch():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert 'choices=["clay", "canonical", "storyhuman", "skeleton", "diagnostic"]' in script
    assert "skeleton_motion.json" in script
    assert "procedural_skeleton_v1" in script
    assert "skeleton_pose_log.json" in script
    assert "ik_target" in script
    assert 'args.render_style == "canonical"' in script


def test_skeleton_verifier_accepts_real_pose_log_and_rejects_missing_bone_frames():
    from scripts.run_complex_scene_suite import verify_skeleton_materialization

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        motion = root / "skeleton_motion.json"
        pose = root / "skeleton_pose_log.json"
        profile = {
            "schema_version": "skeleton-motion-profile-1.0",
            "characters": [{
                "target_id": "person_a",
                "bones": ["hand.L", "foot.L"],
                "bone_tracks": {"hand.L": [{"frame": 0, "position": [0, 0, 1], "rotation": [0, 0, 0]}], "foot.L": [{"frame": 0, "position": [0, 0, 0], "rotation": [0, 0, 0]}]},
                "foot_contacts": [{"frame": 0, "left": True, "right": False}],
                "joint_limits": {"head_clearance_m": 0.12},
            }],
        }
        motion.write_text(json.dumps(profile), encoding="utf-8")
        pose.write_text(json.dumps([{"frame": 0, "characters": {"person_a": {"bones": {"hand.L": {"position": [0, 0, 1], "reachable": True}, "foot.L": {"position": [0, 0, 0], "reachable": True}}, "foot_contacts": profile["characters"][0]["foot_contacts"]}}}]), encoding="utf-8")
        result = verify_skeleton_materialization(motion_path=motion, pose_path=pose, proxy_style="skeleton")
        assert result["status"] == "passed"
        pose.write_text(json.dumps([]), encoding="utf-8")
        failed = verify_skeleton_materialization(motion_path=motion, pose_path=pose, proxy_style="skeleton")
        assert failed["status"] == "failed"


def test_skeleton_revision_is_immutable_next_revision():
    from scripts.run_complex_scene_suite import skeleton_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = skeleton_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_008"
    assert revised["revision"]["parent_revision"] == "revision_007"


def test_camera_staging_revision_separates_passersby_and_avoids_overhead_rear_views():
    from scripts.run_complex_scene_suite import camera_staging_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = camera_staging_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_009"
    assert revised["revision"]["parent_revision"] == "revision_008"

    person_b = next(track for track in revised["tracks"] if track["target_id"] == "person_b")
    person_c = next(track for track in revised["tracks"] if track["target_id"] == "person_c")
    # The musician stays separated from the crossing lane while the passerby
    # remains behind the action, instead of merging into the dancer cluster.
    assert all(point["position"][1] >= 1.45 for point in person_b["points"])
    assert all(point["position"][1] >= 2.4 for point in person_c["points"])

    reverse = next(camera for camera in revised["cameras"] if camera["camera_id"] == "reverse")
    elevated = next(camera for camera in revised["cameras"] if camera["camera_id"] == "elevated")
    assert reverse["target"] == "person_c"
    assert max(point["position"][2] for point in elevated["points"]) < 8.0
    assert elevated["lens_mm"] <= 36.0


def test_camera_readability_revision_uses_grounded_three_quarter_coverage():
    from scripts.run_complex_scene_suite import camera_readability_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = camera_readability_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_010"
    assert revised["revision"]["parent_revision"] == "revision_009"
    assert revised["revision"]["preserved"]
    reverse = next(camera for camera in revised["cameras"] if camera["camera_id"] == "reverse")
    assert reverse["target"] == "person_a"
    assert "three-quarter" in reverse["role"]
    assert all(max(point["position"][2] for point in camera["points"]) <= 6.0 for camera in revised["cameras"])


def test_proxy_legibility_revision_keeps_speaker_grounded_and_strengthens_action_beats():
    from scripts.run_complex_scene_suite import proxy_legibility_revision, _blender_script
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = proxy_legibility_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_011"
    assert revised["revision"]["parent_revision"] == "revision_010"
    dancer = next(item for item in revised["gesture_tracks"] if item["target_id"] == "person_a" and item["limb"] == "left_arm")
    assert max(abs(float(point[1])) for point in dancer["points"]) >= 1.0
    speaker = next(item for item in revised["tracks"] if item["target_id"] == "speaker")
    assert all(float(point["position"][2]) == 0.35 for point in speaker["points"])
    script = _blender_script()
    assert 'eid + "__body", (0, 0.56, 0.66), (0.24, 0.14, 0.32)' in script
    assert 'eid + "__body", (0, 0, 0.28), (0.36, 0.25, 0.26)' in script


def test_motion_legibility_revision_raises_gesture_targets_and_uses_front_reverse_view():
    from scripts.run_complex_scene_suite import motion_legibility_revision, skeleton_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    spec = scene_spec("plaza_dance_circle")
    revised = motion_legibility_revision(spec)
    assert revised["revision"]["id"] == "revision_012"
    assert revised["revision"]["parent_revision"] == "revision_011"
    dancer = next(item for item in skeleton_motion_profile_for(revised)["characters"] if item["target_id"] == "person_a")
    assert dancer["bone_tracks"]["hand.L"][1]["position"][2] > dancer["bone_tracks"]["hand.L"][0]["position"][2]
    reverse = next(camera for camera in revised["cameras"] if camera["camera_id"] == "reverse")
    assert all(point["position"][1] <= 0.0 for point in reverse["points"])


def test_skeleton_gesture_targets_stay_within_two_link_reach():
    from scripts.run_complex_scene_suite import skeleton_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    profile = skeleton_motion_profile_for(scene_spec("plaza_dance_circle"))
    for character in profile["characters"]:
        root_tracks = character["bone_tracks"]["root"]
        for side in ("L", "R"):
            for root, hand in zip(root_tracks, character["bone_tracks"][f"hand.{side}"]):
                sign = 1.0 if side == "L" else -1.0
                shoulder = [float(root["position"][0]) + sign * 0.44, float(root["position"][1]), float(root["position"][2]) + 2.15]
                distance = sum((float(hand["position"][axis]) - shoulder[axis]) ** 2 for axis in range(3)) ** 0.5
                assert distance <= 1.10 + 1e-6


def test_readable_stage_revision_separates_lanes_and_uses_explicit_hand_poses():
    from scripts.run_complex_scene_suite import readable_stage_revision, skeleton_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = readable_stage_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_013"
    assert revised["revision"]["parent_revision"] == "revision_012"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    assert all(point["position"][1] <= -0.4 for point in tracks["person_a"]["points"])
    assert all(point["position"][1] >= 0.6 for point in tracks["person_b"]["points"])
    assert all(point["position"][1] >= 2.8 for point in tracks["person_c"]["points"])
    elevated = next(camera for camera in revised["cameras"] if camera["camera_id"] == "elevated")
    assert elevated["target"] == "person_b"
    profile = skeleton_motion_profile_for(revised)
    dancer = next(item for item in profile["characters"] if item["target_id"] == "person_a")
    left = dancer["bone_tracks"]["hand.L"]
    assert left[1]["position"][2] > 2.4  # K1 explicit raised hand
    assert left[2]["position"][2] > left[0]["position"][2]  # K2 outward silhouette
    assert profile["source"].endswith("explicit down/out/up hand landmarks")


def test_musician_role_revision_adds_proxy_role_cue_without_new_world_entity():
    from scripts.run_complex_scene_suite import musician_role_revision, _blender_script
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    original = scene_spec("plaza_dance_circle")
    revised = musician_role_revision(original)
    assert revised["revision"]["id"] == "revision_014"
    assert revised["revision"]["parent_revision"] == "revision_013"
    assert [item["id"] for item in revised["entities"]] == [item["id"] for item in original["entities"]]
    speaker = next(item for item in revised["tracks"] if item["target_id"] == "speaker")
    musician = next(item for item in revised["tracks"] if item["target_id"] == "person_b")
    assert all(float(a["position"][0]) - float(b["position"][0]) >= 0.95 for a, b in zip(speaker["points"], musician["points"]))
    script = _blender_script()
    assert "__mic_stand" in script
    assert "__mic_head" in script


def test_fixed_stage_camera_revision_exposes_dancer_travel_and_front_facing_musician():
    from scripts.run_complex_scene_suite import fixed_stage_camera_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = fixed_stage_camera_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_015"
    assert revised["revision"]["parent_revision"] == "revision_014"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    assert all(point["rotation"][2] == 0.0 for point in tracks["person_b"]["points"])
    assert all(abs(float(s["position"][0]) - float(m["position"][0])) <= 0.65 for s, m in zip(tracks["speaker"]["points"], tracks["person_b"]["points"]))
    assert {camera["target"] for camera in revised["cameras"]} == {"person_b"}


def test_composition_fit_revision_keeps_all_roles_inside_the_stage_bounds():
    from scripts.run_complex_scene_suite import composition_fit_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = composition_fit_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_016"
    assert revised["revision"]["parent_revision"] == "revision_015"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    xs = [float(point["position"][0]) for target in ("person_a", "person_b", "person_c") for point in tracks[target]["points"]]
    assert min(xs) >= -2.8 and max(xs) <= 4.0
    assert all(float(point["position"][2]) == 0.35 for point in tracks["speaker"]["points"])
    assert all(float(camera["lens_mm"]) >= 36.0 for camera in revised["cameras"])


def test_event_separation_revision_makes_crossing_beats_and_backpack_coupling_explicit():
    from scripts.run_complex_scene_suite import event_separation_revision, skeleton_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = event_separation_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_017"
    assert revised["revision"]["parent_revision"] == "revision_016"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    passerby = tracks["person_c"]
    assert all(float(point["position"][1]) >= 4.0 for point in passerby["points"])
    assert float(passerby["points"][0]["position"][0]) > float(passerby["points"][-1]["position"][0])
    dancer_y = [float(point["position"][1]) for point in tracks["person_a"]["points"]]
    assert dancer_y[1] > dancer_y[0] and dancer_y[2] < dancer_y[1] and dancer_y[3] > dancer_y[2]
    backpack = tracks["backpack"]
    dancer = tracks["person_a"]
    assert all(abs(float(b["position"][0]) - float(d["position"][0]) + 0.55) < 1e-6 for b, d in zip(backpack["points"], dancer["points"]))
    profile = skeleton_motion_profile_for(revised)
    assert profile["source"].endswith("explicit down/out/up hand landmarks")


def test_event_separation_script_has_visible_backpack_material():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "BackpackAccent" in script
    assert 'eid + "__visible_pack", (0, 0.12, 0.72)' in script


def test_canonical_asset_dir_can_reuse_one_verified_humanoid_source_for_each_role():
    from scripts.run_complex_scene_suite import copy_canonical_assets
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    spec = scene_spec("plaza_dance_circle")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "CesiumMan.glb"
        source.write_bytes(b"verified-glb-placeholder")
        out = root / "run"
        copied = copy_canonical_assets(spec=spec, scene_output=out, asset_dir=root)
        assert set(copied) == {"person_a", "person_b", "person_c"}
        assert all((out / copied[eid]).is_file() for eid in copied)
        assert all(Path(copied[eid]).name == f"{eid}.glb" for eid in copied)


def test_blender_script_normalizes_imported_rigged_asset_into_z_up_shared_root():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "canonical_glb" in script
    assert "rotation_euler.x = math.radians(90.0)" in script
    assert "axis_root.location = (0.0, 0.0, 0.0)" in script
    assert "animation_data" in script


def test_blender_script_applies_authored_gestures_to_imported_rigged_bones_and_logs_them():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "rigged_armatures" in script
    assert "Skeleton_arm_joint_L__4_" in script
    assert "__authored_pose" in script
    assert "asset_kind" in script


def test_rigged_staging_revision_moves_bench_out_of_passerby_lane_and_turns_dancer():
    from scripts.run_complex_scene_suite import rigged_staging_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = rigged_staging_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_018"
    assert revised["revision"]["parent_revision"] == "revision_017"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    bench = tracks["bench"]["points"][0]["position"]
    assert bench[0] <= -4.0
    dancer_yaw = tracks["person_a"]["points"][-1]["rotation"][2]
    assert dancer_yaw >= 1.4


def test_blender_script_contains_rigged_leg_bone_mapping():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "leg_joint_L_1" in script
    assert "leg_joint_R_1" in script


def test_choreography_revision_separates_two_side_steps_and_camera_responsibilities():
    from scripts.run_complex_scene_suite import choreography_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = choreography_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_019"
    assert revised["revision"]["parent_revision"] == "revision_018"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    dancer_x = [float(point["position"][0]) for point in tracks["person_a"]["points"]]
    assert dancer_x[2] > dancer_x[1] and dancer_x[3] < dancer_x[2]
    assert float(tracks["person_a"]["points"][-1]["rotation"][2]) >= 1.5
    cameras = {camera["camera_id"]: camera for camera in revised["cameras"]}
    assert cameras["reverse"]["target"] == "person_c"
    assert cameras["lateral"]["target"] == "person_a"


def test_side_step_motion_revision_disables_running_stride_for_lead_and_widens_shots():
    from scripts.run_complex_scene_suite import side_step_motion_revision, canonical_motion_profile_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = side_step_motion_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_020"
    profile = canonical_motion_profile_for(revised)
    dancer = next(item for item in profile["characters"] if item["target_id"] == "person_a")
    assert dancer["motion_mode"] == "side_step_transfer"
    assert all(float(item["angle"]) == 0.0 for item in dancer["limb_tracks"]["left_leg"])
    assert all(float(camera["lens_mm"]) <= 32.0 for camera in revised["cameras"])


def test_blender_script_drives_rigged_forearms_with_authored_gestures():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "Skeleton_arm_joint_L__3_" in script
    assert "Skeleton_arm_joint_R__2_" in script


def test_blender_script_resets_imported_rigged_rest_pose_before_authored_tracks():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "rotation_quaternion = (1.0, 0.0, 0.0, 0.0)" in script
    assert "pose_position" in script


def test_blender_script_contains_bvh_retarget_mapping_and_pose_stats():
    from scripts.run_complex_scene_suite import _blender_script

    script = _blender_script()
    assert "import_anim.bvh" in script
    assert "motion_bvh_alt" in script
    assert "bvh_retarget" in script
    assert "LeftForeArm" in script
    assert "scene.frame_set(int(round(src_frame)))" in script
    assert "rotation_euler.to_quaternion()" in script
    assert "rotation_quaternion.to_euler()" in script
    assert "real motion as the base layer" in script
    assert "retarget limbs only" in script
    assert "bounded authored leg overlay" in script
    assert "arm_sign = -1.0" in script


def test_bvh_motion_revision_preserves_shared_world_and_selects_real_clips():
    from scripts.run_complex_scene_suite import bvh_motion_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = bvh_motion_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_021"
    assert revised["revision"]["parent_revision"] == "revision_020"
    assert "real BVH retarget" in revised["revision"]["reason"]


def test_bvh_readability_revision_separates_lanes_and_times_wave_beats():
    from scripts.run_complex_scene_suite import bvh_readability_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = bvh_readability_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_022"
    assert revised["revision"]["parent_revision"] == "revision_021"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    assert tracks["person_a"]["points"][-1]["position"][1] < -1.5
    gestures = {(item["target_id"], item["limb"]): item for item in revised["gesture_tracks"]}
    assert (30, 1.1) in gestures[("person_a", "left_arm")]["points"]


def test_bvh_upright_revision_keeps_real_limbs_and_widens_side_step():
    from scripts.run_complex_scene_suite import bvh_upright_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = bvh_upright_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_023"
    assert revised["revision"]["parent_revision"] == "revision_022"
    points = revised["tracks"][0]["points"]
    assert float(points[2]["position"][0]) - float(points[1]["position"][0]) > 1.0


def test_bvh_choreography_revision_adds_explicit_side_step_overlay_contract():
    from scripts.run_complex_scene_suite import bvh_choreography_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = bvh_choreography_revision(scene_spec("plaza_dance_circle"))
    assert revised["revision"]["id"] == "revision_024"
    assert revised["revision"]["parent_revision"] == "revision_023"
    assert "bounded authored leg" in revised["revision"]["reason"]


def test_t2v_prompt_is_live_action_and_explicitly_preserves_authored_story():
    from scripts.run_complex_scene_suite import real_t2v_prompt
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    prompt = real_t2v_prompt(scene_spec("plaza_dance_circle"))
    assert "live-action" in prompt
    assert "no clay" in prompt.lower()
    assert "lead dancer" in prompt


def test_seedance_upload_fallback_uses_tested_uguu_without_tos(monkeypatch):
    from scripts.run_complex_scene_suite import _temp_upload_config

    for name in (
        "TOS_ACCESS_KEY", "VOLC_ACCESSKEY", "TOS_SECRET_KEY", "VOLC_SECRETKEY",
        "TOS_BUCKET", "TOS_ENDPOINT", "TOS_REGION", "VIDEOACTAGENT_TEMP_UPLOAD",
        "VIDEOACTAGENT_TEMP_UPLOAD_ENDPOINT", "VIDEOACTAGENT_TEMP_UPLOAD_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    config = _temp_upload_config()
    assert config.temp_upload_enabled is True
    assert config.temp_upload_provider == "uguu"
    assert config.temp_upload_endpoint == "https://uguu.se/upload.php"


def test_indoor_market_readability_revision_widens_reverse_and_preserves_tracks():
    from scripts.run_complex_scene_suite import indoor_market_readability_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    original = scene_spec("indoor_market_exchange")
    revised = indoor_market_readability_revision(original)
    assert revised["revision"]["id"] == "revision_025"
    assert revised["revision"]["parent_revision"] == "base_indoor_market_exchange"
    assert revised["tracks"] == original["tracks"]
    cameras = {item["camera_id"]: item for item in revised["cameras"]}
    assert cameras["reverse"]["target"] == "counter"
    assert cameras["reverse"]["lens_mm"] == 34.0


def test_indoor_market_counter_revision_changes_overhead_role():
    from scripts.run_complex_scene_suite import indoor_market_counter_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = indoor_market_counter_revision(scene_spec("indoor_market_exchange"))
    assert revised["revision"]["id"] == "revision_026"
    elevated = next(item for item in revised["cameras"] if item["camera_id"] == "elevated")
    assert elevated["target"] == "handcart"
    assert elevated["lens_mm"] == 30.0


def test_indoor_market_event_revision_adds_pause_and_preserves_cart_box_coupling():
    from scripts.run_complex_scene_suite import indoor_market_event_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = indoor_market_event_revision(scene_spec("indoor_market_exchange"))
    assert revised["revision"]["id"] == "revision_027"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    customer_delta = [tracks["customer"]["points"][2]["position"][axis] - tracks["handcart"]["points"][2]["position"][axis] for axis in range(2)]
    assert math.isclose(customer_delta[0], -0.2, abs_tol=1e-6) and math.isclose(customer_delta[1], 0.0, abs_tol=1e-6)
    gestures = {(item["target_id"], item["limb"]): item for item in revised["gesture_tracks"]}
    assert (36, 1.25) in gestures[("customer", "right_arm")]["points"]


def test_storyhuman_registry_declares_rounded_articulated_asset():
    from scripts.run_complex_scene_suite import asset_registry_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    registry = asset_registry_for(scene_spec("indoor_market_exchange"), "storyhuman")
    characters = [item for item in registry["assets"] if item["kind"] == "character"]
    assert registry["proxy_style"] == "storyhuman"
    assert all(item["source_kind"] == "storyhuman_procedural_v1" for item in characters)
    assert all("capsule_limbs" in item["parts"] for item in characters)


def test_coupling_rules_bind_boxes_and_backpack_to_authored_parents():
    from scripts.run_complex_scene_suite import coupling_rules_for
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    market = coupling_rules_for(scene_spec("indoor_market_exchange"))
    plaza = coupling_rules_for(scene_spec("plaza_dance_circle"))
    assert market == {"box_a": "handcart", "box_b": "handcart"}
    assert plaza["backpack"] == "person_a"


def test_storyhuman_market_revision_separates_pusher_and_grounds_cart():
    from scripts.run_complex_scene_suite import indoor_market_storyhuman_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = indoor_market_storyhuman_revision(scene_spec("indoor_market_exchange"))
    assert revised["revision"]["id"] == "revision_028"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    assert all(float(point["position"][2]) == 0.0 for point in tracks["handcart"]["points"])
    assert all(float(point["position"][1]) <= -1.3 for point in tracks["customer"]["points"])
    assert all(float(point["position"][1]) <= 0.5 for point in tracks["helper"]["points"])


def test_storyhuman_market_readability_revision_separates_helper_and_paper_landing():
    from scripts.run_complex_scene_suite import indoor_market_storyhuman_readability_revision
    from videoactagent.complex_scene_prompts_v2 import scene_spec

    revised = indoor_market_storyhuman_readability_revision(scene_spec("indoor_market_exchange"))
    assert revised["revision"]["id"] == "revision_029"
    tracks = {item["target_id"]: item for item in revised["tracks"]}
    assert all(float(point["position"][1]) >= 0.7 for point in tracks["helper"]["points"])
    assert tracks["paper_a"]["points"][-1]["position"][:2] == [2.6, 1.3]
    assert tracks["paper_b"]["points"][-1]["position"][:2] == [2.9, 1.4]
