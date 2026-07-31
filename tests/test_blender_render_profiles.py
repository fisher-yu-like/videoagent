"""Unit coverage for Blender render profiles without launching Blender."""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import FrozenInstanceError
import importlib.util
import inspect
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import imageio_ffmpeg

from videoactagent import blender_runner


ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / "videoactagent" / "blender_proxy.py"
BLENDER = Path(r"D:\blender\blender.exe")
RUNNER_PATH = ROOT / "videoactagent" / "blender_runner.py"
SHOT_SCRIPT_PATH = ROOT / "examples" / "station_shotscript.json"


def load_proxy_without_blender():
    """Load CLI/pure helpers while replacing only Blender-owned modules."""

    bpy = types.ModuleType("bpy")
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = object
    spec = importlib.util.spec_from_file_location(
        "_test_blender_render_profiles_proxy", PROXY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {"bpy": bpy, "mathutils": mathutils, spec.name: module},
    ):
        spec.loader.exec_module(module)
    return module


class BlenderRunnerRenderProfileTests(unittest.TestCase):
    def runner_args(self, *extra: str) -> list[str]:
        return [
            "--blender",
            "blender.exe",
            "--shotscript",
            "shot.json",
            "--output-dir",
            "render",
            *extra,
        ]

    def test_defaults_preserve_diagnostic_three_fps_and_960x540(self):
        args = blender_runner.parse_args(self.runner_args())

        self.assertEqual(args.render_style, "diagnostic")
        self.assertEqual(args.fps, 3)
        self.assertEqual(args.resolution, (960, 540))

    def test_runner_forwards_effective_profile_to_blender_script(self):
        completed = types.SimpleNamespace(returncode=0, stdout="BLENDER_PROXY_OK\n", stderr="")
        with mock.patch.object(blender_runner.subprocess, "run", return_value=completed) as run:
            result = blender_runner.main(
                self.runner_args(
                    "--render-style",
                    "clay",
                    "--fps",
                    "12",
                    "--resolution",
                    "1280x720",
                )
            )

        self.assertEqual(result, 0)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--render-style") + 1], "clay")
        self.assertEqual(command[command.index("--fps") + 1], "12")
        self.assertEqual(command[command.index("--resolution") + 1], "1280x720")

    def test_invalid_profile_is_rejected_before_blender_launch(self):
        invalid_cases = (
            ("--render-style", "painted"),
            ("--fps", "0"),
            ("--fps", "-1"),
            ("--fps", "2.5"),
            ("--resolution", "1280"),
            ("--resolution", "0x720"),
            ("--resolution", "1280X720"),
        )
        for flag, value in invalid_cases:
            with self.subTest(flag=flag, value=value), mock.patch.object(
                blender_runner.subprocess, "run"
            ) as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    blender_runner.main(self.runner_args(flag, value))
                run.assert_not_called()


class BlenderProxyRenderProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = load_proxy_without_blender()

    def proxy_args(self, *extra: str) -> list[str]:
        return [
            "--shotscript",
            "shot.json",
            "--output-dir",
            "render",
            *extra,
        ]

    def test_proxy_cli_uses_the_same_defaults_and_validation(self):
        args = self.proxy.parse_args(self.proxy_args())
        self.assertEqual(
            (args.render_style, args.fps, args.resolution),
            ("diagnostic", 3, (960, 540)),
        )

        for flag, value in (
            ("--render-style", "painted"),
            ("--fps", "0"),
            ("--resolution", "960-by-540"),
        ):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.proxy.parse_args(self.proxy_args(flag, value))

    def test_clay_material_colors_are_neutral_and_actor_contrast_is_stable(self):
        profile = self.proxy.RenderProfile("clay", 6, (160, 90))
        original = (0.1, 0.4, 0.9, 1.0)
        converted = self.proxy.effective_material_color(profile, original)
        self.assertEqual(converted[0], converted[1])
        self.assertEqual(converted[1], converted[2])

        first_pass = [self.proxy.clay_actor_color(index) for index in range(4)]
        second_pass = [self.proxy.clay_actor_color(index) for index in range(4)]
        self.assertEqual(first_pass, second_pass)
        self.assertEqual(len({color[0] for color in first_pass}), len(first_pass))
        for color in first_pass:
            self.assertEqual(color[0], color[1])
            self.assertEqual(color[1], color[2])

    def test_clay_hides_actor_labels_and_action_axis_only_by_render_flag(self):
        self.assertTrue(self.proxy.hide_in_clay("action_axis"))
        self.assertTrue(self.proxy.hide_in_clay("actor_a_label"))
        self.assertFalse(self.proxy.hide_in_clay("actor_a_body"))
        self.assertFalse(self.proxy.hide_in_clay("TrajectoryLabel_actor_a_00"))

        source = PROXY_PATH.read_text(encoding="utf-8")
        self.assertIn("obj.hide_render = True", source)
        self.assertNotIn("bpy.data.objects.remove(obj", source)

    def test_render_profile_is_immutable_and_explicitly_consumed(self):
        profile = self.proxy.RenderProfile("clay", 6, (160, 90))
        self.assertEqual(profile.as_dict(), {"fps": 6, "resolution": [160, 90]})
        with self.assertRaises(FrozenInstanceError):
            profile.fps = 12

        source = PROXY_PATH.read_text(encoding="utf-8")
        self.assertNotIn("_ACTIVE_RENDER_STYLE", source)
        for name in (
            "create_material",
            "create_emissive_material",
            "create_environment",
            "create_actor",
            "configure_scene",
            "apply_trajectory",
            "render_outputs",
            "render_trajectory_outputs",
        ):
            with self.subTest(function=name):
                self.assertIn("profile", inspect.signature(getattr(self.proxy, name)).parameters)


