from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BASES = {
    backend: ROOT / "runs" / "trajectory_api_pilot" / "prepared" / backend / "bundle.json"
    for backend in ("kling", "seedance")
}
REVISIONS = {
    backend: ROOT / "runs" / "trajectory_closed_loop" / f"{backend}_s01_revision.json"
    for backend in ("kling", "seedance")
}
CONDITION = "trajectory_compiled_feedback_revision"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RevisionBundlePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [str(path) for path in (*BASES.values(), *REVISIONS.values()) if not path.is_file()]
        if missing:
            raise AssertionError(f"real Task 6/8 inputs missing: {missing}")

    def test_real_task8_revisions_build_backend_specific_hash_bound_bundles(self):
        from videoactagent.trajectory_backend import prepare_revision_api_bundle

        for backend in ("kling", "seedance"):
            with self.subTest(backend=backend):
                bundle = prepare_revision_api_bundle(REVISIONS[backend], BASES[backend], backend)
                revision = json.loads(REVISIONS[backend].read_text(encoding="utf-8"))
                base = json.loads(BASES[backend].read_text(encoding="utf-8"))
                self.assertEqual(bundle["backend"], backend)
                self.assertEqual(bundle["prompt_condition"], CONDITION)
                self.assertEqual(bundle["unsupported"], [])
                self.assertEqual(bundle["submit_retry_limit"], 0)
                self.assertTrue(bundle["submission_ready"])
                self.assertEqual(bundle["trajectory_sha256"], base["trajectory_sha256"])
                self.assertEqual(bundle["source_sha256"], base["source_sha256"])
                self.assertEqual(bundle["base_bundle_sha256"], _sha(BASES[backend]))
                self.assertEqual(bundle["revision_artifact_sha256"], _sha(REVISIONS[backend]))
                self.assertEqual(
                    bundle["revision_prompt_binding"],
                    {
                        "revision_artifact_sha256": _sha(REVISIONS[backend]),
                        "revised_prompt_sha256": bundle["submitted_prompt_sha256"],
                    },
                )
                self.assertEqual(bundle["revision_source_sha256"], revision["source_sha256"])
                self.assertEqual(bundle["revision_generation"], 1)
                self.assertEqual(bundle["operations"], revision["operations"])
                prompt = bundle["shots"][0]["prompts"][CONDITION]
                self.assertEqual(prompt, revision["revised_prompt"])
                self.assertEqual(
                    bundle["submitted_prompt_sha256"],
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                )

    def test_revision_policy_and_hash_relationships_fail_closed(self):
        from videoactagent.trajectory_backend import prepare_revision_api_bundle

        original = json.loads(REVISIONS["kling"].read_text(encoding="utf-8"))
        cases = []
        for field, value in (
            ("revision_generation", True),
            ("revision_generation", 0),
            ("max_revision_generation", 2),
            ("submission_allowed", True),
            ("network_called", True),
            ("operations", []),
            ("operations", ["invent_operation"]),
            ("revised_prompt_sha256", "0" * 64),
        ):
            document = copy.deepcopy(original)
            document[field] = value
            cases.append(document)
        document = copy.deepcopy(original)
        document["operations"] = list(reversed(document["operations"]))
        cases.append(document)
        for source_field in ("evaluation_report", "video", "trajectory", "original_prompt"):
            document = copy.deepcopy(original)
            document["source_sha256"][source_field] = "0" * 64
            cases.append(document)
        document = copy.deepcopy(original)
        document["decisions"][0]["operations"] = ["invent_operation"]
        cases.append(document)
        document = copy.deepcopy(original)
        document["decisions"][0]["operations"] = ["split_time_segments"]
        cases.append(document)
        document = copy.deepcopy(original)
        document["metric_thresholds"].pop("arrival_error_max")
        cases.append(document)
        document = copy.deepcopy(original)
        document["evaluator_provenance"]["unexpected"] = "0" * 64
        cases.append(document)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "revision.json"
            for index, document in enumerate(cases):
                with self.subTest(case=index):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        prepare_revision_api_bundle(path, BASES["kling"], "kling")

    def test_base_bundle_seven_digests_and_trajectory_binding_fail_closed(self):
        from videoactagent.trajectory_backend import prepare_revision_api_bundle

        original = json.loads(BASES["kling"].read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "base.json"
            mutations = []
            for field in original["source_sha256"]:
                document = copy.deepcopy(original)
                document["source_sha256"][field] = True
                mutations.append(document)
            document = copy.deepcopy(original)
            document["trajectory_sha256"] = "0" * 64
            mutations.append(document)
            for index, document in enumerate(mutations):
                with self.subTest(case=index):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        prepare_revision_api_bundle(REVISIONS["kling"], path, "kling")

    def test_duplicate_nonfinite_and_oversized_numbers_are_rejected(self):
        from videoactagent.trajectory_backend import prepare_revision_api_bundle

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "revision.json"
            invalid = (
                '{"schema_version":"0.1","schema_version":"0.1"}',
                '{"revision_generation":NaN}',
                '{"revision_generation":1e400}',
                '{"revision_generation":' + str(10**400) + '}',
            )
            for text in invalid:
                with self.subTest(text=text[:40]):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        prepare_revision_api_bundle(path, BASES["kling"], "kling")

    def test_cli_is_workspace_safe_atomic_immutable_and_input_alias_safe(self):
        from videoactagent.trajectory_backend import main

        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            workspace = Path(root)
            revision = workspace / "revision.json"
            base = workspace / "base.json"
            revision.write_bytes(REVISIONS["kling"].read_bytes())
            base.write_bytes(BASES["kling"].read_bytes())
            output = workspace / "prepared" / "bundle.json"
            args = [
                "prepare-revision", "--revision", str(revision), "--base-bundle", str(base),
                "--backend", "kling", "--workspace", str(workspace), "--output", str(output),
            ]
            self.assertEqual(main(args), 0)
            original = output.read_bytes()
            self.assertEqual(main(args), 2)
            self.assertEqual(output.read_bytes(), original)

            alias = workspace / "revision_alias.json"
            os.link(revision, alias)
            collision = list(args)
            collision[collision.index(str(output))] = str(alias)
            self.assertEqual(main(collision), 2)
            self.assertEqual(revision.read_bytes(), REVISIONS["kling"].read_bytes())

    def test_source_replacement_during_validation_is_detected(self):
        import videoactagent.trajectory_backend as trajectory_backend

        with tempfile.TemporaryDirectory() as root:
            revision_path = Path(root) / "revision.json"
            revision_path.write_bytes(REVISIONS["kling"].read_bytes())
            original_validate = trajectory_backend._validate_revision

            def validate_then_replace(*args, **kwargs):
                result = original_validate(*args, **kwargs)
                document = json.loads(revision_path.read_text(encoding="utf-8"))
                document["track_id"] = "replaced-after-snapshot"
                revision_path.write_text(json.dumps(document), encoding="utf-8")
                return result

            with patch.object(trajectory_backend, "_validate_revision", side_effect=validate_then_replace):
                with self.assertRaisesRegex(ValueError, "changed after validation"):
                    trajectory_backend.prepare_revision_api_bundle(
                        revision_path, BASES["kling"], "kling"
                    )


class RevisionGatewayTests(unittest.TestCase):
    def _bundle(self, backend: str) -> dict:
        from videoactagent.trajectory_backend import prepare_revision_api_bundle

        return prepare_revision_api_bundle(REVISIONS[backend], BASES[backend], backend)

    def _assert_rejected_before_any_side_effect(self, bundle: dict):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            with patch.object(jd_smoke, "require_key") as credential, patch.object(
                jd_smoke, "submit_once"
            ) as submit:
                with self.assertRaises(ValueError):
                    jd_smoke.main([
                        f"submit-{bundle.get('backend', 'kling')}", "--bundle", str(bundle_path),
                        "--shot", "s01", "--prompt", CONDITION, "--run-root", str(run_root),
                    ])
            credential.assert_not_called()
            submit.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_every_revision_provenance_mutation_rejected_before_credential(self):
        original = self._bundle("kling")
        mutations = []
        for field in ("base_bundle_sha256", "revision_artifact_sha256", "submitted_prompt_sha256"):
            document = copy.deepcopy(original)
            document[field] = "0" * 64
            mutations.append(document)
        for field in original["source_sha256"]:
            document = copy.deepcopy(original)
            document["source_sha256"][field] = True
            mutations.append(document)
        for field in original["revision_source_sha256"]:
            document = copy.deepcopy(original)
            document["revision_source_sha256"][field] = True
            mutations.append(document)
        document = copy.deepcopy(original)
        document["revision_source_sha256"]["original_prompt"] = "1" * 64
        mutations.append(document)
        document = copy.deepcopy(original)
        document["shots"][0]["prompts"][CONDITION] += " tampered"
        mutations.append(document)
        document = copy.deepcopy(original)
        document["unsupported"] = ["x"]
        mutations.append(document)
        document = copy.deepcopy(original)
        document["revision_prompt_binding"]["revised_prompt_sha256"] = "0" * 64
        mutations.append(document)
        document = copy.deepcopy(original)
        document["unexpected"] = "smuggled"
        mutations.append(document)
        for mutation in mutations:
            self._assert_rejected_before_any_side_effect(mutation)

    def test_duration_that_rounds_to_zero_is_rejected_before_credential(self):
        bundle = self._bundle("kling")
        bundle["shots"][0]["duration"] = 0.1
        self._assert_rejected_before_any_side_effect(bundle)

    def test_valid_revision_uses_transport_once_and_records_no_secret(self):
        from videoactagent import jd_smoke

        for backend in ("kling", "seedance"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                bundle = self._bundle(backend)
                bundle_path = root_path / "bundle.json"
                bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
                captured = {}

                def submit_once(payload, api_key, base_url, run):
                    captured.update(payload=copy.deepcopy(payload), run=run, api_key=api_key)
                    return "task-id"

                with patch.object(jd_smoke, "require_key", return_value="revision-secret"), patch.object(
                    jd_smoke, "submit_once", side_effect=submit_once
                ) as submit:
                    jd_smoke.main([
                        f"submit-{backend}", "--bundle", str(bundle_path), "--shot", "s01",
                        "--prompt", CONDITION, "--run-root", str(root_path / "runs"),
                    ])
                self.assertEqual(submit.call_count, 1)
                metadata = json.loads((captured["run"].path / "metadata.json").read_text())
                self.assertEqual(metadata["base_bundle_sha256"], bundle["base_bundle_sha256"])
                self.assertEqual(metadata["revision_artifact_sha256"], bundle["revision_artifact_sha256"])
                self.assertEqual(metadata["revision_bundle_sha256"], _sha(bundle_path))
                self.assertEqual(metadata["submit_retry_limit"], 0)
                self.assertEqual(captured["payload"]["content"][0]["text"], bundle["shots"][0]["prompts"][CONDITION])
                for path in captured["run"].path.rglob("*"):
                    if path.is_file():
                        self.assertNotIn("revision-secret", path.read_text(encoding="utf-8"))

    def test_bundle_replacement_during_credential_lookup_fails_before_run_or_transport(self):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            bundle_path.write_text(json.dumps(self._bundle("kling")), encoding="utf-8")

            def credential_then_replace():
                document = json.loads(bundle_path.read_text(encoding="utf-8"))
                document["scene_id"] = "replaced-after-validation"
                bundle_path.write_text(json.dumps(document), encoding="utf-8")
                return "secret"

            with patch.object(jd_smoke, "require_key", side_effect=credential_then_replace), patch.object(
                jd_smoke, "submit_once"
            ) as submit:
                with self.assertRaisesRegex(ValueError, "changed after validation"):
                    jd_smoke.main([
                        "submit-kling", "--bundle", str(bundle_path), "--shot", "s01",
                        "--prompt", CONDITION, "--run-root", str(root_path / "runs"),
                    ])
            submit.assert_not_called()
            self.assertFalse((root_path / "runs").exists())


if __name__ == "__main__":
    unittest.main()
