"""Focused tests for the canonical, API-independent trajectory contract.

Developer run:
    python -m unittest tests.test_trajectory -v

Real CLI run:
    python -m videoactagent.trajectory validate --input examples/trajectory_circle_s01.json --output runs/trajectory/s01/trajectory.json
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
from unittest import mock

from videoactagent.module_io import inspect_manifest, sha256_file
from videoactagent.trajectory import (
    TrajectoryInstruction,
    TrajectoryPoint,
    TrajectoryTarget,
    TrajectoryTrack,
    canonical_bytes,
    main,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "trajectory_circle_s01.json"
MANIFEST = ROOT / "examples" / "module_io_manifest.json"


class TrajectorySchemaTests(unittest.TestCase):
    def test_round_trips_camera_circle_and_actor_polyline(self):
        instruction = TrajectoryInstruction.from_path(EXAMPLE)

        self.assertEqual(instruction.scene_id, "station_platform")
        self.assertEqual(instruction.shot_id, "s01")
        self.assertEqual(instruction.coordinate_space, "normalized_0_1_top_left")
        self.assertEqual(instruction.duration_seconds, 5.0)
        self.assertEqual(instruction.sample_count, 121)
        self.assertEqual(
            [track.target_type for track in instruction.tracks],
            ["camera", "actor"],
        )
        self.assertEqual(instruction.tracks[1].target_id, "actor_a")
        self.assertEqual(instruction.tracks[0].primitive, "circle")
        self.assertEqual(instruction.tracks[0].semantic, "orbit_clockwise")
        self.assertTrue(instruction.tracks[0].points[0].visible)
        self.assertEqual(
            instruction.to_dict(),
            TrajectoryInstruction.from_dict(instruction.to_dict()).to_dict(),
        )
        with self.assertRaises(FrozenInstanceError):
            instruction.tracks[0].points[0].x = 0.0  # type: ignore[misc]

    def test_from_json_bytes_is_strict_and_matches_from_path(self):
        payload = EXAMPLE.read_bytes()
        self.assertEqual(
            TrajectoryInstruction.from_json_bytes(payload).to_dict(),
            TrajectoryInstruction.from_path(EXAMPLE).to_dict(),
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            TrajectoryInstruction.from_json_bytes(b'{"schema_version":"0.1","schema_version":"0.1"}')

    def test_rejects_nonfinite_out_of_range_unsorted_or_duplicate_points(self):
        valid = self._minimal_document()
        invalid_points = (
            [{"t": 0.0, "x": math.nan, "y": 0.5}],
            [{"t": 0.0, "x": math.inf, "y": 0.5}],
            [{"t": 0.0, "x": -0.01, "y": 0.5}],
            [{"t": 1.01, "x": 0.5, "y": 0.5}],
            [
                {"t": 0.5, "x": 0.4, "y": 0.5},
                {"t": 0.4, "x": 0.6, "y": 0.5},
            ],
            [
                {"t": 0.5, "x": 0.4, "y": 0.5},
                {"t": 0.5, "x": 0.6, "y": 0.5},
            ],
        )
        for points in invalid_points:
            with self.subTest(points=points):
                document = json.loads(json.dumps(valid))
                document["tracks"][0]["points"] = points
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(document)

    def test_rejects_huge_integer_coordinates_as_value_errors(self):
        huge = 10**10000
        for field in ("t", "x", "y"):
            with self.subTest(field=field):
                document = self._minimal_document()
                document["tracks"][0]["points"][0][field] = huge
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(document)

    def test_negative_zero_is_canonicalized_for_all_point_coordinates(self):
        for field in ("t", "x", "y"):
            with self.subTest(field=field):
                positive = self._minimal_document()
                negative = json.loads(json.dumps(positive))
                positive["tracks"][0]["points"][0][field] = 0.0
                negative["tracks"][0]["points"][0][field] = -0.0

                positive_bytes = canonical_bytes(
                    TrajectoryInstruction.from_dict(positive)
                )
                negative_bytes = canonical_bytes(
                    TrajectoryInstruction.from_dict(negative)
                )

                self.assertEqual(negative_bytes, positive_bytes)
                self.assertEqual(
                    hashlib.sha256(negative_bytes).hexdigest(),
                    hashlib.sha256(positive_bytes).hexdigest(),
                )

    def test_visible_duration_and_sample_count_have_strict_types(self):
        invalid_mutations = (
            ("duration_seconds", 0),
            ("duration_seconds", math.inf),
            ("duration_seconds", True),
            ("sample_count", 0),
            ("sample_count", True),
            ("sample_count", 1.5),
        )
        for field, value in invalid_mutations:
            with self.subTest(field=field, value=value):
                document = self._minimal_document()
                document[field] = value
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(document)

        for visible in (0, 1, "true", None):
            with self.subTest(visible=visible):
                document = self._minimal_document()
                document["tracks"][0]["points"][0]["visible"] = visible
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(document)

    def test_rejects_invalid_ids_duplicate_tracks_and_unknown_fields(self):
        for field, value in (("scene_id", ""), ("scene_id", "../escape"), ("shot_id", 3)):
            with self.subTest(field=field, value=value):
                document = self._minimal_document()
                document[field] = value
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(document)

        duplicate = self._minimal_document()
        duplicate["tracks"].append(json.loads(json.dumps(duplicate["tracks"][0])))
        with self.assertRaisesRegex(ValueError, "unique"):
            TrajectoryInstruction.from_dict(duplicate)

        unknown = self._minimal_document()
        unknown["surprise"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            TrajectoryInstruction.from_dict(unknown)

    def test_target_primitive_and_semantic_compatibility_is_strict(self):
        incompatible = (
            ("actor", "circle", "move"),
            ("anchor", "polyline", "anchor"),
            ("camera", "circle", "truck_right"),
            ("camera", "polyline", "orbit_clockwise"),
            ("actor", "polyline", "orbit_clockwise"),
        )
        for target_type, primitive, semantic in incompatible:
            with self.subTest(
                target_type=target_type, primitive=primitive, semantic=semantic
            ):
                document = self._minimal_document()
                track = document["tracks"][0]
                track["target"]["type"] = target_type
                track["primitive"] = primitive
                track["semantic"] = semantic
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(document)

    def test_static_anchor_is_valid_and_requires_one_point(self):
        anchor = TrajectoryInstruction.from_dict(
            {
                "schema_version": "0.1",
                "scene_id": "station_platform",
                "shot_id": "s01",
                "coordinate_space": "normalized_0_1_top_left",
                "tracks": [
                    {
                        "track_id": "meeting_anchor",
                        "target": {"type": "anchor", "id": "meeting"},
                        "primitive": "static",
                        "semantic": "anchor",
                        "points": [
                            {"t": 0.0, "x": 0.5, "y": 0.5, "visible": True}
                        ],
                    }
                ],
                "duration_seconds": 5.0,
                "sample_count": 121,
            }
        )
        self.assertEqual(anchor.tracks[0].target.target_type, "anchor")

        document = anchor.to_dict()
        document["tracks"][0]["points"].append(
            {"t": 1.0, "x": 0.5, "y": 0.5, "visible": True}
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            TrajectoryInstruction.from_dict(document)

    def test_local_deformation_is_canonical_but_only_as_move_polyline(self):
        document = self._minimal_document()
        track = document["tracks"][0]
        track["track_id"] = "actor_a_hand"
        track["target"] = {"type": "local_deformation", "id": "actor_a.hand"}
        track["primitive"] = "polyline"
        track["semantic"] = "move"

        instruction = TrajectoryInstruction.from_dict(document)
        self.assertEqual(instruction.tracks[0].target_type, "local_deformation")
        self.assertEqual(instruction.tracks[0].target_id, "actor_a.hand")
        self.assertEqual(
            TrajectoryInstruction.from_dict(instruction.to_dict()).to_dict(),
            instruction.to_dict(),
        )

        for primitive, semantic in (("circle", "move"), ("polyline", "anchor")):
            invalid = json.loads(json.dumps(document))
            invalid["tracks"][0]["primitive"] = primitive
            invalid["tracks"][0]["semantic"] = semantic
            with self.subTest(primitive=primitive, semantic=semantic):
                with self.assertRaises(ValueError):
                    TrajectoryInstruction.from_dict(invalid)

    def test_direct_dataclass_construction_runs_the_same_validation(self):
        with self.assertRaises(ValueError):
            TrajectoryPoint(t=0.0, x=2.0, y=0.0, visible=True)
        with self.assertRaises(ValueError):
            TrajectoryTarget(target_type="actor", target_id="")
        with self.assertRaises(ValueError):
            TrajectoryTrack(
                track_id="bad",
                target=TrajectoryTarget("camera", "main_camera"),
                primitive="circle",
                semantic="truck_right",
                points=(
                    TrajectoryPoint(0.0, 0.2, 0.5, True),
                    TrajectoryPoint(1.0, 0.8, 0.5, True),
                ),
            )

    def test_scene_and_shot_identity_must_match_explicit_expectations(self):
        instruction = TrajectoryInstruction.from_dict(self._minimal_document())
        instruction.validate_identity("station_platform", "s01")
        with self.assertRaisesRegex(ValueError, "scene"):
            instruction.validate_identity("different_scene", "s01")
        with self.assertRaisesRegex(ValueError, "shot"):
            instruction.validate_identity("station_platform", "s02")

    @staticmethod
    def _minimal_document() -> dict[str, object]:
        return {
            "schema_version": "0.1",
            "scene_id": "station_platform",
            "shot_id": "s01",
            "coordinate_space": "normalized_0_1_top_left",
            "duration_seconds": 5.0,
            "sample_count": 121,
            "tracks": [
                {
                    "track_id": "camera_path",
                    "target": {"type": "camera", "id": "main_camera"},
                    "primitive": "polyline",
                    "semantic": "truck_right",
                    "points": [
                        {"t": 0.0, "x": 0.2, "y": 0.5, "visible": True},
                        {"t": 1.0, "x": 0.8, "y": 0.5, "visible": True},
                    ],
                }
            ],
        }


class TrajectoryCLITests(unittest.TestCase):
    def test_from_path_and_cli_reject_recursive_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "runs") as directory:
            run_dir = Path(directory)
            output = run_dir / "trajectory.json"
            duplicates = {
                "top_level": (
                    '{"schema_version":"0.1","schema_version":"0.1"}'
                ),
                "nested_target": (
                    '{"schema_version":"0.1","scene_id":"station_platform",'
                    '"shot_id":"s01","coordinate_space":"normalized_0_1_top_left",'
                    '"duration_seconds":5.0,"sample_count":121,"tracks":[{'
                    '"track_id":"camera_path","target":{"type":"camera",'
                    '"type":"actor","id":"main_camera"},"primitive":"polyline",'
                    '"semantic":"truck_right","points":[{"t":0.0,"x":0.2,'
                    '"y":0.5,"visible":true},{"t":1.0,"x":0.8,"y":0.5,'
                    '"visible":true}]}]}'
                ),
            }
            for name, payload in duplicates.items():
                with self.subTest(name=name):
                    source = run_dir / f"{name}.json"
                    source.write_text(payload, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                        TrajectoryInstruction.from_path(source)

                    output.unlink(missing_ok=True)
                    stderr = StringIO()
                    with redirect_stderr(stderr):
                        code = main(
                            [
                                "validate",
                                "--input",
                                str(source),
                                "--output",
                                str(output),
                                "--expected-scene",
                                "station_platform",
                                "--expected-shot",
                                "s01",
                            ]
                        )
                    self.assertEqual(code, 2)
                    self.assertFalse(output.exists())
                    self.assertIn("duplicate JSON key", stderr.getvalue())

    def test_cli_writes_canonical_json_and_stable_hash(self):
        output = ROOT / "runs" / "trajectory" / "test" / "trajectory.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stdout = StringIO()

        with redirect_stdout(stdout):
            code = main(
                [
                    "validate",
                    "--input",
                    str(EXAMPLE),
                    "--output",
                    str(output),
                    "--expected-scene",
                    "station_platform",
                    "--expected-shot",
                    "s01",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(output.read_bytes(), canonical_bytes(TrajectoryInstruction.from_path(EXAMPLE)))
        self.assertIn("TRAJECTORY_OK", stdout.getvalue())
        self.assertIn(sha256_file(output), stdout.getvalue())
        self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_cli_identity_mismatch_returns_nonzero_without_writing(self):
        output = ROOT / "runs" / "trajectory" / "test" / "identity.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "validate",
                    "--input",
                    str(EXAMPLE),
                    "--output",
                    str(output),
                    "--expected-scene",
                    "station_platform",
                    "--expected-shot",
                    "s02",
                ]
            )
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertIn("shot", stderr.getvalue())

    def test_cli_rejects_input_collision_and_workspace_escape(self):
        stderr = StringIO()
        before = EXAMPLE.read_bytes()
        with redirect_stderr(stderr):
            code = main(["validate", "--input", str(EXAMPLE), "--output", str(EXAMPLE)])
        self.assertEqual(code, 2)
        self.assertEqual(EXAMPLE.read_bytes(), before)
        self.assertIn("collision", stderr.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "trajectory.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(["validate", "--input", str(EXAMPLE), "--output", str(outside)])
            self.assertEqual(code, 2)
            self.assertFalse(outside.exists())
            self.assertIn("workspace", stderr.getvalue())

    def test_cli_replace_failure_cleans_temporary_file(self):
        output = ROOT / "runs" / "trajectory" / "test" / "replace.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stderr = StringIO()
        with mock.patch("videoactagent.trajectory.os.replace", side_effect=OSError("replace failed")):
            with redirect_stderr(stderr):
                code = main(["validate", "--input", str(EXAMPLE), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])
        self.assertIn("replace failed", stderr.getvalue())

    def test_real_manifest_entry_is_hash_verified(self):
        output = ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"
        if not output.is_file():
            self.skipTest("run canonical CLI before manifest verification")
        report = inspect_manifest(MANIFEST, ROOT, ["trajectory_instruction_s01"])
        self.assertTrue(report["ok"])
        record = report["modules"][0]["outputs"][0]
        self.assertEqual(record["sha256"], sha256_file(output))


if __name__ == "__main__":
    unittest.main()
