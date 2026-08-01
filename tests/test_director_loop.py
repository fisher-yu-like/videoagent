from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import videoactagent.director_loop as director_loop_module

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

    def test_new_workspace_iteration_state_and_job_use_schema_1_1(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            documents = (
                json.loads(manifest.read_text(encoding="utf-8")),
                json.loads((manifest.parent / "state.json").read_text(encoding="utf-8")),
                json.loads((manifest.parent / "iterations" / "D0" / "iteration.json").read_text(encoding="utf-8")),
                json.loads(job_path.read_text(encoding="utf-8")),
            )
            self.assertEqual([document["schema_version"] for document in documents], [
                "1.1", "1.1", "1.1", "1.1",
            ])

    def test_legacy_schema_1_0_requires_new_workspace_migration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["schema_version"] = "1.0"
            self.rewrite_json(manifest, document)

            with self.assertRaisesRegex(
                DirectorLoopError,
                "migration required.*prepare a new workspace.*reuse the human annotation",
            ):
                verify_workspace(manifest)

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

    def test_publish_rejects_rehashed_arbitrary_compiled_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            replacements = {
                "trajectory_prompt": b"forged trajectory prompt\n",
                "compiled_prompt": b"forged trajectory prompt\n",
                "restyle_prompt": b"forged restyle prompt\n",
            }
            for name, data in replacements.items():
                path = job_path.parent / job["inputs"][name]["path"]
                path.write_bytes(data)
                self.rebind_record(job["inputs"][name], data)
            self.rewrite_json(job_path, job)
            d0 = manifest.parent / "iterations" / "D0"

            with self.assertRaisesRegex(DirectorLoopError, "compiled director artifact"):
                publish_iteration(
                    manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                )

    def test_failed_compiler_preflight_leaves_publish_state_transactionally_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            state_path = manifest.parent / "state.json"
            state_before = state_path.read_bytes()
            job = json.loads(job_path.read_text(encoding="utf-8"))
            compiled_path = job_path.parent / job["inputs"]["compiled_prompt"]["path"]
            forged = b"rehashed but incompatible compatibility prompt\n"
            compiled_path.write_bytes(forged)
            self.rebind_record(job["inputs"]["compiled_prompt"], forged)
            self.rewrite_json(job_path, job)
            d0 = manifest.parent / "iterations" / "D0"

            with self.assertRaisesRegex(DirectorLoopError, "compiled director artifact"):
                publish_iteration(
                    manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                )

            iteration = job_path.parent
            self.assertFalse((iteration / "iteration.json").exists())
            self.assertFalse((iteration / "approval.json").exists())
            self.assertFalse((iteration / "diagnostic.mp4").exists())
            self.assertFalse((iteration / "clay.mp4").exists())
            self.assertEqual(
                json.loads(job_path.read_text(encoding="utf-8"))["status"], "queued"
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual((state["current_iteration"], state["next_iteration"]), ("D0", 1))

    def test_publish_probes_and_commits_the_same_staged_media_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            diagnostic_source = Path(root) / "external-diagnostic.mp4"
            clay_source = Path(root) / "external-clay.mp4"
            shutil.copyfile(d0 / "diagnostic.mp4", diagnostic_source)
            shutil.copyfile(d0 / "clay.mp4", clay_source)
            diagnostic_before = diagnostic_source.read_bytes()
            real_media = director_loop_module._media
            probes = 0

            def mutate_source_after_first_probe(path: Path, timeline: dict) -> dict:
                nonlocal probes
                result = real_media(path, timeline)
                probes += 1
                if probes == 1:
                    diagnostic_source.write_bytes(clay_source.read_bytes())
                return result

            with mock.patch(
                "videoactagent.director_loop._media",
                side_effect=mutate_source_after_first_probe,
            ):
                iteration_path = publish_iteration(
                    manifest, job_path, diagnostic_source, clay_source
                )

            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            published_diagnostic = manifest.parent / iteration["diagnostic"]["path"]
            published_clay = manifest.parent / iteration["clay"]["path"]
            self.assertEqual(published_diagnostic.read_bytes(), diagnostic_before)
            self.assertNotEqual(
                iteration["diagnostic"]["media"]["decoded_pixel_sha256"],
                iteration["clay"]["media"]["decoded_pixel_sha256"],
            )
            self.assertNotEqual(published_diagnostic.read_bytes(), published_clay.read_bytes())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_publish_accepts_validating_status_from_render_worker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["status"] = "validating"
            self.rewrite_json(job_path, job)
            d0 = manifest.parent / "iterations" / "D0"

            iteration_path = publish_iteration(
                manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
            )

            self.assertTrue(iteration_path.is_file())
            self.assertEqual(
                json.loads(job_path.read_text(encoding="utf-8"))["status"], "succeeded"
            )

    def test_publish_rejects_aliased_compiler_input_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["inputs"]["compiled_prompt"] = deepcopy(
                job["inputs"]["trajectory_prompt"]
            )
            self.rewrite_json(job_path, job)
            d0 = manifest.parent / "iterations" / "D0"

            with self.assertRaisesRegex(DirectorLoopError, "input paths must be distinct"):
                publish_iteration(
                    manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                )

            self.assertFalse((job_path.parent / "iteration.json").exists())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_publish_commits_verified_staged_input_after_queued_input_race(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            prompt_path = job_path.parent / job["inputs"]["restyle_prompt"]["path"]
            verified_prompt = prompt_path.read_bytes()
            d0 = manifest.parent / "iterations" / "D0"
            real_media = director_loop_module._media
            probes = 0

            def mutate_prompt_after_media_preflight(path: Path, timeline: dict) -> dict:
                nonlocal probes
                result = real_media(path, timeline)
                probes += 1
                if probes == 2:
                    prompt_path.write_bytes(b"raced queued restyle prompt\n")
                return result

            with mock.patch(
                "videoactagent.director_loop._media",
                side_effect=mutate_prompt_after_media_preflight,
            ):
                iteration_path = publish_iteration(
                    manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                )

            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            published_prompt = manifest.parent / iteration["inputs"]["restyle_prompt"]["path"]
            self.assertEqual(published_prompt.read_bytes(), verified_prompt)
            self.assertEqual(
                iteration["inputs"]["restyle_prompt"]["sha256"],
                hashlib.sha256(verified_prompt).hexdigest(),
            )
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_publish_rejects_staged_prompt_mutation_during_media_probe(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            state_path = manifest.parent / "state.json"
            job_before = job_path.read_bytes()
            state_before = state_path.read_bytes()
            job = json.loads(job_before)
            input_bytes = {
                name: (job_path.parent / record["path"]).read_bytes()
                for name, record in job["inputs"].items()
            }
            d0 = manifest.parent / "iterations" / "D0"
            real_media = director_loop_module._media
            mutated = False

            def mutate_staged_prompt(path: Path, timeline: dict) -> dict:
                nonlocal mutated
                result = real_media(path, timeline)
                if not mutated:
                    staging = path.parent.parent
                    prompt = next((staging / "inputs" / "restyle_prompt").iterdir())
                    prompt.write_bytes(b"forged staged restyle prompt\n")
                    mutated = True
                return result

            with mock.patch(
                "videoactagent.director_loop._media", side_effect=mutate_staged_prompt
            ):
                with self.assertRaisesRegex(DirectorLoopError, "compiled director artifact"):
                    publish_iteration(
                        manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                    )

            self.assertEqual(job_path.read_bytes(), job_before)
            self.assertEqual(state_path.read_bytes(), state_before)
            for name, record in job["inputs"].items():
                self.assertEqual(
                    (job_path.parent / record["path"]).read_bytes(), input_bytes[name]
                )
            self.assertFalse((job_path.parent / "iteration.json").exists())
            self.assertFalse((job_path.parent / "diagnostic.mp4").exists())
            self.assertFalse((job_path.parent / "clay.mp4").exists())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_publish_rejects_staged_diagnostic_mutation_after_initial_probe(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            state_path = manifest.parent / "state.json"
            job_before = job_path.read_bytes()
            state_before = state_path.read_bytes()
            job = json.loads(job_before)
            input_bytes = {
                name: (job_path.parent / record["path"]).read_bytes()
                for name, record in job["inputs"].items()
            }
            d0 = manifest.parent / "iterations" / "D0"
            real_media = director_loop_module._media
            mutated = False

            def mutate_staged_diagnostic(path: Path, timeline: dict) -> dict:
                nonlocal mutated
                result = real_media(path, timeline)
                if not mutated:
                    path.write_bytes((path.parent / "clay.mp4").read_bytes())
                    mutated = True
                return result

            with mock.patch(
                "videoactagent.director_loop._media", side_effect=mutate_staged_diagnostic
            ):
                with self.assertRaisesRegex(DirectorLoopError, "pixel-identical|changed"):
                    publish_iteration(
                        manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                    )

            self.assertEqual(job_path.read_bytes(), job_before)
            self.assertEqual(state_path.read_bytes(), state_before)
            for name, record in job["inputs"].items():
                self.assertEqual(
                    (job_path.parent / record["path"]).read_bytes(), input_bytes[name]
                )
            self.assertFalse((job_path.parent / "iteration.json").exists())
            self.assertFalse((job_path.parent / "diagnostic.mp4").exists())
            self.assertFalse((job_path.parent / "clay.mp4").exists())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_workspace_reconstructs_rehashed_human_compiler_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            iteration_path = publish_iteration(
                manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
            )
            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            prompt_path = manifest.parent / iteration["inputs"]["restyle_prompt"]["path"]
            forged = b"fully rehashed final restyle forgery\n"
            prompt_path.write_bytes(forged)
            self.rebind_record(iteration["inputs"]["restyle_prompt"], forged)
            self.rewrite_json(iteration_path, iteration)

            with self.assertRaisesRegex(DirectorLoopError, "compiled director artifact"):
                verify_workspace(manifest)

    def test_workspace_redecodes_rehashed_human_media_and_requires_inequality(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            d0 = manifest.parent / "iterations" / "D0"
            iteration_path = publish_iteration(
                manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
            )
            iteration = json.loads(iteration_path.read_text(encoding="utf-8"))
            diagnostic_path = manifest.parent / iteration["diagnostic"]["path"]
            clay_path = manifest.parent / iteration["clay"]["path"]
            clay_bytes = clay_path.read_bytes()
            diagnostic_path.write_bytes(clay_bytes)
            self.rebind_record(iteration["diagnostic"], clay_bytes)
            iteration["diagnostic"]["media"] = deepcopy(iteration["clay"]["media"])
            self.rewrite_json(iteration_path, iteration)

            with self.assertRaisesRegex(DirectorLoopError, "pixel-identical"):
                verify_workspace(manifest)

    def test_post_precommit_staged_mutation_rolls_back_original_queued_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            state_path = manifest.parent / "state.json"
            job_before = job_path.read_bytes()
            state_before = state_path.read_bytes()
            job = json.loads(job_before)
            input_paths = {
                name: job_path.parent / record["path"]
                for name, record in job["inputs"].items()
            }
            input_bytes = {name: path.read_bytes() for name, path in input_paths.items()}
            d0 = manifest.parent / "iterations" / "D0"
            real_atomic_copy = director_loop_module._atomic_copy
            mutated = False

            def mutate_after_precommit(source: Path, target: Path) -> None:
                nonlocal mutated
                if (
                    not mutated
                    and ".publish-" in source.as_posix()
                    and "/inputs/restyle_prompt/" in source.as_posix()
                ):
                    source.write_bytes(b"post-precommit staged forgery\n")
                    mutated = True
                real_atomic_copy(source, target)

            with mock.patch(
                "videoactagent.director_loop._atomic_copy",
                side_effect=mutate_after_precommit,
            ):
                with self.assertRaisesRegex(
                    DirectorLoopError, "restyle_prompt.*(hash/size|compiled director)"
                ):
                    publish_iteration(
                        manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                    )

            self.assertEqual(job_path.read_bytes(), job_before)
            self.assertEqual(state_path.read_bytes(), state_before)
            for name, path in input_paths.items():
                self.assertEqual(path.read_bytes(), input_bytes[name], name)
            self.assertFalse((job_path.parent / "iteration.json").exists())
            self.assertFalse((job_path.parent / "diagnostic.mp4").exists())
            self.assertFalse((job_path.parent / "clay.mp4").exists())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_final_verification_failure_restores_every_prepublication_byte(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            state_path = manifest.parent / "state.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            input_paths = {
                name: job_path.parent / record["path"]
                for name, record in job["inputs"].items()
            }
            before = {
                "job": job_path.read_bytes(),
                "state": state_path.read_bytes(),
                **{name: path.read_bytes() for name, path in input_paths.items()},
            }
            d0 = manifest.parent / "iterations" / "D0"
            real_verify = director_loop_module.verify_workspace
            verifications = 0

            def fail_final_verification(path: Path | str) -> dict:
                nonlocal verifications
                verifications += 1
                if verifications == 2:
                    raise DirectorLoopError("injected final verification failure")
                return real_verify(path)

            with mock.patch(
                "videoactagent.director_loop.verify_workspace",
                side_effect=fail_final_verification,
            ):
                with self.assertRaisesRegex(DirectorLoopError, "injected final verification"):
                    publish_iteration(
                        manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                    )

            iteration = job_path.parent
            self.assertEqual(job_path.read_bytes(), before["job"])
            self.assertEqual(state_path.read_bytes(), before["state"])
            for name, path in input_paths.items():
                self.assertEqual(path.read_bytes(), before[name], name)
            self.assertFalse((iteration / "iteration.json").exists())
            self.assertFalse((iteration / "diagnostic.mp4").exists())
            self.assertFalse((iteration / "clay.mp4").exists())
            self.assertFalse((iteration / "approval.json").exists())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

    def test_mutated_staged_media_backups_cannot_corrupt_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manifest = self.make_workspace(root)
            job_path = prepare_iteration(manifest, self.director_payload(manifest))
            state_path = manifest.parent / "state.json"
            diagnostic_path = job_path.parent / "diagnostic.mp4"
            clay_path = job_path.parent / "clay.mp4"
            diagnostic_before = b"pre-existing diagnostic bytes\n"
            clay_before = b"pre-existing clay bytes\n"
            diagnostic_path.write_bytes(diagnostic_before)
            clay_path.write_bytes(clay_before)
            job_before = job_path.read_bytes()
            state_before = state_path.read_bytes()
            job = json.loads(job_before)
            input_paths = {
                name: job_path.parent / record["path"]
                for name, record in job["inputs"].items()
            }
            input_bytes = {name: path.read_bytes() for name, path in input_paths.items()}
            d0 = manifest.parent / "iterations" / "D0"
            real_atomic_copy = director_loop_module._atomic_copy
            real_verify = director_loop_module.verify_workspace
            mutated = False
            verifications = 0

            def mutate_media_backups(source: Path, target: Path) -> None:
                nonlocal mutated
                if not mutated and ".publish-" in source.as_posix():
                    staging = next(
                        parent for parent in source.parents
                        if parent.name.startswith(".publish-")
                    )
                    media_backup = staging / "original" / "media"
                    media_target = media_backup if media_backup.exists() else staging / "media"
                    (media_target / "diagnostic.mp4").write_bytes(
                        b"forged diagnostic staging bytes\n"
                    )
                    (media_target / "clay.mp4").write_bytes(
                        b"forged clay staging bytes\n"
                    )
                    mutated = True
                real_atomic_copy(source, target)

            def fail_final_verification(path: Path | str) -> dict:
                nonlocal verifications
                verifications += 1
                if verifications == 2:
                    raise DirectorLoopError("injected final verification failure")
                return real_verify(path)

            with (
                mock.patch(
                    "videoactagent.director_loop._atomic_copy",
                    side_effect=mutate_media_backups,
                ),
                mock.patch(
                    "videoactagent.director_loop.verify_workspace",
                    side_effect=fail_final_verification,
                ),
            ):
                with self.assertRaisesRegex(DirectorLoopError, "injected final verification"):
                    publish_iteration(
                        manifest, job_path, d0 / "diagnostic.mp4", d0 / "clay.mp4"
                    )

            self.assertTrue(mutated)
            self.assertEqual(diagnostic_path.read_bytes(), diagnostic_before)
            self.assertEqual(clay_path.read_bytes(), clay_before)
            self.assertEqual(job_path.read_bytes(), job_before)
            self.assertEqual(state_path.read_bytes(), state_before)
            for name, path in input_paths.items():
                self.assertEqual(path.read_bytes(), input_bytes[name], name)
            self.assertFalse((job_path.parent / "iteration.json").exists())
            self.assertFalse((job_path.parent / "approval.json").exists())
            self.assertEqual(list(manifest.parent.glob(".publish-*.staging")), [])

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
