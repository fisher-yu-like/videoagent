import copy
import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.test_director_wizard import VALID_DRAFT


class DirectorWizardHttpTests(unittest.TestCase):
    def setUp(self):
        from videoactagent.director_wizard import create_server, create_workspace

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.blender = self.root / "blender.exe"
        self.blender.write_bytes(b"test blender executable")
        self.manifest = create_workspace(self.blender, self.root / "wizard")
        self.server = create_server(self.manifest, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def approve_reference_pipeline(self):
        from videoactagent import director_multicam
        from videoactagent.director_wizard import (
            approve_reference,
            approve_scene_plan,
            render_reference,
            save_scene_plan,
        )

        save_scene_plan(
            self.manifest, VALID_DRAFT, prompt="delegation test story", parent=None
        )
        approve_scene_plan(self.manifest, "SP1", author_id="human")

        def prepare_without_blender(*args, **kwargs):
            def renderer(*, blender, shotscript, output_dir, fps, resolution):
                del blender, shotscript, fps, resolution
                output_dir.mkdir(parents=True, exist_ok=False)
                video = output_dir / "reference.mp4"
                video.write_bytes(b"delegation reference video")
                return video

            kwargs["reference_renderer"] = renderer
            return director_multicam.prepare_workspace(*args, **kwargs)

        with patch("videoactagent.director_wizard.prepare_workspace", prepare_without_blender):
            render_reference(self.manifest, "SP1")
        approve_reference(self.manifest, "R1", author_id="human")

    def test_http_wizard_flow_uses_async_reference_job_then_delegates_staging(self):
        from videoactagent import director_multicam

        original_prepare = director_multicam.prepare_workspace

        def fake_planner(**kwargs):
            output = Path(kwargs["output_dir"])
            output.mkdir(parents=True, exist_ok=False)
            (output / "draft.json").write_text(
                json.dumps(VALID_DRAFT, ensure_ascii=False), encoding="utf-8"
            )
            evidence = {
                "schema_version": "1.0",
                "status": "succeeded",
                "api_call_count": 1,
                "retry_count": 0,
            }
            (output / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            return evidence

        def prepare_without_blender(*args, **kwargs):
            def renderer(*, blender, shotscript, output_dir, fps, resolution):
                del blender, shotscript, fps, resolution
                output_dir.mkdir(parents=True, exist_ok=False)
                video = output_dir / "reference.mp4"
                video.write_bytes(b"reference video")
                return video

            kwargs["reference_renderer"] = renderer
            return original_prepare(*args, **kwargs)

        with patch("videoactagent.director_wizard.request_scene_plan", fake_planner):
            status, generated = self.request(
                "POST",
                "/api/scene-plans",
                {"story_prompt": "新故事", "duration_seconds": 5.0},
            )
        self.assertEqual(status, 201)
        self.assertEqual(generated["scene_plan_id"], "SP1")

        edited = copy.deepcopy(VALID_DRAFT)
        edited["explanation"] = "人工调整后的版本"
        status, saved = self.request(
            "POST",
            "/api/scene-plans/SP1/save",
            {
                "draft": edited,
                "story_prompt": "新故事",
                "parent_scene_plan_id": "SP1",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(saved["scene_plan_id"], "SP2")
        self.request("POST", "/api/scene-plans/SP2/approve", {"author_id": "human"})

        with patch("videoactagent.director_wizard.prepare_workspace", prepare_without_blender):
            status, queued = self.request("POST", "/api/scene-plans/SP2/render", {})
            self.assertEqual(status, 202)
            self.assertEqual(queued["job_id"], "reference-R1")
            for _unused in range(100):
                _status, job = self.request("GET", "/api/jobs/reference-R1")
                if job["status"] not in {"queued", "rendering"}:
                    break
                time.sleep(0.01)
        self.assertEqual(job["status"], "succeeded")
        self.request("POST", "/api/references/R1/approve", {"author_id": "human"})

        _status, session = self.request("GET", "/api/session")
        self.assertEqual(session["workflow_step"], "staging")
        self.assertEqual(session["approved_reference"], "R1")
        status, staging = self.request(
            "POST",
            "/api/staging",
            {
                "trajectory": session["staging"]["trajectory"],
                "base_staging_id": session["current_staging"],
                "locked_through_keyframe": None,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(staging["staging_id"], "S3")

    def test_http_errors_are_json(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("POST", "/api/not-a-route", {})
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(
            json.loads(caught.exception.read().decode("utf-8")),
            {"error": "route not found"},
        )

    def test_http_delegates_single_staging_preview_before_camera_plan(self):
        from videoactagent import director_multicam

        self.approve_reference_pipeline()
        fake_job = self.root / "preview-job.json"
        fake_job.write_text(
            json.dumps({"job_id": "preview-PV1", "preview_id": "PV1", "status": "queued"}),
            encoding="utf-8",
        )
        with (
            patch("videoactagent.director_multicam.prepare_staging_preview", return_value=fake_job) as prepare,
            patch("videoactagent.director_multicam.Thread") as thread,
        ):
            status, body = self.request("POST", "/api/staging/S1/render", {})
        self.assertEqual(status, 202)
        self.assertEqual(body["job_id"], "preview-PV1")
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[1], "S1")
        thread.return_value.start.assert_called_once_with()

    def test_null_duration_returns_json_error_without_dropping_connection(self):
        with self.assertRaises(HTTPError) as caught:
            self.request(
                "POST",
                "/api/scene-plans",
                {"story_prompt": "新故事", "duration_seconds": None},
            )
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIsInstance(error.get("error"), str)

    def test_wizard_delegates_revision_and_rerender_with_one_body_parse(self):
        self.approve_reference_pipeline()
        real_loads = json.loads
        revision_text = json.dumps({"scope": "camera_b", "feedback": "make B static"})
        revision_request = Request(
            self.base + "/api/plans/P1/revise",
            data=revision_text.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with (
            patch("videoactagent.director_multicam.revise_plan") as revise,
            patch("json.loads", wraps=real_loads) as loads,
        ):
            revise.return_value = {"plan_id": "P2", "revision_scope": "camera_b"}
            with urlopen(revision_request, timeout=5) as response:
                revision_status = response.status
                revision_body = response.read()
        self.assertEqual(
            sum(call.args and call.args[0] == revision_text for call in loads.call_args_list),
            1,
        )
        self.assertEqual(revision_status, 201)
        self.assertEqual(real_loads(revision_body)["plan_id"], "P2")
        revise.assert_called_once()
        self.assertEqual(revise.call_args.args[1], "P1")
        self.assertEqual(revise.call_args.kwargs["scope"], "camera_b")
        self.assertEqual(revise.call_args.kwargs["feedback"], "make B static")

        fake_job = self.root / "rerender-job.json"
        fake_job.write_text(
            json.dumps({"job_id": "render-M2", "status": "queued"}), encoding="utf-8"
        )
        rerender_request = Request(
            self.base + "/api/iterations/M1/rerender",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with (
            patch("videoactagent.director_multicam.prepare_rerender", return_value=fake_job) as rerender,
            patch("videoactagent.director_multicam.Thread") as thread,
            patch("json.loads", wraps=real_loads) as loads,
        ):
            with urlopen(rerender_request, timeout=5) as response:
                rerender_status = response.status
                rerender_body = response.read()
        self.assertEqual(
            sum(call.args and call.args[0] == "{}" for call in loads.call_args_list),
            1,
        )
        self.assertEqual(rerender_status, 202)
        self.assertEqual(real_loads(rerender_body)["job_id"], "render-M2")
        rerender.assert_called_once()
        self.assertEqual(rerender.call_args.args[1], "M1")
        thread.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
