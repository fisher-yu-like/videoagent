"""Trajectory compiler tests.

Run only this module with::

    python -m unittest tests.test_trajectory_compile -v

These tests exercise deterministic local compilation.  They do not submit a
video API request and therefore are not generation-quality evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from videoactagent.shotscript import ShotScript
from videoactagent.trajectory import TrajectoryInstruction, canonical_bytes
from videoactagent.trajectory_compile import compile_trajectory, main
import videoactagent.trajectory_compile as compile_module


ROOT = Path(__file__).resolve().parents[1]


class TrajectoryCompileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instruction = TrajectoryInstruction.from_path(
            ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"
        )
        self.shotscript = ShotScript.from_path(
            ROOT / "examples" / "station_shotscript.json"
        )

    def test_compiles_real_circle_and_actor_curve_deterministically(self) -> None:
        first = compile_trajectory(self.instruction, self.shotscript, "s01")
        second = compile_trajectory(self.instruction, self.shotscript, "s01")

        self.assertEqual(first, second)
        self.assertEqual(
            first["trajectory_sha256"],
            hashlib.sha256(canonical_bytes(self.instruction)).hexdigest(),
        )
        self.assertIn("canonical_shotscript_sha256", first)
        self.assertNotIn("shotscript_sha256", first)
        self.assertEqual(first["backend_capability"], {"t2v": "prompt_approximation"})
        self.assertTrue(first["submission_ready"])
        self.assertEqual(first["unsupported"], [])
        self.assertEqual(first["patched_camera"]["motion"], "arc_clockwise")
        self.assertIn("orbit clockwise", first["prompt"]["cinematic"].lower())
        self.assertNotIn("truck right", first["prompt"]["cinematic"].lower())

        actor = first["patched_actors"][0]
        self.assertEqual(actor["actor_id"], "actor_a")
        self.assertEqual(actor["start_region"], "left-bottom")
        self.assertEqual(actor["end_region"], "centre-middle")
        self.assertEqual(actor["arrival_interval_seconds"], [2.5, 5.0])
        self.assertEqual(actor["facing"], "actor_b")
        self.assertEqual(actor["curve_direction"], "clockwise_curve")
        self.assertEqual(
            first["prompt"]["timed"],
            ["0.0-5.0s: actor A moves from the left-bottom region to the centre-middle region along a clockwise curve while facing actor B, arriving during 2.5-5.0s."],
        )

    def test_sorts_real_validated_tracks_by_id_for_output(self) -> None:
        document = self.instruction.to_dict()
        document["tracks"] = list(reversed(document["tracks"]))
        instruction = TrajectoryInstruction.from_dict(document)
        result = compile_trajectory(instruction, self.shotscript, "s01")
        self.assertEqual(result["track_order"], ["actor_path_01", "camera_circle_01"])
        camera_track = next(track for track in instruction.tracks if track.target_type == "camera")
        self.assertEqual(
            [item["t"] for item in result["patched_camera"]["keyframes"]],
            sorted(point.t for point in camera_track.points),
        )

    def test_rejects_identity_duration_sample_count_and_unknown_actor(self) -> None:
        base = self.instruction.to_dict()
        invalid_documents = []
        for key, value in (
            ("scene_id", "another_scene"),
            ("shot_id", "s02"),
            ("duration_seconds", 4.0),
            ("sample_count", 120),
        ):
            document = json.loads(json.dumps(base))
            document[key] = value
            invalid_documents.append(document)

        for document in invalid_documents:
            with self.subTest(document=document):
                instruction = TrajectoryInstruction.from_dict(document)
                with self.assertRaises(ValueError):
                    compile_trajectory(instruction, self.shotscript, "s01")

        unknown_actor = json.loads(json.dumps(base))
        unknown_actor["tracks"][1]["target"]["id"] = "actor_missing"
        with self.assertRaises(ValueError):
            compile_trajectory(
                TrajectoryInstruction.from_dict(unknown_actor), self.shotscript, "s01"
            )

    def test_local_deformation_is_explicitly_unsupported_and_blocks_submission(self) -> None:
        document = self.instruction.to_dict()
        local_track = document["tracks"][1]
        local_track["track_id"] = "local_hand_01"
        local_track["target"] = {"type": "local_deformation", "id": "actor_a.hand"}
        document["tracks"] = [local_track]
        instruction = TrajectoryInstruction.from_dict(document)

        result = compile_trajectory(instruction, self.shotscript, "s01")
        self.assertFalse(result["submission_ready"])
        self.assertEqual(
            result["unsupported"],
            [
                {
                    "track_id": "local_hand_01",
                    "target_type": "local_deformation",
                    "reason": "unsupported_by_t2v_backend",
                }
            ],
        )
        self.assertEqual(
            result["trajectory_sha256"],
            hashlib.sha256(canonical_bytes(instruction)).hexdigest(),
        )

        with TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source = root / "local_deformation.json"
            source.write_bytes(canonical_bytes(instruction))
            output = root / "compiled"
            self.assertEqual(
                main(
                    [
                        "--trajectory",
                        str(source),
                        "--shotscript",
                        str(ROOT / "examples" / "station_shotscript.json"),
                        "--shot",
                        "s01",
                        "--output-dir",
                        str(output),
                    ]
                ),
                0,
            )
            control = json.loads((output / "compiled_control.json").read_text("utf-8"))
            self.assertFalse(control["submission_ready"])
            self.assertEqual(control["trajectory_sha256"], result["trajectory_sha256"])

    def _camera_instruction(
        self, points: list[tuple[float, float]], *, semantic: str = "orbit_clockwise"
    ) -> TrajectoryInstruction:
        document = self.instruction.to_dict()
        camera = document["tracks"][0]
        camera["semantic"] = semantic
        camera["primitive"] = "circle" if semantic.startswith("orbit_") else "polyline"
        camera["points"] = [
            {
                "t": index / (len(points) - 1),
                "x": x,
                "y": y,
                "visible": True,
            }
            for index, (x, y) in enumerate(points)
        ]
        document["tracks"] = [camera]
        return TrajectoryInstruction.from_dict(document)

    def test_circle_geometry_separates_static_translation_and_radius_change(self) -> None:
        static = compile_trajectory(self.instruction, self.shotscript, "s01")
        self.assertEqual(
            [item["semantic"] for item in static["patched_camera"]["components"]],
            ["orbit_clockwise"],
        )

        translated = self._camera_instruction(
            [
                (0.40, 0.30), (0.60, 0.50), (0.40, 0.70),
                (0.55, 0.70), (0.35, 0.50), (0.55, 0.30),
            ]
        )
        translated_result = compile_trajectory(translated, self.shotscript, "s01")
        self.assertIn(
            "truck_right",
            [item["semantic"] for item in translated_result["patched_camera"]["components"]],
        )

        increasing = self._camera_instruction(
            [
                (0.50, 0.40), (0.60, 0.50), (0.50, 0.60),
                (0.50, 0.75), (0.25, 0.50), (0.50, 0.25),
            ]
        )
        increasing_result = compile_trajectory(increasing, self.shotscript, "s01")
        self.assertIn(
            "dolly_out",
            [item["semantic"] for item in increasing_result["patched_camera"]["components"]],
        )

        decreasing = self._camera_instruction(
            [
                (0.50, 0.25), (0.75, 0.50), (0.50, 0.75),
                (0.50, 0.60), (0.40, 0.50), (0.50, 0.40),
            ]
        )
        decreasing_result = compile_trajectory(decreasing, self.shotscript, "s01")
        self.assertIn(
            "dolly_in",
            [item["semantic"] for item in decreasing_result["patched_camera"]["components"]],
        )

        features = increasing_result["patched_camera"]["geometry_features"]
        self.assertEqual(features["window_points"], 3)
        self.assertGreater(features["end_radius"], features["start_radius"])

    def _two_window_circle(
        self, center_shift: float, radius_delta: float
    ) -> TrajectoryInstruction:
        start_center = (0.4, 0.5)
        end_center = (0.4 + center_shift, 0.5)
        start_radius = 0.15
        end_radius = start_radius + radius_delta
        return self._camera_instruction(
            [
                (start_center[0], start_center[1] - start_radius),
                (start_center[0] + start_radius, start_center[1]),
                (start_center[0], start_center[1] + start_radius),
                (end_center[0], end_center[1] + end_radius),
                (end_center[0] - end_radius, end_center[1]),
                (end_center[0], end_center[1] - end_radius),
            ]
        )

    def test_geometry_thresholds_use_raw_values_below_equal_and_above(self) -> None:
        for value, expected in ((0.049, False), (0.05, False), (0.0500004, True)):
            with self.subTest(center_shift=value):
                result = compile_trajectory(
                    self._two_window_circle(value, 0.0), self.shotscript, "s01"
                )
                semantics = {
                    item["semantic"] for item in result["patched_camera"]["components"]
                }
                self.assertEqual("truck_right" in semantics, expected)

        for value, expected in ((0.039, False), (0.04, False), (0.0400004, True)):
            with self.subTest(radius_delta=value):
                result = compile_trajectory(
                    self._two_window_circle(0.0, value), self.shotscript, "s01"
                )
                semantics = {
                    item["semantic"] for item in result["patched_camera"]["components"]
                }
                self.assertEqual("dolly_out" in semantics, expected)

    def test_near_collinear_circle_fails_closed_without_secondary_inference(self) -> None:
        instruction = self._camera_instruction(
            [
                (0.10, 0.10), (0.20, 0.200000000001), (0.30, 0.30),
                (0.40, 0.40), (0.50, 0.500000000001), (0.60, 0.60),
            ]
        )
        with self.assertRaisesRegex(ValueError, "degenerate"):
            compile_trajectory(instruction, self.shotscript, "s01")

    def test_review_counterexample_and_excessive_radius_fail_without_artifacts(self) -> None:
        # These two windows have normalized determinants of approximately
        # 9.5e-6 and 1.05e-5.  A permissive circumcircle fit yields enormous
        # centres/radii instead of admitting that the points are near-collinear.
        counterexample = self._camera_instruction(
            [
                (0.10, 0.10), (0.10095, 0.1009519), (0.30, 0.30),
                (0.40, 0.40), (0.40105, 0.4010521), (0.60, 0.60),
            ]
        )
        def conditioning(points: tuple[object, ...]) -> tuple[float, float]:
            coordinates = [(point.x, point.y) for point in points]
            (ax, ay), (bx, by), (cx, cy) = coordinates
            denominator = abs(
                2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
            )
            chords = (
                ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5,
                ((bx - cx) ** 2 + (by - cy) ** 2) ** 0.5,
                ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5,
            )
            maximum = max(chords)
            return denominator / (maximum * maximum), (
                chords[0] * chords[1] * chords[2] / denominator / maximum
            )

        start_metrics = conditioning(counterexample.tracks[0].points[:3])
        end_metrics = conditioning(counterexample.tracks[0].points[-3:])
        self.assertTrue(9.0e-6 < start_metrics[0] < 1.1e-5)
        self.assertTrue(9.0e-6 < end_metrics[0] < 1.1e-5)
        self.assertTrue(450.0 < start_metrics[1] < 550.0)
        self.assertTrue(450.0 < end_metrics[1] < 550.0)
        with self.assertRaisesRegex(ValueError, "degenerate"):
            compile_trajectory(counterexample, self.shotscript, "s01")

        # This window passes the determinant guard but its fitted circle is
        # still implausibly large relative to the observed chord.
        excessive_radius = self._camera_instruction(
            [
                (0.10, 0.10), (0.20, 0.2001), (0.30, 0.30),
                (0.40, 0.40), (0.50, 0.5001), (0.60, 0.60),
            ]
        )
        with self.assertRaisesRegex(ValueError, "radius/chord"):
            compile_trajectory(excessive_radius, self.shotscript, "s01")

        with TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            source = root / "counterexample.json"
            source.write_bytes(canonical_bytes(counterexample))
            output = root / "compiled"
            code = main(
                [
                    "--trajectory", str(source),
                    "--shotscript", str(ROOT / "examples" / "station_shotscript.json"),
                    "--shot", "s01",
                    "--output-dir", str(output),
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse(output.exists())

    def test_minimal_three_point_circle_remains_compilable(self) -> None:
        instruction = self._camera_instruction(
            [(0.5, 0.2), (0.8, 0.5), (0.5, 0.8)]
        )
        result = compile_trajectory(instruction, self.shotscript, "s01")
        self.assertEqual(
            [item["semantic"] for item in result["patched_camera"]["components"]],
            ["orbit_clockwise"],
        )
        self.assertEqual(
            result["patched_camera"]["geometry_features"]["window_points"], 3
        )

    def test_linear_pan_and_zoom_prompts_do_not_claim_motion_around_subject(self) -> None:
        for semantic in ("pan_left", "zoom_in"):
            instruction = self._camera_instruction(
                [(0.2, 0.5), (0.8, 0.5)], semantic=semantic
            )
            result = compile_trajectory(instruction, self.shotscript, "s01")
            prompt = result["prompt"]["cinematic"].lower()
            self.assertIn(semantic.replace("_", " "), prompt)
            self.assertNotIn("around", prompt)

    def test_version_0_1_rejects_more_than_one_actor_trajectory(self) -> None:
        document = self.instruction.to_dict()
        second_actor = json.loads(json.dumps(document["tracks"][1]))
        second_actor["track_id"] = "actor_path_02"
        second_actor["target"]["id"] = "actor_b"
        document["tracks"].append(second_actor)
        instruction = TrajectoryInstruction.from_dict(document)

        with self.assertRaisesRegex(ValueError, "only one actor trajectory"):
            compile_trajectory(instruction, self.shotscript, "s01")

    def test_cli_writes_three_hash_bound_outputs_or_none(self) -> None:
        with TemporaryDirectory(dir=ROOT) as temporary:
            output_dir = Path(temporary) / "compiled"
            code = main(
                [
                    "--trajectory",
                    str(ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"),
                    "--shotscript",
                    str(ROOT / "examples" / "station_shotscript.json"),
                    "--shot",
                    "s01",
                    "--output-dir",
                    str(output_dir),
                ]
            )
            self.assertEqual(code, 0)
            expected = {
                "compiled_control.json",
                "trajectory_prompt.txt",
                "patched_shotscript.json",
            }
            self.assertEqual({path.name for path in output_dir.iterdir()}, expected)

            control = json.loads((output_dir / "compiled_control.json").read_text("utf-8"))
            patched = json.loads((output_dir / "patched_shotscript.json").read_text("utf-8"))
            prompt_bytes = (output_dir / "trajectory_prompt.txt").read_bytes()
            patched_bytes = (output_dir / "patched_shotscript.json").read_bytes()
            self.assertEqual(
                control["artifact_sha256"]["trajectory_prompt.txt"],
                hashlib.sha256(prompt_bytes).hexdigest(),
            )
            self.assertEqual(
                control["artifact_sha256"]["patched_shotscript.json"],
                hashlib.sha256(patched_bytes).hexdigest(),
            )
            self.assertEqual(
                patched["trajectory_control"]["trajectory_sha256"],
                control["trajectory_sha256"],
            )
            shotscript_bytes = (ROOT / "examples" / "station_shotscript.json").read_bytes()
            trajectory_bytes = (
                ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"
            ).read_bytes()
            self.assertEqual(
                control["source_artifact_sha256"],
                {
                    "shotscript": "149c5ae13bbcb6e98b8774808956f8062d82903c504f8c3279c9ea7019a1dc28",
                    "trajectory": hashlib.sha256(trajectory_bytes).hexdigest(),
                },
            )
            self.assertEqual(
                control["source_artifact_bytes"],
                {"shotscript": len(shotscript_bytes), "trajectory": len(trajectory_bytes)},
            )

            bad = Path(temporary) / "bad"
            code = main(
                [
                    "--trajectory",
                    str(ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"),
                    "--shotscript",
                    str(ROOT / "examples" / "station_shotscript.json"),
                    "--shot",
                    "missing",
                    "--output-dir",
                    str(bad),
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse(bad.exists())

    def test_cli_parses_private_snapshots_when_sources_change_after_copy(self) -> None:
        with TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            trajectory = root / "trajectory.json"
            shotscript = root / "shotscript.json"
            original_trajectory = (
                ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"
            ).read_bytes()
            original_shotscript = (
                ROOT / "examples" / "station_shotscript.json"
            ).read_bytes()
            trajectory.write_bytes(original_trajectory)
            shotscript.write_bytes(original_shotscript)
            original_copyfile = shutil.copyfile
            copied = 0

            def copy_then_mutate(source: object, target: object, *args: object, **kwargs: object):
                nonlocal copied
                result = original_copyfile(source, target, *args, **kwargs)
                Path(source).write_bytes(b"{}")
                copied += 1
                return result

            output = root / "compiled"
            with patch(
                "videoactagent.trajectory_compile.shutil.copyfile",
                side_effect=copy_then_mutate,
            ):
                code = main(
                    [
                        "--trajectory", str(trajectory),
                        "--shotscript", str(shotscript),
                        "--shot", "s01",
                        "--output-dir", str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(copied, 2)
            control = json.loads((output / "compiled_control.json").read_text("utf-8"))
            self.assertEqual(
                control["source_artifact_sha256"]["trajectory"],
                hashlib.sha256(original_trajectory).hexdigest(),
            )
            self.assertEqual(
                control["source_artifact_sha256"]["shotscript"],
                hashlib.sha256(original_shotscript).hexdigest(),
            )

    def test_cli_rejects_output_outside_workspace(self) -> None:
        with TemporaryDirectory() as outside:
            code = main(
                [
                    "--trajectory",
                    str(ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"),
                    "--shotscript",
                    str(ROOT / "examples" / "station_shotscript.json"),
                    "--shot",
                    "s01",
                    "--output-dir",
                    outside,
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_cli_replace_failure_leaves_no_partial_output_directory(self) -> None:
        with TemporaryDirectory(dir=ROOT) as temporary:
            output_dir = Path(temporary) / "compiled"
            with patch(
                "videoactagent.trajectory_compile.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                code = main(
                    [
                        "--trajectory",
                        str(ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"),
                        "--shotscript",
                        str(ROOT / "examples" / "station_shotscript.json"),
                        "--shot",
                        "s01",
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertFalse(output_dir.exists())

    def test_cli_retries_one_transient_windows_publish_failure(self) -> None:
        with TemporaryDirectory(dir=ROOT) as temporary:
            output_dir = Path(temporary) / "compiled"
            import videoactagent.trajectory_compile as module

            real_replace = module.os.replace
            calls = 0

            def fail_once(source: object, target: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("injected transient scanner lock")
                real_replace(source, target)

            with patch("videoactagent.trajectory_compile.os.replace", side_effect=fail_once):
                code = main(
                    [
                        "--trajectory",
                        str(ROOT / "runs" / "trajectory" / "s01" / "trajectory.json"),
                        "--shotscript",
                        str(ROOT / "examples" / "station_shotscript.json"),
                        "--shot",
                        "s01",
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(calls, 2)
            self.assertTrue((output_dir / "compiled_control.json").is_file())

    def test_output_parent_identity_detects_directory_replacement(self) -> None:
        with TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            parent = root / "publish_parent"
            parent.mkdir()
            identity = compile_module._capture_output_parent(parent)
            moved = root / "moved_parent"
            os.replace(parent, moved)
            parent.mkdir()
            with self.assertRaisesRegex(ValueError, "identity changed"):
                compile_module._verify_output_parent(parent, identity)


if __name__ == "__main__":
    unittest.main()
