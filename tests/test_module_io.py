"""Stage 0/2/6/trajectory local I/O diagnostics.

Run: ``python -m unittest tests.test_module_io -v``. The checks read real
workspace JSON/video inputs and outputs; mechanics-only fixtures exercise
failure handling and never constitute acceptance evidence.
"""

from __future__ import annotations

import hashlib
import gc
import json
import os
import shutil
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from videoactagent.module_io import (
    inspect_json,
    inspect_manifest,
    inspect_text,
    inspect_video,
    main,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples" / "module_io_manifest.json"


class RealModuleIOTests(unittest.TestCase):
    def test_real_stage2_stage6_and_trajectory_records_are_verified(self):
        report = inspect_manifest(MANIFEST, workspace=ROOT)

        self.assertTrue(report["ok"])
        by_id = {item["module_id"]: item for item in report["modules"]}
        self.assertEqual(by_id["stage2_control_bundle"]["status"], "passed")
        self.assertEqual(by_id["stage6_vace_inputs_current"]["status"], "passed")
        self.assertEqual(
            by_id["stage6_source_validation_historical"]["status"], "passed"
        )
        self.assertEqual(by_id["trajectory_instruction_s01"]["status"], "passed")

        stage2_video = next(
            item
            for item in by_id["stage2_control_bundle"]["outputs"]
            if item["kind"] == "video"
        )
        self.assertEqual(stage2_video["inspection"]["frame_count"], 15)
        self.assertEqual(stage2_video["inspection"]["size"], [960, 540])

        declared_manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        declared_validation = next(
            record
            for module in declared_manifest["modules"]
            if module["module_id"] == "stage6_source_validation_historical"
            for record in module["outputs"]
            if record["path"].endswith("source_validation.historical-old-job.json")
        )
        validation = by_id["stage6_source_validation_historical"]["outputs"][0]
        self.assertEqual(validation["sha256"], declared_validation["sha256"])
        self.assertEqual(validation["sha256"], validation["declared_sha256"])
        self.assertEqual(
            validation["inspection"]["evidence_source"],
            "pinned_vace_preprocessor_real_cuda",
        )
        self.assertFalse(validation["inspection"]["inference"]["model_constructed"])
        self.assertFalse(validation["inspection"]["inference"]["checkpoint_loaded"])
        stage6_root = ROOT / "runs" / "stage6_vace_inputs" / "s01"
        self.assertFalse((stage6_root / "source_validation.json").exists())
        self.assertFalse((stage6_root / "vace_job.validated.json").exists())

        trajectory = by_id["trajectory_instruction_s01"]["outputs"][0]
        self.assertEqual(
            trajectory["sha256"],
            "52792642f4887a78cc332898714fa877d3d6336073302e3a127cd69b7d8921e6",
        )
        self.assertTrue(trajectory["hash_match"])
        self.assertTrue(all(item["match"] for item in trajectory["binding_results"]))

    def test_approved_trajectory_api_bundles_are_registered_not_provisional(self):
        report = inspect_manifest(MANIFEST, workspace=ROOT)
        by_id = {item["module_id"]: item for item in report["modules"]}
        module = by_id["trajectory_api_pilot_prepared"]

        self.assertTrue(report["ok"])
        self.assertEqual(module["status"], "passed")
        self.assertEqual(
            [item["path"] for item in module["outputs"]],
            [
                "runs/trajectory_api_pilot/prepared/kling/bundle.json",
                "runs/trajectory_api_pilot/prepared/seedance/bundle.json",
            ],
        )
        self.assertTrue(
            all(
                result["match"]
                for output in module["outputs"]
                for result in output["binding_results"]
            )
        )
        manifest_text = MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("pending_review", manifest_text)
        self.assertNotIn("provisional", manifest_text)

    def test_real_trajectory_pilots_and_manual_evaluations_are_registered(self):
        report = inspect_manifest(MANIFEST, workspace=ROOT)
        by_id = {item["module_id"]: item for item in report["modules"]}

        expected = {
            "trajectory_real_pilot_kling_s01": (
                "5cfce7a0a887bad62904dd05f9934f647770defa11b9c066be5636f1c34bd180",
                "a0be8fc5f2caf3e089f18e9adada0d808b645dd0dc7317307f3861720b2513bf",
            ),
            "trajectory_real_pilot_seedance_s01": (
                "0cc51800c70f45f94314210ac590041a1e3f1ce3a913a375c4f4d24ce960bae2",
                "4281bf331e18089dc63b2673aef8ff2266f4ea75516c9c52b298db5e92b99d7b",
            ),
        }
        for module_id, (video_sha, evaluation_sha) in expected.items():
            with self.subTest(module_id=module_id):
                module = by_id[module_id]
                self.assertEqual(module["status"], "passed")
                video = next(item for item in module["outputs"] if item["kind"] == "video")
                evaluation = next(
                    item
                    for item in module["outputs"]
                    if item["path"].endswith("evaluation.json")
                )
                self.assertEqual(video["sha256"], video_sha)
                self.assertEqual(video["inspection"]["frame_count"], 121)
                self.assertEqual(video["inspection"]["size"], [1280, 720])
                self.assertEqual(evaluation["sha256"], evaluation_sha)
                self.assertTrue(all(item["match"] for item in evaluation["binding_results"]))

    def test_real_bounded_revision_results_are_registered_as_negative_evidence(self):
        report = inspect_manifest(MANIFEST, workspace=ROOT)
        by_id = {item["module_id"]: item for item in report["modules"]}
        expected = {
            "trajectory_revision_real_kling_s01": (
                "53c3bf8b67778882a5cefed654aebefc5ee0a5b317fc91920f48f340870778ca",
                True,
            ),
            "trajectory_revision_real_seedance_s01": (
                "c6fbb1d98e14c1f90436ab79e3ee796bcaccc153bc7902d85f2925e4587f9deb",
                False,
            ),
        }
        for module_id, (video_sha, direction_match) in expected.items():
            with self.subTest(module_id=module_id):
                module = by_id[module_id]
                self.assertEqual(module["status"], "passed")
                video = next(item for item in module["outputs"] if item["kind"] == "video")
                evaluation = next(
                    item for item in module["outputs"] if item["path"].endswith("evaluation.json")
                )
                self.assertEqual(video["sha256"], video_sha)
                direction = next(
                    item
                    for item in evaluation["binding_results"]
                    if item["field"] == "metrics.aggregate.direction_match"
                )
                self.assertEqual(direction["actual"], direction_match)
                self.assertTrue(direction["match"])

    def test_real_json_and_video_public_inspectors_decode_actual_files(self):
        json_report = inspect_json(
            ROOT
            / "runs"
            / "stage6_vace_inputs"
            / "s01"
            / "source_validation.historical-old-job.json"
        )
        video_report = inspect_video(
            ROOT / "runs" / "stage2_control_bridge" / "shots" / "s01" / "proxy.mp4"
        )

        self.assertEqual(json_report["schema_version"], "0.1")
        self.assertEqual(json_report["status"], "passed")
        self.assertEqual(json_report["evidence_source"], "pinned_vace_preprocessor_real_cuda")
        self.assertEqual(video_report["frame_count"], 15)
        self.assertAlmostEqual(video_report["duration_seconds"], 5.0, places=1)
        self.assertEqual(video_report["size"], [960, 540])

    def test_sha256_file_streams_the_real_bytes(self):
        path = ROOT / "runs" / "stage6_vace_inputs" / "s01" / "src_mask.mp4"
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(sha256_file(path), expected)

    def test_real_trajectory_prompt_is_strict_utf8_text(self):
        report = inspect_text(
            ROOT / "runs" / "trajectory" / "s01" / "compiled" / "trajectory_prompt.txt"
        )
        self.assertEqual(report["encoding"], "utf-8")
        self.assertEqual(report["line_count"], 2)
        self.assertGreater(report["character_count"], 100)

    def test_real_video_reader_emits_no_resource_warning(self):
        import imageio_ffmpeg

        original_read_frames = imageio_ffmpeg.read_frames

        class DelayedReader:
            def __init__(self, reader):
                self.reader = reader

            @property
            def gi_frame(self):
                return self.reader.gi_frame

            def __next__(self):
                return next(self.reader)

            def close(self):
                self.reader.close()

        def delayed_read_frames(*args, **kwargs):
            return DelayedReader(original_read_frames(*args, **kwargs))

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with mock.patch(
                "videoactagent.module_io.imageio_ffmpeg.read_frames",
                side_effect=delayed_read_frames,
            ):
                inspect_video(
                    ROOT
                    / "runs"
                    / "stage2_control_bridge"
                    / "shots"
                    / "s01"
                    / "proxy.mp4"
                )
            gc.collect()

        self.assertEqual(
            [item for item in caught if item.category is ResourceWarning],
            [],
        )


class ManifestFailureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_manifest(self, modules: list[dict[str, object]]) -> Path:
        path = self.root / "manifest.json"
        path.write_text(
            json.dumps({"schema_version": "0.1", "modules": modules}),
            encoding="utf-8",
        )
        return path

    def test_missing_required_output_returns_failed_not_passed(self):
        manifest = self.write_manifest(
            [
                {
                    "module_id": "missing",
                    "inputs": [],
                    "outputs": [
                        {"path": "missing.mp4", "kind": "video", "required": True}
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(report["modules"][0]["status"], "failed")
        self.assertEqual(report["modules"][0]["outputs"][0]["error"], "required_missing")

    def test_text_kind_checks_missing_non_utf8_and_declared_hash(self):
        valid = self.root / "prompt.txt"
        valid.write_text("camera orbits clockwise\n", encoding="utf-8")
        invalid = self.root / "invalid.txt"
        invalid.write_bytes(b"\xff\xfe\x80")
        manifest = self.write_manifest(
            [
                {
                    "module_id": "text_records",
                    "inputs": [],
                    "outputs": [
                        {
                            "path": "prompt.txt",
                            "kind": "text",
                            "required": True,
                            "sha256": hashlib.sha256(valid.read_bytes()).hexdigest(),
                        },
                        {"path": "missing.txt", "kind": "text", "required": True},
                        {"path": "invalid.txt", "kind": "text", "required": True},
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)
        records = report["modules"][0]["outputs"]
        self.assertFalse(report["ok"])
        self.assertEqual(records[0]["status"], "passed")
        self.assertTrue(records[0]["hash_match"])
        self.assertEqual(records[0]["inspection"]["encoding"], "utf-8")
        self.assertEqual(records[1]["error"], "required_missing")
        self.assertEqual(records[2]["error"], "text_inspection_failed")

        stale = json.loads(manifest.read_text("utf-8"))
        stale["modules"][0]["outputs"] = [
            {"path": "prompt.txt", "kind": "text", "required": True, "sha256": "0" * 64}
        ]
        manifest.write_text(json.dumps(stale), encoding="utf-8")
        stale_report = inspect_manifest(manifest, workspace=self.root)
        self.assertEqual(
            stale_report["modules"][0]["outputs"][0]["error"], "sha256_mismatch"
        )

    def test_null_required_is_invalid_record_and_cannot_become_optional(self):
        manifest = self.write_manifest(
            [
                {
                    "module_id": "invalid_required",
                    "inputs": [],
                    "outputs": [
                        {"path": "missing.json", "kind": "json", "required": None}
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)

        record = report["modules"][0]["outputs"][0]
        self.assertFalse(report["ok"])
        self.assertEqual(report["modules"][0]["status"], "failed")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "invalid_record")

    def test_non_boolean_required_values_are_invalid_records(self):
        for invalid in (0, 1, "false", [], {}):
            with self.subTest(required=invalid):
                manifest = self.write_manifest(
                    [
                        {
                            "module_id": "invalid_required",
                            "inputs": [],
                            "outputs": [
                                {
                                    "path": "missing.json",
                                    "kind": "json",
                                    "required": invalid,
                                }
                            ],
                        }
                    ]
                )
                report = inspect_manifest(manifest, workspace=self.root)
                self.assertFalse(report["ok"])
                self.assertEqual(
                    report["modules"][0]["outputs"][0]["error"],
                    "invalid_record",
                )

    def test_stale_declared_hash_fails_even_when_json_is_valid(self):
        artifact = self.root / "artifact.json"
        artifact.write_text('{"schema_version":"0.1"}', encoding="utf-8")
        manifest = self.write_manifest(
            [
                {
                    "module_id": "stale",
                    "inputs": [],
                    "outputs": [
                        {
                            "path": "artifact.json",
                            "kind": "json",
                            "required": True,
                            "sha256": "0" * 64,
                        }
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)

        record = report["modules"][0]["outputs"][0]
        self.assertFalse(report["ok"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "sha256_mismatch")
        self.assertFalse(record["hash_match"])

    def test_stale_json_binding_fails_even_when_hash_is_not_declared(self):
        artifact = self.root / "artifact.json"
        artifact.write_text(
            '{"schema_version":"0.1","evidence":{"inference_success":false}}',
            encoding="utf-8",
        )
        manifest = self.write_manifest(
            [
                {
                    "module_id": "stale_binding",
                    "inputs": [],
                    "outputs": [
                        {
                            "path": "artifact.json",
                            "kind": "json",
                            "required": True,
                            "bindings": {"evidence.inference_success": True},
                        }
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)

        record = report["modules"][0]["outputs"][0]
        self.assertFalse(report["ok"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "binding_mismatch")
        self.assertEqual(
            record["binding_results"],
            [
                {
                    "field": "evidence.inference_success",
                    "expected": True,
                    "actual": False,
                    "match": False,
                }
            ],
        )

    def test_absolute_and_parent_escape_paths_are_rejected(self):
        outside = self.root.parent / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)

        for unsafe_path in (str(outside.resolve()), "../outside.json"):
            with self.subTest(path=unsafe_path):
                manifest = self.write_manifest(
                    [
                        {
                            "module_id": "unsafe",
                            "inputs": [],
                            "outputs": [
                                {
                                    "path": unsafe_path,
                                    "kind": "json",
                                    "required": True,
                                }
                            ],
                        }
                    ]
                )
                report = inspect_manifest(manifest, workspace=self.root)
                record = report["modules"][0]["outputs"][0]
                self.assertFalse(report["ok"])
                self.assertEqual(record["error"], "unsafe_path")

    def test_malformed_json_and_video_are_real_decode_failures(self):
        (self.root / "broken.json").write_text("{not-json", encoding="utf-8")
        (self.root / "broken.mp4").write_bytes(b"not a video container")
        manifest = self.write_manifest(
            [
                {
                    "module_id": "broken",
                    "inputs": [
                        {"path": "broken.json", "kind": "json", "required": True}
                    ],
                    "outputs": [
                        {"path": "broken.mp4", "kind": "video", "required": True}
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)

        module = report["modules"][0]
        self.assertFalse(report["ok"])
        self.assertEqual(module["inputs"][0]["error"], "json_inspection_failed")
        self.assertEqual(module["outputs"][0]["error"], "video_inspection_failed")

    def test_manifest_schema_version_is_strict_and_fail_closed(self):
        invalid_versions = (None, "0.2", 1, ["0.1"])
        for version in invalid_versions:
            with self.subTest(version=version):
                document: dict[str, object] = {"modules": []}
                if version is not None:
                    document["schema_version"] = version
                manifest = self.root / "manifest.json"
                manifest.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "schema_version"):
                    inspect_manifest(manifest, workspace=self.root)

    def test_manifest_and_artifacts_are_consumed_from_private_snapshots(self):
        artifact = self.root / "artifact.json"
        original_artifact = {
            "schema_version": "0.1",
            "evidence": {"state": "before"},
        }
        artifact.write_text(json.dumps(original_artifact), encoding="utf-8")
        expected_artifact_hash = sha256_file(artifact)
        manifest = self.write_manifest(
            [
                {
                    "module_id": "snapshot",
                    "inputs": [],
                    "outputs": [
                        {
                            "path": "artifact.json",
                            "kind": "json",
                            "required": True,
                            "sha256": expected_artifact_hash,
                            "bindings": {"evidence.state": "before"},
                        }
                    ],
                }
            ]
        )
        expected_manifest_hash = sha256_file(manifest)
        original_copyfile = shutil.copyfile
        copied_sources: list[Path] = []

        def copy_then_replace(source, target, *args, **kwargs):
            source_path = Path(source).resolve()
            copied_sources.append(source_path)
            result = original_copyfile(source, target, *args, **kwargs)
            if source_path == manifest.resolve():
                manifest.write_text(
                    '{"schema_version":"9.9","modules":[]}', encoding="utf-8"
                )
            elif source_path == artifact.resolve():
                artifact.write_text(
                    '{"schema_version":"0.1","evidence":{"state":"after"}}',
                    encoding="utf-8",
                )
            return result

        with mock.patch("shutil.copyfile", side_effect=copy_then_replace):
            report = inspect_manifest(manifest, workspace=self.root)

        record = report["modules"][0]["outputs"][0]
        self.assertTrue(report["ok"])
        self.assertEqual(report["manifest"]["sha256"], expected_manifest_hash)
        self.assertEqual(record["sha256"], expected_artifact_hash)
        self.assertEqual(record["binding_results"][0]["actual"], "before")
        self.assertIn(manifest.resolve(), copied_sources)
        self.assertIn(artifact.resolve(), copied_sources)
        self.assertEqual(json.loads(artifact.read_text())["evidence"]["state"], "after")

    def test_empty_or_incomplete_ffmpeg_metadata_fails_and_closes_pipes(self):
        video = self.root / "input.mp4"
        video.write_bytes(b"snapshot source bytes")
        manifest = self.write_manifest(
            [
                {
                    "module_id": "video",
                    "inputs": [],
                    "outputs": [
                        {"path": "input.mp4", "kind": "video", "required": True}
                    ],
                }
            ]
        )

        class Pipe:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Process:
            def __init__(self):
                self.stdin = Pipe()
                self.stdout = Pipe()

        class Frame:
            def __init__(self, process):
                self.f_locals = {"process": process}

        class Reader:
            def __init__(self, first):
                self.first = first
                self.process = Process()
                self.gi_frame = Frame(self.process)
                self.closed = False

            def __next__(self):
                if isinstance(self.first, BaseException):
                    raise self.first
                value, self.first = self.first, StopIteration()
                return value

            def close(self):
                self.closed = True

        for first in (StopIteration(), {}, {"size": (1, 1)}):
            with self.subTest(first=first):
                reader = Reader(first)
                with mock.patch(
                    "videoactagent.module_io.imageio_ffmpeg.count_frames_and_secs",
                    return_value=(1, 1.0),
                ), mock.patch(
                    "videoactagent.module_io.imageio_ffmpeg.read_frames",
                    return_value=reader,
                ):
                    report = inspect_manifest(manifest, workspace=self.root)

                record = report["modules"][0]["outputs"][0]
                self.assertFalse(report["ok"])
                self.assertEqual(record["error"], "video_inspection_failed")
                self.assertTrue(reader.closed)
                self.assertTrue(reader.process.stdin.closed)
                self.assertTrue(reader.process.stdout.closed)

    def test_symlink_escape_is_rejected_if_platform_allows_symlinks(self):
        outside = self.root.parent / "outside-symlink-target.json"
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / "linked.json"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        manifest = self.write_manifest(
            [
                {
                    "module_id": "symlink",
                    "inputs": [],
                    "outputs": [
                        {"path": "linked.json", "kind": "json", "required": True}
                    ],
                }
            ]
        )

        report = inspect_manifest(manifest, workspace=self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(report["modules"][0]["outputs"][0]["error"], "unsafe_path")

    def test_simulated_symlink_resolution_escape_is_rejected_without_privilege(self):
        linked = self.root / "linked.json"
        outside = self.root.parent / "resolved-outside.json"
        original_resolve = type(self.root).resolve

        def resolve_with_escape(path, *args, **kwargs):
            if path == linked:
                return outside
            return original_resolve(path, *args, **kwargs)

        manifest = self.write_manifest(
            [
                {
                    "module_id": "simulated_symlink",
                    "inputs": [],
                    "outputs": [
                        {"path": "linked.json", "kind": "json", "required": True}
                    ],
                }
            ]
        )

        with mock.patch.object(type(self.root), "resolve", new=resolve_with_escape):
            report = inspect_manifest(manifest, workspace=self.root)

        self.assertFalse(report["ok"])
        self.assertEqual(report["modules"][0]["outputs"][0]["error"], "unsafe_path")


class ModuleIOCLITests(unittest.TestCase):
    def test_cli_selects_one_module_and_atomically_writes_report(self):
        output = ROOT / "runs" / "local_debug" / "module_io_one_test.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "inspect",
                    "--manifest",
                    str(MANIFEST),
                    "--workspace",
                    str(ROOT),
                    "--module",
                    "stage2_control_bundle",
                    "--output",
                    str(output),
                ]
            )

        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("MODULE_IO_OK", stdout.getvalue())
        self.assertEqual(
            [item["module_id"] for item in report["modules"]],
            ["stage2_control_bundle"],
        )
        self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_cli_replace_failure_cleans_uuid_temp_file(self):
        output = ROOT / "runs" / "local_debug" / "module_io_replace_test.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stderr = StringIO()
        with mock.patch(
            "videoactagent.module_io.os.replace",
            side_effect=OSError("controlled replace failure"),
        ):
            with redirect_stderr(stderr):
                code = main(
                    [
                        "inspect",
                        "--manifest",
                        str(MANIFEST),
                        "--workspace",
                        str(ROOT),
                        "--all",
                        "--output",
                        str(output),
                    ]
                )

        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])
        self.assertIn("controlled replace failure", stderr.getvalue())

    def test_cli_all_inspects_all_registered_real_modules(self):
        output = ROOT / "runs" / "local_debug" / "module_io_all_test.json"
        self.addCleanup(output.unlink, missing_ok=True)
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "inspect",
                    "--manifest",
                    str(MANIFEST),
                    "--workspace",
                    str(ROOT),
                    "--all",
                    "--output",
                    str(output),
                ]
            )

        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        module_ids = {item["module_id"] for item in report["modules"]}
        self.assertTrue(
            {
                "stage2_control_bundle",
                "stage6_vace_inputs_current",
                "stage6_source_validation_historical",
                "trajectory_instruction_s01",
            }.issubset(module_ids)
        )
        self.assertIn("MODULE_IO_OK", stdout.getvalue())

    def test_cli_rejects_all_with_module_without_writing(self):
        output = ROOT / "runs" / "local_debug" / "module_io_conflict_test.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "inspect",
                    "--manifest",
                    str(MANIFEST),
                    "--workspace",
                    str(ROOT),
                    "--all",
                    "--module",
                    "stage2_control_bundle",
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertIn("choose --all or --module", stderr.getvalue())

    def test_cli_refuses_to_overwrite_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = workspace / "manifest.json"
            original = (
                '{"schema_version":"0.1","modules":'
                '[{"module_id":"empty","inputs":[],"outputs":[]}]}'
            )
            manifest.write_text(original, encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "inspect",
                        "--manifest",
                        str(manifest),
                        "--workspace",
                        str(workspace),
                        "--all",
                        "--output",
                        str(manifest),
                    ]
                )

            self.assertEqual(code, 2)
            self.assertEqual(manifest.read_text(encoding="utf-8"), original)
            self.assertIn("output collision", stderr.getvalue())

    def test_cli_refuses_to_overwrite_selected_real_evidence(self):
        evidence = ROOT / "runs" / "stage2_control_bridge" / "control_bundle.json"
        before_hash = sha256_file(evidence)
        stderr = StringIO()
        with mock.patch("videoactagent.module_io._atomic_write_json") as writer:
            with redirect_stderr(stderr):
                code = main(
                    [
                        "inspect",
                        "--manifest",
                        str(MANIFEST),
                        "--workspace",
                        str(ROOT),
                        "--module",
                        "stage2_control_bundle",
                        "--output",
                        str(evidence),
                    ]
                )

        self.assertEqual(code, 2)
        writer.assert_not_called()
        self.assertEqual(sha256_file(evidence), before_hash)
        self.assertIn("output collision", stderr.getvalue())

    def test_cli_rejects_hardlink_alias_of_selected_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "runs") as directory:
            workspace = Path(directory)
            artifact = workspace / "artifact.json"
            artifact.write_text('{"schema_version":"0.1"}', encoding="utf-8")
            alias = workspace / "report.json"
            try:
                os.link(artifact, alias)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            manifest = workspace / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "0.1",
                        "modules": [
                            {
                                "module_id": "hardlink",
                                "inputs": [],
                                "outputs": [
                                    {
                                        "path": "artifact.json",
                                        "kind": "json",
                                        "required": True,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            before_hash = sha256_file(artifact)
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "inspect",
                        "--manifest",
                        str(manifest),
                        "--workspace",
                        str(workspace),
                        "--all",
                        "--output",
                        str(alias),
                    ]
                )

            self.assertEqual(code, 2)
            self.assertEqual(sha256_file(artifact), before_hash)
            self.assertIn("output collision", stderr.getvalue())

    def test_cli_unknown_module_persists_failed_report_and_returns_one(self):
        output = ROOT / "runs" / "local_debug" / "module_io_unknown_test.json"
        output.unlink(missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "inspect",
                    "--manifest",
                    str(MANIFEST),
                    "--workspace",
                    str(ROOT),
                    "--module",
                    "does_not_exist",
                    "--output",
                    str(output),
                ]
            )

        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"], ["unknown module: does_not_exist"])

    def test_cli_rejects_output_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outside-report.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "inspect",
                        "--manifest",
                        str(MANIFEST),
                        "--workspace",
                        str(ROOT),
                        "--all",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(code, 2)
            self.assertFalse(output.exists())
            self.assertIn("output must stay below workspace", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
