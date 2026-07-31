"""Stage 3 API-readiness interface tests for ``videoactagent.backend_prepare``.

Run: ``& $PY -m unittest tests.test_backend_prepare -v`` (see
``docs/USAGE.md``). The bundle below is a minimal code-only contract fixture,
not experimental or backend acceptance evidence. CLI reports are written only
to temporary test directories; no API/media upload occurs.
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest


class BackendPrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = {
            "shots": [{
                "shot_id": "s01",
                "duration": 5.0,
                "prompts": {"plain": "plain", "cinematic": "cinematic"},
            }]
        }

    def test_seedance_report_keeps_prompt_ready_and_media_conditions_blocked(self):
        from videoactagent.backend_prepare import prepare_backend

        report = prepare_backend(self.bundle, "seedance", bindings={})

        self.assertEqual(report["conditions"]["plain"]["status"], "ready")
        self.assertEqual(report["conditions"]["cinematic"]["status"], "ready")
        self.assertEqual(report["conditions"]["first_last"]["status"], "blocked")
        self.assertIn(
            "missing remote bindings: first_frame, last_frame",
            report["conditions"]["first_last"]["blockers"],
        )
        self.assertEqual(report["conditions"]["proxy_video"]["status"], "blocked")
        self.assertIn(
            "JD gateway reference_video capability is unverified",
            report["conditions"]["proxy_video"]["blockers"],
        )
        serialized = json.dumps(report)
        self.assertNotIn("task_id", serialized)
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)

    def test_remote_asset_validation_is_strict_and_only_syntactic(self):
        from videoactagent.backend_prepare import validate_remote_asset

        self.assertEqual(
            validate_remote_asset("asset://controlled-unit-asset"),
            "asset://controlled-unit-asset",
        )
        self.assertEqual(
            validate_remote_asset("https://portal.volccdn.com/controlled/unit.png"),
            "https://portal.volccdn.com/controlled/unit.png",
        )
        invalid = (
            "shots/s01/first.png",
            "file:///tmp/first.png",
            "http://media.example/first.png",
            "data:image/png;base64,AAAA",
            "https://127.0.0.1/first.png",
            "https://localhost/first.png",
            "https://example.com/first.png",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_remote_asset(value)

    def test_asset_bindings_unlock_first_last_shape_but_not_proxy_video(self):
        from videoactagent.backend_prepare import prepare_backend

        bindings = {
            "s01": {
                "first_frame": "asset://controlled-first",
                "last_frame": "asset://controlled-last",
                "proxy_video": "asset://controlled-proxy",
            }
        }
        report = prepare_backend(self.bundle, "seedance", bindings)

        first_last = report["conditions"]["first_last"]
        self.assertEqual(first_last["status"], "ready")
        self.assertEqual(
            [item.get("role") for item in first_last["payload"]["content"]],
            [None, "first_frame", "last_frame"],
        )
        self.assertEqual(report["conditions"]["proxy_video"]["status"], "blocked")

    def test_verified_evidence_and_model_only_evidence_set_distinct_proxy_states(self):
        import hashlib
        from videoactagent.backend_prepare import prepare_backend
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        common = {
            "observed_at": "2026-07-31T12:34:56Z",
            "model": "Doubao-Seedance-2.5",
            "content_type": "video_url",
            "url_field": "video_url",
            "role": "reference_video",
        }
        document = {
            "schema_version": "seedance-reference-capability/1",
            "model_capability": "model_supported",
            "gateway_capability": "gateway_unverified",
            "model_evidence": {
                **common,
                "source_url": "https://www.volcengine.com/docs/official/model-reference",
            },
            "gateway_evidence": None,
        }
        model_only = load_seedance_capability_evidence(document)
        bindings = {"s01": {"proxy_video": "https://media.volccdn.com/clay.mp4"}}
        combined = prepare_backend(
            self.bundle, "seedance", bindings, capability_evidence=model_only
        )
        self.assertEqual(
            combined["conditions"]["proxy_video"]["status"],
            "ready_for_single_combined_probe",
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            capture = root_path / "capture.json"
            capture.write_bytes(b'{"task_id":"captured"}')
            document["gateway_capability"] = "gateway_verified"
            document["gateway_evidence"] = {
                **common,
                "response_record": {
                    "path": "capture.json",
                    "sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                },
            }
            verified = load_seedance_capability_evidence(document, base_dir=root_path)
            ready = prepare_backend(
                self.bundle, "seedance", bindings, capability_evidence=verified
            )
        self.assertEqual(ready["conditions"]["proxy_video"]["status"], "ready")
        self.assertEqual(
            ready["capability_evidence"]["provided_reference_video"][
                "gateway_capability"
            ],
            "gateway_verified",
        )
        self.assertEqual(
            ready["conditions"]["proxy_video"]["payload"]["content"][1]["role"],
            "reference_video",
        )

    def test_proxy_video_never_accepts_asset_uri_even_with_capability(self):
        from videoactagent.backend_prepare import prepare_backend
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        common = {
            "observed_at": "2026-07-31T12:34:56Z",
            "model": "Doubao-Seedance-2.5",
            "content_type": "video_url",
            "url_field": "video_url",
            "role": "reference_video",
            "source_url": "https://www.volcengine.com/docs/official/model-reference",
        }
        capability = load_seedance_capability_evidence({
            "schema_version": "seedance-reference-capability/1",
            "model_capability": "model_supported",
            "gateway_capability": "gateway_unverified",
            "model_evidence": common,
            "gateway_evidence": None,
        })
        with self.assertRaises(ValueError):
            prepare_backend(
                self.bundle,
                "seedance",
                {"s01": {"proxy_video": "asset://controlled-proxy"}},
                capability_evidence=capability,
            )

    def test_reference_condition_requires_exact_five_second_shot(self):
        from videoactagent.backend_prepare import prepare_backend
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        capability = load_seedance_capability_evidence({
            "schema_version": "seedance-reference-capability/1",
            "model_capability": "model_supported",
            "gateway_capability": "gateway_unverified",
            "model_evidence": {
                "observed_at": "2026-07-31T12:34:56Z",
                "model": "Doubao-Seedance-2.5",
                "content_type": "video_url",
                "url_field": "video_url",
                "role": "reference_video",
                "source_url": "https://www.volcengine.com/docs/official/model-reference",
            },
            "gateway_evidence": None,
        })
        bundle = json.loads(json.dumps(self.bundle))
        bundle["shots"][0]["duration"] = 4.6
        report = prepare_backend(
            bundle,
            "seedance",
            {"s01": {"proxy_video": "https://media.volccdn.com/clay.mp4"}},
            capability_evidence=capability,
        )
        self.assertEqual(report["conditions"]["proxy_video"]["status"], "blocked")
        self.assertIn(
            "Seedance reference_video requires exactly five seconds",
            report["conditions"]["proxy_video"]["blockers"],
        )

    def test_present_incomplete_binding_is_malformed_not_a_blocker(self):
        from videoactagent.backend_prepare import prepare_backend

        with self.assertRaises(ValueError):
            prepare_backend(
                self.bundle,
                "seedance",
                {"s01": {"proxy_video": "https://media.volccdn.com/clay.mp4"}},
                capability_evidence={"gateway_capability": "bogus"},
            )

    def test_cli_writes_readiness_without_network_fields(self):
        from videoactagent.backend_prepare import main

        with tempfile.TemporaryDirectory() as root:
            bundle = Path(root) / "code-only-bundle.json"
            bundle.write_text(json.dumps(self.bundle), encoding="utf-8")
            output = Path(root) / "readiness.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                main(
                    [
                        "--bundle",
                        str(bundle),
                        "--backend",
                        "seedance",
                        "--output",
                        str(output),
                    ]
                )
            self.assertTrue(output.is_file())
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["conditions"]["plain"]["status"], "ready")
            self.assertFalse(report["network_called"])
            self.assertIn("BACKEND_READINESS_OK", stdout.getvalue())

    def test_cli_accepts_local_capability_evidence_without_network(self):
        from videoactagent.backend_prepare import main

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            bundle = root_path / "code-only-bundle.json"
            bundle.write_text(json.dumps(self.bundle), encoding="utf-8")
            bindings = root_path / "bindings.json"
            bindings.write_text(json.dumps({
                "s01": {"proxy_video": "https://media.volccdn.com/clay.mp4"}
            }), encoding="utf-8")
            evidence = root_path / "capability.json"
            evidence.write_text(json.dumps({
                "schema_version": "seedance-reference-capability/1",
                "model_capability": "model_supported",
                "gateway_capability": "gateway_unverified",
                "model_evidence": {
                    "observed_at": "2026-07-31T12:34:56Z",
                    "model": "Doubao-Seedance-2.5",
                    "content_type": "video_url",
                    "url_field": "video_url",
                    "role": "reference_video",
                    "source_url": "https://www.volcengine.com/docs/official/model-reference",
                },
                "gateway_evidence": None,
            }), encoding="utf-8")
            output = root_path / "readiness.json"
            main([
                "--bundle", str(bundle),
                "--backend", "seedance",
                "--bindings", str(bindings),
                "--capability-evidence", str(evidence),
                "--output", str(output),
            ])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            report["conditions"]["proxy_video"]["status"],
            "ready_for_single_combined_probe",
        )
        self.assertFalse(report["network_called"])


if __name__ == "__main__":
    unittest.main()
