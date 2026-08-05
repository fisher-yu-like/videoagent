from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videoactagent.codegen_job import (
    CodegenJobError,
    generate_codegen_job,
    prepare_codegen_job,
    render_codegen_job,
)


ROOT = Path(__file__).resolve().parents[1]


class _Guard:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return b'{"workflow":"staging"}'


class CodegenJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.protected = self.root / "protected"; self.protected.mkdir(); (self.protected / "keep.txt").write_text("keep", encoding="utf-8")

    def prepare(self):
        with patch("videoactagent.codegen_job.urlopen", return_value=_Guard()):
            return prepare_codegen_job(experiments_root=self.root / "experiments", protected_workspace=self.protected, protected_url="http://guard.test/session", prompt_path=ROOT / "prompts/station_reunion.txt", shotscript_path=ROOT / "stories/station_reunion.json", trajectory_path=ROOT / "examples/station_codegen_trajectory.json")

    def test_prepare_selects_cg1_and_hashes_guard_without_api(self):
        job_path = self.prepare()
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(job["job_id"], "CG1")
        self.assertEqual(job["status"], "prepared")
        self.assertEqual(job["api_call_count"], 0)
        self.assertTrue(job["protected"]["tree_sha256"])
        self.assertTrue((job_path.parent / "source/input.json").is_file())
        self.assertFalse((job_path.parent / "renders").exists())

    def test_generate_and_render_transitions_are_hash_bound(self):
        job_path = self.prepare()
        code = "import bpy\ndef build_scene(context):\n    pass\n"
        def requester(**kwargs):
            output = Path(kwargs["output_dir"]); output.mkdir(); (output / "generated_scene.py").write_text(code, encoding="utf-8")
            return {"status": "succeeded", "api_call_count": 1, "retry_count": 0, "code_sha256": "x"}
        evidence = generate_codegen_job(job_path, requester=requester)
        self.assertEqual(evidence["status"], "code_validated")
        def runner(**kwargs):
            output = Path(kwargs["output_dir"]); output.mkdir(parents=True)
            return {"status": "succeeded", "output_dir": str(output), "code_sha256": "x"}
        def verifier(**kwargs): return {"status": "succeeded", "trajectory": {"max_error": 0.0}}
        with patch("videoactagent.codegen_job.urlopen", return_value=_Guard()):
            result = render_codegen_job(job_path, blender=Path("blender"), profile="smoke", runner=runner, verifier=verifier)
        self.assertEqual(result["status"], "smoke_succeeded")
        job = json.loads(job_path.read_text(encoding="utf-8"))
        code_hash = job["code_sha256"]
        self.assertEqual(job["status"], "smoke_succeeded")
        with patch("videoactagent.codegen_job.urlopen", return_value=_Guard()):
            result = render_codegen_job(job_path, blender=Path("blender"), profile="full", runner=runner, verifier=verifier)
        self.assertEqual(result["status"], "succeeded")
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["code_sha256"], code_hash)
        self.assertEqual(set(job["renders"]), {"smoke", "full"})

    def test_full_render_requires_successful_smoke(self):
        job_path = self.prepare()
        code = "import bpy\ndef build_scene(context):\n    pass\n"
        def requester(**kwargs):
            output = Path(kwargs["output_dir"]); output.mkdir(); (output / "generated_scene.py").write_text(code, encoding="utf-8")
            return {"status": "succeeded", "api_call_count": 1, "retry_count": 0}
        generate_codegen_job(job_path, requester=requester)
        with patch("videoactagent.codegen_job.urlopen", return_value=_Guard()):
            with self.assertRaisesRegex(CodegenJobError, "smoke_succeeded"):
                render_codegen_job(job_path, blender=Path("blender"), profile="full", runner=lambda **kwargs: {}, verifier=lambda **kwargs: {})

    def test_render_profile_cannot_be_repeated(self):
        job_path = self.prepare()
        code = "import bpy\ndef build_scene(context):\n    pass\n"
        def requester(**kwargs):
            output = Path(kwargs["output_dir"]); output.mkdir(); (output / "generated_scene.py").write_text(code, encoding="utf-8")
            return {"status": "succeeded", "api_call_count": 1, "retry_count": 0}
        generate_codegen_job(job_path, requester=requester)
        def runner(**kwargs):
            output = Path(kwargs["output_dir"]); output.mkdir(parents=True)
            return {"status": "succeeded", "output_dir": str(output), "code_sha256": "x"}
        with patch("videoactagent.codegen_job.urlopen", return_value=_Guard()):
            render_codegen_job(job_path, blender=Path("blender"), profile="smoke", runner=runner, verifier=lambda **kwargs: {"status": "succeeded"})
            with self.assertRaisesRegex(CodegenJobError, "smoke_succeeded"):
                render_codegen_job(job_path, blender=Path("blender"), profile="smoke", runner=runner, verifier=lambda **kwargs: {"status": "succeeded"})

    def test_failures_are_terminal_and_duplicate_active_lock_is_rejected(self):
        job_path = self.prepare()
        (job_path.parent / "job.lock").write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(CodegenJobError, "active"):
            generate_codegen_job(job_path)
        (job_path.parent / "job.lock").unlink()
        def failing(**kwargs): raise RuntimeError("single call failed")
        with self.assertRaises(CodegenJobError):
            generate_codegen_job(job_path, requester=failing)
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(job["status"], "api_failed")


if __name__ == "__main__":
    unittest.main()
