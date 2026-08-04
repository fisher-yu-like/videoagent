from copy import deepcopy
import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.multicam_eval import evaluate_multicam_iteration
from videoactagent.multicam_plan import load_multicam_plan


def _load_multicam_proxy_without_blender():
    bpy = types.ModuleType("bpy")
    bpy.data = types.SimpleNamespace(objects={})
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = object
    blender_proxy = types.ModuleType("videoactagent.blender_proxy")
    for name in (
        "RenderProfile", "apply_camera_trajectory", "apply_trajectory", "configure_scene"
    ):
        setattr(blender_proxy, name, object)
    annotation = types.ModuleType("videoactagent.director_annotation")
    annotation.CameraKeyframe = object
    shotscript = types.ModuleType("videoactagent.shotscript")
    shotscript.ShotScript = object
    trajectory = types.ModuleType("videoactagent.trajectory")
    trajectory.TrajectoryInstruction = object
    path = Path("videoactagent/multicam_blender_proxy.py")
    spec = importlib.util.spec_from_file_location("_test_multicam_proxy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        "bpy": bpy,
        "mathutils": mathutils,
        "videoactagent.blender_proxy": blender_proxy,
        "videoactagent.director_annotation": annotation,
        "videoactagent.shotscript": shotscript,
        "videoactagent.trajectory": trajectory,
    }):
        spec.loader.exec_module(module)
    return module


def _direction(position, target=(0.0, 0.0, 0.8)):
    values = [target[i] - position[i] for i in range(3)]
    length = math.sqrt(sum(value * value for value in values))
    return [value / length for value in values]


def fixture():
    cameras = {}
    positions = {
        "camera_a": [0.0, -7.0, 2.4],
        "camera_b": [-2.0, -5.0, 2.0],
        "camera_c": [2.0, 5.0, 2.0],
    }
    for camera_id in positions:
        cameras[camera_id] = {
            "video": {"frame_count": 3, "fps": 3, "resolution": [160, 90]},
        }
    frames = []
    for frame in range(1, 4):
        frames.append({
            "frame": frame,
            "actors": {"actor_a": [-0.5, 0.0, 0.0], "actor_b": [0.5, 0.0, 0.0]},
            "cameras": {
                camera_id: {
                    "position": position,
                    "view_direction": _direction(position),
                    "focal_length_mm": 35.0,
                }
                for camera_id, position in positions.items()
            },
        })
    return {"shared_world_frames": frames, "cameras": cameras}


class MulticamEvalTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_multicam_plan(
            VALID_PLAN, scene_id="station_reunion", actors=["actor_a", "actor_b"]
        )

    def test_measures_sync_visibility_and_orientation(self):
        report = evaluate_multicam_iteration(plan=self.plan, render_manifest=fixture())
        self.assertEqual(report["sync"], {
            "frame_count_equal": True, "fps_equal": True, "duration_equal": True,
        })
        self.assertEqual(report["coverage"], {
            "camera_a": 1.0, "camera_b": 1.0, "camera_c": 1.0,
        })
        self.assertEqual(report["responsibility_target_visibility"]["minimum"], 1.0)
        self.assertTrue(report["orientation"]["passed"])
        self.assertEqual(report["human_composition_status"], "unknown")
        self.assertTrue(report["automatic_passed"])

    def test_missing_frame_and_abrupt_turn_fail(self):
        value = fixture()
        value["cameras"]["camera_b"]["video"]["frame_count"] = 2
        value["shared_world_frames"][1]["cameras"]["camera_a"]["view_direction"] = [1, 0, 0]
        report = evaluate_multicam_iteration(plan=self.plan, render_manifest=value)
        self.assertFalse(report["sync"]["frame_count_equal"])
        self.assertFalse(report["orientation"]["passed"])
        self.assertFalse(report["automatic_passed"])

    def test_object_target_visibility_uses_shared_object_world_position(self):
        plan_value = deepcopy(VALID_PLAN)
        plan_value["cameras"][2]["target"] = "package"
        plan = load_multicam_plan(
            plan_value,
            scene_id="station_reunion",
            actors=["actor_a", "actor_b", "package"],
        )
        manifest = fixture()
        for frame in manifest["shared_world_frames"]:
            frame["objects"] = {"package": [0.0, 0.0, 0.28]}

        report = evaluate_multicam_iteration(plan=plan, render_manifest=manifest)

        self.assertEqual(
            report["responsibility_target_visibility"]["per_camera"]["camera_c"],
            1.0,
        )
        self.assertEqual(report["responsibility_target_visibility"]["minimum"], 1.0)
        self.assertTrue(report["automatic_passed"])

    def test_objects_mapping_is_optional_but_strict_when_present(self):
        legacy = evaluate_multicam_iteration(plan=self.plan, render_manifest=fixture())
        self.assertTrue(legacy["automatic_passed"])

        malformed = fixture()
        malformed["shared_world_frames"][0]["objects"] = []
        with self.assertRaisesRegex(ValueError, "objects"):
            evaluate_multicam_iteration(plan=self.plan, render_manifest=malformed)

    def test_camera_collision_clearance_remains_actor_only(self):
        manifest = fixture()
        camera_a_position = manifest["shared_world_frames"][0]["cameras"][
            "camera_a"
        ]["position"]
        for frame in manifest["shared_world_frames"]:
            frame["objects"] = {"package": list(camera_a_position)}

        report = evaluate_multicam_iteration(plan=self.plan, render_manifest=manifest)

        self.assertTrue(report["collision"]["camera_actor_clearance_passed"])
        self.assertTrue(report["automatic_passed"])

    def test_world_frames_resolve_and_record_exact_object_roots(self):
        proxy = _load_multicam_proxy_without_blender()
        package = types.SimpleNamespace(
            matrix_world=types.SimpleNamespace(translation=(1.0, 2.0, 0.28))
        )
        proxy.bpy.data.objects = {"prop__package": package}
        instruction = types.SimpleNamespace(tracks=(
            types.SimpleNamespace(target_type="actor", target_id="actor_a"),
            types.SimpleNamespace(target_type="object", target_id="package"),
        ))
        object_roots = proxy._object_roots(instruction)
        scene = types.SimpleNamespace(
            frame_start=1,
            frame_end=1,
            frame_set=lambda _frame: None,
        )

        frames = proxy._world_frames(scene, {}, object_roots, {})

        self.assertEqual(frames[0]["objects"], {"package": [1.0, 2.0, 0.28]})
        proxy.bpy.data.objects = {}
        with self.assertRaisesRegex(RuntimeError, "prop__package"):
            proxy._object_roots(instruction)


if __name__ == "__main__":
    unittest.main()
