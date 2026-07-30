"""Stage 2 real Blender-to-control bridge integration test.

Run: ``& $PY -m unittest tests.test_control_bridge_integration -v`` (see
``docs/DEBUGGING.md``). Inputs: ``D:\\blender\\blender.exe`` and
``examples/station_shotscript.json``; real proxy/control media is rendered and
validated in a temporary directory. This tests Stage 2 media production, not a
remote API or downstream VACE/video-model inference result.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


BLENDER = Path(r"D:\blender\blender.exe")
BLENDER_RUNNER = Path("videoactagent/blender_runner.py")
CONTROL_BRIDGE = Path("videoactagent/control_bridge.py")
SHOT_SCRIPT = Path("examples/station_shotscript.json")


class ControlBridgeIntegrationTests(unittest.TestCase):
    def test_real_blender_exports_per_shot_control_media(self):
        self.assertTrue(BLENDER.is_file(), f"Blender missing at {BLENDER}")
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            proxy_dir = root_path / "proxy"
            stage1 = subprocess.run(
                [
                    sys.executable,
                    str(BLENDER_RUNNER),
                    "--blender",
                    str(BLENDER),
                    "--shotscript",
                    str(SHOT_SCRIPT),
                    "--output-dir",
                    str(proxy_dir),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(stage1.returncode, 0, stage1.stdout + stage1.stderr)
            self.assertIn("BLENDER_PROXY_OK", stage1.stdout + stage1.stderr)

            output_dir = root_path / "controls"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL_BRIDGE),
                    "--blender",
                    str(BLENDER),
                    "--shotscript",
                    str(SHOT_SCRIPT),
                    "--blend",
                    str(proxy_dir / "station_proxy.blend"),
                    "--output-dir",
                    str(output_dir),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=240,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertIn("CONTROL_BRIDGE_OK", completed.stdout + completed.stderr)

            for shot_id in ("s01", "s02", "s03"):
                shot_dir = output_dir / "shots" / shot_id
                first = shot_dir / "first.png"
                last = shot_dir / "last.png"
                video = shot_dir / "proxy.mp4"
                for path in (first, last, video):
                    self.assertTrue(path.is_file(), f"missing real control: {path}")
                    self.assertGreater(path.stat().st_size, 0, f"empty control: {path}")
                with Image.open(first) as image:
                    self.assertEqual(image.size, (960, 540))
                    first_pixels = image.convert("RGB").tobytes()
                with Image.open(last) as image:
                    self.assertEqual(image.size, (960, 540))
                    last_pixels = image.convert("RGB").tobytes()
                self.assertNotEqual(first_pixels, last_pixels, shot_id)

            bundle_path = output_dir / "control_bundle.json"
            self.assertTrue(bundle_path.is_file())
            bundle_text = bundle_path.read_text(encoding="utf-8")
            bundle = json.loads(bundle_text)
            self.assertEqual(bundle["schema_version"], "0.1")
            self.assertEqual(bundle["source_scene_id"], "station_platform")
            self.assertEqual(bundle["fps"], 3)
            self.assertFalse(bundle["submission_ready"])
            self.assertEqual(
                bundle["submission_blocker"],
                "public asset URLs are not configured",
            )
            self.assertEqual(
                [shot["shot_id"] for shot in bundle["shots"]],
                ["s01", "s02", "s03"],
            )
            self.assertEqual(bundle["shots"][0]["frame_range"], [1, 15])
            self.assertEqual(bundle["shots"][2]["frame_range"], [31, 45])
            self.assertIn("wide shot", bundle["shots"][0]["prompts"]["cinematic"])

            for shot in bundle["shots"]:
                for control in shot["controls"].values():
                    path = output_dir / control["path"]
                    contents = path.read_bytes()
                    self.assertEqual(control["bytes"], len(contents))
                    self.assertEqual(
                        control["sha256"],
                        hashlib.sha256(contents).hexdigest(),
                    )
            self.assertNotIn("http://", bundle_text)
            self.assertNotIn("https://", bundle_text)
            self.assertNotIn("task_id", bundle_text)


if __name__ == "__main__":
    unittest.main()
