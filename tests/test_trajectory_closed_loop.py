"""Local-only tests for bounded trajectory revision and experiment planning.

No test in this module imports a transport or calls an API/server. Run with:

    python -m unittest tests.test_trajectory_closed_loop -v
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REAL_PROMPT = ROOT / "runs/trajectory/s01/compiled/trajectory_prompt.txt"
REAL_TRAJECTORY = ROOT / "runs/trajectory/s01/trajectory.json"
REAL_CASES = {
    "kling": {
        "evaluation": ROOT / "runs/trajectory_observation/kling_s01/evaluation.json",
        "video": ROOT / "runs/trajectory_api_pilot/real/20260729T171212Z_kling_1bd8ea1f/result.mp4",
        "manifest": ROOT / "runs/trajectory_observation/kling_s01/session_manifest.json",
        "annotation": ROOT / "runs/trajectory_observation/kling_s01/manual_annotation.json",
    },
    "seedance": {
        "evaluation": ROOT / "runs/trajectory_observation/seedance_s01/evaluation.json",
        "video": ROOT / "runs/trajectory_api_pilot/real/20260729T171238Z_seedance_cad24559/result.mp4",
        "manifest": ROOT / "runs/trajectory_observation/seedance_s01/session_manifest.json",
        "annotation": ROOT / "runs/trajectory_observation/seedance_s01/manual_annotation.json",
    },
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


class RevisionPolicyTests(unittest.TestCase):
    def test_allow_list_is_exact_and_every_verdict_has_one_stable_operation(self):
        from videoactagent.trajectory_closed_loop import (
            ALLOWED_OPERATIONS,
            operations_for_control_verdicts,
        )

        self.assertEqual(
            ALLOWED_OPERATIONS,
            (
                "strengthen_direction",
                "split_time_segments",
                "reduce_amplitude",
                "strengthen_screen_direction",
                "simplify_orbit_to_truck",
                "preserve_matched_control",
            ),
        )
        controls = [
            {"control_id": "z", "verdict": "matched"},
            {"control_id": "a", "verdict": "orbit_mismatch"},
            {"control_id": "b", "verdict": "screen_direction_mismatch"},
            {"control_id": "c", "verdict": "amplitude_excess"},
            {"control_id": "d", "verdict": "timing_mismatch"},
            {"control_id": "e", "verdict": "direction_mismatch"},
            {"control_id": "duplicate", "verdict": "direction_mismatch"},
        ]
        result = operations_for_control_verdicts(reversed(controls))
        self.assertEqual(tuple(result), ALLOWED_OPERATIONS)
        matched = next(item for item in result.decisions if item["control_id"] == "z")
        self.assertEqual(matched["operations"], ["preserve_matched_control"])

    def test_unknown_or_conflicting_control_verdicts_fail_closed(self):
        from videoactagent.trajectory_closed_loop import operations_for_control_verdicts

        with self.assertRaisesRegex(ValueError, "unknown verdict"):
            operations_for_control_verdicts(
                [{"control_id": "direction", "verdict": "looks_good"}]
            )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            operations_for_control_verdicts(
                [
                    {"control_id": "direction", "verdict": "matched"},
                    {"control_id": "direction", "verdict": "direction_mismatch"},
                ]
            )


class ClosedLoopPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = REAL_CASES["kling"]
        self.video = self.root / "pilot.mp4"
        self.trajectory = self.root / "trajectory.json"
        self.observer = self.root / "observer"
        self.manifest = self.observer / "session_manifest.json"
        self.annotation = self.observer / "manual_annotation.json"
        self.prompt = self.root / "prompt.txt"
        shutil.copy2(source["video"], self.video)
        shutil.copy2(REAL_TRAJECTORY, self.trajectory)
        shutil.copy2(REAL_PROMPT, self.prompt)
        shutil.copytree(source["manifest"].parent, self.observer)
        self.report = self.observer / "evaluation.json"
        self.base_report = json.loads(self.report.read_text(encoding="utf-8"))
        session = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.frame_a = self.observer / session["frames"][0]["path"]
        self.frame_b = self.observer / session["frames"][-1]["path"]

    def tearDown(self):
        self.temporary.cleanup()

    def _report(self, **aggregate_changes: object) -> dict:
        report = copy.deepcopy(self.base_report)
        report["metrics"]["aggregate"].update(aggregate_changes)
        return report

    def test_latest_endpoint_contract_is_strict_and_old_contract_is_rejected(self):
        from videoactagent.trajectory_closed_loop import _validate_report

        current = self._report()
        _validate_report(current)
        old = self._report()
        for field in ("endpoint_t", "endpoint_status", "endpoint_reason"):
            del old["metrics"]["aggregate"][field]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            _validate_report(old)

    def test_endpoint_status_reason_error_and_arrival_semantics_are_consistent(self):
        from videoactagent.trajectory_closed_loop import _validate_report

        cases = []
        bad_reason = self._report(endpoint_status="compared", endpoint_reason="observed_endpoint_occluded")
        cases.append((bad_reason, "endpoint_reason"))
        bad_error = self._report(endpoint_status="occluded", endpoint_reason="observed_endpoint_occluded")
        cases.append((bad_error, "endpoint_error"))
        bad_arrival = self._report(observed_arrival_t=None, arrival_error=0.2)
        cases.append((bad_arrival, "arrival"))
        bad_difference = self._report(
            requested_arrival_t=0.9, observed_arrival_t=0.6, arrival_error=0.1
        )
        cases.append((bad_difference, "arrival"))
        for report, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _validate_report(report)

    def _prepare(self, generation: int = 0):
        from videoactagent.trajectory_closed_loop import prepare_revision

        return prepare_revision(
            evaluation_path=self.report,
            prompt_path=self.prompt,
            video_path=self.video,
            trajectory_path=self.trajectory,
            session_manifest_path=self.manifest,
            annotation_path=self.annotation,
            revision_generation=generation,
            workspace=self.root,
        )

    def test_real_source_bytes_are_rehashed_and_revision_is_bounded_offline(self):
        revision = self._prepare()
        self.assertEqual(revision["revision_generation"], 1)
        self.assertEqual(revision["max_revision_generation"], 1)
        self.assertFalse(revision["submission_allowed"])
        self.assertFalse(revision["network_called"])
        self.assertEqual(
            revision["source_sha256"]["evaluation_report"], _sha(self.report)
        )
        self.assertEqual(revision["source_sha256"]["video"], _sha(self.video))
        self.assertEqual(revision["original_prompt_sha256"], _sha(self.prompt))
        self.assertEqual(len(revision["operations"]), len(set(revision["operations"])))
        self.assertIn("split_time_segments", revision["operations"])
        self.assertIn("preserve_matched_control", revision["operations"])

    def test_matched_metrics_only_preserve_controls_and_prompt(self):
        from videoactagent.trajectory_closed_loop import operations_for_control_verdicts

        selected = operations_for_control_verdicts(
            [{"control_id": "direction", "verdict": "matched"}]
        )
        self.assertEqual(list(selected), ["preserve_matched_control"])
        self.assertEqual(
            selected.decisions[0]["operations"], ["preserve_matched_control"]
        )

    def test_second_revision_generation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "maximum revision generation"):
            self._prepare(generation=1)
        for invalid in (True, -1, 0.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self._prepare(generation=invalid)  # type: ignore[arg-type]

    def test_unknown_schema_metric_verdict_nonfinite_bool_and_huge_number_fail(self):
        cases = []
        unknown_schema = self._report()
        unknown_schema["schema_version"] = "9.9"
        cases.append((unknown_schema, "schema"))
        unknown_metric = self._report()
        unknown_metric["metrics"]["aggregate"]["made_up_metric"] = 1
        cases.append((unknown_metric, "unknown"))
        bad_bool = self._report(direction_cosine=True)
        cases.append((bad_bool, "direction_cosine"))
        nonfinite = self._report(endpoint_error=float("nan"))
        cases.append((nonfinite, "finite"))
        huge = self._report(endpoint_error=10**400)
        cases.append((huge, "finite"))
        unknown_verdict = self._report(direction_match="matched-ish")
        cases.append((unknown_verdict, "direction_match"))
        for document, message in cases:
            with self.subTest(message=message):
                self.report.write_text(
                    json.dumps(document, allow_nan=True), encoding="utf-8"
                )
                with self.assertRaisesRegex((ValueError, OverflowError), message):
                    self._prepare()

    def test_cross_metric_contradictions_fail_closed(self):
        cases = []
        time_base = self._report()
        time_base["metrics"]["time_base"]["sample_count"] = 120
        cases.append((time_base, "time_base"))
        dtw = self._report()
        dtw["metrics"]["dtw"]["normalized_cost"] = 0.01
        cases.append((dtw, "DTW"))
        direction = self._report(direction_cosine=0.5, direction_match=False)
        cases.append((direction, "direction"))
        for report, message in cases:
            with self.subTest(message=message):
                _write_json(self.report, report)
                with self.assertRaisesRegex(ValueError, message):
                    self._prepare()

    def test_duplicate_json_keys_and_tampered_sources_fail_closed(self):
        self.report.write_text(
            '{"schema_version":"0.1","schema_version":"0.1"}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self._prepare()
        _write_json(self.report, self._report())
        self.video.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "video.*hash mismatch"):
            self._prepare()

    def test_rehashed_or_missing_session_frame_fails_closed(self):
        self.frame_a.write_bytes(b"tampered-frame")
        with self.assertRaisesRegex(ValueError, "frame.*hash mismatch"):
            self._prepare()

    def test_source_change_during_fresh_recomputation_is_detected(self):
        import videoactagent.trajectory_closed_loop as trajectory_closed_loop

        actual_evaluate = trajectory_closed_loop.evaluate_files

        def evaluate_then_mutate(*args, **kwargs):
            result = actual_evaluate(*args, **kwargs)
            self.prompt.write_text("changed during evaluation", encoding="utf-8")
            return result

        with patch.object(
            trajectory_closed_loop,
            "evaluate_files",
            side_effect=evaluate_then_mutate,
        ):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self._prepare()

    def test_only_real_circle_orbit_can_simplify_orbit_to_truck(self):
        from videoactagent.trajectory import TrajectoryTrack
        from videoactagent.trajectory_closed_loop import (
            _metric_verdicts,
            operations_for_control_verdicts,
        )

        aggregate = copy.deepcopy(self.base_report["metrics"]["aggregate"])
        aggregate["dtw_normalized_cost"] = 0.9
        point_a = {"t": 0.0, "x": 0.2, "y": 0.5, "visible": True}
        point_b = {"t": 1.0, "x": 0.8, "y": 0.5, "visible": True}
        linear = TrajectoryTrack.from_dict(
            {
                "track_id": "camera_linear",
                "target": {"type": "camera", "id": "camera"},
                "primitive": "polyline",
                "semantic": "truck_right",
                "points": [point_a, point_b],
            }
        )
        orbit = TrajectoryTrack.from_dict(
            {
                "track_id": "camera_orbit",
                "target": {"type": "camera", "id": "camera"},
                "primitive": "circle",
                "semantic": "orbit_clockwise",
                "points": [
                    point_a,
                    {"t": 0.5, "x": 0.5, "y": 0.2, "visible": True},
                    point_b,
                ],
            }
        )
        linear_ops = operations_for_control_verdicts(
            _metric_verdicts(self.base_report, aggregate, linear)
        )
        orbit_ops = operations_for_control_verdicts(
            _metric_verdicts(self.base_report, aggregate, orbit)
        )
        self.assertNotIn("simplify_orbit_to_truck", linear_ops)
        self.assertIn("split_time_segments", linear_ops)
        self.assertIn("simplify_orbit_to_truck", orbit_ops)

    def test_cli_is_path_safe_atomic_immutable_and_rejects_input_collision(self):
        from videoactagent.trajectory_closed_loop import main

        output = self.root / "revision.json"
        arguments = [
            "prepare",
            "--evaluation", str(self.report),
            "--prompt", str(self.prompt),
            "--video", str(self.video),
            "--trajectory", str(self.trajectory),
            "--session-manifest", str(self.manifest),
            "--annotation", str(self.annotation),
            "--revision-generation", "0",
            "--workspace", str(self.root),
            "--output", str(output),
        ]
        self.assertEqual(main(arguments), 0)
        original = output.read_bytes()
        self.assertEqual(main(arguments), 2)
        self.assertEqual(output.read_bytes(), original)

        collision = list(arguments)
        collision[collision.index(str(output))] = str(self.prompt)
        prompt_bytes = self.prompt.read_bytes()
        self.assertEqual(main(collision), 2)
        self.assertEqual(self.prompt.read_bytes(), prompt_bytes)

        escape = list(arguments)
        escape[escape.index(str(output))] = str(self.root.parent / "escape.json")
        self.assertEqual(main(escape), 2)


class RealTask7ClosedLoopIntegrationTests(unittest.TestCase):
    def test_both_real_task7_reports_are_recomputed_before_revision(self):
        from videoactagent.trajectory_closed_loop import prepare_revision

        required = [REAL_PROMPT, REAL_TRAJECTORY]
        for case in REAL_CASES.values():
            required.extend(case.values())
        missing = [str(path) for path in required if not path.is_file()]
        self.assertEqual(missing, [], f"real Task 7 evidence is missing: {missing}")
        for backend, case in REAL_CASES.items():
            with self.subTest(backend=backend):
                revision = prepare_revision(
                    evaluation_path=case["evaluation"],
                    prompt_path=REAL_PROMPT,
                    video_path=case["video"],
                    trajectory_path=REAL_TRAJECTORY,
                    session_manifest_path=case["manifest"],
                    annotation_path=case["annotation"],
                    revision_generation=0,
                    workspace=ROOT,
                )
                self.assertEqual(revision["revision_generation"], 1)
                self.assertFalse(revision["submission_allowed"])
                self.assertFalse(revision["network_called"])

    def test_persisted_report_must_exactly_match_fresh_task7_recomputation(self):
        from videoactagent.trajectory_closed_loop import prepare_revision

        source = REAL_CASES["kling"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "result.mp4"
            trajectory = root / "trajectory.json"
            prompt = root / "prompt.txt"
            observer = root / "observer"
            shutil.copy2(source["video"], video)
            shutil.copy2(REAL_TRAJECTORY, trajectory)
            shutil.copy2(REAL_PROMPT, prompt)
            shutil.copytree(source["manifest"].parent, observer)
            evaluation = observer / "evaluation.json"
            report = json.loads(evaluation.read_text(encoding="utf-8"))
            report["metrics"]["aggregate"]["mean_distance"] += 0.001
            _write_json(evaluation, report)
            with self.assertRaisesRegex(ValueError, "fresh Task 7 recomputation"):
                prepare_revision(
                    evaluation_path=evaluation,
                    prompt_path=prompt,
                    video_path=video,
                    trajectory_path=trajectory,
                    session_manifest_path=observer / "session_manifest.json",
                    annotation_path=observer / "manual_annotation.json",
                    revision_generation=0,
                    workspace=root,
                )
            report["metrics"]["aggregate"]["mean_distance"] -= 0.001
            report["metrics"]["aggregate"]["endpoint_t"] = 0.9
            _write_json(evaluation, report)
            with self.assertRaisesRegex(ValueError, "requested last visible point"):
                prepare_revision(
                    evaluation_path=evaluation,
                    prompt_path=prompt,
                    video_path=video,
                    trajectory_path=trajectory,
                    session_manifest_path=observer / "session_manifest.json",
                    annotation_path=observer / "manual_annotation.json",
                    revision_generation=0,
                    workspace=root,
                )


class ExperimentPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _pilot_report(self, backend: str, manual_reviewed: bool = True) -> Path:
        run = self.root / "runs" / backend
        case = REAL_CASES[backend]
        if run.exists():
            shutil.rmtree(run)
        shutil.copytree(case["video"].parent, run)
        observer = self.root / "observer" / backend
        if observer.exists():
            shutil.rmtree(observer)
        shutil.copytree(case["manifest"].parent, observer)
        trajectory = self.root / "trajectory.json"
        if not trajectory.exists():
            shutil.copy2(REAL_TRAJECTORY, trajectory)
        video = run / "result.mp4"
        evaluation = observer / "evaluation.json"
        audit = run / "audit.json"
        report = run / "pilot_review.json"
        _write_json(
            report,
            {
                "schema_version": "0.1",
                "evidence_type": "real_api_trajectory_pilot_manual_review",
                "backend": backend,
                "manual_reviewed": manual_reviewed,
                "source_artifacts": {
                    "result_video": {
                        "path": video.relative_to(self.root).as_posix(),
                        "sha256": _sha(video),
                    },
                    "evaluation_report": {
                        "path": evaluation.relative_to(self.root).as_posix(),
                        "sha256": _sha(evaluation),
                    },
                    "api_audit": {
                        "path": audit.relative_to(self.root).as_posix(),
                        "sha256": _sha(audit),
                    },
                    "trajectory": {
                        "path": trajectory.relative_to(self.root).as_posix(),
                        "sha256": _sha(trajectory),
                    },
                    "session_manifest": {
                        "path": (observer / "session_manifest.json").relative_to(self.root).as_posix(),
                        "sha256": _sha(observer / "session_manifest.json"),
                    },
                    "annotation": {
                        "path": (observer / "manual_annotation.json").relative_to(self.root).as_posix(),
                        "sha256": _sha(observer / "manual_annotation.json"),
                    },
                },
            },
        )
        return report

    def _write_all_ready_bundles(self) -> None:
        from videoactagent.trajectory_backend import prepare_revision_api_bundle
        from videoactagent.trajectory_closed_loop import prepare_revision

        copied_prompt = self.root / "prompt.txt"
        shutil.copy2(REAL_PROMPT, copied_prompt)
        kling_observer = self.root / "observer" / "kling"
        revision = prepare_revision(
            evaluation_path=kling_observer / "evaluation.json",
            prompt_path=copied_prompt,
            video_path=self.root / "runs" / "kling" / "result.mp4",
            trajectory_path=self.root / "trajectory.json",
            session_manifest_path=kling_observer / "session_manifest.json",
            annotation_path=kling_observer / "manual_annotation.json",
            revision_generation=0,
            workspace=self.root,
        )
        revision_path = self.root / "revision.json"
        _write_json(revision_path, revision)
        scenes = ("station_platform", "city_crosswalk", "forest_path", "studio_room")
        for backend in ("kling", "seedance"):
            source_base = ROOT / "runs" / "trajectory_api_pilot" / "prepared" / backend / "bundle.json"
            base = json.loads(source_base.read_text(encoding="utf-8"))
            base_path = self.root / f"base_{backend}.json"
            _write_json(base_path, base)
            feedback = prepare_revision_api_bundle(revision_path, base_path, backend)
            for scene in scenes:
                condition_root = (
                    self.root
                    / "runs"
                    / "trajectory_experiment"
                    / "prepared"
                    / scene
                    / backend
                )
                plain = {
                    "backend": backend,
                    "scene_id": scene,
                    "shots": [
                        {
                            "shot_id": "s01",
                            "duration": 5.0,
                            "prompts": {"plain": f"Manual baseline for {scene}."},
                        }
                    ],
                }
                _write_json(condition_root / "manual_text_baseline" / "bundle.json", plain)
                compiled = copy.deepcopy(base)
                compiled["scene_id"] = scene
                _write_json(
                    condition_root / "trajectory_compiled" / "bundle.json",
                    compiled,
                )
                revised = copy.deepcopy(feedback)
                revised["scene_id"] = scene
                _write_json(
                    condition_root
                    / "trajectory_compiled_feedback_revision"
                    / "bundle.json",
                    revised,
                )

    def test_fixed_matrix_has_24_calls_cost_fields_and_no_submission(self):
        from videoactagent.trajectory_experiment import build_experiment_manifest

        manifest = build_experiment_manifest({}, workspace=self.root)
        self.assertEqual(manifest["planned_call_count"], 24)
        self.assertEqual(len(manifest["jobs"]), 24)
        self.assertEqual(len({job["job_id"] for job in manifest["jobs"]}), 24)
        self.assertEqual({job["backend"] for job in manifest["jobs"]}, {"kling", "seedance"})
        self.assertEqual(len({job["scene_id"] for job in manifest["jobs"]}), 4)
        self.assertEqual(len({job["condition"] for job in manifest["jobs"]}), 3)
        self.assertFalse(manifest["submission_allowed"])
        self.assertFalse(manifest["network_called"])
        self.assertFalse(manifest["submitted"])
        self.assertIn("cost_estimate", manifest)
        self.assertTrue(
            all(job["submission_command_argv"] is None for job in manifest["jobs"])
        )
        self.assertTrue(
            all(job["status"] == "preparation_required" for job in manifest["jobs"])
        )

    def test_gate_opens_only_for_two_hash_bound_real_manual_reviews(self):
        from videoactagent.trajectory_experiment import build_experiment_manifest

        kling = self._pilot_report("kling")
        seedance = self._pilot_report("seedance")
        manifest = build_experiment_manifest(
            {"seedance": seedance, "kling": kling},
            workspace=self.root,
            per_call_cost={"kling": 2.5, "seedance": 3.0},
            currency="CNY",
        )
        self.assertFalse(manifest["submission_allowed"])
        self.assertTrue(manifest["pilot_gate"]["reviews_verified"])
        self.assertEqual(manifest["bundle_gate"]["ready_job_count"], 0)
        self.assertTrue(
            all(job["status"] == "preparation_required" for job in manifest["jobs"])
        )
        self.assertTrue(
            all(job["submission_command_argv"] is None for job in manifest["jobs"])
        )
        self.assertEqual(manifest["cost_estimate"]["total"], 66.0)
        self.assertEqual(
            sorted(manifest["pilot_gate"]["report_sha256"]), ["kling", "seedance"]
        )
        self.assertFalse(manifest["network_called"])
        self.assertFalse(manifest["submitted"])

        _write_json(seedance, {**json.loads(seedance.read_text()), "manual_reviewed": False})
        self.assertFalse(
            build_experiment_manifest(
                {"kling": kling, "seedance": seedance}, workspace=self.root
            )["submission_allowed"]
        )

        # A locally fabricated review flag cannot override an unsuccessful API audit.
        audit_path = self.root / "runs" / "seedance" / "audit.json"
        audit = json.loads(audit_path.read_text())
        audit["checks"]["terminal_status_success"] = False
        _write_json(audit_path, audit)
        report = json.loads(seedance.read_text())
        report["manual_reviewed"] = True
        report["source_artifacts"]["api_audit"]["sha256"] = _sha(audit_path)
        _write_json(seedance, report)
        with self.assertRaisesRegex(ValueError, "terminal success"):
            build_experiment_manifest(
                {"kling": kling, "seedance": seedance}, workspace=self.root
            )

    def test_all_ready_bundles_emit_only_commands_accepted_by_real_jd_parser(self):
        from videoactagent import jd_smoke
        from videoactagent.trajectory_experiment import build_experiment_manifest

        kling = self._pilot_report("kling")
        seedance = self._pilot_report("seedance")
        self._write_all_ready_bundles()
        manifest = build_experiment_manifest(
            {"kling": kling, "seedance": seedance}, workspace=self.root
        )
        self.assertTrue(manifest["submission_allowed"])
        self.assertEqual(manifest["bundle_gate"]["ready_job_count"], 24)
        for job in manifest["jobs"]:
            argv = job["submission_command_argv"]
            self.assertIsInstance(argv, list)
            parsed = jd_smoke.parse_args(argv[3:])
            self.assertEqual(parsed.command, f"submit-{job['backend']}")
            self.assertEqual(
                parsed.prompt,
                {
                    "manual_text_baseline": "plain",
                    "trajectory_compiled": "trajectory_compiled",
                    "trajectory_compiled_feedback_revision": "trajectory_compiled_feedback_revision",
                }[job["condition"]],
            )

    def test_ready_bundle_scene_identity_must_match_matrix_job(self):
        from videoactagent.trajectory_experiment import build_experiment_manifest

        self._pilot_report("kling")
        self._pilot_report("seedance")
        self._write_all_ready_bundles()
        bundle = (
            self.root
            / "runs/trajectory_experiment/prepared/city_crosswalk/kling/manual_text_baseline/bundle.json"
        )
        document = json.loads(bundle.read_text())
        document["scene_id"] = "station_platform"
        _write_json(bundle, document)
        with self.assertRaisesRegex(ValueError, "scene mismatch"):
            build_experiment_manifest({}, workspace=self.root)

    def test_tampered_duplicate_unknown_bool_nonfinite_huge_and_path_escape_fail(self):
        from videoactagent.trajectory_experiment import build_experiment_manifest

        for backend in ("kling", "seedance"):
            report = self._pilot_report(backend)
            document = json.loads(report.read_text())
            document["source_artifacts"]["result_video"]["sha256"] = "0" * 64
            _write_json(report, document)
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    build_experiment_manifest({backend: report}, workspace=self.root)

        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"schema_version":"0.1","schema_version":"0.1"}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            build_experiment_manifest({"kling": duplicate}, workspace=self.root)

        kling = self._pilot_report("kling")
        for invalid in (True, float("nan"), 10**400, -1):
            with self.subTest(cost=repr(invalid)):
                with self.assertRaises((TypeError, ValueError, OverflowError)):
                    build_experiment_manifest(
                        {"kling": kling},
                        workspace=self.root,
                        per_call_cost={"kling": invalid},  # type: ignore[dict-item]
                    )

        outside = self.root.parent / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            with self.assertRaisesRegex(ValueError, "workspace"):
                build_experiment_manifest({"kling": outside}, workspace=self.root)
        finally:
            outside.unlink(missing_ok=True)

    def test_minimal_forged_audit_or_evaluation_cannot_open_review_gate(self):
        from videoactagent.trajectory_experiment import build_experiment_manifest

        review_path = self._pilot_report("kling")
        review = json.loads(review_path.read_text())
        audit_path = self.root / review["source_artifacts"]["api_audit"]["path"]
        _write_json(
            audit_path,
            {
                "schema_version": 1,
                "backend": "kling",
                "evidence_source": "persisted_gateway_records_internal_consistency",
                "checks": {"terminal_status_success": True},
                "result": {"sha256": review["source_artifacts"]["result_video"]["sha256"]},
            },
        )
        review["source_artifacts"]["api_audit"]["sha256"] = _sha(audit_path)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ValueError, "missing fields"):
            build_experiment_manifest({"kling": review_path}, workspace=self.root)

        review_path = self._pilot_report("kling")
        review = json.loads(review_path.read_text())
        evaluation_path = self.root / review["source_artifacts"]["evaluation_report"]["path"]
        _write_json(
            evaluation_path,
            {
                "schema_version": "0.1",
                "evidence_type": "manual_visual_trajectory_evaluation",
                "provenance": {
                    "video_sha256": review["source_artifacts"]["result_video"]["sha256"]
                },
            },
        )
        review["source_artifacts"]["evaluation_report"]["sha256"] = _sha(evaluation_path)
        _write_json(review_path, review)
        with self.assertRaises((KeyError, ValueError)):
            build_experiment_manifest({"kling": review_path}, workspace=self.root)

    def test_rehashed_stage3_evidence_semantic_forgery_is_rejected(self):
        from videoactagent.trajectory_experiment import build_experiment_manifest

        review_path = self._pilot_report("kling")
        review = json.loads(review_path.read_text())
        audit_path = self.root / review["source_artifacts"]["api_audit"]["path"]
        audit = json.loads(audit_path.read_text())
        metadata_path = audit_path.parent / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["backend"] = "seedance"
        _write_json(metadata_path, metadata)
        audit["evidence_files"]["metadata.json"]["bytes"] = metadata_path.stat().st_size
        audit["evidence_files"]["metadata.json"]["sha256"] = _sha(metadata_path)
        _write_json(audit_path, audit)
        review["source_artifacts"]["api_audit"]["sha256"] = _sha(audit_path)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ValueError, "metadata/request mismatch"):
            build_experiment_manifest({"kling": review_path}, workspace=self.root)

    def test_review_change_during_recomputation_is_detected(self):
        import videoactagent.trajectory_experiment as trajectory_experiment

        review_path = self._pilot_report("kling")
        actual_evaluate = trajectory_experiment.evaluate_files

        def evaluate_then_mutate(*args, **kwargs):
            result = actual_evaluate(*args, **kwargs)
            review_path.write_bytes(review_path.read_bytes() + b" ")
            return result

        with patch.object(
            trajectory_experiment,
            "evaluate_files",
            side_effect=evaluate_then_mutate,
        ):
            with self.assertRaisesRegex(ValueError, "source changed"):
                trajectory_experiment.build_experiment_manifest(
                    {"kling": review_path}, workspace=self.root
                )

    def test_cli_only_writes_plan_and_rejects_hardlink_input_collision(self):
        from videoactagent.trajectory_experiment import main

        kling = self._pilot_report("kling")
        output = self.root / "matrix.json"
        args = [
            "prepare",
            "--workspace", str(self.root),
            "--pilot-report", f"kling={kling}",
            "--output", str(output),
        ]
        self.assertEqual(main(args), 0)
        plan = json.loads(output.read_text())
        self.assertFalse(plan["network_called"])
        self.assertFalse(plan["submitted"])
        self.assertEqual(main(args), 2)

        alias = self.root / "pilot_alias.json"
        os.link(kling, alias)
        collision = list(args)
        collision[collision.index(str(output))] = str(alias)
        self.assertEqual(main(collision), 2)
        self.assertTrue(os.path.samefile(alias, kling))


if __name__ == "__main__":
    unittest.main()
