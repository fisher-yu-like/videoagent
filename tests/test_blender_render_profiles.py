"""Unit coverage for Blender render profiles without launching Blender."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
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
TRAJECTORY_PATH = ROOT / "examples" / "trajectory_circle_s01.json"


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
        self.assertEqual(args.timeout, 180)

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
                    "--timeout",
                    "7",
                )
            )

        self.assertEqual(result, 0)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--render-style") + 1], "clay")
        self.assertEqual(command[command.index("--fps") + 1], "12")
        self.assertEqual(command[command.index("--resolution") + 1], "1280x720")
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_runner_forwards_camera_trajectory_with_actor_trajectory(self):
        completed = types.SimpleNamespace(
            returncode=0, stdout="TRAJECTORY_PROXY_OK\n", stderr=""
        )
        with mock.patch.object(
            blender_runner.subprocess, "run", return_value=completed
        ) as run:
            result = blender_runner.main(
                self.runner_args(
                    "--trajectory", "actors.json",
                    "--camera-trajectory", "camera.json",
                )
            )

        self.assertEqual(result, 0)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--trajectory") + 1], str(Path("actors.json").resolve()))
        self.assertEqual(
            command[command.index("--camera-trajectory") + 1],
            str(Path("camera.json").resolve()),
        )

    def test_runner_timeout_returns_124_and_preserves_partial_output(self):
        error = subprocess.TimeoutExpired(
            ["blender.exe"], 7, output="partial stdout\n", stderr="partial stderr\n"
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(blender_runner.subprocess, "run", side_effect=error), \
            redirect_stdout(stdout), redirect_stderr(stderr):
            result = blender_runner.main(self.runner_args("--timeout", "7"))

        self.assertEqual(result, 124)
        self.assertIn("partial stdout", stdout.getvalue())
        self.assertIn("partial stderr", stderr.getvalue())
        self.assertIn("BLENDER_RUNNER_TIMEOUT", stderr.getvalue())

    def test_invalid_profile_is_rejected_before_blender_launch(self):
        invalid_cases = (
            ("--render-style", "painted"),
            ("--fps", "0"),
            ("--fps", "-1"),
            ("--fps", "2.5"),
            ("--resolution", "1280"),
            ("--resolution", "0x720"),
            ("--resolution", "1280X720"),
            ("--timeout", "0"),
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

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.proxy.parse_args(
                self.proxy_args("--camera-trajectory", "camera.json")
            )

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
        self.assertTrue(self.proxy.hide_in_clay("TrajectoryCurve_camera_orbit"))
        self.assertTrue(self.proxy.hide_in_clay("TrajectoryPoint_actor_a_01"))
        self.assertTrue(self.proxy.hide_in_clay("TrajectoryLabel_actor_a_00"))
        self.assertFalse(self.proxy.hide_in_clay("actor_a__torso"))

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

    def test_generic_environment_builds_fixed_neutral_scene(self):
        profile = self.proxy.RenderProfile("diagnostic", 3, (160, 90))
        fill = types.SimpleNamespace(data=types.SimpleNamespace())
        fake_bpy = types.SimpleNamespace(
            ops=types.SimpleNamespace(
                object=types.SimpleNamespace(light_add=mock.Mock())
            ),
            context=types.SimpleNamespace(object=fill),
        )
        cubes = []
        with mock.patch.object(self.proxy, "bpy", fake_bpy), mock.patch.object(
            self.proxy, "create_material", side_effect=("floor", "wall", "post")
        ), mock.patch.object(
            self.proxy, "add_cube", side_effect=lambda name, *args: cubes.append(name)
        ), mock.patch.object(self.proxy, "create_action_axis") as action_axis:
            self.proxy.ENVIRONMENT_BUILDERS["generic"](profile)

        self.assertEqual(cubes, [
            "generic_ground",
            "generic_back_wall",
            "generic_boundary_post_0",
            "generic_boundary_post_1",
            "generic_boundary_post_2",
            "generic_boundary_post_3",
        ])
        fake_bpy.ops.object.light_add.assert_called_once_with(
            type="AREA", location=(0, 0, 6)
        )
        self.assertEqual(fill.name, "GenericNeutralFill")
        self.assertEqual(fill.data.color, (1.0, 1.0, 1.0))
        action_axis.assert_called_once_with(profile)

    def test_every_shot_must_round_to_at_least_one_effective_frame(self):
        profile = self.proxy.RenderProfile("diagnostic", 6, (160, 90))
        script = types.SimpleNamespace(
            shots=(
                types.SimpleNamespace(shot_id="s01", duration=1.0),
                types.SimpleNamespace(shot_id="s02", duration=0.01),
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            r"s02.*0\.01.*6 fps.*0 frames",
        ):
            self.proxy.validate_shot_frame_counts(script, profile)

    def test_top_left_trajectory_y_decodes_back_to_original_world_direction(self):
        point = types.SimpleNamespace(x=0.2, y=0.75)
        with mock.patch.object(self.proxy, "Vector", side_effect=lambda value: value):
            world = self.proxy._actor_world(point, (-5.0, 5.0, -4.0, 4.0))
        self.assertEqual(world, (-3.0, -2.0, 0.0))


@unittest.skipUnless(BLENDER.is_file(), f"Blender missing at {BLENDER}")
class BlenderClayRenderProfileIntegrationTests(unittest.TestCase):
    def write_short_inputs(self, directory: Path) -> tuple[Path, Path]:
        document = json.loads(SHOT_SCRIPT_PATH.read_text(encoding="utf-8"))
        document["fps"] = 99
        document["shots"] = document["shots"][:1]
        document["shots"][0]["duration"] = 1.0
        shotscript = directory / "short_shotscript.json"
        shotscript.write_text(json.dumps(document), encoding="utf-8")

        trajectory_document = json.loads(TRAJECTORY_PATH.read_text(encoding="utf-8"))
        trajectory_document["duration_seconds"] = 1.0
        trajectory_document["sample_count"] = 7
        trajectory_document["tracks"] = [
            track for track in trajectory_document["tracks"]
            if track["target"]["type"] == "actor"
        ]
        trajectory = directory / "short_trajectory.json"
        trajectory.write_text(json.dumps(trajectory_document), encoding="utf-8")
        return shotscript, trajectory

    def write_camera_input(self, directory: Path) -> Path:
        states = []
        times = [0.0, 0.2, 0.5, 0.8, 1.0]
        positions = [
            [0.0, -10.0, 6.0], [0.25, -9.8, 6.1], [0.5, -9.6, 6.2],
            [0.75, -9.4, 6.1], [1.0, -9.2, 6.0],
        ]
        focals = [35.0, 40.0, 50.0, 42.0, 35.0]
        for index, (time, position, focal) in enumerate(zip(times, positions, focals)):
            states.append({
                "keyframe_id": f"K{index}", "t": time,
                "position": position, "look_at": [0.5, 0.0, 1.0],
                "focal_length_mm": focal, "shot_size": "wide",
                "interpolation": "linear", "roll_degrees": 0.0,
            })
        path = directory / "camera_trajectory.json"
        path.write_text(json.dumps({
            "schema_version": "1.0", "scene_id": "station_platform", "shot_id": "s01",
            "duration_seconds": 1.0, "states": states,
        }), encoding="utf-8")
        return path

    def test_real_authored_camera_trajectory_changes_transform_and_lens(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            shotscript, trajectory = self.write_short_inputs(directory)
            camera = self.write_camera_input(directory)
            output = directory / "directed"
            completed = subprocess.run(
                [
                    sys.executable, str(RUNNER_PATH), "--blender", str(BLENDER),
                    "--shotscript", str(shotscript), "--trajectory", str(trajectory),
                    "--camera-trajectory", str(camera), "--output-dir", str(output),
                    "--fps", "6", "--resolution", "160x90",
                ],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            manifest = json.loads(
                (output / "trajectory_proxy_manifest.json").read_text(encoding="utf-8")
            )
            applied = manifest["applied_camera_trajectory"]
            self.assertEqual([item["keyframe_id"] for item in applied], [f"K{i}" for i in range(5)])
            self.assertEqual(applied[2]["frame"], 3)
            self.assertEqual(applied[2]["focal_length_mm"], 50.0)

            expression = (
                "import bpy,json; s=bpy.context.scene; c=bpy.data.objects['DirectorCamera']; "
                "rows=[]; "
                "[(s.frame_set(f),rows.append({'frame':f,'position':[round(float(v),3) for v in c.matrix_world.translation],'lens':round(float(c.data.lens),3)})) for f in [1,2,3,5,6]]; "
                "print('DIRECTOR_CAMERA_PROBE='+json.dumps(rows))"
            )
            probed = subprocess.run(
                [str(BLENDER), "--background", str(output / "trajectory_proxy.blend"),
                 "--python-expr", expression],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            self.assertEqual(probed.returncode, 0, probed.stdout + probed.stderr)
            line = next(line for line in (probed.stdout + probed.stderr).splitlines()
                        if line.startswith("DIRECTOR_CAMERA_PROBE="))
            rows = json.loads(line.partition("=")[2])
            self.assertEqual(rows[0]["position"], [0.0, -10.0, 6.0])
            self.assertEqual(rows[2]["position"], [0.5, -9.6, 6.2])
            self.assertEqual(rows[2]["lens"], 50.0)
            self.assertEqual(rows[-1]["position"], [1.0, -9.2, 6.0])

    def test_camera_keeps_looking_at_fixed_target_across_angle_wrap(self):
        expression = (
            "import bpy,json,sys; "
            f"sys.path.insert(0,{str(ROOT)!r}); "
            "from mathutils import Vector; "
            "from videoactagent.blender_proxy import apply_camera_trajectory; "
            "from videoactagent.director_annotation import CameraKeyframe; "
            "data=bpy.data.cameras.new('WrapCameraData'); "
            "camera=bpy.data.objects.new('WrapCamera',data); "
            "bpy.context.collection.objects.link(camera); "
            "target=(0.0,0.0,1.0); "
            "states=tuple(CameraKeyframe(f'K{i}',t,p,target,35.0,'wide','linear',0.0) "
            "for i,(t,p) in enumerate(((0.0,(0.0,-4.0,2.0)),"
            "(0.5,(0.0,4.0,2.0)),(1.0,(-4.0,0.0,2.0))))); "
            "apply_camera_trajectory(states,camera,1,9); "
            "scene=bpy.context.scene; rows=[]; "
            "[(scene.frame_set(frame),rows.append(round((camera.matrix_world.to_quaternion()@Vector((0,0,-1))).dot((Vector(target)-camera.matrix_world.translation).normalized()),6))) for frame in range(1,10)]; "
            "print('CAMERA_ALIGNMENT='+json.dumps(rows))"
        )
        completed = subprocess.run(
            [str(BLENDER), "--background", "--python-expr", expression],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        line = next(
            line for line in (completed.stdout + completed.stderr).splitlines()
            if line.startswith("CAMERA_ALIGNMENT=")
        )
        alignments = json.loads(line.partition("=")[2])
        self.assertGreater(min(alignments), 0.999)

    def test_real_clay_render_persists_and_decodes_the_effective_profile(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            shotscript, _trajectory = self.write_short_inputs(directory)
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
            self.assertEqual(report["actor_geometry_profile"], "humanoid_v1")
            self.assertEqual(
                report["required_actor_parts"],
                sorted(
                    [
                        "head", "torso", "pelvis",
                        "upper_arm.L", "lower_arm.L", "upper_arm.R", "lower_arm.R",
                        "upper_leg.L", "lower_leg.L", "upper_leg.R", "lower_leg.R",
                    ]
                ),
            )

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

    def test_real_trajectory_overlays_follow_style_without_changing_animation(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            shotscript, trajectory = self.write_short_inputs(directory)
            profiles = {}
            for style in ("diagnostic", "clay"):
                output = directory / style
                completed = subprocess.run(
                    [
                        str(BLENDER),
                        "--background",
                        "--factory-startup",
                        "-F",
                        "FFMPEG",
                        "--python",
                        str(PROXY_PATH),
                        "--",
                        "--shotscript",
                        str(shotscript),
                        "--trajectory",
                        str(trajectory),
                        "--output-dir",
                        str(output),
                        "--render-style",
                        style,
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
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                self.assertIn(
                    "TRAJECTORY_PROXY_OK",
                    completed.stdout + completed.stderr,
                )

                probe_script = """
