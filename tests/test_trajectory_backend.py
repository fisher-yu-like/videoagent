"""Offline Task 6 tests for trajectory-compiled JD pilot bundles.

These tests use the real Task 4 compiler output and an explicitly selectable
Task 5 Blender proxy candidate.  Network submission is always patched or
blocked; passing these tests does not approve the proxy candidate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


COMPILED = Path("runs/trajectory/s01/compiled/compiled_control.json")
PROXY_MANIFEST = Path(
    os.environ.get(
        "VIDEOACTAGENT_TASK5_PROXY_MANIFEST",
        "runs/trajectory/s01/proxy_blender_fixed_20260730T001819CST/"
        "trajectory_proxy_manifest.json",
    )
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TrajectoryBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (COMPILED, PROXY_MANIFEST):
            if not path.is_file():
                raise AssertionError(f"real prerequisite artifact is missing: {path}")

    def assertTrajectoryBundleRejectedBeforeSideEffects(self, bundle):
        """Exercise the real CLI gate and prove rejection is side-effect free."""

        from videoactagent import jd_smoke

        command = f"submit-{bundle['backend']}"
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-secret"}):
                with patch.object(jd_smoke, "submit_once") as submit:
                    with self.assertRaisesRegex(ValueError, "trajectory bundle"):
                        jd_smoke.main(
                            [
                                command,
                                "--bundle",
                                str(bundle_path),
                                "--shot",
                                "s01",
                                "--prompt",
                                "trajectory_compiled",
                                "--run-root",
                                str(run_root),
                            ]
                        )
            submit.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_real_task4_and_selected_task5_candidate_prepare_hash_bound_bundles(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        compiled = json.loads(COMPILED.read_text(encoding="utf-8"))
        expected_prompt = (COMPILED.parent / "trajectory_prompt.txt").read_text(
            encoding="utf-8"
        )
        for backend in ("kling", "seedance"):
            with self.subTest(backend=backend):
                bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, backend)
                self.assertEqual(bundle["backend"], backend)
                self.assertEqual(bundle["prompt_condition"], "trajectory_compiled")
                self.assertEqual(
                    bundle["backend_capability"], {"t2v": "prompt_approximation"}
                )
                self.assertTrue(bundle["submission_ready"])
                self.assertEqual(bundle["unsupported"], [])
                self.assertEqual(bundle["submit_retry_limit"], 0)
                self.assertEqual(
                    bundle["shots"][0]["prompts"]["trajectory_compiled"],
                    expected_prompt,
                )
                expected_prompt_sha = hashlib.sha256(
                    expected_prompt.encode("utf-8")
                ).hexdigest()
                self.assertEqual(
                    bundle["submitted_prompt_sha256"], expected_prompt_sha
                )
                self.assertEqual(
                    bundle["trajectory_sha256"], compiled["trajectory_sha256"]
                )
                source = bundle["source_sha256"]
                self.assertEqual(source["compiled_control"], sha256_file(COMPILED))
                self.assertEqual(
                    source["trajectory_prompt"],
                    sha256_file(COMPILED.parent / "trajectory_prompt.txt"),
                )
                self.assertEqual(
                    source["proxy_manifest"], sha256_file(PROXY_MANIFEST)
                )
                self.assertEqual(
                    source["trajectory"],
                    compiled["source_artifact_sha256"]["trajectory"],
                )
                self.assertEqual(
                    source["shotscript"],
                    compiled["source_artifact_sha256"]["shotscript"],
                )
                proxy = json.loads(PROXY_MANIFEST.read_text(encoding="utf-8"))
                self.assertEqual(source["proxy_video"], proxy["video"]["sha256"])
                self.assertEqual(
                    bundle["submitted_prompt_sha256"],
                    source["trajectory_prompt"],
                )

    def test_preparation_rejects_unsupported_control(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        compiled = json.loads(COMPILED.read_text(encoding="utf-8"))
        compiled["unsupported"] = [
            {
                "track_id": "local_deformation_01",
                "target_type": "deformation",
                "reason": "unsupported_by_t2v_backend",
            }
        ]
        compiled["submission_ready"] = False
        with tempfile.TemporaryDirectory() as root:
            mutated = Path(root) / "compiled_control.json"
            mutated.write_text(json.dumps(compiled), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                prepare_api_bundle(mutated, PROXY_MANIFEST, "kling")

    def test_preparation_rejects_proxy_trajectory_hash_mismatch(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        proxy = json.loads(PROXY_MANIFEST.read_text(encoding="utf-8"))
        proxy["trajectory_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            mutated = root_path / "trajectory_proxy_manifest.json"
            mutated.write_text(json.dumps(proxy), encoding="utf-8")
            proxy_video = PROXY_MANIFEST.parent / proxy["video"]["path"]
            (root_path / proxy["video"]["path"]).write_bytes(proxy_video.read_bytes())
            with self.assertRaisesRegex(ValueError, "trajectory hash mismatch"):
                prepare_api_bundle(COMPILED, mutated, "seedance")

    def test_cli_writes_once_and_refuses_to_mutate_existing_bundle(self):
        from videoactagent.trajectory_backend import main

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "kling" / "bundle.json"
            args = [
                "prepare",
                "--compiled",
                str(COMPILED),
                "--proxy-manifest",
                str(PROXY_MANIFEST),
                "--backend",
                "kling",
                "--output",
                str(output),
            ]
            self.assertEqual(main(args), 0)
            original = output.read_bytes()
            self.assertEqual(main(args), 2)
            self.assertEqual(output.read_bytes(), original)

    def test_concurrent_target_creation_cannot_be_clobbered(self):
        import videoactagent.trajectory_backend as trajectory_backend

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "kling" / "bundle.json"
            sentinel = b"concurrently-created\n"
            original_link = os.link

            def race_then_link(source, destination):
                Path(destination).write_bytes(sentinel)
                return original_link(source, destination)

            with patch.object(
                trajectory_backend.os, "link", side_effect=race_then_link
            ):
                result = trajectory_backend.main(
                    [
                        "prepare",
                        "--compiled",
                        str(COMPILED),
                        "--proxy-manifest",
                        str(PROXY_MANIFEST),
                        "--backend",
                        "kling",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(result, 2)
            self.assertEqual(output.read_bytes(), sentinel)

    def test_missing_credential_fails_before_trajectory_run_creation(self):
        from videoactagent import jd_smoke
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": ""}):
                with self.assertRaisesRegex(RuntimeError, "JD_KLING_KEY is required"):
                    jd_smoke.main(
                        [
                            "submit-kling",
                            "--bundle",
                            str(bundle_path),
                            "--shot",
                            "s01",
                            "--prompt",
                            "trajectory_compiled",
                            "--run-root",
                            str(run_root),
                        ]
                    )
            self.assertFalse(run_root.exists())

    def test_unsupported_trajectory_bundle_is_blocked_before_run_or_transport(self):
        from videoactagent import jd_smoke
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        bundle["submission_ready"] = False
        bundle["unsupported"] = [{"reason": "unsupported_by_t2v_backend"}]
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-secret"}):
                with patch.object(jd_smoke, "submit_once") as submit:
                    with self.assertRaisesRegex(ValueError, "unsupported"):
                        jd_smoke.main(
                            [
                                "submit-kling",
                                "--bundle",
                                str(bundle_path),
                                "--shot",
                                "s01",
                                "--prompt",
                                "trajectory_compiled",
                                "--run-root",
                                str(run_root),
                            ]
                        )
            submit.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_missing_unsupported_field_is_blocked_before_run_or_transport(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        del bundle["unsupported"]
        self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_nonempty_or_nonlist_unsupported_is_blocked_before_side_effects(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        original = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        for unsupported in (None, {}, ["unsupported_by_t2v_backend"]):
            with self.subTest(unsupported=unsupported):
                bundle = copy.deepcopy(original)
                bundle["unsupported"] = unsupported
                self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_each_required_source_digest_is_required_before_side_effects(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        original = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        for field in (
            "trajectory",
            "shotscript",
            "compiled_control",
            "trajectory_prompt",
            "patched_shotscript",
            "proxy_manifest",
            "proxy_video",
        ):
            with self.subTest(field=field):
                bundle = copy.deepcopy(original)
                del bundle["source_sha256"][field]
                self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_each_required_source_digest_must_be_lowercase_sha256(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        original = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "seedance")
        for field in original["source_sha256"]:
            with self.subTest(field=field):
                bundle = copy.deepcopy(original)
                bundle["source_sha256"][field] = "A" * 64
                self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_trajectory_digest_must_equal_source_trajectory_before_side_effects(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        bundle["trajectory_sha256"] = "0" * 64
        self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_canonical_submitted_prompt_binding_is_required_before_side_effects(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "kling")
        bundle.pop("submitted_prompt_sha256", None)
        self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_tampered_submitted_prompt_is_blocked_before_run_or_transport(self):
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "seedance")
        bundle["shots"][0]["prompts"]["trajectory_compiled"] += " tampered"
        self.assertTrajectoryBundleRejectedBeforeSideEffects(bundle)

    def test_submit_reuses_jd_transport_once_and_persists_provenance_not_secret(self):
        from videoactagent import jd_smoke
        from videoactagent.trajectory_backend import prepare_api_bundle

        bundle = prepare_api_bundle(COMPILED, PROXY_MANIFEST, "seedance")
        captured = {}

        def capture_submit(payload, api_key, base_url, run):
            captured.update(
                payload=copy.deepcopy(payload),
                api_key=api_key,
                base_url=base_url,
                run=run,
            )
            return "controlled-task-id"

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            environment = {
                "JD_KLING_KEY": "unit-secret",
                "JD_KLING_BASE": "https://modelservice.jdcloud.com",
            }
            with patch.dict(os.environ, environment):
                with patch.object(
                    jd_smoke, "submit_once", side_effect=capture_submit
                ) as submit:
                    jd_smoke.main(
                        [
                            "submit-seedance",
                            "--bundle",
                            str(bundle_path),
                            "--shot",
                            "s01",
                            "--prompt",
                            "trajectory_compiled",
                            "--run-root",
                            str(run_root),
                        ]
                    )

            self.assertEqual(submit.call_count, 1)
            run = captured["run"]
            metadata = json.loads(
                (run.path / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["prompt_condition"], "trajectory_compiled")
            self.assertEqual(metadata["submit_retry_limit"], 0)
            self.assertEqual(
                metadata["trajectory_sha256"], bundle["trajectory_sha256"]
            )
            self.assertEqual(metadata["source_sha256"], bundle["source_sha256"])
            self.assertEqual(
                metadata["compiled_prompt"],
                bundle["shots"][0]["prompts"]["trajectory_compiled"],
            )
            self.assertEqual(
                metadata["backend_capability"], {"t2v": "prompt_approximation"}
            )
            self.assertEqual(
                captured["payload"]["content"][0]["text"],
                metadata["compiled_prompt"],
            )
            self.assertEqual(captured["api_key"], "unit-secret")
            for path in run.path.rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        "unit-secret", path.read_text(encoding="utf-8")
                    )


if __name__ == "__main__":
    unittest.main()
