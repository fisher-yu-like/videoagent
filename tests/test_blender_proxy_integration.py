"""Stage 1 real Blender proxy integration test for ``blender_runner.py``.

Run: ``& $PY -m unittest tests.test_blender_proxy_integration -v`` (see
``docs/DEBUGGING.md``). Inputs: ``D:\\blender\\blender.exe`` and
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
            self.assertEqual(report["shots"][0]["actor_positions"]["actor_a"]["end"], [-1.0, 0.0, 0.0])
            self.assertEqual(report["shots"][1]["actor_positions"]["actor_a"]["start"], [-1.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
