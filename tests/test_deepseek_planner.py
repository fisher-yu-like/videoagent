import json
from pathlib import Path
import tempfile
import unittest

from tests.test_multicam_plan import VALID_PLAN
from tests.test_scene_plan import VALID_DRAFT
from videoactagent.deepseek_planner import (
    DeepSeekPlannerError,
    request_multicam_plan,
    request_scene_plan,
)


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
            "id": "call-1-secret-value",
            "model": "deepseek-v4-pro-secret-value",
            "diagnostic": {"nested": ["Bearer secret-value"]},
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": content},
            }],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "provider": {"echo": "secret-value"},
            },
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
        self.assertEqual(request_payload["model"], "deepseek-v4-flash")
        self.assertEqual(request_payload["thinking"], {"type": "disabled"})
        user_context = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(
            [camera["camera_id"] for camera in user_context["required_json_schema_example"]["cameras"]],
            ["camera_a", "camera_b", "camera_c"],
        )

    def test_scoped_revision_adds_complete_context_to_one_non_streaming_call(self):
        calls = []
        previous_camera_bundle = {
            "schema_version": "1.0",
            "cameras": {"camera_b": {"states": [{"keyframe_id": "K0"}]}},
        }
        response = json.dumps({
            "id": "revision-call",
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(VALID_PLAN)},
            }],
        }).encode()

        def transport(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            result = request_multicam_plan(
                scene_context=SCENE_CONTEXT,
                output_dir=Path(root) / "P2",
                environ={
                    "DEEPSEEK_API_KEY": "secret",
                    "DEEPSEEK_BASE_URL": "https://example.test/v1",
                },
                transport=transport,
                previous_plan=VALID_PLAN,
                previous_camera_bundle=previous_camera_bundle,
                revision_scope="camera_b",
                feedback="  keep B static and looking at actor_b  ",
            )

        self.assertEqual(len(calls), 1)
        request_payload = json.loads(calls[0][0].data.decode("utf-8"))
        self.assertFalse(request_payload["stream"])
        self.assertEqual(result["api_call_count"], 1)
        self.assertEqual(result["retry_count"], 0)
        user_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(
            user_payload["revision"],
            {
                "scope": "camera_b",
                "feedback": "keep B static and looking at actor_b",
                "previous_plan": VALID_PLAN,
                "previous_camera_bundle": previous_camera_bundle,
            },
        )
        self.assertEqual(
            set(user_payload["revision"]),
            {"scope", "feedback", "previous_plan", "previous_camera_bundle"},
        )
        system_prompt = request_payload["messages"][0]["content"]
        self.assertIn("unselected camera assignment exactly", system_prompt)

    def test_revision_secrets_are_redacted_only_in_saved_request_artifact(self):
        secret = "revision-secret"
        calls = []
        response = json.dumps({
            "id": "revision-redaction-call",
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(VALID_PLAN)},
            }],
        }).encode()

        def transport(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "P2"
            request_multicam_plan(
                scene_context=SCENE_CONTEXT,
                output_dir=output,
                environ={
                    "DEEPSEEK_API_KEY": secret,
                    "DEEPSEEK_BASE_URL": "https://example.test/v1",
                },
                transport=transport,
                previous_plan={"credential": secret},
                previous_camera_bundle={"nested": [secret]},
                revision_scope="camera_b",
                feedback=f"keep {secret} out of saved evidence",
            )
            saved_request = (output / "request.json").read_text(encoding="utf-8")

        sent_request = calls[0][0].data.decode("utf-8")
        self.assertIn(secret, sent_request)
        self.assertNotIn(secret, saved_request)
        self.assertIn("[REDACTED]", saved_request)

    def test_invalid_revision_arguments_fail_before_transport(self):
        valid_revision = {
            "previous_plan": VALID_PLAN,
            "previous_camera_bundle": {"cameras": {}},
            "revision_scope": "camera_b",
            "feedback": "keep B static",
        }
        invalid_revisions = {
            "partial": {"previous_plan": VALID_PLAN},
            "scope": {**valid_revision, "revision_scope": "camera_d"},
            "empty_feedback": {**valid_revision, "feedback": "   "},
            "long_feedback": {**valid_revision, "feedback": "x" * 2001},
            "plan_type": {**valid_revision, "previous_plan": []},
            "bundle_type": {**valid_revision, "previous_camera_bundle": []},
        }

        for name, revision in invalid_revisions.items():
            calls = []
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(DeepSeekPlannerError):
                    request_multicam_plan(
                        scene_context=SCENE_CONTEXT,
                        output_dir=Path(root) / "invalid",
                        environ={
                            "DEEPSEEK_API_KEY": "secret",
                            "DEEPSEEK_BASE_URL": "https://example.test/v1",
                        },
                        transport=lambda *_args, **_kwargs: calls.append(True),
                        **revision,
                    )
            self.assertEqual(calls, [])

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

    def test_length_response_reports_truncation_without_retry(self):
        calls = []
        response = json.dumps({
            "id": "call-truncated",
            "model": "deepseek-v4-pro",
            "choices": [{
                "finish_reason": "length",
                "message": {"content": '{"schema_version":"1.0"'},
            }],
        }).encode()

        def transport(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "P1"
            with self.assertRaisesRegex(
                DeepSeekPlannerError,
                "truncated at max_tokens=4096",
            ):
                request_multicam_plan(
                    scene_context=SCENE_CONTEXT,
                    output_dir=output,
                    environ={
                        "DEEPSEEK_API_KEY": "secret",
                        "DEEPSEEK_BASE_URL": "https://example.test/v1",
                    },
                    transport=transport,
                )
            evidence = json.loads(
                (output / "evidence.json").read_text(encoding="utf-8")
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(evidence["api_call_count"], 1)
        self.assertEqual(evidence["retry_count"], 0)
        self.assertEqual(evidence["status"], "failed")

    def test_scene_plan_uses_flash_once_and_persists_validated_evidence(self):
        calls = []
        response = json.dumps({
            "id": "scene-call-1-secret",
            "model": "deepseek-v4-flash-secret",
            "diagnostic": {"nested": ["Bearer secret"]},
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(VALID_DRAFT)},
            }],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 120,
                "provider": {"echo": "secret"},
            },
        }).encode()

        def transport(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "SP1"
            result = request_scene_plan(
                story_prompt="A traveler walks toward a friend.",
                duration_seconds=5,
                output_dir=output,
                environ={
                    "DEEPSEEK_API_KEY": "secret",
                    "DEEPSEEK_BASE_URL": "https://example.test/v1?credential=hidden",
                    "DEEPSEEK_MODEL": "must-be-ignored",
                },
                transport=transport,
            )
            request_payload = json.loads(calls[0][0].data.decode("utf-8"))
            persisted = {
                path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in output.glob("*.json")
            }

        self.assertEqual(len(calls), 1)
        self.assertEqual(request_payload["model"], "deepseek-v4-flash")
        self.assertEqual(result["api_call_count"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["response_artifact"], "response.json")
        self.assertEqual(result["model"], "deepseek-v4-flash")
        self.assertEqual(persisted["draft.json"], VALID_DRAFT)
        self.assertEqual(
            set(persisted), {"request.json", "response.json", "draft.json", "evidence.json"}
        )
        serialized = json.dumps(persisted)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("credential=hidden", serialized)

        user_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(
            user_payload["allowed_actor_actions"],
            ["walk", "wait", "stand", "approach", "cross", "follow", "carry"],
        )
        self.assertEqual(user_payload["allowed_object_semantics"], ["move", "static"])
        system_prompt = request_payload["messages"][0]["content"]
        for action in user_payload["allowed_actor_actions"]:
            self.assertIn(action, system_prompt)

    def test_scene_plan_feedback_includes_previous_draft_without_changing_model(self):
        calls = []
        response = json.dumps({
            "id": "scene-call-2",
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(VALID_DRAFT)},
            }],
        }).encode()

        def transport(request, timeout):
            calls.append(request)
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            request_scene_plan(
                story_prompt="story",
                duration_seconds=5,
                feedback="Keep the suitcase closer.",
                previous_draft=VALID_DRAFT,
                output_dir=Path(root) / "SP2",
                environ={
                    "DEEPSEEK_API_KEY": "secret",
                    "DEEPSEEK_BASE_URL": "https://example.test/v1",
                },
                transport=transport,
            )
        payload = json.loads(calls[0].data.decode("utf-8"))
        user_payload = json.loads(payload["messages"][1]["content"])
        self.assertEqual(user_payload["feedback"], "Keep the suitcase closer.")
        self.assertEqual(user_payload["previous_draft"], VALID_DRAFT)
        self.assertEqual(payload["model"], "deepseek-v4-flash")

    def test_scene_plan_accepts_model_override_and_repair_feedback(self):
        calls = []
        response = json.dumps({
            "id": "scene-pro",
            "model": "deepseek-v4-pro",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(VALID_DRAFT)},
            }],
        }).encode()

        def transport(request, timeout):
            calls.append(request)
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "PF1"
            result = request_scene_plan(
                story_prompt="A station meeting.", duration_seconds=5,
                output_dir=output, model="deepseek-v4-pro",
                feedback="Previous response used an invalid environment_preset.",
                environ={"DEEPSEEK_API_KEY": "secret", "DEEPSEEK_BASE_URL": "https://example.test/v1"},
                transport=transport,
            )
            payload = json.loads(calls[0].data.decode("utf-8"))
            user_payload = json.loads(payload["messages"][1]["content"])
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(user_payload["feedback"], "Previous response used an invalid environment_preset.")
        self.assertEqual(result["model"], "deepseek-v4-pro")

    def test_scene_plan_default_model_remains_flash(self):
        calls = []
        response = json.dumps({
            "id": "scene-default",
            "model": "deepseek-v4-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(VALID_DRAFT)}}],
        }).encode()

        def transport(request, timeout):
            calls.append(request)
            return FakeResponse(response)

        with tempfile.TemporaryDirectory() as root:
            request_scene_plan(
                story_prompt="A station meeting.", duration_seconds=5,
                output_dir=Path(root) / "SP1",
                environ={"DEEPSEEK_API_KEY": "secret", "DEEPSEEK_BASE_URL": "https://example.test/v1"},
                transport=transport,
            )
        payload = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(payload["model"], "deepseek-v4-flash")

    def test_scene_plan_rejects_finish_reason_and_schema_without_retry(self):
        bodies = (
            {
                "id": "scene-length",
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "length", "message": {"content": "{}"}}],
            },
            {
                "id": "scene-schema",
                "model": "deepseek-v4-flash",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"schema_version": "scene-plan-1.0"})},
                }],
            },
        )
        for index, body in enumerate(bodies):
            calls = []

            def transport(request, timeout):
                calls.append(request)
                return FakeResponse(json.dumps(body).encode())

            with self.subTest(index=index), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "SP"
                with self.assertRaises(DeepSeekPlannerError):
                    request_scene_plan(
                        story_prompt="story",
                        duration_seconds=5,
                        output_dir=output,
                        environ={
                            "DEEPSEEK_API_KEY": "secret",
                            "DEEPSEEK_BASE_URL": "https://example.test/v1",
                        },
                        transport=transport,
                    )
                evidence = json.loads((output / "evidence.json").read_text("utf-8"))
                self.assertEqual(len(calls), 1)
                self.assertEqual(evidence["api_call_count"], 1)
                self.assertEqual(evidence["retry_count"], 0)
                self.assertEqual(evidence["status"], "failed")
                self.assertFalse((output / "draft.json").exists())

    def test_non_json_raw_response_is_persisted_for_both_request_types(self):
        raw = b'upstream failure: \xff secret-must-not-leak'

        for request_kind in ("multicam", "scene"):
            calls = []

            def transport(request, timeout):
                calls.append(request)
                return FakeResponse(raw)

            with self.subTest(request_kind=request_kind), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "failed"
                kwargs = {
                    "output_dir": output,
                    "environ": {
                        "DEEPSEEK_API_KEY": "secret-must-not-leak",
                        "DEEPSEEK_BASE_URL": "https://example.test/v1",
                    },
                    "transport": transport,
                }
                with self.assertRaisesRegex(DeepSeekPlannerError, "not JSON"):
                    if request_kind == "multicam":
                        request_multicam_plan(scene_context=SCENE_CONTEXT, **kwargs)
                    else:
                        request_scene_plan(
                            story_prompt="story", duration_seconds=5, **kwargs
                        )

                evidence = json.loads((output / "evidence.json").read_text("utf-8"))
                saved = (output / "response.raw").read_bytes()
                self.assertEqual(len(calls), 1)
                self.assertEqual(evidence["response_artifact"], "response.raw")
                self.assertTrue((output / "request.json").is_file())
                self.assertFalse((output / "response.json").exists())
                self.assertIn(b"upstream failure", saved)
                self.assertNotIn(b"secret-must-not-leak", saved)


if __name__ == "__main__":
    unittest.main()
