from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from videoactagent.deepseek_blender_codegen import DeepSeekCodegenError, request_blender_code


VALID_INPUT = {
    "schema_version": "blender-codegen-input-1.0",
    "prompt": "station",
    "shotscript": {"scene_id": "station_reunion"},
    "trajectory": {"scene_id": "station_reunion", "shot_id": "whole"},
    "render_contract": {"fps": 8, "resolution": [640, 360]},
}
CODE = "import bpy\ndef build_scene(context):\n    bpy.ops.mesh.primitive_cube_add()\n"


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class DeepSeekCodegenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "out"
        self.calls = []

    def transport(self, request, timeout):
        self.calls.append((request, timeout))
        body = {"id": "x", "model": "deepseek-v4-pro", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"schema_version": "blender-codegen-response-1.0", "summary": "safe station", "python_code": CODE})}}]}
        return _Response(json.dumps(body).encode())

    def test_calls_v4_pro_once_without_retry_and_saves_code(self):
        evidence = request_blender_code(codegen_input=VALID_INPUT, output_dir=self.output, environ={"DEEPSEEK_API_KEY": "secret", "DEEPSEEK_BASE_URL": "https://example.test/v1?credential=hidden"}, transport=self.transport)
        payload = json.loads(self.calls[0][0].data.decode())
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["stream"], False)
        self.assertEqual(self.calls[0][1], 120)
        self.assertEqual(evidence["api_call_count"], 1)
        self.assertEqual(evidence["retry_count"], 0)
        self.assertTrue((self.output / "generated_scene.py").is_file())
        self.assertEqual(evidence["code_sha256"], hashlib.sha256((self.output / "generated_scene.py").read_bytes()).hexdigest())
        saved = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in self.output.rglob("*"))
        self.assertNotIn("secret", saved)
        self.assertNotIn("credential=hidden", saved)

    def test_missing_environment_makes_zero_calls(self):
        with self.assertRaises(DeepSeekCodegenError):
            request_blender_code(codegen_input=VALID_INPUT, output_dir=self.output, environ={}, transport=self.transport)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(json.loads((self.output / "evidence.json").read_text())["api_call_count"], 0)

    def test_rejects_truncation_and_extra_response_fields(self):
        def transport(request, timeout):
            self.calls.append(request)
            body = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
            return _Response(json.dumps(body).encode())
        with self.assertRaisesRegex(DeepSeekCodegenError, "truncated"):
            request_blender_code(codegen_input=VALID_INPUT, output_dir=self.output, environ={"DEEPSEEK_API_KEY": "s", "DEEPSEEK_BASE_URL": "https://x.test/v1"}, transport=transport)

    def test_rejects_non_json_content_and_large_code_without_retry(self):
        def transport(request, timeout):
            self.calls.append(request)
            code = "def build_scene(context):\n    x = '" + ("a" * 40960) + "'\n"
            body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"schema_version": "blender-codegen-response-1.0", "summary": "x", "python_code": code})}}]}
            return _Response(json.dumps(body).encode())
        with self.assertRaisesRegex(DeepSeekCodegenError, "40 KiB"):
            request_blender_code(codegen_input=VALID_INPUT, output_dir=self.output, environ={"DEEPSEEK_API_KEY": "s", "DEEPSEEK_BASE_URL": "https://x.test/v1"}, transport=transport)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
