import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import imageio_ffmpeg

from tests.test_director_multicam import (
    BLENDER,
    PROMPT,
    SHOTSCRIPT,
    fake_planner,
    fake_reference_renderer,
)
from videoactagent.director_multicam import (
    approve_plan,
    create_plan,
    prepare_render,
    prepare_workspace,
)


def decode_count(path):
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        count = sum(1 for _frame in reader)
    finally:
        reader.close()
    return count, float(metadata["fps"]), tuple(metadata["size"])


class MulticamBlenderIntegrationTests(unittest.TestCase):
    def test_real_blender_renders_three_synced_views_and_structure_passes(self):
        self.assertTrue(BLENDER.is_file())
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            script_value = json.loads(SHOTSCRIPT.read_text(encoding="utf-8"))
            script_value["shots"][0]["duration"] = 1.0
            shotscript = directory / "station_1s.json"
            shotscript.write_text(json.dumps(script_value), encoding="utf-8")
            manifest = prepare_workspace(
                PROMPT, shotscript, BLENDER, directory / "workspace",
                reference_renderer=fake_reference_renderer,
            )
            create_plan(manifest, planner=fake_planner)
            approve_plan(manifest, "P1", author_id="integration-test-human-gate")
            job = prepare_render(manifest, "P1")
            output = directory / "render"
            source_script = manifest.parent / "source" / "shotscript.json"
            completed = subprocess.run([
                sys.executable, "-m", "videoactagent.multicam_blender_runner",
                "--blender", str(BLENDER),
                "--shotscript", str(source_script),
                "--trajectory", str(job.parent / "input" / "actor_trajectory.json"),
                "--camera-bundle", str(job.parent / "input" / "camera_bundle.json"),
                "--output-dir", str(output),
                "--render-style", "diagnostic",
                "--fps", "3",
                "--resolution", "160x90",
                "--timeout", "240",
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=260)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            render_manifest = json.loads(
                (output / "multicam_manifest.json").read_text(encoding="utf-8")
            )
            decoded = {
                camera_id: decode_count(output / record["video"]["path"])
                for camera_id, record in render_manifest["cameras"].items()
            }
            self.assertEqual(set(decoded.values()), {(3, 3.0, (160, 90))})
            self.assertEqual(len({
                record["video"]["sha256"]
                for record in render_manifest["cameras"].values()
            }), 3)
            for record in render_manifest["cameras"].values():
                self.assertEqual(len(record["structure_frames"]), 3)
                self.assertEqual(
                    record["structure_passes"],
                    ["Depth", "CryptoObject00", "CryptoObject01", "CryptoObject02"],
                )
                for path in record["structure_frames"]:
                    self.assertGreater((output / path).stat().st_size, 0)
            self.assertEqual(len(render_manifest["shared_world_frames"]), 3)
            self.assertTrue((output / render_manifest["shared_blend"]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
