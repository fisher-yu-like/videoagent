"""Unit coverage for Blender render profiles without launching Blender."""

from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from videoactagent import blender_runner


ROOT = Path(__file__).resolve().parents[1]
PROXY_PATH = ROOT / "videoactagent" / "blender_proxy.py"


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
    with mock.patch.dict(sys.modules, {"bpy": bpy, "mathutils": mathutils}):
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
        original = (0.1, 0.4, 0.9, 1.0)
        converted = self.proxy.effective_material_color("clay", original)
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

    def test_scene_scheduling_and_reports_use_the_effective_profile(self):
        source = PROXY_PATH.read_text(encoding="utf-8")
        self.assertIn("scene.render.fps = fps", source)
        self.assertIn("shot.duration * fps", source)
        self.assertNotIn("shot.duration * script.fps", source)
        self.assertIn('"render_style": render_style', source)
        self.assertIn('"effective_profile":', source)


if __name__ == "__main__":
    unittest.main()
