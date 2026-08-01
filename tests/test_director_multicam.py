import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.director_multicam import (
    DirectorMulticamError,
    approve_plan,
    approve_staging,
    create_plan,
    prepare_render,
    prepare_workspace,
    run_render_job,
    save_staging,
    session_document,
)


ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "prompts" / "station_reunion.txt"
SHOTSCRIPT = ROOT / "stories" / "station_reunion.json"
BLENDER = Path(r"D:\blender\blender.exe")


def fake_reference_renderer(*, blender, shotscript, output_dir, fps, resolution):
    output_dir.mkdir(parents=True, exist_ok=False)
    video = output_dir / "reference.mp4"
    video.write_bytes(b"code-test-reference-not-experiment")
    return video


def fake_planner(*, scene_context, output_dir, environ=None):
    output_dir.mkdir(parents=True, exist_ok=False)
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["scene_id"] = scene_context["scene_id"]
    plan["locked_through_keyframe"] = scene_context["locked_through_keyframe"]
    (output_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    evidence = {
        "schema_version": "1.0", "status": "succeeded",
        "api_call_count": 1, "retry_count": 0,
    }
    (output_dir / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return evidence


class DirectorMulticamTests(unittest.TestCase):
    def prepare(self, root):
        self.assertTrue(BLENDER.is_file())
        return prepare_workspace(
            PROMPT, SHOTSCRIPT, BLENDER, Path(root) / "multicam",
            reference_renderer=fake_reference_renderer,
        )

    def approve_initial_staging(self, manifest):
        return approve_staging(
            manifest, "S1", author_id="human-staging-reviewer"
        )

    def test_prepare_is_offline_and_creates_independent_source_bound_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            session = session_document(manifest)
            self.assertEqual(session["schema_version"], "multicam-director-1.0")
            self.assertEqual(session["story_id"], "station_reunion")
            self.assertEqual(session["current_plan"], None)
            self.assertEqual(session["current_staging"], "S1")
            self.assertEqual(session["approved_staging"], None)
            self.assertEqual(len(session["staging"]["trajectory"]["tracks"]), 2)
            self.assertEqual(len(session["actor_keyframes"]), 5)
            self.assertTrue((manifest.parent / "reference" / "reference.mp4").is_file())

    def test_plan_requires_human_approval_before_render(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            with self.assertRaisesRegex(DirectorMulticamError, "staging"):
                create_plan(manifest, planner=fake_planner)
            self.approve_initial_staging(manifest)
            plan = create_plan(manifest, planner=fake_planner)
            self.assertEqual(plan["plan_id"], "P1")
            with self.assertRaisesRegex(DirectorMulticamError, "approved"):
                prepare_render(manifest, "P1")
            approval = approve_plan(manifest, "P1", author_id="human-reviewer")
            self.assertTrue(approval.is_file())
            job = prepare_render(manifest, "P1")
            document = json.loads(job.read_text(encoding="utf-8"))
            self.assertEqual(document["status"], "queued")
            self.assertEqual(document["staging_id"], "S1")
            self.assertIn("approved_staging", document["source_bindings"])
            self.assertTrue((job.parent / "input" / "camera_bundle.json").is_file())
            self.assertTrue((job.parent / "input" / "actor_trajectory.json").is_file())

    def test_plan_and_approval_are_immutable(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.approve_initial_staging(manifest)
            create_plan(manifest, planner=fake_planner)
            approve_plan(manifest, "P1", author_id="human-reviewer")
            with self.assertRaisesRegex(DirectorMulticamError, "already approved"):
                approve_plan(manifest, "P1", author_id="second-reviewer")
            with self.assertRaisesRegex(FileExistsError, "P2"):
                (manifest.parent / "plans" / "P2").mkdir()
                create_plan(manifest, planner=fake_planner)

    def test_locked_suffix_plan_remains_bound_through_render_preparation(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.approve_initial_staging(manifest)
            create_plan(
                manifest,
                locked_through_keyframe="K1",
                planner=fake_planner,
            )
            approve_plan(manifest, "P1", author_id="human-reviewer")
            job = prepare_render(manifest, "P1")
            self.assertTrue(job.is_file())

    def test_locked_suffix_plan_reaches_blender_worker_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.approve_initial_staging(manifest)
            create_plan(
                manifest,
                locked_through_keyframe="K1",
                planner=fake_planner,
            )
            approve_plan(manifest, "P1", author_id="human-reviewer")
            job = prepare_render(manifest, "P1")
            with patch("videoactagent.director_multicam.subprocess.run") as launch:
                launch.return_value.returncode = 1
                launch.return_value.stdout = ""
                launch.return_value.stderr = "controlled test stop"
                run_render_job(manifest, job)
            self.assertEqual(launch.call_count, 1)
            job_value = json.loads(job.read_text(encoding="utf-8"))
            self.assertIn("real Blender multicamera render failed", job_value["error"])

    def test_staging_suffix_is_immutable_and_invalidates_active_camera_plan(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.approve_initial_staging(manifest)
            create_plan(manifest, planner=fake_planner)
            original = session_document(manifest)["staging"]["trajectory"]
            edited = json.loads(json.dumps(original))
            edited["tracks"][0]["points"][2]["x"] += 0.05
            record = save_staging(
                manifest, edited, base_staging_id="S1", locked_through_keyframe="K1"
            )
            self.assertEqual(record["staging_id"], "S2")
            session = session_document(manifest)
            self.assertEqual(session["current_staging"], "S2")
            self.assertIsNone(session["current_plan"])
            tampered = json.loads(json.dumps(edited))
            tampered["tracks"][0]["points"][0]["x"] += 0.1
            with self.assertRaisesRegex(DirectorMulticamError, "frozen prefix"):
                save_staging(
                    manifest, tampered, base_staging_id="S2",
                    locked_through_keyframe="K1",
                )

    def test_approved_staging_drives_planner_context_and_render_input(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            trajectory = session_document(manifest)["staging"]["trajectory"]
            trajectory["tracks"].append({
                "track_id": "object_package_path",
                "target": {"type": "object", "id": "package"},
                "primitive": "polyline", "semantic": "move",
                "points": [
                    {"t": t, "x": 0.5, "y": 0.5, "visible": True}
                    for t in (0.0, 0.2, 0.5, 0.8, 1.0)
                ],
            })
            save_staging(manifest, trajectory, base_staging_id="S1")
            approve_staging(manifest, "S2", author_id="human-reviewer")
            seen = {}

            def planner(**kwargs):
                seen.update(kwargs["scene_context"])
                return fake_planner(**kwargs)

            create_plan(manifest, planner=planner)
            self.assertEqual(seen["approved_staging_id"], "S2")
            self.assertEqual(len(seen["trajectory"]["tracks"]), 3)
            approve_plan(manifest, "P1", author_id="human-reviewer")
            job = prepare_render(manifest, "P1")
            actor_input = json.loads(
                (job.parent / "input" / "actor_trajectory.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(actor_input["tracks"]), 3)

    def test_render_worker_rejects_staging_binding_tampering_before_blender(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.approve_initial_staging(manifest)
            create_plan(manifest, planner=fake_planner)
            approve_plan(manifest, "P1", author_id="human-reviewer")
            job = prepare_render(manifest, "P1")
            trajectory = manifest.parent / "staging" / "S1" / "trajectory.json"
            trajectory.write_bytes(trajectory.read_bytes() + b" ")
            with patch("videoactagent.director_multicam.subprocess.run") as launch:
                run_render_job(manifest, job)
            self.assertFalse(launch.called)
            job_value = json.loads(job.read_text(encoding="utf-8"))
            self.assertEqual(job_value["status"], "failed")
            self.assertIn("binding", job_value["error"])

    def test_object_cannot_reuse_an_actor_target_id(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            trajectory = session_document(manifest)["staging"]["trajectory"]
            trajectory["tracks"].append({
                "track_id": "object_duplicate_path",
                "target": {"type": "object", "id": "actor_a"},
                "primitive": "polyline", "semantic": "move",
                "points": [
                    {"t": t, "x": 0.5, "y": 0.5, "visible": True}
                    for t in (0.0, 0.2, 0.5, 0.8, 1.0)
                ],
            })
            with self.assertRaisesRegex(DirectorMulticamError, "target IDs"):
                save_staging(manifest, trajectory, base_staging_id="S1")


if __name__ == "__main__":
    unittest.main()
