from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from videoactagent.director_loop import (
    DirectorLoopError,
    approve_iteration,
    byte_range,
    export_approved_iteration,
    prepare_iteration,
    prepare_workspace,
    publish_iteration,
    preview_prompt,
    session_document,
    verify_workspace,
    _parser,
)
from videoactagent.restyle_prompt import RESTYLE_COMPILER_VERSION
from videoactagent.trajectory_prompt import PROMPT_COMPILER_VERSION


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "runs" / "work" / "coded_draft_v1" / "station_reunion" / "bundle.json"
BLENDER = Path(r"D:\blender\blender.exe")


class DirectorLoopTests(unittest.TestCase):
    @staticmethod
    def rewrite_json(path: Path, value: object) -> bytes:
        data = (json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n").encode("utf-8")
        path.write_bytes(data)
        return data

    @staticmethod
    def rebind_record(record: dict, data: bytes) -> None:
        record["sha256"] = hashlib.sha256(data).hexdigest()
        record["bytes"] = len(data)

    def tamper_annotation_versions(self, path: Path) -> bytes:
        annotation = json.loads(path.read_text(encoding="utf-8"))
        annotation["prompt_compiler_version"] = "adversarial-trajectory"
        annotation["restyle_compiler_version"] = "adversarial-restyle"
        return self.rewrite_json(path, annotation)

    def make_workspace(self, root: str, restyle_profile: Path | None = None) -> Path:
        self.assertTrue(BUNDLE.is_file(), f"missing real D0 bundle: {BUNDLE}")
        self.assertTrue(BLENDER.is_file(), f"missing Blender: {BLENDER}")
        return prepare_workspace(
            BUNDLE, BLENDER, Path(root) / "director",
            restyle_profile_path=restyle_profile,
        )

    def test_prepare_snapshots_exact_supplied_restyle_profile_with_provenance(self) -> None:
        source_bytes = (ROOT / "configs" / "restyle" / "station_reunion.json").read_bytes()
        with tempfile.TemporaryDirectory() as root:
            profile = Path(root) / "profile.json"
            profile.write_bytes(source_bytes)
            manifest = self.make_workspace(root, profile)
            profile.write_text("source drift after prepare", encoding="utf-8")
            workspace = verify_workspace(manifest)
            record = workspace["document"]["source"]["restyle_profile"]
            snapshot = manifest.parent / record["path"]

            self.assertEqual(record["provenance"], "provided")
            self.assertEqual(snapshot.read_bytes(), source_bytes)
            self.assertEqual(record["bytes"], len(source_bytes))
            self.assertEqual(record["sha256"], hashlib.sha256(source_bytes).hexdigest())

    def test_prepare_derives_canonical_generic_profile_without_story_motion(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            workspace = verify_workspace(manifest)
            record = workspace["document"]["source"]["restyle_profile"]
            snapshot = manifest.parent / record["path"]
            profile = json.loads(snapshot.read_text(encoding="utf-8"))

            self.assertEqual(record["provenance"], "derived_generic")
            self.assertEqual(
                [subject["actor_id"] for subject in profile["subjects"]],
                workspace["document"]["actors"],
            )
            self.assertIn("station", profile["environment"])
            self.assertNotIn("walks from the far left", snapshot.read_text(encoding="utf-8"))
            self.assertEqual(snapshot.read_bytes(), json.dumps(
                profile, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ).encode("utf-8") + b"\n")

    def test_prepare_cli_accepts_restyle_profile(self) -> None:
        profile = ROOT / "configs" / "restyle" / "station_reunion.json"
        args = _parser().parse_args([
            "prepare", "--bundle", str(BUNDLE), "--blender", str(BLENDER),
            "--output-dir", "workspace", "--restyle-profile", str(profile),
        ])
        self.assertEqual(args.restyle_profile, profile)

    def director_payload(self, manifest: Path, boundary: str = "K0") -> dict:
        session = session_document(manifest)
        boundary_index = int(boundary[1:])
        frames = deepcopy(session["inherited_keyframes"])
        for frame in frames:
            frame["camera_source"] = "inherited"
        return {
            "schema_version": "1.0",
            "author_id": "sy",
            "iteration_id": session["next_iteration_id"],
            "parent_iteration_id": session["current"]["iteration_id"],
            "auto_filled_values": 0,
            "visual_style": "source_default",
            "mood": "source_default",
            "frozen_through_keyframe": boundary,
            "inheritance_sha256": session["inheritance_sha256"],
            "inherited_locked_values": deepcopy(
                session["inherited_keyframes"][:boundary_index + 1]
            ),
            "keyframes": frames,
        }

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
        self.assertEqual(len(session["inherited_keyframes"]), 5)
        self.assertEqual(
            session["inherited_keyframes"][0]["actors"]["actor_a"],
            {"x": 0.16, "y": 0.5},
        )
        self.assertNotIn("visible_state", session["inherited_keyframes"][0])
        self.assertEqual(
            session["inherited_keyframes"][0]["camera"]["position"],
            [0.0, -10.0, 6.0],
        )
        self.assertFalse(session["human_values_present"])

    def test_prepare_iteration_writes_only_human_inputs_and_queued_job(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            iteration = job_path.parent

            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["iteration_id"], "D1")
            self.assertTrue((iteration / "input" / "annotation.json").is_file())
            self.assertTrue((iteration / "input" / "actor_trajectory.json").is_file())
            self.assertTrue((iteration / "input" / "camera_trajectory.json").is_file())
            self.assertTrue((iteration / "input" / "compiled_prompt.txt").is_file())
            self.assertTrue((iteration / "input" / "trajectory_prompt.txt").is_file())
            self.assertTrue((iteration / "input" / "restyle_prompt.txt").is_file())
            self.assertEqual(
                (iteration / "input" / "compiled_prompt.txt").read_bytes(),
                (iteration / "input" / "trajectory_prompt.txt").read_bytes(),
            )
            self.assertEqual(
                job["source"]["restyle_profile"],
                json.loads(manifest.read_text(encoding="utf-8"))["source"]["restyle_profile"],
            )
            self.assertEqual(job["trajectory_compiler_version"], PROMPT_COMPILER_VERSION)
            self.assertEqual(job["restyle_compiler_version"], RESTYLE_COMPILER_VERSION)
            self.assertFalse((iteration / "approval.json").exists())
            self.assertFalse(any(path.name.startswith("vace") for path in iteration.rglob("*")))

    def test_prompt_preview_is_read_only_and_matches_iteration_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            payload_value = self.director_payload(manifest)
            before = {
                path.relative_to(manifest.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in manifest.parent.rglob("*") if path.is_file()
            }
            state_before = (manifest.parent / "state.json").read_bytes()
            preview = preview_prompt(manifest, payload_value)
            after = {
                path.relative_to(manifest.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in manifest.parent.rglob("*") if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertEqual((manifest.parent / "state.json").read_bytes(), state_before)
            self.assertEqual(set(preview), {
                "trajectory_compiler_version", "restyle_compiler_version",
                "trajectory_prompt", "restyle_prompt",
            })
            self.assertEqual(preview["trajectory_compiler_version"], PROMPT_COMPILER_VERSION)
            self.assertEqual(preview["restyle_compiler_version"], RESTYLE_COMPILER_VERSION)
            job = prepare_iteration(manifest, payload_value)
            trajectory = (job.parent / "input" / "trajectory_prompt.txt").read_text(encoding="utf-8").strip()
            restyle = (job.parent / "input" / "restyle_prompt.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(trajectory, preview["trajectory_prompt"])
            self.assertEqual(restyle, preview["restyle_prompt"])

    def test_approval_rejects_unrendered_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            prepare_iteration(manifest, self.director_payload(manifest))
            with self.assertRaisesRegex(DirectorLoopError, "succeeded"):
                approve_iteration(manifest, "D1", "sy")

    def test_publish_approve_export_are_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            verify_workspace(manifest)
            job = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            publish_iteration(
                manifest, job,
                d0 / "diagnostic.mp4", d0 / "clay.mp4",
            )
            approval = approve_iteration(manifest, "D1", "sy")
            inherited = session_document(manifest)["inherited_keyframes"]
            exported = export_approved_iteration(manifest)

            self.assertEqual(exported["iteration_id"], "D1")
            self.assertEqual(
                exported["compiled_prompt"]["sha256"],
                exported["trajectory_prompt"]["sha256"],
            )
            self.assertIn("restyle_prompt", exported)
            self.assertIn("restyle_profile", exported)
            self.assertEqual(
                exported["trajectory_compiler_version"], PROMPT_COMPILER_VERSION
            )
            self.assertEqual(
                exported["restyle_compiler_version"], RESTYLE_COMPILER_VERSION
            )
            self.assertEqual(
                exported["approval"]["sha256"],
                hashlib.sha256(approval.read_bytes()).hexdigest(),
            )
            saved = json.loads(
                (manifest.parent / exported["annotation"]["path"]).read_text(encoding="utf-8")
            )["keyframes"]
            self.assertEqual(inherited, [
                {key: value for key, value in frame.items() if key != "camera_source"}
                for frame in saved
            ])
            diagnostic = manifest.parent / exported["diagnostic"]["path"]
            diagnostic.write_bytes(diagnostic.read_bytes() + b"tamper")
            with self.assertRaisesRegex(DirectorLoopError, "hash"):
                export_approved_iteration(manifest)

    def test_prompt_and_profile_tampering_each_blocks_approved_export(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(
                root, ROOT / "configs" / "restyle" / "station_reunion.json"
            )
            job = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            publish_iteration(manifest, job, d0 / "diagnostic.mp4", d0 / "clay.mp4")
            approve_iteration(manifest, "D1", "sy")
            exported = export_approved_iteration(manifest)

            for name in (
                "trajectory_prompt", "restyle_prompt", "compiled_prompt", "restyle_profile"
            ):
                with self.subTest(name=name):
                    path = manifest.parent / exported[name]["path"]
                    original = path.read_bytes()
                    path.write_bytes(original + b"tamper")
                    with self.assertRaisesRegex(DirectorLoopError, "hash/size mismatch"):
                        export_approved_iteration(manifest)
                    path.write_bytes(original)

    def test_publish_rejects_coordinated_job_compiler_version_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["trajectory_compiler_version"] = "adversarial-trajectory"
            job["restyle_compiler_version"] = "adversarial-restyle"
            self.rewrite_json(job_path, job)
            d0 = manifest.parent / "iterations" / "D0"

            with self.assertRaisesRegex(DirectorLoopError, "compiler version"):
                publish_iteration(
                    manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                )

    def test_approve_rejects_published_iteration_compiler_version_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            iteration_path = publish_iteration(
                manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
            )
            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            iteration["trajectory_compiler_version"] = "adversarial-trajectory"
            iteration["restyle_compiler_version"] = "adversarial-restyle"
            self.rewrite_json(iteration_path, iteration)

            with self.assertRaisesRegex(DirectorLoopError, "compiler version"):
                approve_iteration(manifest, "D1", "sy")

    def test_export_rejects_coordinated_approval_compiler_version_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            publish_iteration(manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4")
            approval_path = approve_iteration(manifest, "D1", "sy")
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval["trajectory_compiler_version"] = "adversarial-trajectory"
            approval["restyle_compiler_version"] = "adversarial-restyle"
            approval_bytes = self.rewrite_json(approval_path, approval)
            state_path = manifest.parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["approval"]["sha256"] = hashlib.sha256(approval_bytes).hexdigest()
            state["approval"]["bytes"] = len(approval_bytes)
            self.rewrite_json(state_path, state)

            with self.assertRaisesRegex(DirectorLoopError, "compiler version"):
                export_approved_iteration(manifest)

    def test_publish_rejects_rehashed_annotation_compiler_version_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            annotation_path = job_path.parent / job["inputs"]["annotation"]["path"]
            annotation_bytes = self.tamper_annotation_versions(annotation_path)
            self.rebind_record(job["inputs"]["annotation"], annotation_bytes)
            self.rewrite_json(job_path, job)
            d0 = manifest.parent / "iterations" / "D0"

            with self.assertRaisesRegex(DirectorLoopError, "annotation compiler version"):
                publish_iteration(
                    manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                )

    def test_iteration_verification_and_approve_reject_rehashed_annotation_versions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            iteration_path = publish_iteration(
                manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
            )
            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            annotation_path = manifest.parent / iteration["inputs"]["annotation"]["path"]
            annotation_bytes = self.tamper_annotation_versions(annotation_path)
            self.rebind_record(iteration["inputs"]["annotation"], annotation_bytes)
            self.rewrite_json(iteration_path, iteration)

            with self.assertRaisesRegex(DirectorLoopError, "annotation compiler version"):
                verify_workspace(manifest)
            with self.assertRaisesRegex(DirectorLoopError, "annotation compiler version"):
                approve_iteration(manifest, "D1", "sy")

    def test_export_rejects_fully_rehashed_annotation_version_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            iteration_path = publish_iteration(
                manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
            )
            approval_path = approve_iteration(manifest, "D1", "sy")

            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            annotation_path = manifest.parent / iteration["inputs"]["annotation"]["path"]
            annotation_bytes = self.tamper_annotation_versions(annotation_path)
            self.rebind_record(iteration["inputs"]["annotation"], annotation_bytes)
            iteration_bytes = self.rewrite_json(iteration_path, iteration)

            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            self.rebind_record(approval["annotation"], annotation_bytes)
            self.rebind_record(approval["iteration_manifest"], iteration_bytes)
            approval_bytes = self.rewrite_json(approval_path, approval)

            state_path = manifest.parent / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.rebind_record(state["approval"], approval_bytes)
            self.rewrite_json(state_path, state)

            with self.assertRaisesRegex(DirectorLoopError, "annotation compiler version"):
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

    def test_panel_explains_boundary_and_camera_terms_in_chinese(self) -> None:
        html = (ROOT / "static" / "director_panel.html").read_text(encoding="utf-8")
        self.assertIn('id="frozen-boundary"', html)
        self.assertIn("锁定起点", html)
        self.assertIn("什么是景别", html)
        self.assertIn("什么是插值", html)
        self.assertIn("什么是画面倾斜", html)
        self.assertIn("高级镜头参数", html)
        self.assertIn("要修改 K2，请把锁定起点选为 K1", html)

    def test_panel_auto_initializes_camera_with_provenance(self) -> None:
        html = (ROOT / "static" / "director_panel.html").read_text(encoding="utf-8")
        self.assertIn("camera_source", html)
        self.assertIn("来自当前 Proxy，可修改", html)
        self.assertIn("人工修改", html)
        self.assertIn("function missingFields", html)
        self.assertIn("function refreshCameraSource", html)
        self.assertIn("addEventListener('input'", html)
        self.assertIn("还缺少：", html)

    def test_panel_uses_trajectory_prompt_preview(self) -> None:
        html = (ROOT / "static" / "director_panel.html").read_text(encoding="utf-8")
        self.assertNotIn('id="prompt"', html)
        self.assertIn('id="visual-style"', html)
        self.assertIn('id="mood"', html)
        self.assertIn('id="prompt-preview"', html)
        self.assertIn('id="trajectory-prompt-output"', html)
        self.assertIn('id="restyle-prompt-output"', html)
        self.assertIn("预览自动 Prompt", html)
        self.assertIn("/api/prompt-preview", html)
        self.assertIn('id="trajectory-prompt-output" readonly', html)
        self.assertIn('id="restyle-prompt-output" readonly', html)
        self.assertNotIn('textarea id="prompt"', html)

    def test_panel_switches_diagnostic_and_clay_without_changing_iteration(self) -> None:
        html = (ROOT / "static" / "director_panel.html").read_text(encoding="utf-8")
        self.assertIn('id="proxy-view"', html)
        self.assertIn('value="diagnostic"', html)
        self.assertIn('value="clay"', html)
        self.assertIn("只有中性粘土视图会送入生成", html)
        self.assertIn("session.current.diagnostic_url", html)
        self.assertIn("session.current.clay_url", html)
        self.assertIn("selectedProxyView", html)
        self.assertNotIn("$('proxy-view').value='diagnostic'", html)

    def test_approved_iteration_builds_vace_job_without_rerendering_proxy(self) -> None:
        from videoactagent.vace_coded_draft import (
            build_vace_director_job,
            verify_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            publish_iteration(manifest, job, d0 / "diagnostic.mp4", d0 / "clay.mp4")
            approve_iteration(manifest, "D1", "sy")
            vace_job = build_vace_director_job(manifest, Path(root) / "vace_job")
            verified = verify_vace_coded_draft_job(vace_job)

        self.assertTrue(verified["director_binding"]["approved"])
        self.assertEqual(verified["mapping"]["src_video"], "control/src_video.mp4")
        self.assertIsNone(verified["mapping"]["src_ref_images"])
        self.assertIn("K0 to K1", verified["prompt"]["text"])
        self.assertNotIn("Subjects and wardrobe", verified["prompt"]["text"])


if __name__ == "__main__":
    unittest.main()
