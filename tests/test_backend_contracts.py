import json
import unittest

from videoactagent.prompts import compile_shot_prompts
from videoactagent.shotscript import ShotScript


SHOT_SCRIPT = "examples/station_shotscript.json"


class BackendContractTests(unittest.TestCase):
    def test_capabilities_separate_model_support_from_jd_gateway_evidence(self):
        from videoactagent.backends.capabilities import gateway_capability

        seedance = gateway_capability("seedance")
        kling = gateway_capability("kling")

        self.assertEqual(seedance.reference_video, "gateway_unverified")
        self.assertEqual(seedance.first_last_frame, "client_declared")
        self.assertEqual(seedance.text_to_video, "client_declared")
        self.assertEqual(kling.text_to_video, "client_declared")
        self.assertEqual(kling.first_last_frame, "unsupported")
        self.assertEqual(kling.reference_video, "gateway_unverified")

    def test_builds_prompt_payloads_in_inherited_jd_demo_shape(self):
        from videoactagent.backends.jd import build_kling_t2v, build_seedance_t2v

        prompt = self._cinematic_prompt()
        kling = build_kling_t2v(prompt)
        seedance = build_seedance_t2v(prompt)

        self.assertEqual(kling["model"], "Kling-V2-5-Turbo")
        self.assertEqual(kling["content"], [{"type": "text", "text": prompt}])
        self.assertEqual(
            kling["parameters"],
            {"duration": 5, "mode": "std", "aspect_ratio": "16:9"},
        )
        self.assertEqual(seedance["model"], "Doubao-Seedance-2.0")
        self.assertEqual(seedance["content"], [{"type": "text", "text": prompt}])
        self.assertEqual(seedance["parameters"]["duration"], 5)

    def test_builds_seedance_first_last_roles_without_reference_video(self):
        from videoactagent.backends.jd import build_seedance_first_last

        payload = build_seedance_first_last(
            self._cinematic_prompt(),
            "asset://controlled-first-frame",
            "asset://controlled-last-frame",
        )

        self.assertEqual(
            [item.get("role") for item in payload["content"]],
            [None, "first_frame", "last_frame"],
        )
        self.assertEqual(payload["content"][1]["type"], "image_url")
        self.assertEqual(
            payload["content"][2]["image_url"]["url"],
            "asset://controlled-last-frame",
        )
        self.assertNotIn("video_url", json.dumps(payload))

    def _cinematic_prompt(self) -> str:
        script = ShotScript.from_path(SHOT_SCRIPT)
        return compile_shot_prompts(script.shots[0]).cinematic


if __name__ == "__main__":
    unittest.main()