import bpy
import json

scene = bpy.context.scene

def rounded(values):
    return [round(float(value), 6) for value in values]

def transform_samples(obj):
    samples = []
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame)
        samples.append({
            "frame": frame,
            "location": rounded(obj.matrix_world.translation),
            "rotation": rounded(obj.rotation_euler),
        })
    return samples

def keyframes(owner):
    action = owner.animation_data.action if owner.animation_data else None
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
    return sorted(
        (
            curve.data_path,
            curve.array_index,
            [[round(float(value), 6) for value in point.co] for point in curve.keyframe_points],
        )
        for curve in curves
    )

camera = bpy.data.objects["DirectorCamera"]
actors = [bpy.data.objects[name] for name in ("actor_a", "actor_b")]
actor_objects = sorted(
    (
        obj for obj in bpy.data.objects
        if obj.name in {"actor_a", "actor_b", "actor_a_label", "actor_b_label"}
        or obj.name.startswith(("actor_a__", "actor_b__"))
    ),
    key=lambda obj: obj.name,
)
payload = {
    "overlays": {
        obj.name: bool(obj.hide_render)
        for obj in bpy.data.objects
        if obj.name.startswith(("TrajectoryCurve_", "TrajectoryPoint_", "TrajectoryLabel_"))
    },
    "transforms": {
        "camera": transform_samples(camera),
        **{obj.name: transform_samples(obj) for obj in actor_objects},
    },
    "keyframes": {
        "camera": keyframes(camera),
        "camera_data": keyframes(camera.data),
        **{obj.name: keyframes(obj) for obj in actor_objects},
    },
}
print("BLENDER_STYLE_PROBE=" + json.dumps(payload, sort_keys=True))
"""
                probed = subprocess.run(
                    [
                        str(BLENDER),
                        "--background",
                        str(output / "trajectory_proxy.blend"),
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
                evidence = probed.stdout + probed.stderr
                lines = [
                    line
                    for line in evidence.splitlines()
                    if line.startswith("BLENDER_STYLE_PROBE=")
                ]
                self.assertTrue(lines, evidence)
                line = lines[0]
                profiles[style] = json.loads(line.partition("=")[2])

            diagnostic = profiles["diagnostic"]
            clay = profiles["clay"]
            self.assertTrue(diagnostic["overlays"])
            self.assertEqual(set(diagnostic["overlays"]), set(clay["overlays"]))
            self.assertTrue(all(not hidden for hidden in diagnostic["overlays"].values()))
            self.assertTrue(all(clay["overlays"].values()))
            self.assertEqual(diagnostic["transforms"], clay["transforms"])
            self.assertEqual(diagnostic["keyframes"], clay["keyframes"])
            self.assertTrue(diagnostic["keyframes"]["camera"])
            self.assertTrue(diagnostic["keyframes"]["actor_a"])
            self.assertTrue(diagnostic["keyframes"]["actor_a__anchor__upper_arm.L"])
            self.assertTrue(diagnostic["keyframes"]["actor_a_label"])
            manifest = json.loads(
                (directory / "diagnostic" / "trajectory_proxy_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["actor_geometry_profile"], "humanoid_v1")
            self.assertEqual(manifest["required_actor_parts"], sorted(manifest["required_actor_parts"]))


if __name__ == "__main__":
    unittest.main()
