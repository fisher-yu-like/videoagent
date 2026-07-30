"""Generated-result gates use checked-in real MP4 evidence, never fake bytes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
API_VIDEO = ROOT / "runs/trajectory_api_pilot/real/20260729T171212Z_kling_1bd8ea1f/result.mp4"
V4_PROXY = ROOT / "runs/work/whole_story_v4/station_departure/proxy.mp4"


def _expected_trajectory(path: Path) -> None:
    """Write only the expected trajectory contract; video input remains real."""

    path.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "scene_id": "station_departure",
                "shot_id": "whole",
                "coordinate_space": "normalized_0_1_top_left",
                "duration_seconds": 5.0,
                "sample_count": 11,
                "tracks": [
                    {
                        "track_id": "actor_a_track",
                        "target": {"type": "actor", "id": "actor_a"},
                        "primitive": "polyline",
                        "semantic": "move",
                        "points": [
                            {"t": 0.0, "x": 0.3, "y": 0.5, "visible": True},
                            {"t": 1.0, "x": 0.1, "y": 0.5, "visible": True},
                        ],
                    },
                    {
                        "track_id": "actor_b_track",
                        "target": {"type": "actor", "id": "actor_b"},
                        "primitive": "polyline",
                        "semantic": "move",
                        "points": [
                            {"t": 0.0, "x": 0.5, "y": 0.5, "visible": True},
                            {"t": 1.0, "x": 0.8, "y": 0.5, "visible": True},
                        ],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


class GeneratedResultTests(unittest.TestCase):
    def test_five_second_api_video_is_complete_for_five_second_story(self):
        from videoactagent.generated_result import inspect_generated_video

        report = inspect_generated_video(
            API_VIDEO, requested_duration=5.0, expected_resolution=(1280, 720)
        )

        self.assertEqual(report["status"], "media_complete")
        self.assertTrue(report["movement_scoring_allowed"])
        self.assertEqual(report["frame_count"], 121)
        self.assertEqual(report["counted_duration_seconds"], 5.04)
        self.assertEqual(report["stream_duration_seconds"], 5.04)
        self.assertEqual(report["size"], [1280, 720])
        self.assertEqual(report["key_frame_indices"], [0, 30, 60, 90, 120])
        self.assertTrue(report["key_times_decodable"])
        self.assertEqual(len(report["mp4_sha256"]), 64)

    def test_same_video_is_incomplete_for_fifteen_second_request(self):
        from videoactagent.generated_result import inspect_generated_video

        report = inspect_generated_video(
            API_VIDEO, requested_duration=15.0, expected_resolution=(1280, 720)
        )

        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["movement_scoring_allowed"])

    def test_v4_proxy_passes_profile_but_remains_unscored_without_manual_annotations(self):
        from videoactagent.generated_result import ExpectedProfile, publish_result_report

        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            job_dir.mkdir()
            _expected_trajectory(job_dir / "expected_trajectory.json")
            report = publish_result_report(
                job_dir,
                V4_PROXY,
                ExpectedProfile(
                    duration_seconds=5.0,
                    resolution=(960, 540),
                    minimum_coverage=0.95,
                    maximum_coverage=1.05,
                ),
            )

            self.assertEqual(report["status"], "complete_unscored")
            self.assertFalse(report["movement_scoring_allowed"])
            self.assertEqual(report["annotated_frame_count"], 0)
            self.assertGreater(report["interpolation_sample_count"], 0)
            self.assertTrue((job_dir / "generated_result_report.json").is_file())
            self.assertTrue((job_dir / "private_source.mp4").is_file())
            self.assertEqual(
                report["key_frame_indices"], [0, 4, 7, 10, 14]
            )
            self.assertEqual(
                {item["actor_id"] for item in report["manual_sessions"]},
                {"actor_a", "actor_b"},
            )
            for session in report["manual_sessions"]:
                manifest = json.loads(
                    (job_dir / session["path"] / "session_manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    [item["frame"] for item in manifest["frames"]], [0, 4, 7, 10, 14]
                )
            self.assertEqual(
                report["camera_assessment"]["evidence_type"],
                "heuristic_phase_correlation",
            )
            self.assertTrue(report["camera_assessment"]["manual_review_required"])

    def test_wrong_resolution_is_rejected_before_observation_publication(self):
        from videoactagent.generated_result import ExpectedProfile, publish_result_report

        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            job_dir.mkdir()
            _expected_trajectory(job_dir / "expected_trajectory.json")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                publish_result_report(
                    job_dir,
                    V4_PROXY,
                    ExpectedProfile(duration_seconds=5.0, resolution=(1280, 720)),
                )
            self.assertFalse((job_dir / "manual_sessions").exists())

    def test_zero_decoded_frame_claim_is_rejected_even_for_a_real_mp4(self):
        from videoactagent.generated_result import inspect_generated_video

        with patch(
            "videoactagent.generated_result.inspect_video",
            return_value={"frame_count": 0},
        ):
            with self.assertRaisesRegex(ValueError, "zero frames"):
                inspect_generated_video(
                    V4_PROXY, requested_duration=5.0, expected_resolution=(960, 540)
                )

    def test_missing_key_frame_makes_real_media_incomplete(self):
        from videoactagent.generated_result import inspect_generated_video

        with patch(
            "videoactagent.generated_result._decode_key_frame_indices",
            return_value={
                "indices": [0, 4, 7, 10, 14],
                "decoded_indices": [0, 4, 7, 10],
                "decodable": False,
                "missing_indices": [14],
            },
        ):
            report = inspect_generated_video(
                V4_PROXY, requested_duration=5.0, expected_resolution=(960, 540)
            )
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["movement_scoring_allowed"])

    def test_mp4_hash_change_during_observation_rejects_without_publishing(self):
        from videoactagent.generated_result import ExpectedProfile, publish_result_report
        import videoactagent.generated_result as generated_result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_dir = root / "job"
            job_dir.mkdir()
            source = root / "real_v4_proxy.mp4"
            source.write_bytes(V4_PROXY.read_bytes())
            _expected_trajectory(job_dir / "expected_trajectory.json")
            original_prepare = generated_result.prepare_session

            def prepare_then_replace(*args, **kwargs):
                manifest = original_prepare(*args, **kwargs)
                source.write_bytes(API_VIDEO.read_bytes())
                return manifest

            with patch.object(
                generated_result, "prepare_session", side_effect=prepare_then_replace
            ):
                with self.assertRaisesRegex(ValueError, "MP4 hash changed"):
                    publish_result_report(
                        job_dir,
                        source,
                        ExpectedProfile(duration_seconds=5.0, resolution=(960, 540)),
                    )
            self.assertFalse((job_dir / "generated_result_report.json").exists())
            self.assertFalse((job_dir / "manual_sessions").exists())


if __name__ == "__main__":
    unittest.main()
