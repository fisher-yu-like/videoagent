"""Real Blender integration for the handwritten codegen fixture.

This test never calls DeepSeek and makes no claim about model output quality.
It proves only that the trusted entrypoint produces a decodable, hash-bound MP4.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from videoactagent.codegen_blender_runner import run_codegen_blender
from videoactagent.codegen_contract import snapshot_codegen_inputs
from videoactagent.codegen_verify import verify_codegen_render


ROOT = Path(__file__).resolve().parents[1]
BLENDER = Path(r"D:\blender\blender.exe")


class CodegenBlenderIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(BLENDER.is_file(), f"Blender missing at {BLENDER}")
    def test_real_blender_renders_handwritten_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = json.loads((ROOT / "stories/station_reunion.json").read_text(encoding="utf-8"))
            script["fps"] = 3
            script["shots"][0]["duration"] = 1.0
            trajectory = json.loads((ROOT / "examples/station_codegen_trajectory.json").read_text(encoding="utf-8"))
            trajectory["duration_seconds"] = 1.0
            script_path = root / "shotscript.json"; script_path.write_text(json.dumps(script), encoding="utf-8")
            trajectory_path = root / "trajectory.json"; trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
            prompt_path = root / "prompt.txt"; prompt_path.write_text("station fixture", encoding="utf-8")
            source = root / "source"
            snapshot_codegen_inputs(prompt_path=prompt_path, shotscript_path=script_path, trajectory_path=trajectory_path, destination=source, fps=3, resolution=(160, 90))
            job_root = root / "job"; job_root.mkdir()
            input_path = job_root / "input.json"; shutil.copyfile(source / "input.json", input_path)
            code_path = job_root / "scene.py"; shutil.copyfile(ROOT / "tests/fixtures/codegen/safe_station_scene.py", code_path)
            output = job_root / "render"
            result = run_codegen_blender(blender=BLENDER, job_root=job_root, input_path=input_path, code_path=code_path, output_dir=output, timeout=180)
            self.assertEqual(result["status"], "succeeded")
            verified = verify_codegen_render(job_root=job_root, input_path=input_path, render_dir=output)
            self.assertEqual(verified["video"]["frame_count"], 3)
            self.assertLessEqual(verified["trajectory"]["max_error"], 0.15)
            self.assertTrue((output / "scene.blend").stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
