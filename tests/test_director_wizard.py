import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_multicam_plan import VALID_PLAN


VALID_DRAFT = {
    "schema_version": "scene-plan-1.0",
    "scene_id": "new_station_story",
    "environment_preset": "station",
    "duration_seconds": 5.0,
    "fps": 3,
    "world_bounds": [-5.0, 5.0, -4.0, 4.0],
    "actors": [
        {
            "id": "traveler",
            "color": "#F28E2B",
            "action": "walk",
            "start": [-3.0, 0.0, 0.0],
            "end": [-0.5, 0.0, 0.0],
            "facing": "friend",
        },
        {
            "id": "friend",
            "color": "#4E79A7",
            "action": "wait",
            "start": [1.5, 0.0, 0.0],
            "end": [1.5, 0.0, 0.0],
            "facing": "traveler",
        },
    ],
    "objects": [
        {
            "id": "suitcase",
            "primitive": "cube",
            "semantic": "move",
            "start": [-2.8, 0.2],
            "end": [-0.3, 0.2],
        }
    ],
    "initial_camera": {
        "shot_size": "wide",
        "focal_length_mm": 35,
        "motion": "static",
        "start": [0.0, -10.0, 6.0],
        "end": [0.0, -10.0, 6.0],
        "look_at": "actors_midpoint",
    },
    "explanation": "先用固定全景检查两人的会合调度。",
}


