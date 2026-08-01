import json
from pathlib import Path
import tempfile
import unittest

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.director_multicam import (
    DirectorMulticamError,
    approve_plan,
    create_plan,
    prepare_render,
    prepare_workspace,
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

    def test_prepare_is_offline_and_creates_independent_source_bound_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            session = session_document(manifest)
            self.assertEqual(session["schema_version"], "multicam-director-1.0")
            self.assertEqual(session["story_id"], "station_reunion")
            self.assertEqual(session["current_plan"], None)
            self.assertEqual(len(session["actor_keyframes"]), 5)
            self.assertTrue((manifest.parent / "reference" / "reference.mp4").is_file())

    def test_plan_requires_human_approval_before_render(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
            plan = create_plan(manifest, planner=fake_planner)
            self.assertEqual(plan["plan_id"], "P1")
            with self.assertRaisesRegex(DirectorMulticamError, "approved"):
                prepare_render(manifest, "P1")
            approval = approve_plan(manifest, "P1", author_id="human-reviewer")
            self.assertTrue(approval.is_file())
            job = prepare_render(manifest, "P1")
            document = json.loads(job.read_text(encoding="utf-8"))
            self.assertEqual(document["status"], "queued")
            self.assertTrue((job.parent / "input" / "camera_bundle.json").is_file())
            self.assertTrue((job.parent / "input" / "actor_trajectory.json").is_file())

    def test_plan_and_approval_are_immutable(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = self.prepare(root)
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
            create_plan(
                manifest,
                locked_through_keyframe="K1",
                planner=fake_planner,
            )
            approve_plan(manifest, "P1", author_id="human-reviewer")
            job = prepare_render(manifest, "P1")
            self.assertTrue(job.is_file())


if __name__ == "__main__":
    unittest.main()
