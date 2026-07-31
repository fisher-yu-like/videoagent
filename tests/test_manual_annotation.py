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
from unittest import mock

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
        payload = self._payload(session, "reference", "story_a")
        payload["target_id"] = "../story_a"
        with self.assertRaisesRegex(ValueError, "target_id must be a safe token"):
            save_annotation(session_dir, payload)

    def test_result_requires_hash_bound_reviewed_reference_annotation(self) -> None:
        from videoactagent.manual_annotation import review_annotation, save_annotation

        session_dir = self.root / "annotation"
        session = __import__(
            "videoactagent.manual_annotation", fromlist=["prepare_workspace"]
        ).prepare_workspace(self.index, session_dir, workspace=self.root)

        reference_payload = self._payload(session, "reference", "story_a")
        draft_reference = save_annotation(session_dir, reference_payload)
        self.assertEqual(draft_reference["annotation_status"], "draft")
        reference = review_annotation(
            session_dir, "reference", "story_a", reviewer_id="reviewer_1"
        )
        reference_path = session_dir / "annotations" / "reference__story_a.json"
        reference_sha = _sha(reference_path)
        self.assertTrue(reference["eligible_for_paper"])
        self.assertEqual(reference["review"]["reviewer_id"], "reviewer_1")
        self.assertTrue(reference["review"]["reviewed_at_utc"].endswith("Z"))

        result_payload = self._payload(session, "result", "story_a__kling", reference_sha)
        result_payload["actors"]["actor_a"][0]["identity"] = "ambiguous"
        draft_result = save_annotation(session_dir, result_payload)
        result = review_annotation(
            session_dir, "result", "story_a__kling", reviewer_id="reviewer_2"
        )
        self.assertEqual(result["reference_annotation_sha256"], reference_sha)
        self.assertFalse(draft_result["actors"]["actor_a"][0]["geometry_eligible"])
        self.assertTrue(result["actors"]["actor_b"][0]["geometry_eligible"])
        self.assertEqual(result["camera"]["category"], "pan_left")
        self.assertNotIn("pose", result["camera"])
        self.assertTrue(result["eligible_for_paper"])

        wrong = self._payload(session, "result", "story_a__kling", "f" * 64)
        with self.assertRaisesRegex(ValueError, "reference annotation SHA-256 mismatch"):
            save_annotation(session_dir, wrong)

        forbidden_pose = self._payload(session, "result", "story_a__kling", reference_sha)
        forbidden_pose["camera"]["pose"] = [0, 0, 0]
        with self.assertRaisesRegex(ValueError, "category, intensity, and confidence only"):
            save_annotation(session_dir, forbidden_pose)

    def test_draft_is_saved_but_excluded_from_paper_statistics(self) -> None:
        from videoactagent.manual_annotation import prepare_workspace, save_annotation

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        annotation = save_annotation(
            session_dir, self._payload(session, "reference", "story_a")
        )
        self.assertFalse(annotation["eligible_for_paper"])

    def test_reviewed_reference_snapshot_survives_current_file_becoming_draft(self) -> None:
        from videoactagent.manual_annotation import (
            annotation_is_paper_eligible,
            prepare_workspace,
            review_annotation,
            save_annotation,
        )

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        save_annotation(
            session_dir, self._payload(session, "reference", "story_a")
        )
        reviewed = review_annotation(
            session_dir, "reference", "story_a", reviewer_id="reviewer_1"
        )
        current_reference = session_dir / "annotations" / "reference__story_a.json"
        reviewed_sha = _sha(current_reference)
        snapshot = session_dir / "annotations" / "by_sha256" / (reviewed_sha + ".json")
        self.assertTrue(snapshot.is_file())
        self.assertEqual(_sha(snapshot), reviewed_sha)

        save_annotation(
            session_dir,
            self._payload(session, "result", "story_a__kling", reviewed_sha),
        )
        result = review_annotation(
            session_dir, "result", "story_a__kling", reviewer_id="reviewer_2"
        )
        result_path = session_dir / "annotations" / "result__story_a__kling.json"
        self.assertTrue(result["eligible_for_paper"])
        self.assertTrue(annotation_is_paper_eligible(session_dir, result_path))

        save_annotation(
            session_dir, self._payload(session, "reference", "story_a")
        )
        self.assertEqual(
            json.loads(current_reference.read_text(encoding="utf-8"))["annotation_status"],
            "draft",
        )
        self.assertEqual(_sha(snapshot), reviewed_sha)
        self.assertTrue(annotation_is_paper_eligible(session_dir, result_path))
        self.assertEqual(reviewed["annotation_status"], "reviewed")

    def test_first_save_cannot_self_declare_reviewed_and_review_requires_identity(self) -> None:
        from videoactagent.manual_annotation import (
            prepare_workspace,
            review_annotation,
            save_annotation,
        )

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        payload = self._payload(session, "reference", "story_a")
        payload["annotation_status"] = "reviewed"
        with self.assertRaisesRegex(ValueError, "first annotation save must be draft"):
            save_annotation(session_dir, payload)
        payload["annotation_status"] = "draft"
        payload["annotator_id"] = "  "
        with self.assertRaisesRegex(ValueError, "annotator_id"):
            save_annotation(session_dir, payload)
        payload["annotator_id"] = "annotator_1"
        save_annotation(session_dir, payload)
        with self.assertRaisesRegex(ValueError, "reviewer_id"):
            review_annotation(session_dir, "reference", "story_a", reviewer_id="  ")
        with self.assertRaisesRegex(ValueError, "different from annotator_id"):
            review_annotation(
                session_dir,
                "reference",
                "story_a",
                reviewer_id="annotator_1",
            )

    def test_paper_eligibility_rejects_orphan_and_tampered_annotations(self) -> None:
        from videoactagent.manual_annotation import annotation_is_paper_eligible

        session_dir, result_path = self._reviewed_result()
        original = result_path.read_bytes()
        value = json.loads(original)
        orphan = session_dir / "annotations" / "orphan.json"
        orphan.write_bytes(original)
        self.assertFalse(annotation_is_paper_eligible(session_dir, orphan))

        manifest_path = session_dir / "session_manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest_path.write_bytes(manifest_bytes + b" ")
        self.assertFalse(annotation_is_paper_eligible(session_dir, result_path))
        manifest_path.write_bytes(manifest_bytes)

        mutations = (
            lambda item: item.__setitem__("session_manifest_sha256", "0" * 64),
            lambda item: item.__setitem__("video_sha256", "0" * 64),
            lambda item: item["actors"]["actor_a"][0].__setitem__(
                "frame_sha256", "0" * 64
            ),
            lambda item: item["actors"].pop("actor_b"),
            lambda item: item["camera"].__setitem__("pose", [0, 0, 0]),
            lambda item: item.__setitem__("annotator_id", ""),
            lambda item: item["review"].__setitem__(
                "reviewer_id", item["annotator_id"]
            ),
        )
        for mutate in mutations:
            changed = json.loads(original)
            mutate(changed)
            result_path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertFalse(annotation_is_paper_eligible(session_dir, result_path))
        result_path.write_bytes(original)

    def test_frame_tamper_blocks_serving_saving_and_eligibility(self) -> None:
        from http.server import ThreadingHTTPServer

        from videoactagent.manual_annotation import (
            _handler,
            annotation_is_paper_eligible,
            save_annotation,
        )

        session_dir, result_path = self._reviewed_result()
        manifest = json.loads(
            (session_dir / "session_manifest.json").read_text(encoding="utf-8")
        )
        frame_record = manifest["items"][0]["video"]["frames"][0]
        frame = session_dir / frame_record["path"]
        reference_sha = json.loads(result_path.read_text(encoding="utf-8"))[
            "reference_annotation_sha256"
        ]
        frame.write_bytes(b"not a png")
        self.assertFalse(annotation_is_paper_eligible(session_dir, result_path))
        with self.assertRaisesRegex(ValueError, "frame.*(SHA|PNG)"):
            save_annotation(
                session_dir,
                self._payload(manifest, "result", "story_a__kling", reference_sha),
            )

        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(session_dir))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                "http://127.0.0.1:"
                + str(server.server_address[1])
                + "/"
                + frame_record["path"]
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(url, timeout=5)
            self.assertEqual(raised.exception.code, 409)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_prepare_detects_source_index_change_during_decode(self) -> None:
        import videoactagent.manual_annotation as module

        original = module._decode_bound_video
        changed = False

        def mutate_index(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                self.index.write_bytes(self.index.read_bytes() + b" ")
            return result

        with mock.patch.object(module, "_decode_bound_video", side_effect=mutate_index):
            with self.assertRaisesRegex(ValueError, "full result index changed"):
                module.prepare_workspace(
                    self.index, self.root / "annotation", workspace=self.root
                )

    def test_html_avoids_template_literals_and_exposes_required_controls(self) -> None:
        html = (ROOT / "static" / "manual_annotation.html").read_text(encoding="utf-8")
        self.assertNotIn("`", html)
        for token in (
            "visibility",
            "identity",
            "camera-category",
            "camera-intensity",
            "camera-confidence",
            "reviewer-id",
            "review-button",
            "/annotation",
            "/review",
            "targetStates",
            "currentImage = null",
            'canvas.style.pointerEvents = "none"',
        ):
            self.assertIn(token, html)
        self.assertIn("function decisionKey(targetType, targetId", html)
        self.assertNotIn('id="annotation-status"', html)

    def test_html_loading_state_guards_every_annotation_control(self) -> None:
        html = (ROOT / "static" / "manual_annotation.html").read_text(encoding="utf-8")
        for selector in (
            "#visibility",
            "#identity",
            "#actor",
            "#clear",
            "#camera-category",
            "#camera-intensity",
            "#camera-confidence",
        ):
            self.assertIn(
                'document.querySelector("' + selector + '").disabled = !enabled;',
                html,
            )
        self.assertIn(
            "function updateCurrentLabels() {\n  if (loading || !ready || saving)",
            html,
        )
        self.assertIn(
            'document.querySelector("#clear").onclick = function () {\n  if (loading || !ready || saving) return;',
            html,
        )
        self.assertIn(
            'document.querySelector("#actor").onchange = function (event) {\n  if (loading || !ready || saving) return;',
            html,
        )
        self.assertIn("function updateCameraState() {\n  if (loading || !ready || saving) return;", html)

        show_frame = html.split("function showFrame(index) {", 1)[1].split(
            "function selectTarget() {", 1
        )[0]
        image_creation = show_frame.index("var image = new Image();")
        for reset in (
            "currentImage = null;",
            'document.querySelector("#visibility").value = "";',
            'document.querySelector("#identity").value = "";',
            "context.clearRect(0, 0, canvas.width, canvas.height);",
            "setInteractive(false);",
        ):
            self.assertLess(show_frame.index(reset), image_creation)

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
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        from videoactagent.manual_annotation import save_annotation

        save_annotation(session_dir, self._payload(session, "reference", "story_a"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(session_dir))
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_address[1])
            with urllib.request.urlopen(base + "/session", timeout=5) as response:
                self.assertEqual(response.status, 200)
            frame = session["references"]["story_a"]["video"]["frames"][0]
            with urllib.request.urlopen(base + "/" + frame["path"], timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(hashlib.sha256(response.read()).hexdigest(), frame["sha256"])
            request = urllib.request.Request(base + "/frames/%2e%2e/session_manifest.json")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 400)
            raised.exception.close()
            review_data = json.dumps(
                {
                    "target_type": "reference",
                    "target_id": "story_a",
                    "reviewer_id": "reviewer_http",
                }
            ).encode("utf-8")
            review_request = urllib.request.Request(
                base + "/review",
                data=review_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(review_request, timeout=5) as response:
                reviewed = json.loads(response.read())
            self.assertEqual(reviewed["annotation_status"], "reviewed")
            self.assertEqual(reviewed["review"]["reviewer_id"], "reviewer_http")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _payload(
        self,
        session: dict,
        target_type: str,
        target_id: str,
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
            "annotation_status": "draft",
            "annotator_id": "annotator_1",
            "reference_annotation_sha256": reference_sha,
            "actors": actors,
            "camera": {
                "category": "pan_left",
                "intensity": "medium",
                "confidence": 0.8,
            },
        }

    def _reviewed_result(self) -> tuple[Path, Path]:
        from videoactagent.manual_annotation import (
            prepare_workspace,
            review_annotation,
            save_annotation,
        )

        session_dir = self.root / "annotation"
        session = prepare_workspace(self.index, session_dir, workspace=self.root)
        save_annotation(session_dir, self._payload(session, "reference", "story_a"))
        review_annotation(
            session_dir, "reference", "story_a", reviewer_id="reviewer_1"
        )
        reference_path = session_dir / "annotations" / "reference__story_a.json"
        save_annotation(
            session_dir,
            self._payload(session, "result", "story_a__kling", _sha(reference_path)),
        )
        review_annotation(
            session_dir, "result", "story_a__kling", reviewer_id="reviewer_2"
        )
        return session_dir, session_dir / "annotations" / "result__story_a__kling.json"


if __name__ == "__main__":
    unittest.main()
