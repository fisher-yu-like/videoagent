"""Offline contract tests for story-level JD gateway jobs.

All transports are controlled test doubles: these tests never contact JD.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from videoactagent.full_chain import ExperimentJob


def _job(backend: str = "kling", release: str = "canary") -> ExperimentJob:
    prompt = "Two old friends meet again on a quiet station platform."
    return ExperimentJob(
        story_id="station_reunion",
        backend=backend,
        release=release,
        conditioning_mode="prompt_only",
        adapter_status="offline_input_manifest_not_submission_payload",
        prompt=prompt,
        document={
            "schema_version": "1.0",
            "story_id": "station_reunion",
            "backend": backend,
            "duration_seconds": 5.0,
            "prompt": prompt,
            "conditioning_mode": "prompt_only",
        },
    )


def _approve(prepared, release: str = "canary") -> Path:
    source = json.loads((prepared.path / "source_snapshot.json").read_text(encoding="utf-8"))
    token = {
        "schema_version": "1.0",
        "matrix_sha256": source["matrix_sha256"],
        "release": release,
        "budgets": source["budgets"],
    }
    path = prepared.path.parent / f"release_{release}.json"
    path.write_text(json.dumps(token), encoding="utf-8")
    return path


class _SubmitTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, payload, api_key, base_url, run):
        self.calls += 1
        self.payload = payload
        return "real-shape-task-id"


class _QueryTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, api_key, run):
        self.calls += 1
        return {"task_status": "processing"}


class _DownloadTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, run):
        self.calls += 1
        target = run.path / "result.mp4"
        target.write_bytes(b"controlled-video")
        return target


class WholeStoryGatewayTests(unittest.TestCase):
    def _prepared(self, root: Path, backend: str = "kling"):
        from videoactagent.whole_story_gateway import prepare_api_job

        return prepare_api_job(_job(backend), root / "jobs" / backend)

    def test_kling_story_payload_uses_root_duration_and_no_shot_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root), "kling")

            self.assertEqual(prepared.payload["model"], "Kling-V2-5-Turbo")
            self.assertEqual(
                prepared.payload["parameters"],
                {"duration": 5, "mode": "std", "aspect_ratio": "16:9"},
            )
            self.assertEqual(prepared.conditioning_mode, "prompt_only")
            self.assertNotIn("shot_id", json.dumps(prepared.payload))
            self.assertEqual(
                prepared.payload["content"],
                [{"type": "text", "text": _job().prompt}],
            )

    def test_seedance_story_payload_uses_720p_and_no_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root), "seedance")

            self.assertEqual(prepared.payload["model"], "Doubao-Seedance-2.0")
            self.assertEqual(
                prepared.payload["parameters"],
                {"ratio": "16:9", "resolution": "720p", "duration": 5, "watermark": False},
            )
            self.assertEqual(prepared.conditioning_mode, "prompt_only")
            self.assertNotIn("shot_id", json.dumps(prepared.payload))

    def test_prepare_is_offline_and_persists_zero_count_snapshot_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            files = {path.name for path in prepared.path.iterdir()}
            self.assertEqual(files, {"request.json", "metadata.json", "source_snapshot.json", "state.json"})
            state = json.loads((prepared.path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state,
                {
                    "generation_submission_count": 0,
                    "status_query_count": 0,
                    "download_count": 0,
                    "task_id": None,
                    "status": "prepared",
                },
            )
            self.assertEqual(prepared.request_sha256, hashlib.sha256((prepared.path / "request.json").read_bytes()).hexdigest())
            self.assertFalse(any("unit-secret" in path.read_text(encoding="utf-8") for path in prepared.path.iterdir()))

    def test_unapproved_release_makes_zero_transport_calls(self) -> None:
        from videoactagent.whole_story_gateway import ReleaseError, submit_prepared_job

        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            transport = _SubmitTransport()
            with self.assertRaisesRegex(ReleaseError, "release is not approved"):
                submit_prepared_job(prepared.path, "remainder", transport)
            self.assertEqual(transport.calls, 0)

    def test_submit_is_single_attempt_and_duplicate_is_rejected(self) -> None:
        from videoactagent.whole_story_gateway import ReleaseError, submit_prepared_job

        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            _approve(prepared)
            transport = _SubmitTransport()
            self.assertEqual(submit_prepared_job(prepared.path, "canary", transport), "real-shape-task-id")
            self.assertEqual(transport.calls, 1)
            state = json.loads((prepared.path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["generation_submission_count"], 1)
            with self.assertRaisesRegex(ReleaseError, "already attempted"):
                submit_prepared_job(prepared.path, "canary", transport)
            self.assertEqual(transport.calls, 1)

    def test_tampered_request_is_rejected_before_submission(self) -> None:
        from videoactagent.whole_story_gateway import ReleaseError, submit_prepared_job

        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            _approve(prepared)
            (prepared.path / "request.json").write_text('{"replaced":true}', encoding="utf-8")
            transport = _SubmitTransport()
            with self.assertRaisesRegex(ReleaseError, "snapshot hash mismatch"):
                submit_prepared_job(prepared.path, "canary", transport)
            self.assertEqual(transport.calls, 0)

    def test_tampered_source_snapshot_is_rejected_before_submission(self) -> None:
        from videoactagent.whole_story_gateway import ReleaseError, submit_prepared_job

        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            _approve(prepared)
            source_path = prepared.path / "source_snapshot.json"
            source = json.loads(source_path.read_text(encoding="utf-8"))
            source["release"] = "remainder"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            transport = _SubmitTransport()
            with self.assertRaisesRegex(ReleaseError, "snapshot hash mismatch"):
                submit_prepared_job(prepared.path, "canary", transport)
            self.assertEqual(transport.calls, 0)

    def test_failed_submit_is_counted_and_audited_once(self) -> None:
        from videoactagent.whole_story_gateway import submit_prepared_job

        def fail_once(payload, api_key, base_url, run):
            raise RuntimeError("controlled transport failure")

        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            _approve(prepared)
            with self.assertRaisesRegex(RuntimeError, "controlled transport failure"):
                submit_prepared_job(prepared.path, "canary", fail_once)
            state = json.loads((prepared.path / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["generation_submission_count"], 1)
            failure = json.loads((prepared.path / "attempts" / "submit_1" / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["type"], "RuntimeError")

    def test_query_five_and_download_two_are_rejected_before_transport(self) -> None:
        from videoactagent.whole_story_gateway import download_prepared_job, query_prepared_job

        with tempfile.TemporaryDirectory() as root:
            prepared = self._prepared(Path(root))
            query = _QueryTransport()
            for _ in range(4):
                self.assertEqual(query_prepared_job(prepared.path, query)["task_status"], "processing")
            with self.assertRaisesRegex(RuntimeError, "query limit"):
                query_prepared_job(prepared.path, query)
            self.assertEqual(query.calls, 4)
            download = _DownloadTransport()
            self.assertTrue(download_prepared_job(prepared.path, download).is_file())
            with self.assertRaisesRegex(RuntimeError, "download limit"):
                download_prepared_job(prepared.path, download)
            self.assertEqual(download.calls, 1)


if __name__ == "__main__":
    unittest.main()
