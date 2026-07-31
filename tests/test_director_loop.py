from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_director_annotation import payload
from videoactagent.director_loop import (
    DirectorLoopError,
    approve_iteration,
    byte_range,
    export_approved_iteration,
    prepare_iteration,
    prepare_workspace,
    publish_iteration,
    session_document,
    verify_workspace,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "runs" / "work" / "coded_draft_v1" / "station_reunion" / "bundle.json"
BLENDER = Path(r"D:\blender\blender.exe")


class DirectorLoopTests(unittest.TestCase):
    def make_workspace(self, root: str) -> Path:
        self.assertTrue(BUNDLE.is_file(), f"missing real D0 bundle: {BUNDLE}")
        self.assertTrue(BLENDER.is_file(), f"missing Blender: {BLENDER}")
        return prepare_workspace(BUNDLE, BLENDER, Path(root) / "director")

    def test_prepare_exposes_complete_real_d0_video_and_five_markers(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            verified = verify_workspace(manifest)
            session = session_document(manifest)

        self.assertEqual(verified["state"]["current_iteration"], "D0")
        self.assertIsNone(verified["state"]["approved_iteration"])
        self.assertEqual(session["current"]["diagnostic_url"], "/media/D0/diagnostic.mp4")
        self.assertEqual(session["current"]["media"]["diagnostic"]["frame_count"], 120)
        self.assertEqual([item["id"] for item in session["keyframes"]], [f"K{i}" for i in range(5)])
        self.assertFalse(session["human_values_present"])

    def test_prepare_iteration_writes_only_human_inputs_and_queued_job(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, payload())
            job = json.loads(job_path.read_text(encoding="utf-8"))
            iteration = job_path.parent

            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["iteration_id"], "D1")
            self.assertTrue((iteration / "input" / "annotation.json").is_file())
            self.assertTrue((iteration / "input" / "actor_trajectory.json").is_file())
            self.assertTrue((iteration / "input" / "camera_trajectory.json").is_file())
            self.assertTrue((iteration / "input" / "compiled_prompt.txt").is_file())
            self.assertFalse((iteration / "approval.json").exists())
            self.assertFalse(any(path.name.startswith("vace") for path in iteration.rglob("*")))

    def test_approval_rejects_unrendered_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            prepare_iteration(manifest, payload())
            with self.assertRaisesRegex(DirectorLoopError, "succeeded"):
                approve_iteration(manifest, "D1", "sy")

    def test_publish_approve_export_are_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            verify_workspace(manifest)
            job = prepare_iteration(manifest, payload())
            d0 = manifest.parent / "iterations" / "D0"
            publish_iteration(
                manifest, job,
                d0 / "diagnostic.mp4", d0 / "clay.mp4",
            )
            approval = approve_iteration(manifest, "D1", "sy")
            exported = export_approved_iteration(manifest)

            self.assertEqual(exported["iteration_id"], "D1")
            self.assertEqual(
                exported["approval"]["sha256"],
                hashlib.sha256(approval.read_bytes()).hexdigest(),
            )
            diagnostic = manifest.parent / exported["diagnostic"]["path"]
            diagnostic.write_bytes(diagnostic.read_bytes() + b"tamper")
            with self.assertRaisesRegex(DirectorLoopError, "hash"):
                export_approved_iteration(manifest)

    def test_byte_ranges_support_video_seeking(self) -> None:
        self.assertEqual(byte_range(None, 100), (0, 99, False))
        self.assertEqual(byte_range("bytes=10-19", 100), (10, 19, True))
        self.assertEqual(byte_range("bytes=90-", 100), (90, 99, True))
        self.assertEqual(byte_range("bytes=-10", 100), (90, 99, True))
        with self.assertRaises(DirectorLoopError):
            byte_range("bytes=30-20", 100)

    def test_panel_contains_complete_video_and_separate_render_approval_actions(self) -> None:
        html = (ROOT / "static" / "director_panel.html").read_text(encoding="utf-8")
        self.assertIn("<video", html)
        self.assertIn("controls", html)
        self.assertIn("/api/iterations", html)
        self.assertIn("/approve", html)
        self.assertIn("Generate next proxy", html)
        self.assertIn("Approve current proxy", html)
        self.assertIn("camera-position", html)
        self.assertIn("camera-look-at", html)


if __name__ == "__main__":
    unittest.main()
