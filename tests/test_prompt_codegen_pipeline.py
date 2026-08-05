from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.test_scene_plan import VALID_DRAFT
from videoactagent.prompt_codegen_pipeline import PromptCodegenRunner, PromptPipelineConfig, PromptPipelineError


SAFE_CODE = "import bpy\ndef build_scene(context):\n    return None\n"


class PromptCodegenRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.blender = root / "blender.exe"
        self.blender.write_text("fixture", encoding="utf-8")
        self.protected = root / "protected"
        self.protected.mkdir()
        (self.protected / "keep.txt").write_text("keep", encoding="utf-8")
        self.planner_calls = 0
        self.codegen_calls = 0
        self.planner_feedback: list[str | None] = []
        self.codegen_feedback: list[str | None] = []

    def config(self) -> PromptPipelineConfig:
        return PromptPipelineConfig(
            experiments_root=self.root / "experiments",
            blender=self.blender,
            protected_workspace=self.protected,
            protected_url="http://guard.test/session",
            protection_probe=lambda: {"tree_sha256": "stable", "guard_sha256": "stable"},
        )

    def planner(self, *, story_prompt, duration_seconds, output_dir, model, feedback, **kwargs):
        self.planner_calls += 1
        self.planner_feedback.append(feedback)
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "draft.json").write_text(json.dumps(VALID_DRAFT), encoding="utf-8")
        return {"status": "succeeded", "model": model, "api_call_count": 1, "retry_count": 0}

    def codegen(self, *, codegen_input, output_dir, **kwargs):
        self.codegen_calls += 1
        self.codegen_feedback.append(codegen_input.get("repair_feedback"))
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "generated_scene.py").write_text(SAFE_CODE, encoding="utf-8")
        return {"status": "succeeded", "api_call_count": 1, "retry_count": 0}

    def runner(self, *, output_dir, **kwargs):
        output = Path(output_dir)
        output.mkdir(parents=True)
        (output / "video.mp4").write_bytes(b"fixture-video")
        return {"status": "succeeded", "output_dir": str(output)}

    def verifier(self, **kwargs):
        return {"status": "succeeded", "video": {"duration_seconds": 5.0}}

    def make_runner(self, **overrides):
        values = {
            "planner": self.planner,
            "codegen": self.codegen,
            "blender_runner": self.runner,
            "verifier": self.verifier,
        }
        values.update(overrides)
        return PromptCodegenRunner(self.config(), **values)

    def test_prompt_pipeline_creates_shotscript_then_code_and_returns_video(self):
        result = self.make_runner().run("a station meeting")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(Path(result["video"]).is_file())
        self.assertTrue(Path(result["job"]).is_file())
        self.assertEqual(self.planner_calls, 1)
        self.assertEqual(self.codegen_calls, 1)
        job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
        self.assertEqual(job["model"], "deepseek-v4-pro")
        self.assertTrue((Path(result["job"]).parent / "source" / "input.json").is_file())

    def test_planner_schema_failure_retries_with_error_feedback(self):
        calls = {"count": 0}
        def planner(**kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ValueError("scene plan schema is invalid: actors")
            return self.planner(**kwargs)
        result = self.make_runner(planner=planner).run("repair the scene")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(calls["count"], 2)
        self.assertIn("scene plan schema is invalid", self.planner_feedback[0])

    def test_blender_failure_retries_codegen_with_render_log_feedback(self):
        calls = {"count": 0}
        def failing_runner(**kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                Path(kwargs["job_root"]).joinpath("render.log").write_text("camera keyframe error", encoding="utf-8")
                raise RuntimeError("Blender exited with code 1")
            return self.runner(**kwargs)
        result = self.make_runner(blender_runner=failing_runner).run("repair the render")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.codegen_calls, 2)
        self.assertIn("camera keyframe error", self.codegen_feedback[1])

    def test_retry_budget_is_three_attempts_per_stage(self):
        def planner(**kwargs):
            self.planner_calls += 1
            raise ValueError("always invalid")
        with self.assertRaises(PromptPipelineError):
            self.make_runner(planner=planner).run("always invalid")
        self.assertEqual(self.planner_calls, 3)

    def test_failure_never_returns_video_artifact(self):
        def planner(**kwargs):
            raise ValueError("always invalid")
        with self.assertRaises(PromptPipelineError) as error:
            self.make_runner(planner=planner).run("always invalid")
        self.assertTrue(error.exception.job_path)
        self.assertFalse(list(self.root.glob("experiments/PF*/renders/smoke/video.mp4")))


if __name__ == "__main__":
    unittest.main()
