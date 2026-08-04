from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.director_multicam import (
    DirectorMulticamError,
    approve_plan,
    approve_staging,
    create_plan,
    prepare_rerender,
    prepare_render,
    prepare_workspace,
    revise_plan,
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

    def prepare_approved_plan(self, manifest):
        self.approve_initial_staging(manifest)
        create_plan(manifest, planner=fake_planner)
        approve_plan(manifest, "P1", author_id="human-reviewer")

    def mark_iteration_succeeded(self, manifest, job):
        job_value = json.loads(job.read_text(encoding="utf-8"))
        job_value["status"] = "succeeded"
        job.write_text(json.dumps(job_value), encoding="utf-8")
        (job.parent / "evaluation.json").write_text(
            json.dumps({"automatic_passed": True}), encoding="utf-8"
        )
        state_path = manifest.parent / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["current_iteration"] = job_value["iteration_id"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return job_value

    def successful_iteration(self, manifest, *, bundle=None):
        job = prepare_render(manifest, "P1", camera_bundle_override=bundle)
        self.mark_iteration_succeeded(manifest, job)
        return job

    def revision_planner(self, calls, *, change_camera="camera_b"):
        def planner(**kwargs):
            calls.append(kwargs)
            output = Path(kwargs["output_dir"])
            output.mkdir(parents=True, exist_ok=False)
            plan = json.loads(json.dumps(VALID_PLAN))
            plan["scene_id"] = kwargs["scene_context"]["scene_id"]
            plan["cameras"][[
                item["camera_id"] for item in plan["cameras"]
            ].index(change_camera)]["rationale"] = "revised assignment"
            if change_camera == "camera_b":
                plan["cameras"][1]["motion"] = "static"
            (output / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            evidence = {
                "schema_version": "1.0", "status": "succeeded",
                "api_call_count": 1, "retry_count": 0,
            }
            (output / "evidence.json").write_text(
                json.dumps(evidence), encoding="utf-8"
            )
            return evidence

        return planner

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

    def test_scoped_revision_preserves_unselected_assignments_and_render_states(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            first_job = prepare_render(manifest, "P1")
            source_bundle_path = first_job.parent / "input" / "camera_bundle.json"
            source_bundle = json.loads(source_bundle_path.read_text(encoding="utf-8"))
            source_bundle["cameras"]["camera_a"]["states"][0]["position"][2] += 0.25
            source_bundle_path.write_text(json.dumps(source_bundle), encoding="utf-8")
            first_job_value = json.loads(first_job.read_text(encoding="utf-8"))
            first_job_value["inputs"]["camera_bundle"] = {
                "path": source_bundle_path.relative_to(manifest.parent).as_posix(),
                "sha256": hashlib.sha256(source_bundle_path.read_bytes()).hexdigest(),
                "bytes": source_bundle_path.stat().st_size,
            }
            first_job.write_text(json.dumps(first_job_value), encoding="utf-8")
            self.mark_iteration_succeeded(manifest, first_job)
            original_plan = (manifest.parent / "plans" / "P1" / "plan.json").read_bytes()
            original_job = first_job.read_bytes()
            calls = []

            record = revise_plan(
                manifest, "P1", scope="camera_b", feedback="make B static",
                planner=self.revision_planner(calls),
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["previous_plan"], json.loads(original_plan))
            self.assertEqual(calls[0]["previous_camera_bundle"], source_bundle)
            self.assertEqual(calls[0]["revision_scope"], "camera_b")
            self.assertEqual(calls[0]["feedback"], "make B static")
            self.assertEqual(record["plan_id"], "P2")
            self.assertEqual(record["parent_plan_id"], "P1")
            self.assertEqual(record["revision_scope"], "camera_b")
            self.assertEqual(record["feedback"], "make B static")
            revised_bundle = json.loads(
                (manifest.parent / record["camera_bundle"]["path"]).read_text(encoding="utf-8")
            )
            for camera_id in ("camera_a", "camera_c"):
                self.assertEqual(
                    revised_bundle["cameras"][camera_id],
                    source_bundle["cameras"][camera_id],
                )
            self.assertNotEqual(
                revised_bundle["cameras"]["camera_b"],
                source_bundle["cameras"]["camera_b"],
            )
            session = session_document(manifest)
            self.assertEqual(session["current_plan"], "P2")
            self.assertIsNone(session["approved_plan"])
            self.assertEqual(session["camera_bundle"], revised_bundle)
            self.assertEqual(
                (manifest.parent / "plans" / "P1" / "plan.json").read_bytes(),
                original_plan,
            )
            self.assertEqual(first_job.read_bytes(), original_job)

            approve_plan(manifest, "P2", author_id="human-reviewer")
            second_job = prepare_render(manifest, "P2")
            self.assertEqual(
                (second_job.parent / "input" / "camera_bundle.json").read_bytes(),
                (manifest.parent / "plans" / "P2" / "camera_bundle.json").read_bytes(),
            )

    def test_scoped_revision_rejects_unselected_assignment_change_without_switching_state(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            self.successful_iteration(manifest)
            state_path = manifest.parent / "state.json"
            original_state = state_path.read_bytes()

            with self.assertRaisesRegex(DirectorMulticamError, "unselected"):
                revise_plan(
                    manifest, "P1", scope="camera_b", feedback="make B static",
                    planner=self.revision_planner([], change_camera="camera_a"),
                )

            session = session_document(manifest)
            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertFalse((manifest.parent / "plans" / "P2").exists())
            self.assertEqual(session["current_plan"], "P1")
            self.assertEqual(session["approved_plan"], "P1")
            self.assertEqual(session["current_iteration"], "M1")

    def test_scoped_revision_allows_canonical_equivalent_unselected_assignment(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            self.successful_iteration(manifest)
            calls = []
            base_planner = self.revision_planner(calls)

            def whitespace_planner(**kwargs):
                evidence = base_planner(**kwargs)
                plan_path = Path(kwargs["output_dir"]) / "plan.json"
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                plan["cameras"][0]["rationale"] = (
                    "  " + plan["cameras"][0]["rationale"] + "  "
                )
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                return evidence

            record = revise_plan(
                manifest, "P1", scope="camera_b", feedback="make B static",
                planner=whitespace_planner,
            )

            self.assertEqual(record["plan_id"], "P2")
            self.assertEqual(len(calls), 1)
            self.assertTrue((manifest.parent / "plans" / "P2" / "record.json").is_file())

    def test_blocked_revision_and_rerender_merge_state_without_reusing_ids(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            self.successful_iteration(manifest)
            planner_started = Event()
            release_planner = Event()
            base_planner = self.revision_planner([])

            def blocked_planner(**kwargs):
                planner_started.set()
                if not release_planner.wait(5):
                    raise RuntimeError("test planner barrier timed out")
                return base_planner(**kwargs)

            with ThreadPoolExecutor(max_workers=2) as pool:
                revision = pool.submit(
                    revise_plan, manifest, "P1", scope="camera_b",
                    feedback="make B static", planner=blocked_planner,
                )
                self.assertTrue(planner_started.wait(2))
                try:
                    rerender = pool.submit(prepare_rerender, manifest, "M1").result(2)
                finally:
                    release_planner.set()
                revised = revision.result(5)

            self.assertEqual(revised["plan_id"], "P2")
            self.assertEqual(rerender.parent.name, "M2")
            state = json.loads(
                (manifest.parent / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["next_plan"], 3)
            self.assertEqual(state["next_iteration"], 3)
            approve_plan(manifest, "P2", author_id="human-reviewer")
            next_job = prepare_render(manifest, "P2")
            self.assertEqual(next_job.parent.name, "M3")

    def test_revision_keeps_published_plan_when_state_write_committed_then_raised(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            self.successful_iteration(manifest)
            state_path = manifest.parent / "state.json"
            from videoactagent import director_multicam

            real_write = director_multicam._write

            def committed_then_raised(path, value):
                real_write(path, value)
                if path == state_path:
                    raise OSError("injected after committed state replace")

            with patch("videoactagent.director_multicam._write", committed_then_raised):
                record = revise_plan(
                    manifest, "P1", scope="camera_b", feedback="make B static",
                    planner=self.revision_planner([]),
                )

            self.assertEqual(record["plan_id"], "P2")
            self.assertTrue((manifest.parent / "plans" / "P2" / "record.json").is_file())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["current_plan"], "P2")
            self.assertEqual(state["next_plan"], 3)

    def test_failed_revision_preserves_audit_evidence_and_api_count(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            self.successful_iteration(manifest)
            from videoactagent.director_wizard import _pipeline_api_call_count

            state_path = manifest.parent / "state.json"
            original_state = state_path.read_bytes()
            original_count = _pipeline_api_call_count(manifest)

            def failing_planner(**kwargs):
                output = Path(kwargs["output_dir"])
                output.mkdir(parents=True, exist_ok=False)
                (output / "request.json").write_text("{}", encoding="utf-8")
                (output / "response.json").write_text("{}", encoding="utf-8")
                (output / "evidence.json").write_text(
                    json.dumps({
                        "schema_version": "1.0", "status": "failed",
                        "api_call_count": 1, "retry_count": 0,
                    }),
                    encoding="utf-8",
                )
                raise RuntimeError("controlled planner API failure")

            with self.assertRaisesRegex(RuntimeError, "controlled"):
                revise_plan(
                    manifest, "P1", scope="all", feedback="change all",
                    planner=failing_planner,
                )

            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertFalse((manifest.parent / "plans" / "P2").exists())
            failed_evidence = list(
                (manifest.parent / "plans" / "_failed").glob("P2-*/evidence.json")
            )
            self.assertEqual(len(failed_evidence), 1)
            self.assertEqual(_pipeline_api_call_count(manifest), original_count + 1)

    def test_revision_rejects_invalid_scope_blank_feedback_and_missing_current_success(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            calls = []
            planner = self.revision_planner(calls)
            with self.assertRaisesRegex(DirectorMulticamError, "scope"):
                revise_plan(manifest, "P1", scope="camera_d", feedback="change", planner=planner)
            with self.assertRaisesRegex(DirectorMulticamError, "feedback"):
                revise_plan(manifest, "P1", scope="all", feedback="  ", planner=planner)
            with self.assertRaisesRegex(DirectorMulticamError, "successful"):
                revise_plan(manifest, "P1", scope="all", feedback="change", planner=planner)
            self.assertEqual(calls, [])

    def test_prepare_rerender_snapshots_exact_current_successful_camera_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            source_job = self.successful_iteration(manifest)
            source_bundle = source_job.parent / "input" / "camera_bundle.json"
            source_bytes = source_bundle.read_bytes()

            with patch("videoactagent.director_multicam.request_multicam_plan") as planner:
                rerender_job = prepare_rerender(manifest, "M1")

            planner.assert_not_called()
            self.assertEqual(rerender_job.parent.name, "M2")
            rerender_value = json.loads(rerender_job.read_text(encoding="utf-8"))
            self.assertEqual(rerender_value["plan_id"], "P1")
            self.assertEqual(rerender_value["staging_id"], "S1")
            rerender_bundle = rerender_job.parent / "input" / "camera_bundle.json"
            self.assertEqual(rerender_bundle.read_bytes(), source_bytes)
            self.assertEqual(
                rerender_value["inputs"]["camera_bundle"]["sha256"],
                hashlib.sha256(source_bytes).hexdigest(),
            )
            self.assertEqual(
                json.loads((manifest.parent / "state.json").read_text(encoding="utf-8"))[
                    "current_iteration"
                ],
                "M1",
            )

    def test_prepare_rerender_rejects_stale_failed_and_plan_mismatched_iterations(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            first = self.successful_iteration(manifest)
            second = prepare_render(manifest, "P1")
            self.mark_iteration_succeeded(manifest, second)
            with self.assertRaisesRegex(DirectorMulticamError, "current"):
                prepare_rerender(manifest, "M1")

            second_value = json.loads(second.read_text(encoding="utf-8"))
            second_value["status"] = "failed"
            second.write_text(json.dumps(second_value), encoding="utf-8")
            with self.assertRaisesRegex(DirectorMulticamError, "succeeded"):
                prepare_rerender(manifest, "M2")

            second_value["status"] = "succeeded"
            second_value["plan_id"] = "P2"
            second.write_text(json.dumps(second_value), encoding="utf-8")
            with self.assertRaisesRegex(DirectorMulticamError, "approved"):
                prepare_rerender(manifest, "M2")
            self.assertTrue(first.is_file())

    def test_session_iteration_is_bound_to_matching_plan_and_real_camera_input(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            self.prepare_approved_plan(manifest)
            job = self.successful_iteration(manifest)
            bundle = json.loads(
                (job.parent / "input" / "camera_bundle.json").read_text(encoding="utf-8")
            )
            session = session_document(manifest)
            self.assertEqual(session["workflow_step"], "result")
            self.assertEqual(session["iteration"]["plan_id"], "P1")
            self.assertEqual(session["iteration"]["camera_bundle"], bundle)

            revise_plan(
                manifest, "P1", scope="camera_b", feedback="make B static",
                planner=self.revision_planner([]),
            )
            stale_session = session_document(manifest)
            self.assertEqual(stale_session["current_iteration"], "M1")
            self.assertEqual(stale_session["workflow_step"], "camera")


if __name__ == "__main__":
    unittest.main()
