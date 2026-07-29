"""Task 5 real Blender trajectory proxy integration test.

Run: ``& $PY -m unittest tests.test_trajectory_proxy_integration -v``.
Inputs are the real ``D:\blender\blender.exe``,
``examples/station_shotscript.json``, and
``runs/trajectory/s01/trajectory.json``. The test launches Blender, renders a
real H.264 MP4 plus Blender PNG frames, and checks input hashes and applied
camera/actor keyframes. It does not call an API and is not a Pillow-only fake.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import imageio_ffmpeg
from PIL import Image

from videoactagent.trajectory_proxy import camera_world_xy, signed_turn_orientation


BLENDER = Path(r"D:\blender\blender.exe")
SHOT_SCRIPT = Path("examples/station_shotscript.json")
TRAJECTORY = Path("runs/trajectory/s01/trajectory.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_proxy(output: Path, blender: Path = BLENDER, timeout_seconds: int = 240):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "videoactagent.trajectory_proxy",
            "--blender",
            str(blender),
            "--shotscript",
            str(SHOT_SCRIPT),
            "--trajectory",
            str(TRAJECTORY),
            "--output-dir",
            str(output),
            "--timeout-seconds",
            str(timeout_seconds),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(30, timeout_seconds + 30),
    )


def _inspect_blend(blend: Path, output: Path, script_path: Path) -> dict:
    script_path.write_text(
        "import bpy, json, sys\n"
        "from pathlib import Path\n"
        "argv=sys.argv[sys.argv.index('--')+1:]\n"
        "out=Path(argv[argv.index('--output')+1])\n"
        "scene=bpy.context.scene\n"
        "camera=bpy.data.objects['DirectorCamera']\n"
        "actor=bpy.data.objects['actor_a']\n"
        "camera_frames=[1,3,5,6,8,10,11,13,15]\n"
        "actor_frames=[1,8,15]\n"
        "def positions(obj, frames):\n"
        " result=[]\n"
        " for frame in frames:\n"
        "  scene.frame_set(frame)\n"
        "  result.append([round(float(v),6) for v in obj.matrix_world.translation])\n"
        " return result\n"
        "curves=sorted(o.name for o in bpy.data.objects if o.name.startswith('TrajectoryCurve_'))\n"
        "labels={o.name:o.data.body for o in bpy.data.objects if o.name.startswith('TrajectoryLabel_')}\n"
        "payload={'camera_frames':camera_frames,'camera_positions':positions(camera,camera_frames),'actor_frames':actor_frames,'actor_positions':positions(actor,actor_frames),'curves':curves,'labels':labels}\n"
        "out.write_text(json.dumps(payload,sort_keys=True),encoding='utf-8')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(BLENDER),
            "--background",
            str(blend),
            "--python",
            str(script_path),
            "--",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    return json.loads(output.read_text(encoding="utf-8"))


class TrajectoryProxyIntegrationTests(unittest.TestCase):
    def test_top_left_camera_projection_preserves_declared_clockwise_direction(self):
        instruction = json.loads(TRAJECTORY.read_text(encoding="utf-8"))
        track = next(item for item in instruction["tracks"] if item["target"]["type"] == "camera")
        world_xy = [camera_world_xy(point["x"], point["y"], 0.0, 0.0) for point in track["points"]]
        self.assertLess(signed_turn_orientation(world_xy), 0.0)

    def test_nonzero_blender_exit_persists_logs_and_failure_manifest(self):
        Path("runs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path("runs").resolve()) as root:
            output = Path(root) / "nonzero"
            completed = _run_proxy(output, blender=Path(sys.executable), timeout_seconds=5)
            self.assertNotEqual(completed.returncode, 0)
            stdout_log = output / "blender.stdout.log"
            stderr_log = output / "blender.stderr.log"
            failure_path = output / "failure_manifest.json"
            for path in (stdout_log, stderr_log, failure_path):
                self.assertTrue(path.is_file(), f"missing failure evidence: {path}")
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertFalse(failure["timed_out"])
            self.assertNotEqual(failure["exit_code"], 0)
            self.assertEqual(failure["logs"]["stdout"]["sha256"], _sha256(stdout_log))
            self.assertEqual(failure["logs"]["stderr"]["sha256"], _sha256(stderr_log))

    def test_timeout_persists_captured_logs_and_failure_manifest(self):
        Path("runs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path("runs").resolve()) as root:
            output = Path(root) / "timeout"
            completed = _run_proxy(output, timeout_seconds=1)
            self.assertNotEqual(completed.returncode, 0)
            failure_path = output / "failure_manifest.json"
            self.assertTrue(failure_path.is_file())
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertTrue(failure["timed_out"])
            self.assertIsNone(failure["exit_code"])
            for name in ("blender.stdout.log", "blender.stderr.log"):
                self.assertTrue((output / name).is_file())

    def test_real_blender_renders_hash_bound_trajectory_proxy(self):
        self.assertTrue(BLENDER.is_file(), f"Blender missing at {BLENDER}")
        self.assertTrue(SHOT_SCRIPT.is_file(), f"missing {SHOT_SCRIPT}")
        self.assertTrue(TRAJECTORY.is_file(), f"missing {TRAJECTORY}")

        Path("runs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path("runs").resolve()) as root:
            output = Path(root) / "trajectory_proxy"
            completed = _run_proxy(output)
            evidence = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 0, evidence)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertIn("TRAJECTORY_PROXY_OK", evidence)
            self.assertFalse(
                list(output.parent.glob(f".{output.name}.*.tmp")),
                "staging directory remained visible after atomic publication",
            )

            video = output / "trajectory_proxy.mp4"
            blend = output / "trajectory_proxy.blend"
            manifest_path = output / "trajectory_proxy_manifest.json"
            frame_paths = [
                output / "frames" / "first.png",
                output / "frames" / "middle.png",
                output / "frames" / "last.png",
            ]
            overlay = output / "trajectory_overlay.png"
            stdout_log = output / "blender.stdout.log"
            stderr_log = output / "blender.stderr.log"
            for path in [video, blend, manifest_path, overlay, *frame_paths]:
                self.assertTrue(path.is_file(), f"missing real Blender output: {path}")
                self.assertGreater(path.stat().st_size, 0, f"empty output: {path}")
            for path in (stdout_log, stderr_log):
                self.assertTrue(path.is_file(), f"missing persisted Blender log: {path}")

            frame_count, duration = imageio_ffmpeg.count_frames_and_secs(str(video))
            metadata = imageio_ffmpeg.read_frames(str(video))
            stream_meta = next(metadata)
            metadata.close()
            self.assertEqual(frame_count, 45)
            self.assertAlmostEqual(duration, 15.0, places=1)
            self.assertEqual(tuple(stream_meta["size"]), (960, 540))
            self.assertAlmostEqual(float(stream_meta["fps"]), 3.0, places=2)

            rendered_bytes = []
            for path in frame_paths:
                with Image.open(path) as image:
                    self.assertEqual(image.size, (960, 540))
                    rendered_bytes.append(image.convert("RGB").tobytes())
            self.assertNotEqual(rendered_bytes[0], rendered_bytes[1])
            self.assertNotEqual(rendered_bytes[1], rendered_bytes[2])

            with Image.open(overlay) as image:
                rgb = image.convert("RGB")
                self.assertEqual(rgb.size, (960, 540))
                bright_trajectory_pixels = sum(
                    1
                    for red, green, blue in rgb.get_flattened_data()
                    if (red > 150 and blue > 120 and red > green * 1.15)
                    or (green > 140 and blue > 140 and red < 150)
                )
            self.assertGreater(
                bright_trajectory_pixels,
                25,
                "Blender overlay does not contain visible trajectory-colour pixels",
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "0.1")
            self.assertEqual(manifest["renderer"], "blender")
            self.assertEqual(manifest["shotscript_sha256"], _sha256(SHOT_SCRIPT))
            self.assertEqual(manifest["trajectory_sha256"], _sha256(TRAJECTORY))
            self.assertEqual(manifest["video"]["sha256"], _sha256(video))
            self.assertEqual(manifest["video"]["frame_count"], 45)
            self.assertEqual(manifest["video"]["resolution"], [960, 540])
            self.assertEqual(manifest["blend"]["sha256"], _sha256(blend))
            self.assertEqual(manifest["overlay"]["sha256"], _sha256(overlay))
            for name, path in zip(("first", "middle", "last"), frame_paths):
                self.assertEqual(manifest["frames"][name]["sha256"], _sha256(path))
            self.assertEqual(manifest["logs"]["stdout"]["sha256"], _sha256(stdout_log))
            self.assertEqual(manifest["logs"]["stderr"]["sha256"], _sha256(stderr_log))
            self.assertEqual(manifest["controlled_shot"]["shot_id"], "s01")
            self.assertEqual(manifest["controlled_shot"]["frame_range"], [1, 15])
            self.assertEqual(
                manifest["applied_tracks"]["camera_circle_01"]["keyframe_count"], 9
            )
            self.assertEqual(
                manifest["applied_tracks"]["actor_path_01"]["keyframe_count"], 3
            )
            self.assertNotEqual(
                manifest["applied_tracks"]["camera_circle_01"]["first_world"],
                manifest["applied_tracks"]["camera_circle_01"]["middle_world"],
            )
            self.assertNotEqual(
                manifest["applied_tracks"]["actor_path_01"]["first_world"],
                manifest["applied_tracks"]["actor_path_01"]["last_world"],
            )
            self.assertIn("TrajectoryCurve_camera_circle_01", manifest["scene_objects"])
            self.assertIn("TrajectoryCurve_actor_path_01", manifest["scene_objects"])

            probe = _inspect_blend(
                blend,
                Path(root) / "blend_probe.json",
                Path(root) / "inspect_blend.py",
            )
            self.assertEqual(
                probe["curves"],
                ["TrajectoryCurve_actor_path_01", "TrajectoryCurve_camera_circle_01"],
            )
            self.assertEqual(len(probe["labels"]), 12)
            self.assertEqual(set(probe["labels"].values()), {str(i) for i in range(1, 10)})
            camera_xy = [position[:2] for position in probe["camera_positions"]]
            self.assertLess(signed_turn_orientation(camera_xy), 0.0)
            self.assertEqual(probe["camera_frames"], [1, 3, 5, 6, 8, 10, 11, 13, 15])
            self.assertEqual(probe["actor_frames"], [1, 8, 15])
            self.assertEqual(probe["actor_positions"][0], [-3.4, -1.44, 0.0])
            self.assertEqual(probe["actor_positions"][-1], [-0.2, -0.48, 0.0])


if __name__ == "__main__":
    unittest.main()
