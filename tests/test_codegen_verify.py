from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import imageio_ffmpeg

from videoactagent.codegen_verify import CodegenVerifyError, verify_codegen_render


class CodegenVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / "input.json"
        self.render = self.root / "render"
        self.render.mkdir()
        self._write_input()
        self._write_render()

    def _write_input(self):
        self.document = {
            "schema_version": "blender-codegen-input-1.0",
            "render_contract": {"fps": 4, "resolution": [32, 24], "frame_start": 1, "frame_end": 4, "duration_seconds": 1.0, "world_bounds": [-1, 1, -1, 1]},
            "shotscript": {"shots": [{"actors": [{"id": "actor_a"}]}]},
            "trajectory": {"tracks": [{"target": {"id": "actor_a"}, "points": [{"keyframe_id": "K0", "t": 0.0, "world": [-0.5, 0.0]}, {"keyframe_id": "K1", "t": 0.2, "world": [-0.25, 0.0]}, {"keyframe_id": "K2", "t": 0.5, "world": [0.0, 0.0]}, {"keyframe_id": "K3", "t": 0.8, "world": [0.25, 0.0]}, {"keyframe_id": "K4", "t": 1.0, "world": [0.5, 0.0]}]}]},
        }
        self.input.write_text(json.dumps(self.document), encoding="utf-8")

    def _write_render(self):
        video = self.render / "video.mp4"
        writer = imageio_ffmpeg.write_frames(str(video), (32, 24), fps=4, codec="libx264", pix_fmt_in="rgb24", macro_block_size=1)
        writer.send(None)
        for index in range(4):
            writer.send(bytes([index * 50, 40, 120]) * (32 * 24))
        writer.close()
        blend = self.render / "scene.blend"; blend.write_bytes(b"blend")
        frames = self.render / "frames"; frames.mkdir()
        for name in ("first", "middle", "last"):
            (frames / f"{name}.png").write_bytes(name.encode())
        def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "blender-codegen-manifest-1.0",
            "input_sha256": sha(self.input), "code_sha256": "c" * 64,
            "fps": 4, "resolution": [32, 24], "frame_start": 1, "frame_end": 4,
            "video": {"path": "video.mp4", "sha256": sha(video), "bytes": video.stat().st_size},
            "blend": {"path": "scene.blend", "sha256": sha(blend), "bytes": blend.stat().st_size},
            "frames": {name: {"path": f"frames/{name}.png", "sha256": sha(frames / f"{name}.png"), "bytes": (frames / f"{name}.png").stat().st_size} for name in ("first", "middle", "last")},
            "actor_transforms": {"actor_a": {key: {"frame": frame, "expected_world": expected, "observed": [expected[0], expected[1], 0.8]} for key, frame, expected in (("K0", 1, [-0.5, 0.0]), ("K2", 3, [0.0, 0.0]), ("K4", 4, [0.5, 0.0]))}},
        }
        (self.render / "codegen_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_decodes_and_checks_real_mp4_and_trajectory(self):
        value = verify_codegen_render(job_root=self.root, input_path=self.input, render_dir=self.render)
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["video"]["frame_count"], 4)
        self.assertLessEqual(value["trajectory"]["max_error"], 0.15)
        self.assertEqual(len(value["video"]["sampled_pixel_sha256"]), 3)

    def test_rejects_bad_trajectory_and_unsafe_artifact(self):
        manifest = json.loads((self.render / "codegen_manifest.json").read_text())
        manifest["actor_transforms"]["actor_a"]["K2"]["observed"][0] = 3.0
        (self.render / "codegen_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(CodegenVerifyError, "trajectory"):
            verify_codegen_render(job_root=self.root, input_path=self.input, render_dir=self.render)

    def test_rejects_uniform_dark_video(self):
        video = self.render / "video.mp4"
        writer = imageio_ffmpeg.write_frames(str(video), (32, 24), fps=4, codec="libx264", pix_fmt_in="rgb24", macro_block_size=1)
        writer.send(None)
        for index in range(4):
            value = 50 + (index % 2)
            writer.send(bytes([value, value, value]) * (32 * 24))
        writer.close()
        manifest = json.loads((self.render / "codegen_manifest.json").read_text())
        manifest["video"] = {"path": "video.mp4", "sha256": hashlib.sha256(video.read_bytes()).hexdigest(), "bytes": video.stat().st_size}
        (self.render / "codegen_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(CodegenVerifyError, "visual"):
            verify_codegen_render(job_root=self.root, input_path=self.input, render_dir=self.render)


if __name__ == "__main__":
    unittest.main()
