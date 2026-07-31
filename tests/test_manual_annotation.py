"""Tests for the comparison-oriented, human-only annotation workspace.

The temporary videos are genuinely encoded and decoded.  The tests verify
annotation mechanics and provenance; they do not create or claim human labels.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import imageio_ffmpeg


ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_real_video(path: Path, frame_count: int, fps: int) -> None:
    width, height = 16, 12
    pixels = bytearray()
    for frame in range(frame_count):
        for y in range(height):
            for x in range(width):
                pixels.extend(((frame * 31 + x) % 256, (y * 17) % 256, 70))
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        input=bytes(pixels),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))


def _shotscript(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scene_id": "story_a",
                "shots": [
                    {
                        "shot_id": "whole",
                        "actors": [{"id": "actor_a"}, {"id": "actor_b"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class ManualAnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run = self.root / "full_chain"
        self.index = self.run / "final_results" / "full_result_index.json"
        self.proxy = (
            self.run
            / "source_evidence"
            / "whole_story_v4"
            / "story_a"
            / "proxy.mp4"
        )
        self.result = self.run / "jobs" / "story_a__kling" / "result.mp4"
        self.proxy.parent.mkdir(parents=True)
        self.result.parent.mkdir(parents=True)
        self.index.parent.mkdir(parents=True)
        _write_real_video(self.proxy, frame_count=15, fps=3)
        _write_real_video(self.result, frame_count=121, fps=24)
        _shotscript(self.proxy.parent / "sources" / "shotscript.json")
        self.index.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "rows": [
                        {
                            "job_id": "story_a__kling",
                            "story_id": "story_a",
                            "backend": "kling",
                            "status": "success",
                            "video_path": "jobs/story_a__kling/result.mp4",
                            "sha256": _sha(self.result),
                        },
                        {
                            "job_id": "story_a__seedance",
                            "story_id": "story_a",
                            "backend": "seedance",
                            "status": "failed",
                            "error": "provider rejected request",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_normalized_timepoints_map_to_real_frame_count(self) -> None:
        from videoactagent.manual_annotation import (
            NORMALIZED_TIMEPOINTS,
            frame_indices_for_count,
        )

        self.assertEqual(
            NORMALIZED_TIMEPOINTS,
            (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0),
        )
        self.assertEqual(frame_indices_for_count(15), (0, 2, 4, 5, 7, 9, 10, 12, 14))
        self.assertEqual(frame_indices_for_count(121), (0, 15, 30, 45, 60, 75, 90, 105, 120))
        self.assertEqual(frame_indices_for_count(81), (0, 10, 20, 30, 40, 50, 60, 70, 80))

    def test_prepare_builds_one_real_reference_and_preserves_failed_item(self) -> None:
        from videoactagent.manual_annotation import prepare_workspace

        output = self.root / "annotation"
        session = prepare_workspace(self.index, output, workspace=self.root)

        self.assertEqual(list(session["references"]), ["story_a"])
        reference = session["references"]["story_a"]
        self.assertEqual(reference["video"]["frame_count"], 15)
        self.assertEqual(reference["video"]["fps"], 3.0)
        self.assertEqual(reference["video"]["duration_seconds"], 5.0)
        self.assertEqual(reference["video"]["sha256"], _sha(self.proxy))
        self.assertEqual(
            [frame["frame"] for frame in reference["video"]["frames"]],
            [0, 2, 4, 5, 7, 9, 10, 12, 14],
        )
        self.assertEqual(reference["actor_ids"], ["actor_a", "actor_b"])
        success, failure = session["items"]
        self.assertFalse(success["disabled"])
        self.assertEqual(success["reference_id"], "story_a")
        self.assertEqual(success["video"]["frame_count"], 121)
        self.assertEqual(success["video"]["fps"], 24.0)
        self.assertEqual(success["video"]["duration_seconds"], 5.04)
        self.assertTrue(failure["disabled"])
        self.assertEqual(failure["status"], "failed")
        self.assertNotIn("video", failure)
        self.assertFalse((output / "annotations").exists())

    def test_success_video_hash_must_match_real_bytes(self) -> None:
        from videoactagent.manual_annotation import prepare_workspace

        value = json.loads(self.index.read_text(encoding="utf-8"))
        value["rows"][0]["sha256"] = "0" * 64
        self.index.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "result video SHA-256 mismatch"):
            prepare_workspace(self.index, self.root / "annotation", workspace=self.root)

    def test_prepare_rejects_path_like_job_id_without_overwriting_any_manifest(self) -> None:
        from videoactagent.manual_annotation import prepare_workspace

        sentinel = self.root / "session_manifest.json"
        sentinel.write_bytes(b"sentinel-manifest\n")
        before_index = self.index.read_bytes()
        value = json.loads(before_index)
        value["rows"][0]["job_id"] = "../../../session_manifest.json"
        self.index.write_text(json.dumps(value), encoding="utf-8")
        malicious_index = self.index.read_bytes()
        output = self.root / "annotation"

        with self.assertRaisesRegex(ValueError, "job_id must be a safe token"):
            prepare_workspace(self.index, output, workspace=self.root)

        self.assertEqual(self.index.read_bytes(), malicious_index)
        self.assertEqual(sentinel.read_bytes(), b"sentinel-manifest\n")
        self.assertFalse((output / "session_manifest.json").exists())

    def test_save_defensively_rejects_path_like_target_id(self) -> None:
        from videoactagent.manual_annotation import prepare_workspace, save_annotation

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        payload = self._payload(session, "reference", "story_a", "draft")
        payload["target_id"] = "../story_a"
        with self.assertRaisesRegex(ValueError, "target_id must be a safe token"):
            save_annotation(session_dir, payload)

    def test_result_requires_hash_bound_reviewed_reference_annotation(self) -> None:
        from videoactagent.manual_annotation import save_annotation

        session_dir = self.root / "annotation"
        session = __import__(
            "videoactagent.manual_annotation", fromlist=["prepare_workspace"]
        ).prepare_workspace(self.index, session_dir, workspace=self.root)

        reference_payload = self._payload(session, "reference", "story_a", "reviewed")
        reference = save_annotation(session_dir, reference_payload)
        reference_path = session_dir / "annotations" / "reference__story_a.json"
        reference_sha = _sha(reference_path)
        self.assertTrue(reference["eligible_for_paper"])

        result_payload = self._payload(
            session, "result", "story_a__kling", "reviewed", reference_sha
        )
        result_payload["actors"]["actor_a"][0]["identity"] = "ambiguous"
        result = save_annotation(session_dir, result_payload)
        self.assertEqual(result["reference_annotation_sha256"], reference_sha)
        self.assertFalse(result["actors"]["actor_a"][0]["geometry_eligible"])
        self.assertTrue(result["actors"]["actor_b"][0]["geometry_eligible"])
        self.assertEqual(result["camera"]["category"], "pan_left")
        self.assertNotIn("pose", result["camera"])
        self.assertTrue(result["eligible_for_paper"])

        wrong = self._payload(
            session, "result", "story_a__kling", "reviewed", "f" * 64
        )
        with self.assertRaisesRegex(ValueError, "reference annotation SHA-256 mismatch"):
            save_annotation(session_dir, wrong)

        forbidden_pose = self._payload(
            session, "result", "story_a__kling", "reviewed", reference_sha
        )
        forbidden_pose["camera"]["pose"] = [0, 0, 0]
        with self.assertRaisesRegex(ValueError, "category, intensity, and confidence only"):
            save_annotation(session_dir, forbidden_pose)

    def test_draft_is_saved_but_excluded_from_paper_statistics(self) -> None:
        from videoactagent.manual_annotation import prepare_workspace, save_annotation

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        annotation = save_annotation(
            session_dir, self._payload(session, "reference", "story_a", "draft")
        )
        self.assertFalse(annotation["eligible_for_paper"])

    def test_reviewed_reference_snapshot_survives_current_file_becoming_draft(self) -> None:
        from videoactagent.manual_annotation import (
            annotation_is_paper_eligible,
            prepare_workspace,
            save_annotation,
        )

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        reviewed = save_annotation(
            session_dir, self._payload(session, "reference", "story_a", "reviewed")
        )
        current_reference = session_dir / "annotations" / "reference__story_a.json"
        reviewed_sha = _sha(current_reference)
        snapshot = session_dir / "annotations" / "by_sha256" / (reviewed_sha + ".json")
        self.assertTrue(snapshot.is_file())
        self.assertEqual(_sha(snapshot), reviewed_sha)

        result = save_annotation(
            session_dir,
            self._payload(
                session, "result", "story_a__kling", "reviewed", reviewed_sha
            ),
        )
        result_path = session_dir / "annotations" / "result__story_a__kling.json"
        self.assertTrue(result["eligible_for_paper"])
        self.assertTrue(annotation_is_paper_eligible(session_dir, result_path))

        save_annotation(
            session_dir, self._payload(session, "reference", "story_a", "draft")
        )
        self.assertEqual(
            json.loads(current_reference.read_text(encoding="utf-8"))["annotation_status"],
            "draft",
        )
        self.assertEqual(_sha(snapshot), reviewed_sha)
        self.assertTrue(annotation_is_paper_eligible(session_dir, result_path))
        self.assertEqual(reviewed["annotation_status"], "reviewed")

    def test_html_avoids_template_literals_and_exposes_required_controls(self) -> None:
        html = (ROOT / "static" / "manual_annotation.html").read_text(encoding="utf-8")
        self.assertNotIn("`", html)
        for token in (
            "visibility",
            "identity",
            "camera-category",
            "camera-intensity",
            "camera-confidence",
            "annotation-status",
            "/annotation",
        ):
            self.assertIn(token, html)

    def test_cli_dispatches_annotate_help(self) -> None:
        import io
        from contextlib import redirect_stdout

        from videoactagent.cli import main

        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["annotate", "--help"])
        self.assertEqual(result, 0)
        self.assertIn("prepare", output.getvalue())
        self.assertIn("serve", output.getvalue())

    def test_server_is_localhost_only_and_rejects_frame_path_escape(self) -> None:
        from http.server import ThreadingHTTPServer

        from videoactagent.manual_annotation import _handler, prepare_workspace

        session_dir = self.root / "annotation"
        prepare_workspace(self.index, session_dir, workspace=self.root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(session_dir))
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_address[1])
            with urllib.request.urlopen(base + "/session", timeout=5) as response:
                self.assertEqual(response.status, 200)
            request = urllib.request.Request(base + "/frames/%2e%2e/session_manifest.json")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 400)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _payload(
        self,
        session: dict,
        target_type: str,
        target_id: str,
        status: str,
        reference_sha: str | None = None,
    ) -> dict:
        if target_type == "reference":
            target = session["references"][target_id]
        else:
            target = next(item for item in session["items"] if item["job_id"] == target_id)
        frames = target["video"]["frames"]
        actors = {}
        for actor_id in target["actor_ids"]:
            actors[actor_id] = [
                {
                    "frame": frame["frame"],
                    "t": frame["t"],
                    "footpoint": {"x": 0.25, "y": 0.75},
                    "visibility": "visible",
                    "identity": "confirmed",
                }
                for frame in frames
            ]
        return {
            "schema_version": "1.0",
            "target_type": target_type,
            "target_id": target_id,
            "annotation_status": status,
            "reference_annotation_sha256": reference_sha,
            "actors": actors,
            "camera": {
                "category": "pan_left",
                "intensity": "medium",
                "confidence": 0.8,
            },
        }


if __name__ == "__main__":
    unittest.main()
