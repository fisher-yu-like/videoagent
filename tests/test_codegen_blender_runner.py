from __future__ import annotations

import unittest
from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
from unittest.mock import patch

from videoactagent.codegen_blender_entry import frame_for_time, world_xy
from videoactagent.codegen_blender_runner import CodegenBlenderRunnerError, run_codegen_blender
from videoactagent.codegen_safety import validate_generated_code


class CodegenBlenderEntryTests(unittest.TestCase):
    def test_frame_and_world_mapping(self):
        self.assertEqual(frame_for_time(1, 40, 0.0), 1)
        self.assertEqual(frame_for_time(1, 40, 1.0), 40)
        self.assertEqual(world_xy([-0.5, 0.5, -1.0, 1.0], 0.25, 0.25), (-0.25, 0.5))

    def test_handwritten_fixture_passes_same_gate_as_model_code(self):
        path = Path(__file__).parent / "fixtures" / "codegen" / "safe_station_scene.py"
        evidence = validate_generated_code(path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["status"], "accepted")

    def test_runner_command_and_reduced_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "input.json"; input_path.write_text("{}", encoding="utf-8")
            code_path = root / "scene.py"; code_path.write_text("x", encoding="utf-8")
            output = root / "render"
            def fake_run(command, **kwargs):
                output.mkdir()
                (output / "video.mp4").write_bytes(b"video")
                (output / "scene.blend").write_bytes(b"blend")
                frames = output / "frames"; frames.mkdir()
                for name in ("first", "middle", "last"):
                    (frames / f"{name}.png").write_bytes(name.encode())
                sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
                manifest = {"input_sha256": sha(input_path), "code_sha256": sha(code_path), "video": {"path": "video.mp4", "sha256": sha(output / "video.mp4")}, "blend": {"path": "scene.blend", "sha256": sha(output / "scene.blend")}}
                (output / "codegen_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "BLENDER_CODEGEN_OK={}\n", "")
            with patch("videoactagent.codegen_blender_runner.subprocess.run", side_effect=fake_run) as mocked:
                result = run_codegen_blender(blender=Path("D:/blender/blender.exe"), job_root=root, input_path=input_path, code_path=code_path, output_dir=output)
            command = mocked.call_args.args[0]
            self.assertIn("--background", command)
            self.assertIn("--factory-startup", command)
            self.assertIn("--python", command)
            self.assertEqual(result["input_sha256"], hashlib.sha256(input_path.read_bytes()).hexdigest())
            self.assertNotIn("DEEPSEEK_API_KEY", mocked.call_args.kwargs["env"])

    def test_runner_rejects_timeout_nonzero_marker_and_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "input.json"; input_path.write_text("{}", encoding="utf-8")
            code_path = root / "scene.py"; code_path.write_text("x", encoding="utf-8")
            output = root / "render"
            with patch("videoactagent.codegen_blender_runner.subprocess.run", side_effect=subprocess.TimeoutExpired(["b"], 1)):
                with self.assertRaisesRegex(CodegenBlenderRunnerError, "timeout"):
                    run_codegen_blender(blender=Path("b"), job_root=root, input_path=input_path, code_path=code_path, output_dir=output, timeout=1)
            for completed, text in ((subprocess.CompletedProcess([], 3, "", "boom"), "exit"), (subprocess.CompletedProcess([], 0, "", ""), "marker")):
                output = root / ("render_" + text)
                with patch("videoactagent.codegen_blender_runner.subprocess.run", return_value=completed):
                    with self.assertRaises(CodegenBlenderRunnerError):
                        run_codegen_blender(blender=Path("b"), job_root=root, input_path=input_path, code_path=code_path, output_dir=output)
            output = root / "render_bad_manifest"
            def fake_bad(*args, **kwargs):
                output.mkdir(); (output / "video.mp4").write_bytes(b"v"); (output / "scene.blend").write_bytes(b"b")
                frames = output / "frames"; frames.mkdir()
                for name in ("first", "middle", "last"): (frames / f"{name}.png").write_bytes(b"p")
                (output / "codegen_manifest.json").write_text(json.dumps({"input_sha256": "wrong", "code_sha256": "wrong"}), encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "BLENDER_CODEGEN_OK={} ", "")
            with patch("videoactagent.codegen_blender_runner.subprocess.run", side_effect=fake_bad):
                with self.assertRaisesRegex(CodegenBlenderRunnerError, "hash"):
                    run_codegen_blender(blender=Path("b"), job_root=root, input_path=input_path, code_path=code_path, output_dir=output)


if __name__ == "__main__":
    unittest.main()
