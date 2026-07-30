from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "whole_story_suite.json"


class WholeStoryContractTests(unittest.TestCase):
    def test_checked_in_suite_is_offline_and_contains_eight_distinct_stories(self):
        from videoactagent.whole_story import load_suite

        suite = load_suite(CONFIG)

        self.assertEqual(suite.config_sha256, hashlib.sha256(CONFIG.read_bytes()).hexdigest())
        self.assertFalse(suite.submit)
        self.assertEqual(suite.max_api_calls, 0)
        self.assertEqual(len(suite.cases), 8)
        self.assertEqual(suite.profile.duration_seconds, 5.0)
        self.assertEqual(suite.profile.proxy_fps, 3)
        self.assertEqual(suite.profile.proxy_resolution, (960, 540))
        self.assertEqual(suite.profile.target_resolution, (1280, 720))
        self.assertIsNone(suite.profile.seed)
        self.assertEqual(suite.profile.seed_support, "unsupported_by_gateway")

        self.assertEqual(len({case.story_id for case in suite.cases}), 8)
        self.assertEqual(len({case.prompt_sha256 for case in suite.cases}), 8)
        self.assertEqual(len({case.shotscript_sha256 for case in suite.cases}), 8)
        self.assertEqual(len({case.motion_signature for case in suite.cases}), 8)

        for case in suite.cases:
            self.assertEqual(len(case.shotscript.shots), 1)
            self.assertEqual(case.shotscript.shots[0].duration, 5.0)
            self.assertEqual(case.shotscript.fps, 3)
            self.assertTrue(case.prompt.strip())
            self.assertEqual(
                case.submitted_prompt_sha256,
                hashlib.sha256(case.prompt.encode("utf-8")).hexdigest(),
            )

    def test_submit_or_nonzero_api_budget_is_rejected(self):
        from videoactagent.whole_story import SuiteError, load_suite

        source = json.loads(CONFIG.read_text(encoding="utf-8"))
        for field, value in (("submit", True), ("max_api_calls", 1)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                document = dict(source)
                document[field] = value
                path = Path(root) / "suite.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(SuiteError, "offline"):
                    load_suite(path, workspace=ROOT)

    def test_schema_version_and_story_identity_are_strict(self):
        from videoactagent.whole_story import SuiteError, load_suite

        source = json.loads(CONFIG.read_text(encoding="utf-8"))
        mutations = []
        wrong_schema = json.loads(json.dumps(source))
        wrong_schema["schema_version"] = "0.9"
        mutations.append((wrong_schema, "schema_version"))
        wrong_identity = json.loads(json.dumps(source))
        wrong_identity["cases"][0]["story_id"] = "not_the_scene_id"
        mutations.append((wrong_identity, "scene_id"))
        unsafe_identity = json.loads(json.dumps(source))
        unsafe_identity["cases"][0]["story_id"] = "../escape"
        mutations.append((unsafe_identity, "safe slug"))
        for document, message in mutations:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "suite.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(SuiteError, message):
                    load_suite(path, workspace=ROOT)

    def test_duration_gate_rejects_incomplete_output_before_scoring(self):
        from videoactagent.whole_story import evaluate_output_completeness

        accepted = evaluate_output_completeness(
            requested_duration=5.0,
            decoded_duration=5.04,
            decoded_resolution=(1280, 720),
            expected_resolution=(1280, 720),
            key_times_decodable=True,
            annotated_frame_count=5,
            interpolation_sample_count=121,
        )
        self.assertEqual(accepted["status"], "complete")
        self.assertAlmostEqual(accepted["duration_coverage"], 1.008)
        self.assertTrue(accepted["movement_scoring_allowed"])
        self.assertEqual(accepted["annotated_frame_count"], 5)
        self.assertEqual(accepted["interpolation_sample_count"], 121)

        rejected = evaluate_output_completeness(
            requested_duration=15.0,
            decoded_duration=5.04,
            decoded_resolution=(1280, 720),
            expected_resolution=(1280, 720),
            key_times_decodable=True,
            annotated_frame_count=5,
            interpolation_sample_count=121,
        )
        self.assertEqual(rejected["status"], "incomplete")
        self.assertAlmostEqual(rejected["duration_coverage"], 0.336)
        self.assertFalse(rejected["movement_scoring_allowed"])

        too_long = evaluate_output_completeness(
            requested_duration=5.0,
            decoded_duration=10.0,
            decoded_resolution=(1280, 720),
            expected_resolution=(1280, 720),
            key_times_decodable=True,
            annotated_frame_count=5,
            interpolation_sample_count=121,
        )
        self.assertEqual(too_long["status"], "incomplete")
        self.assertFalse(too_long["movement_scoring_allowed"])

        unannotated = evaluate_output_completeness(
            requested_duration=5.0,
            decoded_duration=5.0,
            decoded_resolution=(1280, 720),
            expected_resolution=(1280, 720),
            key_times_decodable=True,
            annotated_frame_count=0,
            interpolation_sample_count=0,
        )
        self.assertEqual(unannotated["status"], "complete_unscored")
        self.assertFalse(unannotated["movement_scoring_allowed"])

    def test_story_level_bundles_never_claim_proxy_conditioning_for_text_backends(self):
        from videoactagent.whole_story import build_story_bundles, load_suite

        suite = load_suite(CONFIG)
        case = suite.cases[0]
        proxy = ROOT / "tests" / "fixtures" / "placeholder-proxy.mp4"
        bundles = build_story_bundles(suite, case, proxy, proxy_sha256="a" * 64)

        self.assertEqual(set(bundles), {"kling", "seedance", "vace"})
        for backend in ("kling", "seedance"):
            bundle = bundles[backend]
            self.assertEqual(
                bundle["adapter_status"], "offline_input_manifest_not_submission_payload"
            )
            self.assertEqual(bundle["conditioning_mode"], "prompt_only")
            self.assertNotIn("source_video", bundle)
            self.assertNotIn("shot_id", json.dumps(bundle))
            self.assertEqual(bundle["duration_seconds"], 5.0)
            self.assertIsNone(bundle["seed"])
            self.assertEqual(bundle["target_resolution"], [1280, 720])
        self.assertEqual(bundles["vace"]["conditioning_mode"], "source_video")
        self.assertEqual(
            bundles["vace"]["adapter_status"],
            "offline_input_manifest_not_preprocess_job",
        )
        self.assertEqual(bundles["vace"]["source_video"]["sha256"], "a" * 64)
        self.assertEqual(bundles["vace"]["source_video"]["path_base"], "case_dir")
        self.assertNotIn("seed", bundles["vace"])
        self.assertNotIn("seed_support", bundles["vace"])
        self.assertNotIn("target_resolution", bundles["vace"])
        self.assertNotIn("shot_id", json.dumps(bundles["vace"]))

    def test_runner_refuses_to_overwrite_an_existing_run(self):
        from videoactagent.whole_story import SuiteError, load_suite, run_suite

        suite = load_suite(CONFIG)
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "already-there"
            existing.mkdir()
            with self.assertRaisesRegex(SuiteError, "already exists"):
                run_suite(replace(suite, output_dir=existing))

    def test_repository_exposes_one_positional_config_entrypoint(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("whole-story", completed.stdout.lower())
        self.assertIn("config", completed.stdout.lower())

    def test_contact_sheet_is_a_real_declared_output(self):
        from videoactagent.whole_story import build_contact_sheet

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            inputs = []
            for index, color in enumerate(((255, 0, 0), (0, 0, 255))):
                path = directory / f"case_{index}.png"
                Image.new("RGB", (96, 54), color).save(path)
                inputs.append((f"case_{index}", path))
            record = build_contact_sheet(inputs, directory / "contact_sheet.png")
            self.assertTrue((directory / record["path"]).is_file())
            self.assertEqual(len(record["sha256"]), 64)
            self.assertEqual(record["image_count"], 2)


if __name__ == "__main__":
    unittest.main()
