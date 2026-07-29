"""Task 7 observer/evaluator tests.

Run: ``python -m unittest tests.test_trajectory_eval -v``.  Temporary MP4s
are genuinely encoded and decoded to exercise mechanics.  They are not
presented as manual-annotation or model-quality acceptance evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import warnings
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg


ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_real_video(path: Path, frame_count: int = 5) -> None:
    """Encode deterministic RGB frames with a real ffmpeg process."""

    width, height = 16, 12
    payload = bytearray()
    for frame in range(frame_count):
        for y in range(height):
            for x in range(width):
                payload.extend(((frame * 41 + x) % 256, (y * 19) % 256, 80))
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        "2",
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    completed = subprocess.run(
        command,
        input=bytes(payload),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))


def _trajectory(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "scene_id": "scene",
                "shot_id": "shot",
                "coordinate_space": "normalized_0_1_top_left",
                "duration_seconds": 2.5,
                "sample_count": 5,
                "tracks": [
                    {
                        "track_id": "actor_track",
                        "target": {"type": "actor", "id": "actor_a"},
                        "primitive": "polyline",
                        "semantic": "move",
                        "points": [
                            {"t": 0.0, "x": 0.1, "y": 0.5, "visible": True},
                            {"t": 1.0, "x": 0.9, "y": 0.5, "visible": True},
                        ],
                    }
                ],
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )


class ObserverIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.video = self.root / "real.mp4"
        self.trajectory = self.root / "trajectory.json"
        _write_real_video(self.video)
        _trajectory(self.trajectory)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_prepares_hash_bound_frames_from_real_mp4_then_saves_manual_annotation(self):
        from videoactagent.trajectory_observe import prepare_session, save_annotation

        output = self.root / "observer"
        manifest = prepare_session(
            video_path=self.video,
            trajectory_path=self.trajectory,
            track_id="actor_track",
            output_dir=output,
            frame_indices=(0, 2, 4),
        )

        self.assertEqual(manifest["video_sha256"], _sha(self.video))
        self.assertEqual(manifest["video_frame_count"], 5)
        self.assertEqual(manifest["video_size"], [16, 12])
        self.assertEqual(manifest["shot_id"], "shot")
        self.assertEqual(manifest["track_id"], "actor_track")
        self.assertEqual([item["frame"] for item in manifest["frames"]], [0, 2, 4])
        for item in manifest["frames"]:
            frame = output / item["path"]
            self.assertTrue(frame.is_file())
            self.assertEqual(item["sha256"], _sha(frame))

        annotation = save_annotation(
            session_dir=output,
            payload={
                "schema_version": "0.1",
                "evidence_type": "manual_visual_annotation",
                "scene_id": "scene",
                "shot_id": "shot",
                "track_id": "actor_track",
                "video_sha256": manifest["video_sha256"],
                "trajectory_file_sha256": manifest["trajectory_file_sha256"],
                "points": [
                    {"frame": 0, "x": 0.1, "y": 0.5, "visible": True},
                    {"frame": 2, "x": None, "y": None, "visible": False},
                    {"frame": 4, "x": 0.88, "y": 0.51, "visible": True},
                ],
            },
        )
        saved = output / "manual_annotation.json"
        self.assertEqual(json.loads(saved.read_text("utf-8")), annotation)
        self.assertEqual(annotation["evidence_type"], "manual_visual_annotation")

    def test_prepare_failure_publishes_no_partial_directory(self):
        from videoactagent.trajectory_observe import prepare_session

        output = self.root / "observer"
        with self.assertRaisesRegex(ValueError, "frame index"):
            prepare_session(
                self.video,
                self.trajectory,
                "actor_track",
                output,
                (0, 99),
            )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".observer.*.tmp")), [])

    def test_source_video_change_during_decode_fails_without_publishing(self):
        import videoactagent.trajectory_observe as observer

        replacement = self.root / "replacement.mp4"
        _write_real_video(replacement, frame_count=4)
        original_decode = observer._decode_selected_frames

        def decode_then_change(video_path, frame_indices, frame_dir):
            result = original_decode(video_path, frame_indices, frame_dir)
            shutil.copyfile(replacement, self.video)
            return result

        output = self.root / "changed_source"
        with patch.object(
            observer, "_decode_selected_frames", side_effect=decode_then_change
        ):
            with self.assertRaisesRegex(ValueError, "changed during observation"):
                observer.prepare_session(
                    self.video, self.trajectory, "actor_track", output, (0, 4)
                )
        self.assertFalse(output.exists())

    def test_save_rejects_incomplete_points_without_mutating_existing_annotation(self):
        from videoactagent.trajectory_observe import prepare_session, save_annotation

        output = self.root / "observer"
        manifest = prepare_session(
            self.video, self.trajectory, "actor_track", output, (0, 2, 4)
        )
        saved = output / "manual_annotation.json"
        saved.write_bytes(b"preserve-me")
        with self.assertRaisesRegex(ValueError, "exactly one point"):
            save_annotation(
                output,
                {
                    "schema_version": "0.1",
                    "evidence_type": "manual_visual_annotation",
                    "scene_id": "scene",
                    "shot_id": "shot",
                    "track_id": "actor_track",
                    "video_sha256": manifest["video_sha256"],
                    "trajectory_file_sha256": manifest["trajectory_file_sha256"],
                    "points": [
                        {"frame": 0, "x": 0.1, "y": 0.5, "visible": True}
                    ],
                },
            )
        self.assertEqual(saved.read_bytes(), b"preserve-me")

    def test_save_rejects_unknown_fields_bool_numbers_and_nonfinite_values(self):
        from videoactagent.trajectory_observe import prepare_session, save_annotation

        output = self.root / "observer"
        manifest = prepare_session(
            self.video, self.trajectory, "actor_track", output, (0, 2, 4)
        )
        base = {
            "schema_version": "0.1",
            "evidence_type": "manual_visual_annotation",
            "scene_id": "scene",
            "shot_id": "shot",
            "track_id": "actor_track",
            "video_sha256": manifest["video_sha256"],
            "trajectory_file_sha256": manifest["trajectory_file_sha256"],
            "points": [
                {"frame": 0, "x": 0.1, "y": 0.5, "visible": True},
                {"frame": 2, "x": 0.5, "y": 0.5, "visible": True},
                {"frame": 4, "x": 0.9, "y": 0.5, "visible": True},
            ],
        }
        mutations = []
        unknown = json.loads(json.dumps(base))
        unknown["points"][0]["unknown"] = 1
        mutations.append(unknown)
        bool_number = json.loads(json.dumps(base))
        bool_number["points"][0]["x"] = True
        mutations.append(bool_number)
        nonfinite = json.loads(json.dumps(base))
        nonfinite["points"][0]["y"] = float("inf")
        mutations.append(nonfinite)
        huge = json.loads(json.dumps(base))
        huge["points"][0]["x"] = 10**400
        mutations.append(huge)
        for payload in mutations:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    save_annotation(output, payload)
        self.assertFalse((output / "manual_annotation.json").exists())

    def test_prepare_rejects_output_alias_or_escape(self):
        from videoactagent.trajectory_observe import prepare_session

        with self.assertRaisesRegex(ValueError, "collides"):
            prepare_session(
                self.video,
                self.trajectory,
                "actor_track",
                self.video,
                (0,),
                workspace=self.root,
            )

    def test_relative_output_is_resolved_below_explicit_workspace(self):
        from videoactagent.trajectory_observe import prepare_session

        prepare_session(
            self.video,
            self.trajectory,
            "actor_track",
            Path("relative_observer"),
            (0, 4),
            workspace=self.root,
        )
        self.assertTrue((self.root / "relative_observer" / "session_manifest.json").is_file())

    def test_invalid_server_port_fails_before_publishing_session(self):
        from videoactagent.trajectory_observe import main

        result = main(
            [
                "serve",
                "--video",
                str(self.video),
                "--trajectory",
                str(self.trajectory),
                "--track",
                "actor_track",
                "--output-dir",
                "invalid_port",
                "--frames",
                "0,4",
                "--workspace",
                str(self.root),
                "--port",
                "0",
            ]
        )
        self.assertEqual(result, 2)
        self.assertFalse((self.root / "invalid_port").exists())

    def test_real_localhost_http_session_and_annotation_round_trip(self):
        from http.server import ThreadingHTTPServer
        from videoactagent.trajectory_observe import _handler, prepare_session

        output = self.root / "http_observer"
        manifest = prepare_session(
            self.video, self.trajectory, "actor_track", output, (0, 2, 4)
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(output))
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/session", timeout=5) as response:
                served = json.loads(response.read().decode("utf-8"))
            self.assertEqual(served["video_sha256"], manifest["video_sha256"])
            with urllib.request.urlopen(
                base + "/" + served["frames"][0]["path"], timeout=5
            ) as response:
                served_frame = response.read()
            self.assertEqual(
                hashlib.sha256(served_frame).hexdigest(),
                served["frames"][0]["sha256"],
            )
            payload = {
                "schema_version": "0.1",
                "evidence_type": "manual_visual_annotation",
                "scene_id": "scene",
                "shot_id": "shot",
                "track_id": "actor_track",
                "video_sha256": manifest["video_sha256"],
                "trajectory_file_sha256": manifest["trajectory_file_sha256"],
                "points": [
                    {"frame": 0, "x": 0.1, "y": 0.5, "visible": True},
                    {"frame": 2, "x": 0.5, "y": 0.5, "visible": True},
                    {"frame": 4, "x": 0.9, "y": 0.5, "visible": True},
                ],
            }
            request = urllib.request.Request(
                base + "/annotation",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                saved = json.loads(response.read().decode("utf-8"))
            self.assertEqual(saved["evidence_type"], "manual_visual_annotation")
            self.assertTrue((output / "manual_annotation.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_http_rejects_dns_rebinding_csrf_and_huge_numbers_with_json_errors(self):
        from http.server import ThreadingHTTPServer
        from videoactagent.trajectory_observe import _handler, prepare_session

        output = self.root / "secure_http_observer"
        manifest = prepare_session(
            self.video, self.trajectory, "actor_track", output, (0, 2, 4)
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(output))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        valid_payload = {
            "schema_version": "0.1",
            "evidence_type": "manual_visual_annotation",
            "scene_id": "scene",
            "shot_id": "shot",
            "track_id": "actor_track",
            "video_sha256": manifest["video_sha256"],
            "trajectory_file_sha256": manifest["trajectory_file_sha256"],
            "points": [
                {"frame": 0, "x": 0.1, "y": 0.5, "visible": True},
                {"frame": 2, "x": 0.5, "y": 0.5, "visible": True},
                {"frame": 4, "x": 0.9, "y": 0.5, "visible": True},
            ],
        }

        def rejected(request: urllib.request.Request, expected_code: str) -> None:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(
                caught.exception.headers.get_content_type(), "application/json"
            )
            body = json.loads(caught.exception.read().decode("utf-8"))
            self.assertEqual(body["error"]["code"], expected_code)

        try:
            rejected(
                urllib.request.Request(
                    base + "/session", headers={"Host": "evil.example"}
                ),
                "invalid_host",
            )
            rejected(
                urllib.request.Request(
                    base + "/annotation",
                    data=json.dumps(valid_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                    },
                    method="POST",
                ),
                "invalid_origin",
            )
            huge_payload = json.loads(json.dumps(valid_payload))
            huge_payload["points"][0]["x"] = 10**400
            rejected(
                urllib.request.Request(
                    base + "/annotation",
                    data=json.dumps(huge_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                "invalid_annotation",
            )
            with urllib.request.urlopen(base + "/session", timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.assertFalse((output / "manual_annotation.json").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_observer_page_gates_async_frame_loading_and_saving(self):
        text = (ROOT / "static" / "trajectory_observer.html").read_text("utf-8")
        for required in (
            "let loading",
            "let saving",
            "loadToken",
            "image.onerror",
            "loading || saving || !ready",
            "saving = true",
            "let displayedFrame",
            "let ready",
            "!ready",
            "displayedFrame !== frameRecord()",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_observer_page_requires_every_frame_and_explicit_occlusion(self):
        page = ROOT / "static" / "trajectory_observer.html"
        self.assertTrue(page.is_file())
        text = page.read_text(encoding="utf-8")
        self.assertIn("manual_visual_annotation", text)
        self.assertIn("Mark occluded", text)
        self.assertIn("/annotation", text)
        self.assertIn("/session", text)

    def test_real_frame_extraction_emits_no_resource_warning(self):
        from videoactagent.trajectory_observe import prepare_session

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            prepare_session(
                self.video,
                self.trajectory,
                "actor_track",
                self.root / "warning_check",
                (0, 2, 4),
            )
        self.assertEqual(
            [item for item in caught if item.category is ResourceWarning], []
        )
        with self.assertRaisesRegex(ValueError, "workspace"):
            prepare_session(
                self.video,
                self.trajectory,
                "actor_track",
                self.root.parent / "escape",
                (0,),
                workspace=self.root,
            )


class MetricTests(unittest.TestCase):
    def test_partial_track_endpoint_is_sampled_at_requested_endpoint_time(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        requested = [
            {"t": 0.2, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 0.8, "x": 1.0, "y": 0.5, "visible": True},
        ]
        observed = [
            {"t": 0.2, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 0.8, "x": 1.0, "y": 0.5, "visible": True},
        ]
        result = evaluate_tracks(requested, observed, sample_count=5)
        self.assertEqual(result["aggregate"]["endpoint_t"], 0.8)
        self.assertEqual(result["aggregate"]["endpoint_status"], "compared")
        self.assertIsNone(result["aggregate"]["endpoint_reason"])
        self.assertEqual(result["aggregate"]["endpoint_error"], 0.0)

    def test_occluded_requested_endpoint_reports_none_with_reason(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        requested = [
            {"t": 0.2, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 0.8, "x": 1.0, "y": 0.5, "visible": True},
        ]
        observed = [
            {"t": 0.2, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 0.8, "x": None, "y": None, "visible": False},
        ]
        result = evaluate_tracks(requested, observed, sample_count=5)
        self.assertEqual(result["aggregate"]["endpoint_t"], 0.8)
        self.assertEqual(result["aggregate"]["endpoint_status"], "occluded")
        self.assertEqual(
            result["aggregate"]["endpoint_reason"],
            "observed_endpoint_occluded",
        )
        self.assertIsNone(result["aggregate"]["endpoint_error"])

    def test_single_sample_static_track_is_supported(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        requested = [
            {"t": 0.0, "x": 0.3, "y": 0.4, "visible": True},
        ]
        observed = [
            {"t": 0.0, "x": 0.3, "y": 0.4, "visible": True},
        ]
        result = evaluate_tracks(requested, observed, sample_count=1)
        self.assertEqual([sample["t"] for sample in result["samples"]], [0.0])
        self.assertEqual(result["aggregate"]["endpoint_error"], 0.0)
        self.assertIsNone(result["aggregate"]["direction_match"])
        self.assertEqual(result["dtw"]["normalized_cost"], 0.0)

    def test_fixed_arrays_resample_and_report_all_metrics(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        requested = [
            {"t": 0.0, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 1.0, "x": 1.0, "y": 0.5, "visible": True},
        ]
        observed = [
            {"t": 0.0, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 0.5, "x": 0.4, "y": 0.5, "visible": True},
            {"t": 1.0, "x": 0.8, "y": 0.5, "visible": True},
        ]
        result = evaluate_tracks(requested, observed, sample_count=5)

        self.assertEqual([item["t"] for item in result["samples"]], [0, .25, .5, .75, 1])
        self.assertAlmostEqual(result["aggregate"]["endpoint_error"], 0.2)
        self.assertAlmostEqual(
            result["aggregate"]["normalized_endpoint_error"], 0.2 / math.sqrt(2)
        )
        self.assertAlmostEqual(result["aggregate"]["direction_cosine"], 1.0)
        self.assertTrue(result["aggregate"]["direction_match"])
        self.assertIsNone(result["aggregate"]["arrival_error"])
        self.assertIsNone(result["aggregate"]["observed_arrival_t"])
        self.assertAlmostEqual(result["dtw"]["normalized_cost"], 0.1 / math.sqrt(2))
        self.assertTrue(result["dtw"]["path"])
        self.assertEqual(len(result["dtw"]["distance_matrix"]), 5)

    def test_arrival_error_is_time_difference_to_requested_endpoint_region(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        requested = [
            {"t": 0.0, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 1.0, "x": 1.0, "y": 0.5, "visible": True},
        ]
        observed = [
            {"t": 0.0, "x": 0.0, "y": 0.5, "visible": True},
            {"t": 0.75, "x": 1.0, "y": 0.5, "visible": True},
            {"t": 1.0, "x": 1.0, "y": 0.5, "visible": True},
        ]
        result = evaluate_tracks(requested, observed, sample_count=5)
        self.assertEqual(result["aggregate"]["requested_arrival_t"], 1.0)
        self.assertEqual(result["aggregate"]["observed_arrival_t"], 0.75)
        self.assertEqual(result["aggregate"]["arrival_error"], 0.25)

    def test_occlusion_breaks_interpolation_and_is_explicit(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        requested = [
            {"t": 0.0, "x": 0.0, "y": 0.0, "visible": True},
            {"t": 1.0, "x": 1.0, "y": 0.0, "visible": True},
        ]
        observed = [
            {"t": 0.0, "x": 0.0, "y": 0.0, "visible": True},
            {"t": 0.5, "x": None, "y": None, "visible": False},
            {"t": 1.0, "x": 1.0, "y": 0.0, "visible": True},
        ]
        result = evaluate_tracks(requested, observed, sample_count=5)

        self.assertEqual(
            [item["status"] for item in result["samples"]],
            ["compared", "occluded_gap", "occluded", "occluded_gap", "compared"],
        )
        self.assertEqual(result["aggregate"]["compared_sample_count"], 2)
        self.assertEqual(result["aggregate"]["occluded_sample_count"], 3)

    def test_rejects_unknown_nonfinite_and_bool_numbers(self):
        from videoactagent.trajectory_eval import evaluate_tracks

        valid = [
            {"t": 0.0, "x": 0.0, "y": 0.0, "visible": True},
            {"t": 1.0, "x": 1.0, "y": 0.0, "visible": True},
        ]
        for point in (
            {"t": 0.0, "x": 0.0, "y": 0.0, "visible": True, "extra": 1},
            {"t": True, "x": 0.0, "y": 0.0, "visible": True},
            {"t": 0.0, "x": float("nan"), "y": 0.0, "visible": True},
            {"t": 0.0, "x": 10**400, "y": 0.0, "visible": True},
        ):
            with self.subTest(point=point):
                with self.assertRaises(ValueError):
                    evaluate_tracks([point, valid[1]], valid, sample_count=3)
        with self.assertRaises(ValueError):
            evaluate_tracks(valid, valid, sample_count=10**400)


class EvaluatorProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.video = self.root / "real.mp4"
        self.trajectory = self.root / "trajectory.json"
        _write_real_video(self.video)
        _trajectory(self.trajectory)

        from videoactagent.trajectory_observe import prepare_session, save_annotation

        self.session = self.root / "observer"
        manifest = prepare_session(
            self.video, self.trajectory, "actor_track", self.session, (0, 2, 4)
        )
        save_annotation(
            self.session,
            {
                "schema_version": "0.1",
                "evidence_type": "manual_visual_annotation",
                "scene_id": "scene",
                "shot_id": "shot",
                "track_id": "actor_track",
                "video_sha256": manifest["video_sha256"],
                "trajectory_file_sha256": manifest["trajectory_file_sha256"],
                "points": [
                    {"frame": 0, "x": 0.1, "y": 0.5, "visible": True},
                    {"frame": 2, "x": 0.5, "y": 0.5, "visible": True},
                    {"frame": 4, "x": 0.9, "y": 0.5, "visible": True},
                ],
            },
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _evaluate(self):
        from videoactagent.trajectory_eval import evaluate_files

        return evaluate_files(
            trajectory_path=self.trajectory,
            video_path=self.video,
            session_manifest_path=self.session / "session_manifest.json",
            annotation_path=self.session / "manual_annotation.json",
        )

    def test_evaluator_binds_actual_video_frames_shot_and_track(self):
        report = self._evaluate()
        self.assertEqual(report["evidence_type"], "manual_visual_trajectory_evaluation")
        self.assertEqual(report["provenance"]["video_sha256"], _sha(self.video))
        self.assertEqual(report["shot_id"], "shot")
        self.assertEqual(report["track_id"], "actor_track")
        self.assertEqual(report["metrics"]["aggregate"]["compared_sample_count"], 5)

    def test_rejects_video_frame_shot_track_or_trajectory_provenance_mismatch(self):
        cases = [
            ("annotation", "video_sha256", "0" * 64, "video SHA"),
            ("manifest", "shot_id", "other", "shot"),
            ("annotation", "track_id", "other", "track"),
            ("annotation", "trajectory_file_sha256", "0" * 64, "trajectory"),
        ]
        for location, field, value, message in cases:
            with self.subTest(field=field):
                path = self.session / (
                    "manual_annotation.json" if location == "annotation" else "session_manifest.json"
                )
                original = path.read_bytes()
                document = json.loads(original)
                document[field] = value
                path.write_text(json.dumps(document), encoding="utf-8")
                annotation_path = self.session / "manual_annotation.json"
                annotation_original = annotation_path.read_bytes() if location == "manifest" else None
                if location == "manifest":
                    assert annotation_original is not None
                    annotation_document = json.loads(annotation_original)
                    annotation_document["session_manifest_sha256"] = _sha(path)
                    annotation_path.write_text(json.dumps(annotation_document), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        self._evaluate()
                finally:
                    path.write_bytes(original)
                    if annotation_original is not None:
                        annotation_path.write_bytes(annotation_original)

        manifest_path = self.session / "session_manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        frame_path = self.session / manifest["frames"][0]["path"]
        original_frame = frame_path.read_bytes()
        frame_path.write_bytes(original_frame + b"tamper")
        with self.assertRaisesRegex(ValueError, "frame SHA"):
            self._evaluate()

    def test_duplicate_json_keys_are_rejected(self):
        annotation = self.session / "manual_annotation.json"
        annotation.write_text(
            '{"schema_version":"0.1","schema_version":"0.1"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self._evaluate()

    def test_rejects_unknown_nested_fields_bool_metadata_and_frame_path_escape(self):
        manifest_path = self.session / "session_manifest.json"
        annotation_path = self.session / "manual_annotation.json"
        cases = []
        unknown = json.loads(manifest_path.read_text("utf-8"))
        unknown["frames"][0]["unknown"] = 1
        cases.append((unknown, "unknown fields"))
        bool_metadata = json.loads(manifest_path.read_text("utf-8"))
        bool_metadata["video_fps"] = True
        cases.append((bool_metadata, "video_fps"))
        huge_fps = json.loads(manifest_path.read_text("utf-8"))
        huge_fps["video_fps"] = 10**400
        cases.append((huge_fps, "video_fps"))
        huge_frame_time = json.loads(manifest_path.read_text("utf-8"))
        huge_frame_time["frames"][1]["time_seconds"] = 10**400
        cases.append((huge_frame_time, "time_seconds"))
        false_frame_count = json.loads(manifest_path.read_text("utf-8"))
        false_frame_count["video_frame_count"] = 6
        cases.append((false_frame_count, "frame-count"))
        escape = json.loads(manifest_path.read_text("utf-8"))
        escape["frames"][0]["path"] = "../escape.png"
        cases.append((escape, "escapes"))
        original_manifest = manifest_path.read_bytes()
        original_annotation = annotation_path.read_bytes()
        for manifest, message in cases:
            with self.subTest(message=message):
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                annotation = json.loads(original_annotation)
                annotation["session_manifest_sha256"] = _sha(manifest_path)
                annotation_path.write_text(json.dumps(annotation), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        self._evaluate()
                finally:
                    manifest_path.write_bytes(original_manifest)
                    annotation_path.write_bytes(original_annotation)

    def test_rejects_a_rehashed_frame_not_actually_decoded_from_bound_video(self):
        from PIL import Image

        manifest_path = self.session / "session_manifest.json"
        annotation_path = self.session / "manual_annotation.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        annotation = json.loads(annotation_path.read_text("utf-8"))
        frame_path = self.session / manifest["frames"][0]["path"]
        with Image.open(frame_path) as source:
            changed = source.copy()
        changed.putpixel((0, 0), (255, 0, 255))
        changed.save(frame_path, format="PNG", optimize=False)
        replacement_sha = _sha(frame_path)
        manifest["frames"][0]["sha256"] = replacement_sha
        annotation["points"][0]["frame_sha256"] = replacement_sha
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        annotation["session_manifest_sha256"] = _sha(manifest_path)
        annotation_path.write_text(json.dumps(annotation), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "decoded frame provenance"):
            self._evaluate()

    def test_frame_timing_is_strictly_bound_to_redecoded_video(self):
        manifest_path = self.session / "session_manifest.json"
        annotation_path = self.session / "manual_annotation.json"
        original_manifest = manifest_path.read_bytes()
        original_annotation = annotation_path.read_bytes()
        for field, value, message in (
            ("t", 0.25, "frame t provenance"),
            ("time_seconds", 0.125, "frame time_seconds provenance"),
        ):
            with self.subTest(field=field):
                manifest = json.loads(original_manifest)
                annotation = json.loads(original_annotation)
                manifest["frames"][1][field] = value
                annotation["points"][1][field] = value
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                annotation["session_manifest_sha256"] = _sha(manifest_path)
                annotation_path.write_text(json.dumps(annotation), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        self._evaluate()
                finally:
                    manifest_path.write_bytes(original_manifest)
                    annotation_path.write_bytes(original_annotation)

    def test_detects_every_source_changed_mid_evaluation(self):
        import videoactagent.trajectory_eval as evaluator

        manifest_path = self.session / "session_manifest.json"
        annotation_path = self.session / "manual_annotation.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        frame_path = self.session / manifest["frames"][0]["path"]
        replacement_video = self.root / "replacement.mp4"
        _write_real_video(replacement_video, frame_count=4)
        cases = (
            (annotation_path, lambda path: path.write_bytes(path.read_bytes() + b"\n")),
            (manifest_path, lambda path: path.write_bytes(path.read_bytes() + b"\n")),
            (self.trajectory, lambda path: path.write_bytes(path.read_bytes() + b"\n")),
            (self.video, lambda path: shutil.copyfile(replacement_video, path)),
            (frame_path, lambda path: path.write_bytes(path.read_bytes() + b"tamper")),
            (frame_path, lambda path: path.unlink()),
        )
        real_evaluate_tracks = evaluator.evaluate_tracks
        for source, mutate in cases:
            with self.subTest(source=source.name):
                original = source.read_bytes()

                def mutate_then_evaluate(*args, **kwargs):
                    mutate(source)
                    return real_evaluate_tracks(*args, **kwargs)

                try:
                    with patch.object(
                        evaluator,
                        "evaluate_tracks",
                        side_effect=mutate_then_evaluate,
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "changed during evaluation"
                        ):
                            self._evaluate()
                finally:
                    source.write_bytes(original)

    def test_evaluator_cli_writes_atomically_and_rejects_input_collision(self):
        from videoactagent.trajectory_eval import main

        output = self.root / "evaluation.json"
        arguments = [
            "evaluate",
            "--trajectory",
            str(self.trajectory),
            "--video",
            str(self.video),
            "--session-manifest",
            str(self.session / "session_manifest.json"),
            "--annotation",
            str(self.session / "manual_annotation.json"),
            "--output",
            str(output),
            "--workspace",
            str(self.root),
        ]
        self.assertEqual(main(arguments), 0)
        self.assertEqual(
            json.loads(output.read_text("utf-8"))["evidence_type"],
            "manual_visual_trajectory_evaluation",
        )
        collision = arguments.copy()
        collision[collision.index(str(output))] = str(
            self.session / "manual_annotation.json"
        )
        original = (self.session / "manual_annotation.json").read_bytes()
        self.assertEqual(main(collision), 2)
        self.assertEqual((self.session / "manual_annotation.json").read_bytes(), original)

        alias = self.root / "annotation_hardlink.json"
        os.link(self.session / "manual_annotation.json", alias)
        hardlink = arguments.copy()
        hardlink[hardlink.index(str(output))] = str(alias)
        self.assertEqual(main(hardlink), 2)
        self.assertTrue(os.path.samefile(alias, self.session / "manual_annotation.json"))

        manifest = json.loads(
            (self.session / "session_manifest.json").read_text("utf-8")
        )
        frame_sources = [self.session / item["path"] for item in manifest["frames"]]
        original_frame_bytes = {path: path.read_bytes() for path in frame_sources}

        direct_frame = arguments.copy()
        direct_frame[direct_frame.index(str(output))] = str(frame_sources[0])
        self.assertEqual(main(direct_frame), 2)
        self.assertEqual(frame_sources[0].read_bytes(), original_frame_bytes[frame_sources[0]])

        frame_alias = self.root / "frame_hardlink.png"
        os.link(frame_sources[1], frame_alias)
        hardlink_frame = arguments.copy()
        hardlink_frame[hardlink_frame.index(str(output))] = str(frame_alias)
        self.assertEqual(main(hardlink_frame), 2)
        self.assertTrue(os.path.samefile(frame_alias, frame_sources[1]))

        output_directory = self.root / "existing_output_directory"
        output_directory.mkdir()
        sentinel = output_directory / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        directory_output = arguments.copy()
        directory_output[directory_output.index(str(output))] = str(output_directory)
        self.assertEqual(main(directory_output), 2)
        self.assertEqual(sentinel.read_text("utf-8"), "preserve")

        for path, expected in original_frame_bytes.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_evaluator_cli_rejects_symlink_alias_of_source_frame_when_available(self):
        from videoactagent.trajectory_eval import main

        manifest = json.loads(
            (self.session / "session_manifest.json").read_text("utf-8")
        )
        frame = self.session / manifest["frames"][0]["path"]
        alias = self.root / "frame_symlink.png"
        try:
            alias.symlink_to(frame)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        original = frame.read_bytes()
        result = main(
            [
                "evaluate",
                "--trajectory",
                str(self.trajectory),
                "--video",
                str(self.video),
                "--session-manifest",
                str(self.session / "session_manifest.json"),
                "--annotation",
                str(self.session / "manual_annotation.json"),
                "--output",
                str(alias),
                "--workspace",
                str(self.root),
            ]
        )
        self.assertEqual(result, 2)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(frame.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
