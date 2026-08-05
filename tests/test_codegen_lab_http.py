from __future__ import annotations

import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from videoactagent.codegen_lab import CodegenLabApplication, CodegenLabConfig, create_server


class CodegenLabHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.blender = self.root / "blender.exe"
        self.blender.write_text("fixture", encoding="utf-8")
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "codegen_lab.html").write_text("<html>Codegen Lab</html>", encoding="utf-8")
        self.calls: list[str] = []

        def preparer(**kwargs):
            self.calls.append("prepare")
            job_root = Path(kwargs["experiments_root"]) / "CG1"
            job_root.mkdir(parents=True)
            (job_root / "job.json").write_text(json.dumps({"job_id": "CG1", "status": "prepared", "api_call_count": 0, "retry_count": 0, "paths": {"job_root": str(job_root)}}), encoding="utf-8")
            return job_root / "job.json"

        def generator(job_path, **kwargs):
            self.calls.append("generate")
            return {"status": "code_validated", "api_call_count": 1}

        def renderer(job_path, **kwargs):
            self.calls.append("render")
            return {"status": "smoke_succeeded", "profile": kwargs["profile"]}

        config = CodegenLabConfig(self.root, self.blender, port=0, static_root_path=self.static)
        app = CodegenLabApplication(config, prepare_fn=preparer, generate_fn=generator, render_fn=renderer)
        self.server = create_server(config, application=app)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 2)

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None, headers: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()

    def prepare_payload(self):
        return {"protected_workspace": str(self.root), "protected_url": "http://127.0.0.1:8770/api/session", "prompt": str(self.root / "prompt.txt"), "shotscript": str(self.root / "shot.json"), "trajectory": str(self.root / "trajectory.json")}

    def test_root_and_health_are_available(self):
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Codegen Lab", body)
        status, _, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_prepare_returns_zero_api_calls(self):
        status, _, body = self.request("/api/prepare", method="POST", payload=self.prepare_payload())
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual(self.calls, ["prepare"])

    def test_generate_returns_202_and_can_be_polled(self):
        _, _, body = self.request("/api/prepare", method="POST", payload=self.prepare_payload())
        job = json.loads(body)["job_id"]
        job_path = str(self.root / "runs" / "work" / "codegen_blender_v1" / job / "job.json")
        status, _, body = self.request("/api/generate", method="POST", payload={"job": job_path})
        operation = json.loads(body)
        self.assertEqual(status, 202)
        self.assertIn(operation["state"], {"queued", "running", "done"})
        status, _, body = self.request(f"/api/status?job={job_path}&operation={operation['operation_id']}")
        self.assertEqual(status, 200)
        self.assertIn("operation", json.loads(body))

    def test_render_accepts_only_smoke_or_full(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/render", method="POST", payload={"job": "x", "profile": "fast"})
        self.assertEqual(raised.exception.code, 400)

    def test_artifact_supports_video_range_requests(self):
        _, _, body = self.request("/api/prepare", method="POST", payload=self.prepare_payload())
        job = json.loads(body)["job_id"]
        job_path = self.root / "runs" / "work" / "codegen_blender_v1" / job / "job.json"
        video = job_path.parent / "clip.mp4"
        video.write_bytes(b"0123456789")
        status, headers, body = self.request(f"/api/artifact?job={job_path}&path=clip.mp4", headers={"Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

    def test_artifact_rejects_parent_traversal_and_foreign_jobs(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/artifact?job=C:/not-a-job/job.json&path=../secret.txt")
        self.assertEqual(raised.exception.code, 400)

    def test_errors_never_contain_api_keys(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/artifact?job=x&path=../secret-DO_NOT_RETURN.txt")
        body = raised.exception.read().decode("utf-8")
        self.assertNotIn("DO_NOT_RETURN", body)


if __name__ == "__main__":
    unittest.main()