class DirectorWizardTests(unittest.TestCase):
    def setUp(self):
        from videoactagent.director_wizard import create_workspace

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.blender = self.root / "blender.exe"
        self.blender.write_bytes(b"test blender executable")
        self.manifest = create_workspace(self.blender, self.root / "project")

    def save(self, draft=None, *, prompt="新故事", parent=None):
        from videoactagent.director_wizard import save_scene_plan

        return save_scene_plan(
            self.manifest,
            copy.deepcopy(VALID_DRAFT if draft is None else draft),
            prompt=prompt,
            parent=parent,
        )

    def test_local_form_save_does_not_call_deepseek(self):
        from videoactagent.director_wizard import session_document

        with patch("videoactagent.director_wizard.request_scene_plan") as planner:
            plan = self.save()
        planner.assert_not_called()
        self.assertEqual(plan["scene_plan_id"], "SP1")
        session = session_document(self.manifest)
        self.assertEqual(session["workflow_step"], "scene_plan_review")
        self.assertEqual(session["current_scene_plan"], "SP1")
        self.assertEqual(session["scene_plan"]["draft"], VALID_DRAFT)
        self.assertEqual(session["story_prompt"], "新故事")
        self.assertEqual(session["duration_seconds"], 5.0)
        self.assertEqual(
            session["scene_plan"]["source_hash"], plan["draft"]["sha256"]
        )
        self.assertEqual(
            session["scene_plan"]["evidence"],
            {"status": "local", "api_call_count": 0, "source": "local_form"},
        )
        self.assertEqual(session["api_call_count"], 0)

    def test_agent_feedback_creates_a_child_scene_plan(self):
        from videoactagent.director_wizard import generate_scene_plan

        first = self.save()
        seen = {}

        def fake_planner(**kwargs):
            seen.update(kwargs)
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
            (output / "evidence.json").write_text(
                json.dumps(evidence), encoding="utf-8"
            )
            return evidence

        with patch("videoactagent.director_wizard.request_scene_plan", fake_planner):
            second = generate_scene_plan(
                self.manifest, "新故事", 5.0, feedback="把行李箱放近一点"
            )
        self.assertEqual(second["scene_plan_id"], "SP2")
        self.assertEqual(second["parent"], first["scene_plan_id"])
        self.assertEqual(seen["feedback"], "把行李箱放近一点")
        self.assertEqual(seen["previous_draft"], VALID_DRAFT)

    def test_new_scene_plan_invalidates_old_reference_and_downstream_gate(self):
        from videoactagent.director_wizard import (
            DirectorWizardError,
            _Handler,
            approve_reference,
            session_document,
        )

        _plan, reference = self._render_with_real_pipeline()
        approve_reference(self.manifest, reference["reference_id"], author_id="human")
        self.save(parent="SP1")

        session = session_document(self.manifest)
        self.assertIsNone(session["approved_scene_plan"])
        self.assertIsNone(session["current_reference"])
        self.assertIsNone(session["approved_reference"])
        handler = object.__new__(_Handler)
        handler.wizard_manifest = self.manifest
        with self.assertRaisesRegex(DirectorWizardError, "reference must be approved"):
            handler._downstream()

    def test_failed_agent_record_binding_rolls_back_published_scene_plan(self):
        from videoactagent import director_wizard

        first = self.save()

        def planner(**kwargs):
            output = Path(kwargs["output_dir"])
            output.mkdir(parents=True, exist_ok=False)
            (output / "draft.json").write_text(json.dumps(VALID_DRAFT), encoding="utf-8")
            evidence = {"status": "succeeded", "api_call_count": 1, "retry_count": 0}
            (output / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            return evidence

        original_write = director_wizard._write

        def fail_agent_record(path, value):
            if Path(path).name == "record.json" and isinstance(value, dict) and value.get("source") == "agent":
                raise OSError("injected agent record failure")
            return original_write(path, value)

        with patch("videoactagent.director_wizard.request_scene_plan", planner), patch(
            "videoactagent.director_wizard._write", fail_agent_record
        ):
            with self.assertRaisesRegex(OSError, "injected agent record failure"):
                director_wizard.generate_scene_plan(self.manifest, "new story", 5.0)

        session = director_wizard.session_document(self.manifest)
        self.assertEqual(session["current_scene_plan"], first["scene_plan_id"])
        self.assertFalse((self.manifest.parent / "scene_plans" / "SP2").exists())
        self.assertEqual(session["api_call_count"], 1)

    def test_failed_reference_state_publish_cleans_target_and_reuses_id(self):
        from videoactagent import director_wizard

        plan = self.save()
        director_wizard.approve_scene_plan(
            self.manifest, plan["scene_plan_id"], author_id="human"
        )
        original_write = director_wizard._write
        failed = False

        def fail_state_once(path, value):
            nonlocal failed
            if Path(path).name == "state.json" and value.get("next_reference") == 2 and not failed:
                failed = True
                raise OSError("injected reference state failure")
            return original_write(path, value)

        with patch("videoactagent.director_wizard.prepare_workspace", self._prepare_without_blender), patch(
            "videoactagent.director_wizard._write", fail_state_once
        ):
            with self.assertRaisesRegex(OSError, "injected reference state failure"):
                director_wizard.render_reference(self.manifest, plan["scene_plan_id"])
        self.assertFalse((self.manifest.parent / "references" / "R1").exists())
        with patch("videoactagent.director_wizard.prepare_workspace", self._prepare_without_blender):
            reference = director_wizard.render_reference(self.manifest, plan["scene_plan_id"])
        self.assertEqual(reference["reference_id"], "R1")

    def test_failed_approval_state_updates_are_retryable(self):
        from videoactagent import director_wizard

        plan = self.save()
        original_write = director_wizard._write
        failed = False

        def fail_scene_state_once(path, value):
            nonlocal failed
            if Path(path).name == "state.json" and value.get("approved_scene_plan") == "SP1" and not failed:
                failed = True
                raise OSError("injected scene approval failure")
            return original_write(path, value)

        with patch("videoactagent.director_wizard._write", fail_scene_state_once):
            with self.assertRaisesRegex(OSError, "injected scene approval failure"):
                director_wizard.approve_scene_plan(self.manifest, "SP1", author_id="human")
        self.assertFalse((self.manifest.parent / "scene_plans" / "SP1" / "approval.json").exists())
        director_wizard.approve_scene_plan(self.manifest, "SP1", author_id="human")

        with patch("videoactagent.director_wizard.prepare_workspace", self._prepare_without_blender):
            reference = director_wizard.render_reference(self.manifest, "SP1")
        failed = False

        def fail_reference_state_once(path, value):
            nonlocal failed
            if Path(path).name == "state.json" and value.get("approved_reference") == "R1" and not failed:
                failed = True
                raise OSError("injected reference approval failure")
            return original_write(path, value)

        with patch("videoactagent.director_wizard._write", fail_reference_state_once):
            with self.assertRaisesRegex(OSError, "injected reference approval failure"):
                director_wizard.approve_reference(self.manifest, "R1", author_id="human")
        self.assertFalse((self.manifest.parent / "references" / "R1" / "approval.json").exists())
        director_wizard.approve_reference(self.manifest, "R1", author_id="human")

    def test_unapproved_scene_plan_cannot_render(self):
        from videoactagent.director_wizard import DirectorWizardError, render_reference

        plan = self.save()
        with self.assertRaisesRegex(DirectorWizardError, "approved"):
            render_reference(self.manifest, plan["scene_plan_id"])

    def test_approving_an_older_scene_plan_marks_descendants_stale(self):
        from videoactagent.director_wizard import approve_scene_plan, session_document

        first = self.save()
        second = self.save(parent=first["scene_plan_id"])
        approval = approve_scene_plan(
            self.manifest, first["scene_plan_id"], author_id="human"
        )
        self.assertTrue(approval.is_file())
        session = session_document(self.manifest)
        self.assertEqual(session["approved_scene_plan"], "SP1")
        self.assertEqual(session["current_scene_plan"], "SP1")
        stale = json.loads(
            (self.manifest.parent / "scene_plans" / second["scene_plan_id"] / "record.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(stale["stale"])

    def test_scene_plan_source_hash_tampering_is_rejected(self):
        from videoactagent.director_wizard import DirectorWizardError, approve_scene_plan

        plan = self.save()
        draft = self.manifest.parent / "scene_plans" / plan["scene_plan_id"] / "draft.json"
        draft.write_bytes(draft.read_bytes() + b" ")
        with self.assertRaisesRegex(DirectorWizardError, "binding"):
            approve_scene_plan(
                self.manifest, plan["scene_plan_id"], author_id="human"
            )

    def test_concurrent_local_saves_allocate_unique_scene_plan_ids(self):
        from videoactagent.director_wizard import save_scene_plan, session_document

        def save(index):
            value = copy.deepcopy(VALID_DRAFT)
            value["explanation"] = f"concurrent save {index}"
            return save_scene_plan(
                self.manifest, value, prompt="新故事", parent=None
            )["scene_plan_id"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(save, (1, 2)))
        self.assertEqual(set(ids), {"SP1", "SP2"})
        self.assertEqual(session_document(self.manifest)["current_scene_plan"], "SP2")
        self.assertEqual(
            json.loads((self.manifest.parent / "state.json").read_text(encoding="utf-8"))[
                "next_scene_plan"
            ],
            3,
        )

    def test_failed_scene_plan_publish_cleans_staging_and_can_retry_same_id(self):
        from videoactagent import director_wizard

        original_write = director_wizard._write

        def fail_on_shotscript(path, value):
            if Path(path).name == "shotscript.json":
                raise OSError("injected write failure")
            return original_write(path, value)

        with patch("videoactagent.director_wizard._write", fail_on_shotscript):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                self.save()
        self.assertFalse((self.manifest.parent / "scene_plans" / "SP1").exists())
        self.assertEqual(
            list((self.manifest.parent / "scene_plans").glob(".SP1.*.staging")),
            [],
        )
        self.assertEqual(self.save()["scene_plan_id"], "SP1")

    def test_reference_hash_tampering_is_rejected_before_approval(self):
        from videoactagent.director_wizard import (
            DirectorWizardError,
            approve_reference,
        )

        _plan, reference = self._render_with_real_pipeline()
        video = (
            self.manifest.parent
            / "references"
            / reference["reference_id"]
            / "pipeline"
            / "reference"
            / "reference.mp4"
        )
        video.write_bytes(video.read_bytes() + b"tampered")
        with self.assertRaisesRegex(DirectorWizardError, "binding"):
            approve_reference(
                self.manifest, reference["reference_id"], author_id="human"
            )

    def test_start_rejects_resume_with_a_different_blender_path(self):
        from videoactagent.director_wizard import main

        other = self.root / "other-blender.exe"
        other.write_bytes(b"different blender")
        errors = io.StringIO()
        with patch("videoactagent.director_wizard.serve_workspace") as serve:
            with redirect_stderr(errors):
                code = main(
                    [
                        "start",
                        "--blender",
                        str(other),
                        "--workspace",
                        str(self.manifest.parent),
                    ]
                )
        self.assertEqual(code, 2)
        self.assertFalse(serve.called)
        self.assertIn("different Blender path", errors.getvalue())

    def _render_with_real_pipeline(self):
        from videoactagent import director_multicam
        from videoactagent.director_wizard import approve_scene_plan, render_reference

        plan = self.save()
        approve_scene_plan(self.manifest, plan["scene_plan_id"], author_id="human")
        with patch(
            "videoactagent.director_wizard.prepare_workspace",
            self._prepare_without_blender,
        ):
            reference = render_reference(self.manifest, plan["scene_plan_id"])
        return plan, reference

    @staticmethod
    def _prepare_without_blender(*args, **kwargs):
        from videoactagent import director_multicam

        def fake_renderer(*, blender, shotscript, output_dir, fps, resolution):
            del blender, shotscript, fps, resolution
            output_dir.mkdir(parents=True, exist_ok=False)
            video = output_dir / "reference.mp4"
            video.write_bytes(b"reference video")
            return video

        kwargs["reference_renderer"] = fake_renderer
        return director_multicam.prepare_workspace(*args, **kwargs)

    def test_approved_scene_plan_builds_existing_multicam_workspace(self):
        from videoactagent.director_wizard import session_document

        _plan, reference = self._render_with_real_pipeline()
        self.assertEqual(reference["reference_id"], "R1")
        self.assertTrue(
            (self.manifest.parent / "references" / "R1" / "pipeline" / "multicam_manifest.json")
            .is_file()
        )
        session = session_document(self.manifest)
        self.assertEqual(session["workflow_step"], "reference_review")
        self.assertNotIn("staging", session)
        self.assertEqual(session["current_reference"], "R1")
        self.assertEqual(
            session["reference"]["source_hash"], reference["video"]["sha256"]
        )

    def test_reference_renderer_passes_initial_object_trajectory_to_blender(self):
        from videoactagent.director_wizard import approve_scene_plan, render_reference

        plan = self.save()
        approve_scene_plan(self.manifest, plan["scene_plan_id"], author_id="human")
        commands = []

        def fake_prepare(prompt, shotscript, blender, output, *, reference_renderer):
            del prompt
            output.mkdir(parents=True)
            manifest = output / "multicam_manifest.json"
            manifest.write_text("{}", encoding="utf-8")

            def launch(command, **kwargs):
                del kwargs
                commands.append(command)
                destination = command[command.index("--output-dir") + 1]
                rendered = Path(destination)
                rendered.mkdir(parents=True, exist_ok=True)
                (rendered / "trajectory_proxy.mp4").write_bytes(b"video")
                return type(
                    "Completed", (), {"returncode": 0, "stdout": "TRAJECTORY_PROXY_OK", "stderr": ""}
                )()

            with patch("videoactagent.director_wizard.subprocess.run", launch):
                reference_renderer(
                    blender=Path(blender),
                    shotscript=Path(shotscript),
                    output_dir=output / "reference",
                    fps=3,
                    resolution=(960, 540),
                )
            return manifest

        with patch("videoactagent.director_wizard.prepare_workspace", fake_prepare):
            with patch("videoactagent.director_wizard.save_staging") as save_staging:
                save_staging.return_value = {"staging_id": "S2"}
                render_reference(self.manifest, plan["scene_plan_id"])
        command = commands[0]
        self.assertIn("--trajectory", command)
        trajectory = Path(command[command.index("--trajectory") + 1])
        self.assertEqual(
            [track["target"]["id"] for track in json.loads(trajectory.read_text())["tracks"]],
            ["traveler", "friend", "suitcase"],
        )
        save_staging.assert_called_once()
        self.assertEqual(save_staging.call_args.kwargs["base_staging_id"], "S1")

    def test_staging_appears_only_after_reference_approval_and_keeps_objects(self):
        from videoactagent.director_wizard import approve_reference, session_document

        _plan, reference = self._render_with_real_pipeline()
        before = session_document(self.manifest)
        self.assertNotIn("staging", before)
        approve_reference(
            self.manifest, reference["reference_id"], author_id="human"
        )
        after = session_document(self.manifest)
        self.assertEqual(after["workflow_step"], "staging")
        self.assertEqual(after["approved_reference"], "R1")
        self.assertEqual(
            [track["target"]["id"] for track in after["staging"]["trajectory"]["tracks"]],
            ["traveler", "friend", "suitcase"],
        )

    def test_session_sums_bound_scene_and_current_pipeline_api_evidence(self):
        from videoactagent.director_multicam import approve_staging, create_plan
        from videoactagent.director_wizard import (
            approve_reference,
            approve_scene_plan,
            generate_scene_plan,
            render_reference,
            session_document,
        )

        def generate(count, feedback=None):
            def planner(**kwargs):
                output = Path(kwargs["output_dir"])
                output.mkdir(parents=True, exist_ok=False)
                (output / "draft.json").write_text(
                    json.dumps(VALID_DRAFT, ensure_ascii=False), encoding="utf-8"
                )
                evidence = {
                    "schema_version": "1.0",
                    "status": "succeeded",
                    "api_call_count": count,
                    "retry_count": 0,
                }
                (output / "evidence.json").write_text(
                    json.dumps(evidence), encoding="utf-8"
                )
                return evidence

            with patch("videoactagent.director_wizard.request_scene_plan", planner):
                return generate_scene_plan(
                    self.manifest, "新故事", 5.0, feedback=feedback
                )

        first = generate(2)
        second = generate(4, feedback="再改一次")
        before_reference = session_document(self.manifest)
        self.assertEqual(before_reference["api_call_count"], 6)
        self.assertEqual(before_reference["scene_plan"]["evidence"]["api_call_count"], 4)
        self.assertEqual(
            before_reference["scene_plan"]["source_hash"], second["draft"]["sha256"]
        )
        self.assertNotEqual(first["scene_plan_id"], second["scene_plan_id"])

        approve_scene_plan(
            self.manifest, second["scene_plan_id"], author_id="human"
        )
        with patch(
            "videoactagent.director_wizard.prepare_workspace",
            self._prepare_without_blender,
        ):
            reference = render_reference(self.manifest, second["scene_plan_id"])
        approve_reference(self.manifest, reference["reference_id"], author_id="human")
        pipeline = (
            self.manifest.parent
            / "references"
            / reference["reference_id"]
            / "pipeline"
            / "multicam_manifest.json"
        )
        approve_staging(pipeline, "S2", author_id="human")

        def camera_planner(count):
            def planner(*, scene_context, output_dir, environ=None):
                del environ
                output = Path(output_dir)
                output.mkdir(parents=True, exist_ok=False)
                plan = copy.deepcopy(VALID_PLAN)
                plan["scene_id"] = scene_context["scene_id"]
                plan["locked_through_keyframe"] = scene_context[
                    "locked_through_keyframe"
                ]
                plan["cameras"][1]["target"] = scene_context[
                    "controllable_targets"
                ][0]
                plan["cameras"][2]["target"] = scene_context[
                    "controllable_targets"
                ][-1]
                (output / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
                evidence = {
                    "schema_version": "1.0",
                    "status": "succeeded",
                    "api_call_count": count,
                    "retry_count": 0,
                }
                (output / "evidence.json").write_text(
                    json.dumps(evidence), encoding="utf-8"
                )
                return evidence

            return planner

        create_plan(pipeline, planner=camera_planner(5))
        create_plan(pipeline, planner=camera_planner(7))

        def failed_camera_planner(*, scene_context, output_dir, environ=None):
            del scene_context, environ
            output = Path(output_dir)
            output.mkdir(parents=True, exist_ok=False)
            (output / "evidence.json").write_text(
                json.dumps({
                    "schema_version": "1.0",
                    "status": "failed",
                    "api_call_count": 11,
                    "retry_count": 0,
                }),
                encoding="utf-8",
            )
            raise RuntimeError("camera planning failed after one call")

        with self.assertRaisesRegex(RuntimeError, "camera planning failed"):
            create_plan(pipeline, planner=failed_camera_planner)
        self.assertEqual(session_document(self.manifest)["api_call_count"], 29)


if __name__ == "__main__":
    unittest.main()
