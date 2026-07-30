"""Stage 3 API-readiness interface tests for ``videoactagent.backend_prepare``.

Run: ``& $PY -m unittest tests.test_backend_prepare -v`` (see
``docs/USAGE.md``). Real input: ``runs/stage2_control_bridge/control_bundle.json``;
CLI reports are written only to temporary test directories. URL checks are
syntactic and no API/media upload occurs, so this is not backend acceptance.
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest


BUNDLE_PATH = Path("runs/stage2_control_bridge/control_bundle.json")


class BackendPrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BUNDLE_PATH.is_file():
            raise AssertionError(
                "real Stage 2 bundle is required; run the Control Bridge experiment first"
            )
        cls.bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))

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

    def test_cli_writes_readiness_without_network_fields(self):
        from videoactagent.backend_prepare import main

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "readiness.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                main(
                    [
                        "--bundle",
                        str(BUNDLE_PATH),
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


if __name__ == "__main__":
    unittest.main()
