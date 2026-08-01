"""Stage 3 JD smoke-command safety/mechanics tests for ``videoactagent.jd_smoke``.

Run: ``& $PY -m unittest tests.test_jd_smoke -v`` (see ``docs/USAGE.md``).
Inputs and run directories are temporary. Network transport and responses are
patched/controlled, so these tests verify one-attempt behavior, recording, and
secret handling only; they must not be reported as a real API submission.
"""

import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def _reference_candidate(source_root: Path, *, gateway_verified: bool = False):
    from tests.test_seedance_reference import (
        approved_export, evidence_document, gateway_capture,
    )
    from videoactagent.seedance_reference import (
        load_seedance_capability_evidence,
        prepare_reference_candidate,
    )

    approved = approved_export()
    prompt = approved["restyle_prompt"]["text"].encode("utf-8")
    contents = {
        "approval": b"real approval evidence\n",
        "clay": b"real approved proxy source bytes\n",
        "restyle_prompt": prompt,
        "restyle_profile": b'{"style":"live action"}\n',
        "compiled_prompt": b"real shared compiled prompt\n",
        "trajectory_prompt": b"real shared compiled prompt\n",
        "annotation": b'{"human_approved":true}\n',
        "iteration_manifest": b'{"iteration":"D1"}\n',
    }
    for name, data in contents.items():
        path = source_root / approved[name]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        approved[name]["sha256"] = hashlib.sha256(data).hexdigest()
        approved[name]["bytes"] = len(data)
    evidence = evidence_document(verified=False)
    if gateway_verified:
        capture = source_root / "captures" / "jd-gateway.json"
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_bytes(gateway_capture())
        evidence = evidence_document(
            verified=True, response_sha256=hashlib.sha256(capture.read_bytes()).hexdigest()
        )
    return prepare_reference_candidate(
        approved_export=approved,
        proxy_url="https://media.volccdn.com/approved/clay.mp4",
        capability=load_seedance_capability_evidence(evidence, base_dir=source_root),
    )


