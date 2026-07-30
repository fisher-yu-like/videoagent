"""Stage 3 JD smoke-command safety/mechanics tests for ``videoactagent.jd_smoke``.

Run: ``& $PY -m unittest tests.test_jd_smoke -v`` (see ``docs/USAGE.md``).
Inputs and run directories are temporary. Network transport and responses are
patched/controlled, so these tests verify one-attempt behavior, recording, and
secret handling only; they must not be reported as a real API submission.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class JdSmokeCliTests(unittest.TestCase):
    def test_submit_seedance_requires_key_before_creating_run_directory(self):
        from videoactagent import jd_smoke

        bundle = {
            "shots": [
                {
                    "shot_id": "s01",
                    "duration": 5.0,
                    "prompts": {"plain": "A plain prompt."},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": ""}):
                with self.assertRaisesRegex(
                    RuntimeError, "JD_KLING_KEY is required"
                ):
                    jd_smoke.main(
                        [
                            "submit-seedance",
                            "--bundle",
                            str(bundle_path),
                            "--shot",
                            "s01",
                            "--prompt",
                            "plain",
                            "--run-root",
                            str(run_root),
                        ]
                    )

            self.assertFalse(run_root.exists())

    def test_submit_seedance_builds_payload_and_auditable_run(self):
        from videoactagent import jd_smoke

        bundle = {
            "shots": [
                {
                    "shot_id": "s01",
                    "duration": 5.0,
                    "prompts": {
                        "plain": "A plain prompt.",
                        "cinematic": "A cinematic prompt.",
                    },
                }
            ]
        }
        submitted = {}

        def capture_submit(payload, api_key, base_url, run):
            submitted.update(
                payload=payload,
                api_key=api_key,
                base_url=base_url,
                run=run,
            )
            return "unit-task-id"

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            environment = {
                "JD_KLING_KEY": "unit-key",
                "JD_KLING_BASE": "https://modelservice.jdcloud.com",
            }
            with patch.dict(os.environ, environment):
                with patch.object(
                    jd_smoke, "submit_once", side_effect=capture_submit
                ):
                    jd_smoke.main(
                        [
                            "submit-seedance",
                            "--bundle",
                            str(bundle_path),
                            "--shot",
                            "s01",
                            "--prompt",
                            "cinematic",
                            "--run-root",
                            str(run_root),
                        ]
                    )

            run = submitted["run"]
            metadata = json.loads(
                (run.path / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run.path.parent, run_root)
            self.assertIn("_seedance_", run.path.name)
            self.assertEqual(
                metadata,
                {
                    "backend": "seedance",
                    "shot_id": "s01",
                    "prompt_condition": "cinematic",
                    "submit_retry_limit": 0,
                },
            )
            self.assertEqual(submitted["api_key"], "unit-key")
            self.assertEqual(
                submitted["base_url"], "https://modelservice.jdcloud.com"
            )
            self.assertEqual(submitted["payload"]["model"], "Doubao-Seedance-2.0")
            self.assertEqual(
                submitted["payload"]["content"],
                [{"type": "text", "text": "A cinematic prompt."}],
            )
            self.assertEqual(submitted["payload"]["parameters"]["duration"], 5)
            for path in run.path.rglob("*"):
                if path.is_file():
                    self.assertNotIn("unit-key", path.read_text(encoding="utf-8"))

    def test_submit_seedance_transport_failure_is_attempted_once_and_audited_once(self):
        from videoactagent import jd_smoke

        bundle = {
            "shots": [
                {
                    "shot_id": "s01",
                    "duration": 5.0,
                    "prompts": {"plain": "A plain prompt."},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle_path = root_path / "bundle.json"
            run_root = root_path / "runs"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-key"}):
                with patch(
                    "videoactagent.backends.jd._request_once",
                    side_effect=RuntimeError("controlled transport failure"),
                ) as request_once:
                    with self.assertRaisesRegex(
                        RuntimeError, "controlled transport failure"
                    ):
                        jd_smoke.main(
                            [
                                "submit-seedance",
                                "--bundle",
                                str(bundle_path),
                                "--shot",
                                "s01",
                                "--prompt",
                                "plain",
                                "--run-root",
                                str(run_root),
                            ]
                        )

            self.assertEqual(request_once.call_count, 1)
            runs = list(run_root.iterdir())
            self.assertEqual(len(runs), 1)
            run_path = runs[0]
            self.assertTrue((run_path / "request.json").is_file())
            self.assertTrue((run_path / "failure.json").is_file())
            self.assertFalse((run_path / "response.json").exists())
            self.assertFalse((run_path / "state.json").exists())
            failure = json.loads(
                (run_path / "failure.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                failure,
                {
                    "type": "RuntimeError",
                    "message": "controlled transport failure",
                },
            )


if __name__ == "__main__":
    unittest.main()
