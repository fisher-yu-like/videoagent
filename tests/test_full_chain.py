"""Contract tests for the offline immutable full-chain matrix compiler."""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "runs" / "work" / "whole_story_v4"


class FullChainMatrixTests(unittest.TestCase):
    def _copy_frozen_source(self, root: Path) -> None:
        destination = root / "runs" / "work" / "whole_story_v4"
        destination.mkdir(parents=True)
        shutil.copy2(SOURCE_ROOT / "summary.json", destination / "summary.json")
        summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
        for case in summary["cases"]:
            story_id = case["story_id"]
            source_case = SOURCE_ROOT / story_id
            target_case = destination / story_id
            (target_case / "sources").mkdir(parents=True)
            (target_case / "bundles").mkdir()
            shutil.copy2(source_case / "manifest.json", target_case / "manifest.json")
            for name in ("prompt.txt", "shotscript.json"):
                shutil.copy2(source_case / "sources" / name, target_case / "sources" / name)
            for backend in ("kling", "seedance", "vace"):
                shutil.copy2(source_case / "bundles" / f"{backend}.json", target_case / "bundles" / f"{backend}.json")

    def _config_document(self) -> dict[str, object]:
        summary = json.loads((SOURCE_ROOT / "summary.json").read_text(encoding="utf-8"))
        return {
            "schema_version": "1.0",
            "source_summary": "runs/work/whole_story_v4/summary.json",
            "story_ids": [case["story_id"] for case in summary["cases"]],
            "canary_story_ids": ["station_reunion", "studio_formation"],
            "backends": ["kling", "seedance", "vace"],
            "request": {"duration_seconds": 5.0, "query_limit": 4, "download_limit": 1},
            "vace": {"frame_count": 81, "fps": 16, "seed": 2026},
            "budgets": {
                "generation_submissions": 16,
                "status_queries": 64,
                "downloads": 16,
                "vace_inferences": 8,
                "automatic_retries": 0,
            },
            "submit": False,
            "release_policy": "explicit_matrix_bound_token",
            "release_gates": {"canary_enabled": False, "remainder_enabled": False},
        }

    def _temporary_config(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        self._copy_frozen_source(root)
        config_path = root / "configs" / "full_chain_matrix.json"
        config_path.parent.mkdir()
        config_path.write_text(
            json.dumps(self._config_document(), indent=2) + "\n", encoding="utf-8"
        )
        return temporary, config_path

    def _mutate_config(self, path: Path, mutate) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def test_matrix_has_24_story_level_jobs_and_exact_release_budgets(self) -> None:
        from videoactagent.full_chain import compile_matrix

        matrix = compile_matrix(ROOT / "configs" / "full_chain_matrix.json")

        self.assertEqual(len(matrix.jobs), 24)
        self.assertEqual({job.backend for job in matrix.jobs}, {"kling", "seedance", "vace"})
        self.assertTrue(all("shot_id" not in json.dumps(job.document) for job in matrix.jobs))
        self.assertEqual(matrix.canary_story_ids, ("station_reunion", "studio_formation"))
        self.assertEqual(len([job for job in matrix.jobs if job.release == "canary"]), 6)
        self.assertEqual(
            matrix.budgets,
            {
                "generation_submissions": 16,
                "status_queries": 64,
                "downloads": 16,
                "vace_inferences": 8,
                "automatic_retries": 0,
            },
        )

    def test_prepare_snapshots_all_inputs_with_hashes_and_no_release_token(self) -> None:
        from videoactagent.full_chain import compile_matrix, write_prepared_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        prepared = write_prepared_matrix(compile_matrix(config_path), Path(temporary.name) / "prepared")

        self.assertEqual(prepared["job_count"], 24)
        self.assertNotIn("release_token", prepared)
        self.assertTrue((Path(temporary.name) / "prepared" / "matrix.json").is_file())
        self.assertGreaterEqual(len(prepared["snapshot_sha256"]), 35)
        self.assertTrue(any(name.endswith("station_reunion/sources/prompt.txt") for name in prepared["snapshot_sha256"]))
        with self.assertRaises(ValueError):
            write_prepared_matrix(compile_matrix(config_path), Path(temporary.name) / "prepared")

    def test_rejects_altered_proxy_hash(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        manifest_path = Path(temporary.name) / "runs/work/whole_story_v4/station_reunion/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["media"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_changed_submitted_prompt_hash(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        case_dir = Path(temporary.name) / "runs/work/whole_story_v4/station_reunion"
        manifest_path = case_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_hashes"]["submitted_prompt_sha256"] = "0" * 64
        for backend in ("kling", "seedance", "vace"):
            bundle_path = case_dir / "bundles" / f"{backend}.json"
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle["source_hashes"]["submitted_prompt_sha256"] = "0" * 64
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            manifest["bundles"][backend] = bundle
            manifest["bundle_files"][backend]["sha256"] = hashlib.sha256(
                bundle_path.read_bytes()
            ).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_missing_backend_manifest(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        (Path(temporary.name) / "runs/work/whole_story_v4/station_reunion/bundles/vace.json").unlink()
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_duplicate_backend_job(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        self._mutate_config(config_path, lambda document: document.update(backends=["kling", "kling", "vace"]))
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_path_escape(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        self._mutate_config(config_path, lambda document: document.update(source_summary="../escape"))
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_nonzero_automatic_retries(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        self._mutate_config(config_path, lambda document: document["budgets"].update(automatic_retries=1))
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_query_limit_other_than_four(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        self._mutate_config(config_path, lambda document: document["request"].update(query_limit=3))
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)

    def test_rejects_remainder_enabled_before_canary(self) -> None:
        from videoactagent.full_chain import ExperimentConfigError, compile_matrix

        temporary, config_path = self._temporary_config()
        self.addCleanup(temporary.cleanup)
        self._mutate_config(
            config_path,
            lambda document: document.update(release_gates={"canary_enabled": False, "remainder_enabled": True}),
        )
        with self.assertRaises(ExperimentConfigError):
            compile_matrix(config_path)


if __name__ == "__main__":
    unittest.main()
