"""Stage 1 real Blender proxy integration test for ``blender_runner.py``.

Run: ``& $PY -m unittest tests.test_blender_proxy_integration -v`` (see
``docs/USAGE.md``). Inputs: ``D:\\blender\\blender.exe`` and
``examples/station_shotscript.json``; rendered proxy files are created in a
temporary directory and inspected before cleanup. This invokes real Blender;
it does not call a video-generation API or validate photorealistic output.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


BLENDER = Path(r"D:\blender\blender.exe")
RUNNER = Path("videoactagent/blender_runner.py")
SHOT_SCRIPT = Path("examples/station_shotscript.json")


class BlenderProxyIntegrationTests(unittest.TestCase):
    def test_real_blender_preserves_heading_context_across_stationary_shots(self):
        self.assertTrue(BLENDER.is_file(), f"Blender missing at {BLENDER}")
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            document = json.loads(SHOT_SCRIPT.read_text(encoding="utf-8"))
            document["shots"] = document["shots"][:2]
            for shot in document["shots"]:
                shot["duration"] = 1.0

            actor_a_first, actor_b_first = document["shots"][0]["actors"]
            actor_a_first["start"], actor_a_first["end"] = [0, 0, 0], [0, 2, 0]
            actor_b_first["start"], actor_b_first["end"] = [-2, 0, 0], [-2, 0, 0]
            actor_a_second, actor_b_second = document["shots"][1]["actors"]
            actor_a_second["start"] = actor_a_second["end"] = [0, 2, 0]
            actor_b_second["start"], actor_b_second["end"] = [-2, 0, 0], [0, 0, 0]

            shotscript = directory / "cross_shot_heading.json"
            shotscript.write_text(json.dumps(document), encoding="utf-8")
            output = directory / "heading_proxy"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--blender",
                    str(BLENDER),
                    "--shotscript",
                    str(shotscript),
                    "--output-dir",
                    str(output),
                    "--fps",
                    "3",
                    "--resolution",
                    "160x90",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            probe_script = """
import bpy
import json
scene = bpy.context.scene

def action_curves(obj):
    action = obj.animation_data.action
    if hasattr(action, "fcurves"):
        return action.fcurves
    return [
        curve
        for layer in action.layers
        for strip in layer.strips
        for channelbag in strip.channelbags
        for curve in channelbag.fcurves
    ]

payload = {}
for actor_id in ("actor_a", "actor_b"):
    actor = bpy.data.objects[actor_id]
    values = {}
    for frame in range(1, 7):
        scene.frame_set(frame)
        values[str(frame)] = round(float(actor.rotation_euler.z), 6)
    location_frames = sorted({
        round(float(point.co.x))
        for curve in action_curves(actor)
        if curve.data_path == "location"
        for point in curve.keyframe_points
    })
    payload[actor_id] = {"headings": values, "location_frames": location_frames}