@unittest.skipUnless(BLENDER.is_file(), f"Blender missing at {BLENDER}")
class BlenderClayRenderProfileIntegrationTests(unittest.TestCase):
    def test_real_clay_render_persists_and_decodes_the_effective_profile(self):
        document = json.loads(SHOT_SCRIPT_PATH.read_text(encoding="utf-8"))
        document["fps"] = 99
        document["shots"] = document["shots"][:1]
        document["shots"][0]["duration"] = 1.0

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            shotscript = directory / "short_shotscript.json"
            shotscript.write_text(json.dumps(document), encoding="utf-8")
            output = directory / "clay"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER_PATH),
                    "--blender",
                    str(BLENDER),
                    "--shotscript",
                    str(shotscript),
                    "--output-dir",
                    str(output),
                    "--render-style",
                    "clay",
                    "--fps",
                    "6",
                    "--resolution",
                    "160x90",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("BLENDER_PROXY_OK", completed.stdout + completed.stderr)

            report = json.loads(
                (output / "trajectory_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["render_style"], "clay")
            self.assertEqual(
                report["effective_profile"],
                {"fps": 6, "resolution": [160, 90]},
            )
            self.assertEqual(report["scene_frame_end"], 6)
            self.assertEqual(report["rendered_frames"], 6)

            reader = imageio_ffmpeg.read_frames(
                str(output / "station_proxy.mp4"), pix_fmt="rgb24"
            )
            try:
                metadata = next(reader)
                frame_count = sum(1 for _frame in reader)
            finally:
                reader.close()
            self.assertEqual(tuple(metadata["size"]), (160, 90))
            self.assertAlmostEqual(float(metadata["fps"]), 6.0, places=2)
            self.assertEqual(frame_count, 6)

            probe_script = """
import bpy
import json

materials = {}
all_neutral = True
for material in bpy.data.materials:
    diffuse = [float(value) for value in material.diffuse_color]
    base = [float(value) for value in material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value]
    materials[material.name] = {"diffuse": diffuse, "base": base}
    for color in (diffuse, base):
        all_neutral = all_neutral and abs(color[0] - color[1]) < 1e-6 and abs(color[1] - color[2]) < 1e-6

hidden = {
    obj.name: bool(obj.hide_render)
    for obj in bpy.data.objects
    if obj.name == "action_axis" or obj.name.endswith("_label")
}
payload = {
    "all_neutral": all_neutral,
    "materials": materials,
    "hidden": hidden,
    "frame_end": bpy.context.scene.frame_end,
}
print("BLENDER_CLAY_PROBE=" + json.dumps(payload, sort_keys=True))
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
                if line.startswith("BLENDER_CLAY_PROBE=")
            )
            probe = json.loads(line.partition("=")[2])
            self.assertTrue(probe["all_neutral"])
            self.assertGreater(len(probe["materials"]), 0)
            self.assertNotEqual(
                probe["materials"]["actor_a_mat"]["diffuse"][0],
                probe["materials"]["actor_b_mat"]["diffuse"][0],
            )
            self.assertEqual(
                probe["hidden"],
                {
                    "action_axis": True,
                    "actor_a_label": True,
                    "actor_b_label": True,
                },
            )
            self.assertEqual(probe["frame_end"], 6)


if __name__ == "__main__":
    unittest.main()
