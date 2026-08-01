import json
from pathlib import Path
import tempfile
import unittest

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.deepseek_planner import DeepSeekPlannerError, request_multicam_plan


SCENE_CONTEXT = {
    "scene_id": "station_reunion",
    "story_prompt": "Two travelers reunite on a station platform.",
    "actors": ["actor_a", "actor_b"],
    "world_bounds": [-5.0, 5.0, -4.0, 4.0],
    "keyframes": [{"id": f"K{i}", "t": t} for i, t in enumerate((0, .2, .5, .8, 1))],
    "locked_through_keyframe": None,
}


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class DeepSeekPlannerTests(unittest.TestCase):
    def test_single_call_writes_redacted_validated_evidence(self):
        calls = []
        content = json.dumps(VALID_PLAN)
        response = json.dumps({
            "id": "call-1",
            "model": "deepseek-v4-pro",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": content},
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200},
        }).encode()

        def transport(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "P1"
            result = request_multicam_plan(
                scene_context=SCENE_CONTEXT,
                output_dir=output,
                environ={
                    "DEEPSEEK_API_KEY": "secret-value",
                    "DEEPSEEK_BASE_URL": "https://example.test/v1?credential=hidden",
                },
                transport=transport,
            )
            serialized = "\n".join(
                path.read_text(encoding="utf-8") for path in output.glob("*.json")
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["api_call_count"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("credential=hidden", serialized)
        self.assertEqual(calls[0][0].get_header("Authorization"), "Bearer secret-value")
        request_payload = json.loads(calls[0][0].data.decode("utf-8"))
        user_context = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(
            [camera["camera_id"] for camera in user_context["required_json_schema_example"]["cameras"]],
            ["camera_a", "camera_b", "camera_c"],
        )

    def test_missing_environment_fails_without_transport(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "P1"
            with self.assertRaisesRegex(DeepSeekPlannerError, "environment"):
                request_multicam_plan(
                    scene_context=SCENE_CONTEXT,
                    output_dir=output,
                    environ={},
                    transport=lambda *_args, **_kwargs: calls.append(True),
                )
            evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(calls, [])
        self.assertEqual(evidence["api_call_count"], 0)
        self.assertEqual(evidence["status"], "environment_blocked")

    def test_empty_or_schema_invalid_response_is_not_retried(self):
        bodies = [
            {
                "id": "call-empty", "model": "deepseek-v4-pro",
                "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
            },
            {
                "id": "call-invalid", "model": "deepseek-v4-pro",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"schema_version": "1.0"})},
                }],
            },
        ]
        for index, body in enumerate(bodies):
            calls = []

            def transport(request, timeout):
                calls.append(request)
                return FakeResponse(json.dumps(body).encode())

            with self.subTest(index=index), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "P1"
                with self.assertRaises(DeepSeekPlannerError):
                    request_multicam_plan(
                        scene_context=SCENE_CONTEXT,
                        output_dir=output,
                        environ={
                            "DEEPSEEK_API_KEY": "secret",
                            "DEEPSEEK_BASE_URL": "https://example.test/v1",
                        },
                        transport=transport,
                    )
                evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
                self.assertEqual(len(calls), 1)
                self.assertEqual(evidence["retry_count"], 0)
                self.assertEqual(evidence["status"], "failed")


if __name__ == "__main__":
    unittest.main()