print("CROSS_SHOT_HEADING_PROBE=" + json.dumps(payload, sort_keys=True))
"""
            probed = subprocess.run(
                [
                    str(BLENDER),
                    "--background",
                    str(output / "station_proxy.blend"),
                    "--python-expr",
                    probe_script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(probed.returncode, 0, probed.stdout + probed.stderr)
            line = next(
                line
                for line in (probed.stdout + probed.stderr).splitlines()
                if line.startswith("CROSS_SHOT_HEADING_PROBE=")
            )
            headings = json.loads(line.partition("=")[2])

            for frame in (3, 4, 5, 6):
                self.assertAlmostEqual(
                    headings["actor_a"]["headings"][str(frame)], 0.0, places=5
                )
            for frame in (1, 2, 3, 4):
                self.assertAlmostEqual(
                    headings["actor_b"]["headings"][str(frame)],
                    -1.570796,
                    places=5,
                )
            self.assertEqual(headings["actor_a"]["location_frames"], [1, 3, 4, 6])
            self.assertEqual(headings["actor_b"]["location_frames"], [1, 3, 4, 6])

            trajectory = directory / "stationary_s02_trajectory.json"
            trajectory.write_text(
                json.dumps(
                    {
                        "schema_version": "0.1",
                        "scene_id": document["scene_id"],
                        "shot_id": document["shots"][1]["shot_id"],
                        "coordinate_space": "normalized_0_1_top_left",
                        "duration_seconds": 1.0,
                        "sample_count": 2,
                        "tracks": [
                            {
                                "track_id": "actor_a_stationary",
                                "target": {"type": "actor", "id": "actor_a"},
                                "primitive": "polyline",
                                "semantic": "move",
                                "points": [
                                    {"t": 0.0, "x": 0.5, "y": 0.25, "visible": True},
                                    {"t": 1.0, "x": 0.5, "y": 0.25, "visible": True},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            trajectory_output = directory / "trajectory_heading_proxy"
            trajectory_completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--blender",
                    str(BLENDER),
                    "--shotscript",
                    str(shotscript),
                    "--trajectory",
                    str(trajectory),
                    "--output-dir",
                    str(trajectory_output),
                    "--fps",
                    "3",
                    "--resolution",
                    "160x90",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(
                trajectory_completed.returncode,
                0,
                trajectory_completed.stdout + trajectory_completed.stderr,
            )
            trajectory_probed = subprocess.run(
                [
                    str(BLENDER),
                    "--background",
                    str(trajectory_output / "trajectory_proxy.blend"),
                    "--python-expr",
                    probe_script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(
                trajectory_probed.returncode,
                0,
                trajectory_probed.stdout + trajectory_probed.stderr,
            )
            trajectory_line = next(
                line
                for line in (trajectory_probed.stdout + trajectory_probed.stderr).splitlines()
                if line.startswith("CROSS_SHOT_HEADING_PROBE=")
            )
            trajectory_headings = json.loads(trajectory_line.partition("=")[2])
            for frame in (3, 4, 5, 6):
                self.assertAlmostEqual(
                    trajectory_headings["actor_a"]["headings"][str(frame)],
                    0.0,
                    places=5,
                )

    def test_real_blender_renders_station_proxy_artifacts(self):
        self.assertTrue(BLENDER.is_file(), f"Blender missing at {BLENDER}")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "stage1_blender"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--blender",
                    str(BLENDER),
                    "--shotscript",
                    str(SHOT_SCRIPT),
                    "--output-dir",
                    str(output),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertIn("BLENDER_PROXY_OK", completed.stdout + completed.stderr)

            blend = output / "station_proxy.blend"
            video = output / "station_proxy.mp4"
            report_path = output / "trajectory_report.json"
            keyframes = [
                output / "keyframes" / "shot_01.png",
                output / "keyframes" / "shot_02.png",
                output / "keyframes" / "shot_03.png",
            ]
            for path in [blend, video, report_path, *keyframes]:
                self.assertTrue(path.is_file(), f"missing real Blender output: {path}")
                self.assertGreater(path.stat().st_size, 0, f"empty output: {path}")

            with Image.open(keyframes[0]) as image:
                self.assertEqual(image.size, (960, 540))
                first_pixels = image.convert("RGB").tobytes()
            with Image.open(keyframes[2]) as image:
                self.assertEqual(image.size, (960, 540))
                third_pixels = image.convert("RGB").tobytes()
            self.assertNotEqual(first_pixels, third_pixels)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["scene_frame_start"], 1)
            self.assertEqual(report["scene_frame_end"], 45)
            self.assertEqual(report["rendered_frames"], 45)
            expected_parts = sorted([
                "head", "torso", "pelvis",
                "upper_arm.L", "lower_arm.L", "upper_arm.R", "lower_arm.R",
                "upper_leg.L", "lower_leg.L", "upper_leg.R", "lower_leg.R",
            ])
            self.assertEqual(report["actor_geometry_profile"], "humanoid_v1")
            self.assertEqual(report["required_actor_parts"], expected_parts)
            self.assertEqual(report["shots"][0]["actor_positions"]["actor_a"]["end"], [-1.0, 0.0, 0.0])
            self.assertEqual(report["shots"][1]["actor_positions"]["actor_a"]["start"], [-1.0, 0.0, 0.0])

            probe_script = """
import bpy
import json
from mathutils import Vector

required = [
    "head", "torso", "pelvis",
    "upper_arm.L", "lower_arm.L", "upper_arm.R", "lower_arm.R",
    "upper_leg.L", "lower_leg.L", "upper_leg.R", "lower_leg.R",
]

