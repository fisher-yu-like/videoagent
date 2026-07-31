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
            line = next(
                line for line in (probed.stdout + probed.stderr).splitlines()
                if line.startswith("HUMANOID_PROBE=")
            )
            probe = json.loads(line.partition("=")[2])
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


if __name__ == "__main__":
    unittest.main()
