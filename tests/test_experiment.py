"""Offline contract tests for the gated full-chain prepare entry point."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "full_chain_matrix.json"


class FullChainExperimentTests(unittest.TestCase):
    def test_prepare_writes_all_jobs_with_zero_attempts_and_no_release_tokens(self) -> None:
        from experiment import prepare_experiment

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "full_chain_24_v1"
            summary = prepare_experiment(CONFIG, output)

            self.assertEqual(summary["job_count"], 24)
            self.assertEqual(summary["api_job_count"], 16)
            self.assertEqual(summary["vace_job_count"], 8)
            self.assertEqual(summary["canary_job_count"], 6)
            self.assertEqual(summary["attempts"], {"submissions": 0, "status_queries": 0, "downloads": 0, "vace_inferences": 0})
            self.assertEqual(
                summary["offline"],
                {"api_called": False, "gpu_called": False, "network_called": False},
            )
            self.assertTrue((output / "matrix.json").is_file())
            self.assertTrue((output / "summary.json").is_file())
            self.assertEqual(len(summary["jobs"]), 24)
            self.assertTrue(all((output / item["prepared_path"]).is_dir() for item in summary["jobs"]))
            self.assertFalse(any(output.rglob("release_*.json")))
            self.assertFalse(any("token" in path.name.lower() for path in output.rglob("*")))
            self.assertEqual(summary, json.loads((output / "summary.json").read_text(encoding="utf-8")))

    def test_cli_exposes_only_prepare_and_offline_status_actions(self) -> None:
        from experiment import parse_args

        self.assertEqual(parse_args([str(CONFIG), "prepare"]).command, "prepare")
        self.assertEqual(parse_args([str(CONFIG), "canary-status"]).command, "canary-status")
        self.assertEqual(parse_args([str(CONFIG), "remainder-status"]).command, "remainder-status")
        with self.assertRaises(SystemExit):
            parse_args([str(CONFIG), "submit"])


if __name__ == "__main__":
    unittest.main()