def reaches_root(obj, root):
    current = obj
    while current is not None and current != root:
        current = current.parent
    return current == root

def keyframe_paths(obj):
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        curves = action.fcurves
    else:
        curves = [
            curve
            for layer in action.layers
            for strip in layer.strips
            for channelbag in strip.channelbags
            for curve in channelbag.fcurves
        ]
    return sorted({curve.data_path for curve in curves})

payload = {}
scene = bpy.context.scene
for actor_id in ("actor_a", "actor_b"):
    root = bpy.data.objects[actor_id]
    parts = [bpy.data.objects[f"{actor_id}__{name}"] for name in required]
    payload[actor_id] = {
        "root_type": root.type,
        "all_parts_meshes": all(obj.type == "MESH" for obj in parts),
        "all_reach_root": all(reaches_root(obj, root) for obj in parts),
        "paths": keyframe_paths(root),
        "limb_paths": keyframe_paths(bpy.data.objects[f"{actor_id}__anchor__upper_arm.L"]),
    }
scene.frame_set(8)
payload["moving_swing"] = [float(value) for value in bpy.data.objects["actor_a__anchor__upper_arm.L"].rotation_euler]
payload["stationary_swing"] = [float(value) for value in bpy.data.objects["actor_b__anchor__upper_arm.L"].rotation_euler]
payload["forward_yaw"] = float(bpy.data.objects["actor_a"].rotation_euler.z)

actor = bpy.data.objects["actor_a"]
head = bpy.data.objects["actor_a__head"]
label = bpy.data.objects["actor_a_label"]
camera = bpy.data.objects["DirectorCamera"]

scene.frame_set(1)
actor_start = actor.matrix_world.translation.copy()
label_start = label.matrix_world.translation.copy()
scene.frame_set(8)
actor_end = actor.matrix_world.translation.copy()
label_end = label.matrix_world.translation.copy()

def world_z_bounds(obj):
    world = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    return [min(point.z for point in world), max(point.z for point in world)]

label_normal = (label.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
to_camera = (camera.matrix_world.translation - label.matrix_world.translation).normalized()
payload["label"] = {
    "head_z": world_z_bounds(head),
    "label_z": world_z_bounds(label),
    "normal_view_dot": abs(float(label_normal.dot(to_camera))),
    "actor_delta": [float(value) for value in actor_end - actor_start],
    "label_delta": [float(value) for value in label_end - label_start],
}
print("HUMANOID_PROBE=" + json.dumps(payload, sort_keys=True))
"""
            probed = subprocess.run(
                [str(BLENDER), "--background", str(blend), "--python-expr", probe_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(probed.returncode, 0, probed.stdout + probed.stderr)
            evidence = probed.stdout + probed.stderr
            lines = [
                line for line in evidence.splitlines()
                if line.startswith("HUMANOID_PROBE=")
            ]
            self.assertTrue(lines, evidence)
            probe = json.loads(lines[0].partition("=")[2])
            for actor_id in ("actor_a", "actor_b"):
                with self.subTest(actor=actor_id):
                    self.assertEqual(probe[actor_id]["root_type"], "EMPTY")
                    self.assertTrue(probe[actor_id]["all_parts_meshes"])
                    self.assertTrue(probe[actor_id]["all_reach_root"])
                    self.assertIn("location", probe[actor_id]["paths"])
                    self.assertIn("rotation_euler", probe[actor_id]["paths"])
                    self.assertIn("rotation_euler", probe[actor_id]["limb_paths"])
            self.assertGreater(abs(probe["moving_swing"][0]), 0.1)
            self.assertAlmostEqual(probe["moving_swing"][1], 0.0, places=6)
            self.assertTrue(all(abs(value) < 1e-6 for value in probe["stationary_swing"]))
            self.assertAlmostEqual(probe["forward_yaw"], -1.570796, places=5)
            self.assertGreater(
                probe["label"]["label_z"][0] - probe["label"]["head_z"][1],
                0.2,
            )
            self.assertGreater(probe["label"]["normal_view_dot"], 0.75)
            for actor_delta, label_delta in zip(
                probe["label"]["actor_delta"], probe["label"]["label_delta"]
            ):
                self.assertAlmostEqual(actor_delta, label_delta, places=5)


if __name__ == "__main__":
    unittest.main()
