from __future__ import annotations

import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

from videoactagent.codegen_lab import CodegenLabApplication, CodegenLabConfig, CodegenLabError, status_document


class CodegenLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.blender = self.root / "blender.exe"
        self.blender.write_text("fixture", encoding="utf-8")
        self.calls: list[str] = []

    def test_defaults_are_derived_from_workspace(self):
        config = CodegenLabConfig(workspace=self.root, blender=self.blender)
        self.assertEqual(config.experiments_root, self.root / "runs" / "work" / "codegen_blender_v1")
        self.assertEqual(config.port, 8781)

    def test_prepare_calls_job_preparer_only(self):
        def preparer(**kwargs):
            self.calls.append("prepare")
            job_root = Path(kwargs["experiments_root"]) / "CG1"
            job_root.mkdir(parents=True)
            (job_root / "job.json").write_text(json.dumps({"job_id": "CG1", "status": "prepared", "api_call_count": 0, "retry_count": 0}), encoding="utf-8")
            return job_root / "job.json"

        def generator(*args, **kwargs): self.calls.append("generate")
        def renderer(*args, **kwargs): self.calls.append("render")
        app = CodegenLabApplication(CodegenLabConfig(self.root, self.blender), prepare_fn=preparer, generate_fn=generator, render_fn=renderer)
        payload = {
            "protected_workspace": str(self.root), "protected_url": "http://127.0.0.1:8770/api/session",
            "prompt": str(self.root / "prompt.txt"), "shotscript": str(self.root / "shotscript.json"),
            "trajectory": str(self.root / "trajectory.json"),
        }
        result = app.prepare(payload)
        self.assertEqual(result["job_id"], "CG1")
        self.assertEqual(result["job"], str(self.root / "runs" / "work" / "codegen_blender_v1" / "CG1" / "job.json"))
        self.assertEqual(self.calls, ["prepare"])

    def test_status_omits_credentials_and_protected_file_inventory(self):
        job_root = self.root / "runs" / "work" / "codegen_blender_v1" / "CG1"
        job_root.mkdir(parents=True)
        job = {
            "job_id": "CG1", "status": "api_failed", "api_call_count": 1, "retry_count": 0,
            "error": "request failed", "protected": {"tree_sha256": "tree", "files": [{"path": "secret", "sha256": "x"}], "url": "http://guard"},
            "api_evidence": {"request": {"api_key": "DO_NOT_RETURN"}}, "history": [], "paths": {"job_root": str(job_root), "api": "api", "renders": "renders"},
        }
        (job_root / "job.json").write_text(json.dumps(job), encoding="utf-8")
        result = status_document(job_root / "job.json")
        encoded = json.dumps(result)
        self.assertNotIn("DO_NOT_RETURN", encoded)
        self.assertNotIn("secret", encoded)
        self.assertEqual(result["status"], "api_failed")

    def test_artifact_path_must_remain_inside_job_root(self):
        job_root = self.root / "runs" / "work" / "codegen_blender_v1" / "CG1"
        job_root.mkdir(parents=True)
        job_path = job_root / "job.json"
        (job_root / "ok.mp4").write_bytes(b"video")
        (job_path).write_text(json.dumps({"job_id": "CG1", "status": "prepared"}), encoding="utf-8")
        app = CodegenLabApplication(CodegenLabConfig(self.root, self.blender))
        self.assertEqual(app.resolve_artifact(job_path, "ok.mp4"), job_root / "ok.mp4")
        with self.assertRaises(CodegenLabError): app.resolve_artifact(job_path, "../CG2/other.mp4")
        with self.assertRaises(CodegenLabError): app.resolve_artifact(job_path, "job.json")

    def test_duplicate_background_operation_is_rejected(self):
        job_root = self.root / "runs" / "work" / "codegen_blender_v1" / "CG1"
        job_root.mkdir(parents=True)
        job_path = job_root / "job.json"
        job_path.write_text(json.dumps({"job_id": "CG1", "status": "prepared"}), encoding="utf-8")
        started = Event()
        release = Event()
        def generate(*args, **kwargs):
            started.set()
            release.wait(2)
            return {"status": "code_validated"}
        app = CodegenLabApplication(CodegenLabConfig(self.root, self.blender), generate_fn=generate)
        first = app.start_generate(job_path)
        self.assertTrue(started.wait(2))
        with self.assertRaises(CodegenLabError): app.start_generate(job_path)
        release.set()
        app.wait_operation(first["operation_id"], timeout=2)

    def test_prompt_run_uses_prompt_runner_and_exposes_only_final_video(self):
        calls = []
        def prompt_runner(prompt):
            calls.append(prompt)
            job_root = self.root / "runs" / "work" / "codegen_blender_v1" / "PF1"
            job_root.mkdir(parents=True)
            video = job_root / "renders" / "smoke" / "video.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            job = job_root / "job.json"
            job.write_text(json.dumps({"job_id": "PF1", "status": "succeeded", "api_call_count": 2, "retry_count": 0}), encoding="utf-8")
            return {"status": "succeeded", "job": str(job), "video": str(video), "document": {"job_id": "PF1", "api_call_count": 2}}
        app = CodegenLabApplication(CodegenLabConfig(self.root, self.blender), prompt_runner=prompt_runner)
        operation = app.start_prompt_run("a station meeting")
        result = app.wait_operation(operation["operation_id"], timeout=2)
        self.assertEqual(result["state"], "done")
        self.assertEqual(calls, ["a station meeting"])
        status = app.prompt_status(operation["operation_id"])
        self.assertEqual(status["status"], "succeeded")
        self.assertNotIn("job", status)
        self.assertTrue(status["video_url"].endswith("renders/smoke/video.mp4"))

    def test_prompt_status_hides_video_until_operation_succeeds(self):
        started = Event()
        release = Event()
        def prompt_runner(prompt):
            started.set()
            release.wait(2)
            raise RuntimeError("planner failed")
        app = CodegenLabApplication(CodegenLabConfig(self.root, self.blender), prompt_runner=prompt_runner)
        operation = app.start_prompt_run("bad scene")
        self.assertTrue(started.wait(2))
        running = app.prompt_status(operation["operation_id"])
        self.assertNotIn("video_url", running)
        release.set()
        finished = app.wait_operation(operation["operation_id"], timeout=2)
        self.assertEqual(finished["state"], "error")
        failed = app.prompt_status(operation["operation_id"])
        self.assertNotIn("video_url", failed)


if __name__ == "__main__":
    unittest.main()