class JdSmokeCliTests(unittest.TestCase):
    def test_submit_reference_accepts_only_exact_candidate_and_claims_one_attempt(self):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "source"
            candidate = _reference_candidate(source_root)
            calls = []
            candidate_path = root_path / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            run_root = root_path / "runs"

            def controlled_submit(payload, api_key, base_url, run):
                calls.append(payload)
                self.assertTrue((run.path / "request.json").is_file())
                return "code-only-task"

            args = [
                "submit-seedance-reference", "--candidate", str(candidate_path),
                "--source-root", str(source_root),
                "--run-root", str(run_root),
            ]
            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-key"}):
                with patch.object(jd_smoke, "submit_once", side_effect=controlled_submit):
                    jd_smoke.main(args)
                    with self.assertRaisesRegex(RuntimeError, "already.*attempted"):
                        jd_smoke.main(args)

            self.assertEqual(calls, [candidate["payload"]])
            runs = [path for path in run_root.iterdir() if path.is_dir() and not path.name.startswith(".")]
            self.assertEqual(len(runs), 1)
            metadata = json.loads((runs[0] / "metadata.json").read_text(encoding="utf-8"))
            candidate_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            self.assertEqual(metadata["candidate_sha256"], candidate_sha)
            self.assertEqual(metadata["generation_submit_limit"], 1)
            self.assertEqual(metadata["generation_submit_count"], 1)
            self.assertEqual(metadata["automatic_retry_limit"], 0)
            self.assertEqual(metadata["input_mode"], "reference_video")
            self.assertEqual(metadata["status"], "combined_probe")
            self.assertEqual(
                metadata["source_snapshot_sha256"], candidate["source_hashes"]
            )

    def test_submit_reference_rejects_tampering_before_credentials_or_run(self):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "source"
            candidate = _reference_candidate(source_root)
            mutations = []
            for mutate in (
                lambda value: value.update(network_called=True),
                lambda value: value.update(payload_sha256="0" * 64),
                lambda value: value["payload"]["parameters"].update(duration=4),
                lambda value: value["capability"].update(role="image"),
            ):
                changed = json.loads(json.dumps(candidate))
                mutate(changed)
                mutations.append(changed)
            for index, changed in enumerate(mutations):
                path = root_path / f"candidate-{index}.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                run_root = root_path / f"runs-{index}"
                with patch.dict(os.environ, {"JD_KLING_KEY": ""}):
                    with self.assertRaises(ValueError):
                        jd_smoke.main([
                            "submit-seedance-reference", "--candidate", str(path),
                            "--source-root", str(source_root),
                            "--run-root", str(run_root),
                        ])
                self.assertFalse(run_root.exists())

    def test_submit_reference_rehashes_real_sources_before_run_and_transport(self):
        from videoactagent import jd_smoke

        for mode in ("missing", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                source_root = root_path / "source"
                candidate = _reference_candidate(source_root)
                candidate_path = root_path / "candidate.json"
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                approval = source_root / candidate["source_records"]["approval"]["path"]
                if mode == "missing":
                    approval.unlink()
                else:
                    approval.write_bytes(approval.read_bytes() + b"drift")
                run_root = root_path / "runs"
                with patch.dict(os.environ, {"JD_KLING_KEY": "unit-key"}):
                    with patch.object(jd_smoke, "submit_once") as submit:
                        with self.assertRaisesRegex(ValueError, "source snapshot"):
                            jd_smoke.main([
                                "submit-seedance-reference", "--candidate", str(candidate_path),
                                "--source-root", str(source_root),
                                "--run-root", str(run_root),
                            ])
                submit.assert_not_called()
                self.assertFalse(run_root.exists())

    def test_submit_reference_revalidates_captured_capability_document(self):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "source"
            candidate = _reference_candidate(source_root, gateway_verified=True)
            capture_record = candidate["capability_evidence"]["gateway"]["capture"]
            capture = source_root / capture_record["path"]
            capture.write_text('{"request":{},"response":{"status":"submitted"}}', encoding="utf-8")
            capture_record["sha256"] = hashlib.sha256(capture.read_bytes()).hexdigest()
            candidate_path = root_path / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": ""}):
                with patch.object(jd_smoke, "submit_once") as submit:
                    with self.assertRaisesRegex(ValueError, "captured gateway"):
                        jd_smoke.main([
                            "submit-seedance-reference", "--candidate", str(candidate_path),
                            "--source-root", str(source_root),
                            "--run-root", str(root_path / "runs"),
                        ])
            submit.assert_not_called()
            self.assertFalse((root_path / "runs").exists())

    def test_submit_reference_transport_failure_consumes_only_claim(self):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_root = root_path / "source"
            candidate = _reference_candidate(source_root)
            candidate_path = root_path / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            args = [
                "submit-seedance-reference", "--candidate", str(candidate_path),
                "--source-root", str(source_root), "--run-root", str(root_path / "runs"),
            ]
            calls = 0

            def fail(*_args):
                nonlocal calls
                calls += 1
                raise RuntimeError("controlled code-only transport failure")

            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-key"}):
                with patch.object(jd_smoke, "submit_once", side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError, "controlled code-only"):
                        jd_smoke.main(args)
                    with self.assertRaisesRegex(RuntimeError, "already.*attempted"):
                        jd_smoke.main(args)
            self.assertEqual(calls, 1)
            claims = list((root_path / "runs" / ".seedance_reference_claims").glob("*.json"))
            self.assertEqual(len(claims), 1)
            self.assertEqual(json.loads(claims[0].read_text())["generation_submit_count"], 1)

    def test_query_records_immutable_attempt_hash_and_cumulative_count(self):
        from videoactagent import jd_smoke

        response = {"task_id": "task-1", "status": "processing", "content": []}
        canonical = json.dumps(
            response, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as root:
            run = Path(root) / "run"
            run.mkdir()
            (run / "state.json").write_text(json.dumps({
                "base_url": "https://modelservice.jdcloud.com",
                "task_id": "task-1", "status": "submitted", "video_urls": [],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-key"}):
                with patch("videoactagent.backends.jd._request_once", return_value=response):
                    with patch.object(jd_smoke, "submit_once") as submit:
                        jd_smoke.main(["query", "--run-dir", str(run)])
                        jd_smoke.main(["query", "--run-dir", str(run)])
            submit.assert_not_called()
            state = json.loads((run / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["query_count"], 2)
            attempts = sorted(run.glob("query_attempt_*.json"))
            self.assertEqual(len(attempts), 2)
            values = [json.loads(path.read_text(encoding="utf-8")) for path in attempts]
            self.assertEqual([value["query_count"] for value in values], [1, 2])
            for value in values:
                self.assertTrue(value["attempted_at_utc"].endswith("Z"))
                self.assertEqual(
                    value["response_sha256"], hashlib.sha256(canonical).hexdigest()
                )

    def test_query_failure_is_audited_and_next_query_is_explicit_only(self):
        from videoactagent import jd_smoke

        with tempfile.TemporaryDirectory() as root:
            run = Path(root) / "run"
            run.mkdir()
            (run / "state.json").write_text(json.dumps({
                "base_url": "https://modelservice.jdcloud.com", "task_id": "task-1",
                "status": "submitted", "video_urls": [],
            }), encoding="utf-8")
            response = {"status": "processing", "content": []}
            with patch.dict(os.environ, {"JD_KLING_KEY": "unit-key"}):
                with patch("videoactagent.backends.jd._request_once", side_effect=[
                    RuntimeError("controlled query failure"), response,
                ]) as request:
                    with patch.object(jd_smoke, "submit_once") as submit:
                        with self.assertRaisesRegex(RuntimeError, "controlled query failure"):
                            jd_smoke.main(["query", "--run-dir", str(run)])
                        self.assertEqual(request.call_count, 1)
                        jd_smoke.main(["query", "--run-dir", str(run)])
            submit.assert_not_called()
            state = json.loads((run / "state.json").read_text())
            self.assertEqual(state["query_count"], 2)
            attempts = sorted(run.glob("query_attempt_*.json"))
            self.assertEqual(len(attempts), 2)
            failed = json.loads(attempts[0].read_text())
            self.assertEqual(failed["query_count"], 1)
            self.assertEqual(failed["failure"]["type"], "RuntimeError")

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
