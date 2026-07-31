"""Tests for preparing a clay-only VACE job from a coded-draft bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import imageio_ffmpeg


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_video(path: Path, *, offset: int) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height, fps, frame_count = 64, 32, 5, 25
    writer = imageio_ffmpeg.write_frames(
        str(path),
        (width, height),
        pix_fmt_in="rgb24",
        pix_fmt_out="yuv420p",
        fps=fps,
        codec="libx264",
        macro_block_size=1,
        ffmpeg_log_level="error",
    )
    writer.send(None)
    for index in range(frame_count):
        value = (offset + index * 7) % 256
        writer.send(bytes([value, value, value]) * width * height)
    writer.close()

    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    metadata = next(reader)
    pixels = hashlib.sha256()
    decoded = 0
    try:
        for frame in reader:
            pixels.update(frame)
            decoded += 1
    finally:
        reader.close()
    return {
        "frame_count": decoded,
        "fps": float(metadata["fps"]),
        "duration_seconds": decoded / float(metadata["fps"]),
        "stream_duration_seconds": float(metadata["duration"]),
        "resolution": [width, height],
        "codec": metadata.get("codec"),
        "bytes": path.stat().st_size,
        "decoded_pixel_sha256": pixels.hexdigest(),
    }


def _record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _make_coded_draft(root: Path) -> tuple[Path, Path]:
    clay_path = root / "renders" / "clay" / "proxy.mp4"
    diagnostic_path = root / "renders" / "diagnostic" / "proxy.mp4"
    clay_media = _write_video(clay_path, offset=35)
    diagnostic_media = _write_video(diagnostic_path, offset=145)

    semantic_path = root / "sources" / "semantic_plan.json"
    semantic_path.parent.mkdir(parents=True)
    semantic_path.write_text(
        json.dumps(
            {
                "story_id": "station_reunion",
                "duration_seconds": 5.0,
                "appearance_instruction": "Natural people and realistic station light.",
                "semantic_keyframes": [
                    {"id": "K0", "t": 0.0},
                    {"id": "K1", "t": 1.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    semantic_binding = {
        "original_path": "ignored-original.json",
        "original_sha256": _sha256(semantic_path),
        "snapshot_path": "sources/semantic_plan.json",
        "snapshot_sha256": _sha256(semantic_path),
        "bytes": semantic_path.stat().st_size,
        "verified_equal": True,
    }
    clay = {**_record(clay_path, root), "media": clay_media}
    diagnostic = {**_record(diagnostic_path, root), "media": diagnostic_media}
    bundle = {
        "schema_version": "1.0",
        "story_id": "station_reunion",
        "conditioning_mode": "source_video_edit",
        "conditioning_video": clay,
        "appearance_instruction": "Natural people and realistic station light.",
        "story_prompt": "One continuous take. Two travelers approach each other.",
        "motion_semantics": {
            "semantic_plan": semantic_binding,
            "keyframes": [
                {"semantic_id": "K0", "t": 0.0, "frame_index": 0},
                {"semantic_id": "K1", "t": 1.0, "frame_index": 24},
            ],
        },
        "diagnostic_video": {
            **diagnostic,
            "role": "evidence_only",
            "backend_consumed": False,
        },
        "backend_consumed": False,
    }
    bundle_path = root / "bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    bundle_record = _record(bundle_path, root)
    inventory = [
        _record(semantic_path, root),
        _record(clay_path, root),
        _record(diagnostic_path, root),
        bundle_record,
    ]
    manifest = {
        "schema_version": "1.0",
        "story_id": "station_reunion",
        "expected_media": {
            "frame_count": 25,
            "fps": 5,
            "duration_seconds": 5.0,
            "resolution": [64, 32],
        },
        "sources": {"semantic_plan": semantic_binding},
        "videos": {"clay": clay, "diagnostic": diagnostic},
        "outputs": {"bundle": bundle_record},
        "backend_consumed": False,
        "artifact_inventory": inventory,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return bundle_path, manifest_path


class VaceCodedDraftTests(unittest.TestCase):
    def test_prepare_builds_verified_clay_only_job_without_reference_image(self):
        from videoactagent.vace_coded_draft import (
            build_vace_coded_draft_job,
            verify_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            output = root / "job"

            job_path = build_vace_coded_draft_job(bundle, manifest, output)
            job = verify_vace_coded_draft_job(job_path)

            self.assertEqual(job_path, output / "vace_job.json")
            self.assertEqual(job["story_id"], "station_reunion")
            self.assertEqual(job["conditioning_mode"], "source_video_edit")
            self.assertEqual(job["control_mode"], "source_video_edit")
            self.assertEqual(job["mapping"]["src_video"], "control/src_video.mp4")
            self.assertEqual(job["mapping"]["src_ref_images"], None)
            self.assertEqual(job["source"]["conditioning_video"]["role"], "clay_only")
            self.assertEqual(job["source"]["conditioning_video"]["resampling"], "none")
            self.assertFalse(job["source"]["conditioning_video"]["ai_interpolation"])
            self.assertEqual(
                _sha256(output / "source" / "clay.mp4"),
                _sha256(root / "coded" / "renders" / "clay" / "proxy.mp4"),
            )
            control = job["control"]
            self.assertEqual(control["path"], "control/src_video.mp4")
            self.assertEqual(control["frame_count"], 81)
            self.assertEqual(control["fps"], 16.0)
            self.assertEqual(control["resolution"], [832, 480])
            self.assertEqual(
                control["source_clay_sha256"],
                _sha256(root / "coded" / "renders" / "clay" / "proxy.mp4"),
            )
            self.assertEqual(control["resampling"], "ffmpeg_scale_fps_final_frame_clone")
            self.assertFalse(control["ai_interpolation"])
            self.assertEqual(job["mask"]["frame_count"], 81)
            self.assertEqual(job["mask"]["fps"], 16.0)
            self.assertEqual(job["mask"]["dimensions"], [832, 480])
            self.assertFalse((output / "source" / "first.png").exists())
            self.assertFalse((output / "source" / "diagnostic.mp4").exists())
            self.assertEqual(
                job["prompt"]["components"],
                {
                    "story_prompt": "One continuous take. Two travelers approach each other.",
                    "appearance_instruction": "Natural people and realistic station light.",
                },
            )
            self.assertEqual(
                job["motion_semantics"]["keyframes"],
                [
                    {
                        "semantic_id": "K0",
                        "t": 0.0,
                        "source_frame_index": 0,
                        "control_frame_index": 0,
                    },
                    {
                        "semantic_id": "K1",
                        "t": 1.0,
                        "source_frame_index": 24,
                        "control_frame_index": 80,
                    },
                ],
            )
            self.assertEqual(
                job["motion_semantics"]["explicit_trajectory_binding"],
                {
                    "available": False,
                    "path": None,
                    "sha256": None,
                    "policy": "not_required_for_initial_clay_only_pilot",
                },
            )
            self.assertNotIn("diagnostic", json.dumps(job).lower())
            self.assertEqual(job["api_calls"], 0)
            self.assertEqual(
                job["evidence"],
                {"source_validation_passed": True, "inference_success": False},
            )

    def test_output_directory_must_be_new_and_failed_attempt_leaves_no_staging(self):
        from videoactagent.vace_coded_draft import (
            VaceCodedDraftError,
            build_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            output = root / "job"
            output.mkdir()
            with self.assertRaises(VaceCodedDraftError):
                build_vace_coded_draft_job(bundle, manifest, output)
            output.rmdir()
            job = build_vace_coded_draft_job(bundle, manifest, output)
            self.assertTrue(job.is_file())
            self.assertEqual(list(root.glob(".job.*.staging")), [])

    def test_verification_rejects_tampering_of_the_snapshotted_clay_pixels(self):
        from videoactagent.vace_coded_draft import (
            VaceCodedDraftError,
            build_vace_coded_draft_job,
            verify_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            job_path = build_vace_coded_draft_job(bundle, manifest, root / "job")
            clay = job_path.parent / "source" / "clay.mp4"
            clay.write_bytes(clay.read_bytes() + b"tamper")
            with self.assertRaises(VaceCodedDraftError):
                verify_vace_coded_draft_job(job_path)

    def test_prepare_rejects_a_bundle_already_marked_backend_consumed(self):
        from videoactagent.vace_coded_draft import (
            VaceCodedDraftError,
            build_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            document = json.loads(bundle.read_text(encoding="utf-8"))
            document["backend_consumed"] = True
            bundle.write_text(json.dumps(document, indent=2), encoding="utf-8")
            with self.assertRaises(VaceCodedDraftError):
                build_vace_coded_draft_job(bundle, manifest, root / "job")

    def test_prepare_decodes_the_evidence_video_instead_of_trusting_media_json(self):
        from videoactagent.vace_coded_draft import (
            VaceCodedDraftError,
            build_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            bundle_document = json.loads(bundle.read_text(encoding="utf-8"))
            bundle_document["diagnostic_video"]["media"]["frame_count"] = 999
            bundle.write_text(json.dumps(bundle_document, indent=2), encoding="utf-8")
            bundle_record = _record(bundle, bundle.parent)

            manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_document["videos"]["diagnostic"]["media"]["frame_count"] = 999
            manifest_document["outputs"]["bundle"] = bundle_record
            for index, record in enumerate(manifest_document["artifact_inventory"]):
                if record["path"] == "bundle.json":
                    manifest_document["artifact_inventory"][index] = bundle_record
            manifest.write_text(json.dumps(manifest_document, indent=2), encoding="utf-8")

            with self.assertRaises(VaceCodedDraftError):
                build_vace_coded_draft_job(bundle, manifest, root / "job")

    def test_prepare_rejects_files_added_inside_the_hash_bound_coded_draft(self):
        from videoactagent.vace_coded_draft import (
            VaceCodedDraftError,
            build_vace_coded_draft_job,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, manifest = _make_coded_draft(root / "coded")
            added = bundle.parent / "nested-job" / "manifest.json"
            added.parent.mkdir()
            added.write_text("{}", encoding="utf-8")
            with self.assertRaises(VaceCodedDraftError):
                build_vace_coded_draft_job(bundle, manifest, root / "job")


if __name__ == "__main__":
    unittest.main()
