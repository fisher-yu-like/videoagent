"""Acceptance-manifest contract tests.

These temporary fixtures verify schema and hash recomputation mechanics only.
They are not manual browser evidence and never create a persistent acceptance
manifest in ``runs/``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from PIL import Image

from videoactagent.trajectory import TrajectoryInstruction, canonical_bytes


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TrajectoryAcceptanceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        artifacts = self.workspace / "artifacts"
        artifacts.mkdir()
        self.preview = artifacts / "preview.png"
        self.overlay = artifacts / "overlay.png"
        Image.new("RGB", (960, 540), (10, 20, 30)).save(self.preview)
        Image.new("RGB", (960, 540), (30, 20, 10)).save(self.overlay)
        self.trajectory = artifacts / "trajectory.json"
        instruction = TrajectoryInstruction.from_dict(
            {
                "schema_version": "0.1",
                "scene_id": "station_platform",
                "shot_id": "s01",
                "coordinate_space": "normalized_0_1_top_left",
                "duration_seconds": 5.0,
                "sample_count": 121,
                "tracks": [
                    {
                        "track_id": "actor_path_01",
                        "target": {"type": "actor", "id": "actor_a"},
                        "primitive": "polyline",
                        "semantic": "move",
                        "points": [
                            {"t": 0.2, "x": 0.1, "y": 0.5, "visible": True},
                            {"t": 0.8, "x": 0.8, "y": 0.5, "visible": True},
                        ],
                    }
                ],
            }
        )
        self.trajectory.write_bytes(canonical_bytes(instruction))
        self.manifest = {
            "schema_version": "0.1",
            "evidence_type": "manual_browser_interaction",
            "timestamp": "2026-07-29T21:00:00+08:00",
            "scene_id": "station_platform",
            "shot_id": "s01",
            "track_id": "actor_path_01",
            "operations": [
                {"action": "set_slider", "value": 0.8},
                {"action": "drag_point", "original_t": 0.2},
                {"action": "Finish"},
                {"action": "Save"},
                {"action": "Load"},
            ],
            "input_preview": {"path": "artifacts/preview.png", "sha256": _sha(self.preview)},
            "trajectory": {"path": "artifacts/trajectory.json", "sha256": _sha(self.trajectory)},
            "overlay": {
                "path": "artifacts/overlay.png",
                "sha256": _sha(self.overlay),
                "width": 960,
                "height": 540,
                "mode": "RGB",
            },
            "final_times": [0.2, 0.8],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validates_schema_and_recomputes_all_artifact_claims(self) -> None:
        from videoactagent.trajectory_acceptance import validate_acceptance_manifest

        result = validate_acceptance_manifest(self.manifest, self.workspace)
        self.assertEqual("manual_browser_interaction", result["evidence_type"])
        self.assertEqual([0.2, 0.8], result["final_times"])
        self.assertEqual([960, 540], result["overlay_dimensions"])

    def test_rejects_tampered_hash_and_unverified_operation(self) -> None:
        from videoactagent.trajectory_acceptance import validate_acceptance_manifest

        tampered = json.loads(json.dumps(self.manifest))
        tampered["overlay"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "overlay.*sha256"):
            validate_acceptance_manifest(tampered, self.workspace)
        tampered = json.loads(json.dumps(self.manifest))
        tampered["operations"][1] = {"action": "drag_point", "original_t": 0.3}
        with self.assertRaisesRegex(ValueError, "operations"):
            validate_acceptance_manifest(tampered, self.workspace)

    def test_rejects_final_times_not_bound_to_trajectory(self) -> None:
        from videoactagent.trajectory_acceptance import validate_acceptance_manifest

        tampered = json.loads(json.dumps(self.manifest))
        tampered["final_times"] = [0.0, 1.0]
        with self.assertRaisesRegex(ValueError, "final_times"):
            validate_acceptance_manifest(tampered, self.workspace)

    def test_cli_validates_but_does_not_generate_manual_evidence(self) -> None:
        from videoactagent.trajectory_acceptance import main

        manifest_path = self.workspace / "acceptance_manifest.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "validate",
                    "--manifest",
                    "acceptance_manifest.json",
                    "--workspace",
                    str(self.workspace),
                ]
            )
        self.assertEqual(0, result)
        self.assertIn("TRAJECTORY_ACCEPTANCE_OK", output.getvalue())
        self.assertEqual(
            ["acceptance_manifest.json"],
            [path.name for path in self.workspace.glob("*manifest*.json")],
            "validator must not generate a second or replacement evidence manifest",
        )


if __name__ == "__main__":
    unittest.main()
