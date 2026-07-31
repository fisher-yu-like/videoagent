"""Pure Seedance reference-video evidence and candidate contracts.

These code-only fixtures are not gateway acceptance evidence.  Every test patches
the network entry points so candidate construction proves it performs no submit.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODEL = "Doubao-Seedance-2.5"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def record(path: str, sha256: str = SHA_A, size: int = 12, **extra: object) -> dict:
    return {"path": path, "sha256": sha256, "bytes": size, **extra}


def evidence_document(*, verified: bool, response_sha256: str | None = None, model: str = MODEL) -> dict:
    contract = {
        "observed_at": "2026-07-31T12:34:56Z",
        "model": model,
        "content_type": "video_url",
        "url_field": "video_url",
        "role": "reference_video",
    }
    result = {
        "schema_version": "seedance-reference-capability/1",
        "model_capability": "model_supported",
        "gateway_capability": "gateway_verified" if verified else "gateway_unverified",
        "model_evidence": {
            **contract,
            "source_url": "https://www.volcengine.com/docs/official/model-reference",
        },
        "gateway_evidence": None,
    }
    if verified:
        result["gateway_evidence"] = {
            **contract,
            "response_record": {
                "path": "captures/jd-gateway.json",
                "sha256": response_sha256,
            },
        }
    return result


def approved_export() -> dict:
    prompt = "Preserve the approved humanoid motion; restyle as cinematic live action."
    prompt_bytes = prompt.encode("utf-8")
    return {
        "iteration_id": "D1",
        "trajectory_compiler_version": "trajectory-v2",
        "restyle_compiler_version": "restyle-v1",
        "approval": record("iterations/D1/approval.json", SHA_A, 311),
        "clay": record(
            "iterations/D1/clay.mp4",
            SHA_B,
            4096,
            media={"duration_seconds": 5.0},
        ),
        "restyle_prompt": record(
            "iterations/D1/input/restyle_prompt.txt",
            hashlib.sha256(prompt_bytes).hexdigest(),
            len(prompt_bytes),
            text=prompt,
        ),
        "restyle_profile": record(
            "source/restyle_profile.json", SHA_C, 901,
            provenance={"source": "task4-approved-export"},
        ),
        "compiled_prompt": record("iterations/D1/input/compiled_prompt.txt", SHA_A, 44),
    }


class SeedanceCapabilityEvidenceTests(unittest.TestCase):
    def test_official_model_page_cannot_verify_gateway(self) -> None:
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        value = evidence_document(verified=False)
        capability = load_seedance_capability_evidence(value)

        self.assertEqual(capability.model_capability, "model_supported")
        self.assertEqual(capability.gateway_capability, "gateway_unverified")
        self.assertEqual(capability.model_evidence.model, MODEL)

        forged = deepcopy(value)
        forged["gateway_capability"] = "gateway_verified"
        forged["gateway_evidence"] = deepcopy(forged["model_evidence"])
        with self.assertRaisesRegex(ValueError, "captured.*response"):
            load_seedance_capability_evidence(forged)

    def test_gateway_verified_requires_local_response_and_matching_sha256(self) -> None:
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            response_path = root_path / "captures" / "jd-gateway.json"
            response_path.parent.mkdir()
            response_path.write_bytes(b'{"task_id":"captured-real-response"}')
            digest = hashlib.sha256(response_path.read_bytes()).hexdigest()
            value = evidence_document(verified=True, response_sha256=digest)

            loaded = load_seedance_capability_evidence(value, base_dir=root_path)
            self.assertEqual(loaded.gateway_capability, "gateway_verified")

            response_path.write_bytes(b'{"task_id":"tampered"}')
            with self.assertRaisesRegex(ValueError, "hash"):
                load_seedance_capability_evidence(value, base_dir=root_path)

            missing = evidence_document(verified=True, response_sha256=digest)
            missing["gateway_evidence"]["response_record"]["path"] = (
                "captures/missing-response.json"
            )
            with self.assertRaisesRegex(ValueError, "cannot resolve captured"):
                load_seedance_capability_evidence(missing, base_dir=root_path)

    def test_gateway_capture_must_show_successful_task_response(self) -> None:
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "unsupported.json"
            path.write_bytes(b'{"error":"unsupported content type"}')
            value = evidence_document(
                verified=True,
                response_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            value["gateway_evidence"]["response_record"]["path"] = path.name
            with self.assertRaisesRegex(ValueError, "successful"):
                load_seedance_capability_evidence(value, base_dir=Path(root))

    def test_evidence_rejects_unknown_fields_bad_contract_and_unsafe_capture(self) -> None:
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        cases = []
        unknown = evidence_document(verified=False)
        unknown["product_name"] = "Seedance 2.5"
        cases.append(unknown)
        wrong_role = evidence_document(verified=False)
        wrong_role["model_evidence"]["role"] = "input_video"
        cases.append(wrong_role)
        wrong_field = evidence_document(verified=False)
        wrong_field["model_evidence"]["url_field"] = "url"
        cases.append(wrong_field)
        bad_time = evidence_document(verified=False)
        bad_time["model_evidence"]["observed_at"] = "2026-07-31"
        cases.append(bad_time)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    load_seedance_capability_evidence(value)

        with tempfile.TemporaryDirectory() as root:
            unsafe = evidence_document(verified=True, response_sha256=SHA_A)
            unsafe["gateway_evidence"]["response_record"]["path"] = "../response.json"
            with self.assertRaisesRegex(ValueError, "safe relative"):
                load_seedance_capability_evidence(unsafe, base_dir=Path(root))


class SeedanceReferenceRequestTests(unittest.TestCase):
    def test_remote_video_asset_is_public_https_only(self) -> None:
        from videoactagent.seedance_reference import validate_remote_video_asset

        good = "https://media.volccdn.com/approved/clay.mp4"
        self.assertEqual(validate_remote_video_asset(good), good)
        invalid = (
            "iterations/D1/clay.mp4",
            "asset://controlled-proxy",
            "file:///tmp/clay.mp4",
            "http://media.volccdn.com/clay.mp4",
            "data:video/mp4;base64,AAAA",
            "https://127.0.0.1/clay.mp4",
            "https://169.254.1.2/clay.mp4",
            "https://8.8.8.8/clay.mp4",
            "https://example.com/clay.mp4",
            "https://cdn.example.com/clay.mp4",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_remote_video_asset(value)

    def test_request_shape_has_one_text_one_reference_video_and_no_image(self) -> None:
        from videoactagent.backends.jd import build_seedance_reference_video

        payload = build_seedance_reference_video(
            "approved prompt",
            "https://media.volccdn.com/approved/clay.mp4",
            model=MODEL,
        )
        self.assertEqual(payload, {
            "model": MODEL,
            "content": [
                {"type": "text", "text": "approved prompt"},
                {
                    "type": "video_url",
                    "video_url": {"url": "https://media.volccdn.com/approved/clay.mp4"},
                    "role": "reference_video",
                },
            ],
            "parameters": {
                "ratio": "16:9",
                "resolution": "720p",
                "duration": 5,
                "watermark": False,
            },
        })
        self.assertNotIn("image", json.dumps(payload))
        self.assertNotIn("audio", json.dumps(payload))

        for prompt, model, duration in (
            ("", MODEL, 5),
            ("ok", "", 5),
            ("ok", "Not-A-Seedance-Model", 5),
            ("ok", MODEL, 6),
            ("ok", MODEL, 5.0),
        ):
            with self.subTest(prompt=prompt, model=model, duration=duration):
                with self.assertRaises(ValueError):
                    build_seedance_reference_video(
                        prompt,
                        "https://media.volccdn.com/approved/clay.mp4",
                        model=model,
                        duration=duration,
                    )


class SeedanceReferenceCandidateTests(unittest.TestCase):
    def _capability(self, *, verified: bool):
        from videoactagent.seedance_reference import load_seedance_capability_evidence

        if not verified:
            return load_seedance_capability_evidence(evidence_document(verified=False))
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        path = root / "captures" / "jd-gateway.json"
        path.parent.mkdir()
        path.write_bytes(b'{"task_id":"real-captured-task"}')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return load_seedance_capability_evidence(
            evidence_document(verified=True, response_sha256=digest), base_dir=root
        )

    def test_verified_candidate_is_ready_hash_bound_deterministic_and_zero_network(self) -> None:
        from videoactagent.seedance_reference import prepare_reference_candidate

        source = approved_export()
        capability = self._capability(verified=True)
        url = "https://media.volccdn.com/approved/clay.mp4"
        with (
            patch("urllib.request.urlopen", side_effect=AssertionError("network called")) as urlopen,
            patch("videoactagent.backends.jd._request_once", side_effect=AssertionError("request called")) as request,
            patch("videoactagent.backends.jd.submit_once", side_effect=AssertionError("submit called")) as submit,
        ):
            first = prepare_reference_candidate(
                approved_export=source, proxy_url=url, capability=capability
            )
            second = prepare_reference_candidate(
                approved_export=deepcopy(source), proxy_url=url, capability=capability
            )

        self.assertEqual(first["state"], "ready")
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")).encode(),
            json.dumps(second, sort_keys=True, separators=(",", ":")).encode(),
        )
        self.assertFalse(first["network_called"])
        self.assertEqual(first["generation_submit_limit"], 1)
        self.assertEqual(first["automatic_retry_limit"], 0)
        self.assertFalse(first["fallbacks"]["reference_image"])
        self.assertFalse(first["fallbacks"]["prompt_only"])
        self.assertEqual(first["source_hashes"]["clay"], source["clay"]["sha256"])
        self.assertEqual(
            first["source_hashes"]["restyle_prompt"],
            source["restyle_prompt"]["sha256"],
        )
        self.assertEqual(
            first["source_hashes"]["compiled_prompt"],
            source["compiled_prompt"]["sha256"],
        )
        self.assertEqual(
            first["compiler_hashes"]["restyle_prompt"],
            source["restyle_prompt"]["sha256"],
        )
        self.assertEqual(
            first["capability_evidence"]["gateway"]["capture"]["sha256"],
            capability.gateway_evidence.response_record.sha256,
        )
        canonical_payload = json.dumps(
            first["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(
            first["payload_sha256"], hashlib.sha256(canonical_payload).hexdigest()
        )
        serialized = json.dumps(first)
        self.assertNotIn("task_id", serialized)
        self.assertNotIn("response", serialized)
        self.assertNotIn("image_url", serialized)
        urlopen.assert_not_called()
        request.assert_not_called()
        submit.assert_not_called()

    def test_model_supported_unverified_candidate_is_single_combined_probe(self) -> None:
        from videoactagent.seedance_reference import prepare_reference_candidate

        candidate = prepare_reference_candidate(
            approved_export=approved_export(),
            proxy_url="https://media.volccdn.com/approved/clay.mp4",
            capability=self._capability(verified=False),
        )
        self.assertEqual(candidate["state"], "ready_for_single_combined_probe")
        self.assertEqual(candidate["payload"]["model"], MODEL)

    def test_verified_doubao_seedance_2_0_requires_exact_evidence_model(self) -> None:
        from videoactagent.seedance_reference import load_seedance_capability_evidence, prepare_reference_candidate

        capability = load_seedance_capability_evidence(
            evidence_document(verified=False, model="Doubao-Seedance-2.0")
        )
        candidate = prepare_reference_candidate(
            approved_export=approved_export(),
            proxy_url="https://media.volccdn.com/approved/clay.mp4",
            capability=capability,
        )
        self.assertEqual(candidate["state"], "ready_for_single_combined_probe")
        self.assertEqual(candidate["payload"]["model"], "Doubao-Seedance-2.0")

    def test_legitimate_absence_blocks_but_malformed_records_raise(self) -> None:
        from videoactagent.seedance_reference import prepare_reference_candidate

        capability = self._capability(verified=False)
        missing_approval = approved_export()
        missing_approval.pop("approval")
        blocked = prepare_reference_candidate(
            approved_export=missing_approval,
            proxy_url="https://media.volccdn.com/approved/clay.mp4",
            capability=capability,
        )
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("missing approved human iteration evidence", blocked["blockers"])
        self.assertNotIn("payload", blocked)

        missing_url = prepare_reference_candidate(
            approved_export=approved_export(), proxy_url="", capability=capability
        )
        self.assertEqual(missing_url["state"], "blocked")
        self.assertIn("missing public Seedance proxy video URL", missing_url["blockers"])

        bare = approved_export()
        bare["restyle_prompt"].pop("text")
        not_materialized = prepare_reference_candidate(
            approved_export=bare,
            proxy_url="https://media.volccdn.com/approved/clay.mp4",
            capability=capability,
        )
        self.assertEqual(not_materialized["state"], "blocked")
        self.assertIn(
            "restyle prompt text is not materialized", not_materialized["blockers"]
        )

        tampered = approved_export()
        tampered["restyle_prompt"]["text"] += " tampered"
        with self.assertRaisesRegex(ValueError, "restyle_prompt hash/size"):
            prepare_reference_candidate(
                approved_export=tampered,
                proxy_url="https://media.volccdn.com/approved/clay.mp4",
                capability=capability,
            )

        partial = approved_export()
        partial["clay"].pop("sha256")
        with self.assertRaisesRegex(ValueError, "clay record is missing"):
            prepare_reference_candidate(
                approved_export=partial,
                proxy_url="https://media.volccdn.com/approved/clay.mp4",
                capability=capability,
            )

    def test_model_name_alone_and_static_capability_never_unlock_candidate(self) -> None:
        from videoactagent.backends.capabilities import gateway_capability
        from videoactagent.seedance_reference import (
            SeedanceCapabilityEvidence,
            SeedanceEvidenceRecord,
            prepare_reference_candidate,
        )

        for capability in (MODEL, gateway_capability("seedance"), None):
            with self.subTest(capability=capability):
                candidate = prepare_reference_candidate(
                    approved_export=approved_export(),
                    proxy_url="https://media.volccdn.com/approved/clay.mp4",
                    capability=capability,
                )
                self.assertEqual(candidate["state"], "blocked")
                self.assertTrue(candidate["blockers"])

        forged_model_evidence = SeedanceEvidenceRecord(
            observed_at="2026-07-31T12:34:56Z",
            model=MODEL,
            content_type="video_url",
            url_field="video_url",
            role="reference_video",
            source_url="https://www.volcengine.com/docs/official/model-reference",
        )
        with self.assertRaises(ValueError):
            SeedanceCapabilityEvidence(
                schema_version="seedance-reference-capability/1",
                model_capability="model_supported",
                gateway_capability="gateway_verified",
                model_evidence=forged_model_evidence,
                gateway_evidence=None,
            )

        forged_unverified = SeedanceCapabilityEvidence(
            schema_version="seedance-reference-capability/1",
            model_capability="model_supported",
            gateway_capability="gateway_unverified",
            model_evidence=forged_model_evidence,
            gateway_evidence=None,
        )
        object.__setattr__(forged_unverified, "_capture_verified", True)
        candidate = prepare_reference_candidate(
            approved_export=approved_export(),
            proxy_url="https://media.volccdn.com/approved/clay.mp4",
            capability=forged_unverified,
        )
        self.assertEqual(candidate["state"], "blocked")


    def test_candidate_binds_proxy_record_and_url(self) -> None:
        from videoactagent.seedance_reference import prepare_reference_candidate

        candidate = prepare_reference_candidate(
            approved_export=approved_export(),
            proxy_url="https://media.volccdn.com/approved/clay.mp4",
            capability=self._capability(verified=False),
        )
        self.assertEqual(
            candidate["proxy"]["url"], "https://media.volccdn.com/approved/clay.mp4"
        )
        self.assertEqual(candidate["proxy"]["sha256"], approved_export()["clay"]["sha256"])
        self.assertEqual(candidate["proxy"]["bytes"], approved_export()["clay"]["bytes"])
        self.assertEqual(candidate["proxy"]["path"], approved_export()["clay"]["path"])

    def test_materialize_prompt_reads_safe_verified_utf8_without_mutating_export(self) -> None:
        from videoactagent.seedance_reference import materialize_approved_prompt

        source = approved_export()
        prompt = source["restyle_prompt"].pop("text")
        original = deepcopy(source)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / source["restyle_prompt"]["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(prompt.encode("utf-8"))
            materialized = materialize_approved_prompt(source, Path(root))

        self.assertEqual(source, original)
        self.assertEqual(materialized["restyle_prompt"]["text"], prompt)

    def test_materialize_prompt_rejects_traversal_and_hash_drift(self) -> None:
        from videoactagent.seedance_reference import materialize_approved_prompt

        traversal = approved_export()
        traversal["restyle_prompt"].pop("text")
        traversal["restyle_prompt"]["path"] = "../prompt.txt"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "safe relative"):
                materialize_approved_prompt(traversal, Path(root))

        drift = approved_export()
        drift["restyle_prompt"].pop("text")
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / drift["restyle_prompt"]["path"]
            target.parent.mkdir(parents=True)
            target.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash/size"):
                materialize_approved_prompt(drift, Path(root))


if __name__ == "__main__":
    unittest.main()
