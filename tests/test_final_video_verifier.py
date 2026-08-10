from pathlib import Path
import json
import subprocess

import pytest

from pipeline_v2.final_video_verifier import aggregate_final_video_reports, verify_final_video


def test_final_video_verifier_fails_missing_media(tmp_path: Path) -> None:
    report = verify_final_video(
        tmp_path / "missing.mp4",
        expected_frame_count=120,
        expected_fps=24,
        expected_duration=5.0,
    )
    assert report["verdict"] == "failed"
    assert report["checks"][0]["status"] == "failed"


REAL_VIDEO = Path("runs/results/complex_scene_suite_20260809_085258/plaza_dance_circle_20260809_085258/sandbox/master.mp4")


def test_final_video_verifier_keeps_visual_review_pending() -> None:
    if not REAL_VIDEO.is_file():
        pytest.skip("requires the real Blender Proxy artifact from the first canonical run")
    video = REAL_VIDEO

    def probe(path: Path):
        payload = subprocess.check_output([
            r"D:\ACLOS\Cross\recorder-release\ffprobe.exe", "-v", "error",
            "-show_entries", "stream=width,height,avg_frame_rate,nb_frames:format=duration",
            "-of", "json", str(path),
        ], text=True)
        document = json.loads(payload)
        stream = document["streams"][0]
        return {"nb_frames": int(stream["nb_frames"]), "r_frame_rate": stream["avg_frame_rate"], "duration": float(document["format"]["duration"]), "width": int(stream["width"]), "height": int(stream["height"])}

    report = verify_final_video(
        video,
        expected_frame_count=120,
        expected_fps=24,
        expected_duration=5.0,
        expected_resolution=(640, 360),
        probe_video=probe,
        blackdetect_events=0,
    )
    assert report["verdict"] == "pending_review"
    assert report["video"]["sha256"]


def test_final_video_verifier_accepts_float_scene_plan_fps(tmp_path: Path) -> None:
    video = tmp_path / "seedance.mp4"
    video.write_bytes(b"real-media-placeholder-for-probe-hook")

    report = verify_final_video(
        video,
        expected_frame_count=120,
        expected_fps=24.0,
        expected_duration=5.0,
        probe_video=lambda _path: {
            "nb_frames": 121,
            "r_frame_rate": "24/1",
            "duration": 5.062,
            "width": 1280,
            "height": 720,
        },
        blackdetect_events=0,
    )

    assert report["verdict"] == "pending_review"
    assert report["checks"][1]["status"] == "passed"


def test_aggregate_final_video_reports_preserves_each_camera_and_fails_closed() -> None:
    report = aggregate_final_video_reports(
        {
            "master": {"verdict": "pending_review"},
            "lateral": {"verdict": "failed"},
        }
    )
    assert report["verdict"] == "failed"
    assert sorted(report["cameras"]) == ["lateral", "master"]
