"""Stage 6 provenance-safe VACE input preparation tests for ``vace_inputs``.

Run: ``& $PY -m unittest tests.test_vace_inputs -v`` (see
``docs/DEBUGGING.md``). Real source media comes from
``runs/stage2_control_bridge``; prepared proxy/mask/job outputs are exercised in
temporary directories (persistent examples live in ``runs/stage6_vace_inputs``).
Failure injections and patched ffmpeg pipes are mechanics tests; passing does
not prove VACE source preprocessing, CUDA execution, or generated-video quality.
"""

from __future__ import annotations

import copy
import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import warnings

import imageio_ffmpeg

from videoactagent.vace_inputs import (
    ProvenanceError,
    _atomic_write_json,
    build_vace_inputs,
    verify_prepared_job,
    write_full_generation_mask,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_STAGE2 = REPO_ROOT / "runs" / "stage2_control_bridge"
REAL_BUNDLE = REAL_STAGE2 / "control_bundle.json"
VACE_COMMIT = "48eb44f1c4be87cc65a98bff985a26976841e9f3"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decode_video(path: Path) -> tuple[dict, list[bytes]]:
    frame_count, _ = imageio_ffmpeg.count_frames_and_secs(str(path))
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        frames = [next(reader) for _ in range(frame_count)]
    finally:
        reader.close()
    return metadata, frames


class VaceInputProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(REAL_BUNDLE.is_file(), "real Stage 2 bundle is required")

    def _copy_real_stage2(self, root: Path) -> Path:
        copied = root / "stage2_control_bridge"
        shutil.copytree(REAL_STAGE2, copied)
        return copied / "control_bundle.json"

    def _mutate_bundle(self, bundle_path: Path, change) -> None:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        change(bundle)
        bundle_path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_mask_verification_closes_ffmpeg_pipes_after_process_exit(self):
        original_read_frames = imageio_ffmpeg.read_frames

        class DelayedFinalFrameReader:
            def __init__(self, reader):
                self.reader = reader
                self.yield_count = 0

            @property
            def gi_frame(self):
                return self.reader.gi_frame

            def __next__(self):
                value = next(self.reader)
                self.yield_count += 1
                if self.yield_count == 4:  # metadata plus three decoded frames
                    time.sleep(0.1)
                return value

            def close(self):
                self.reader.close()

        def delayed_read_frames(*args, **kwargs):
            return DelayedFinalFrameReader(original_read_frames(*args, **kwargs))

        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings(
            record=True
        ) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with mock.patch(
                "videoactagent.vace_inputs.imageio_ffmpeg.read_frames",
                side_effect=delayed_read_frames,
            ):
                write_full_generation_mask(
                    Path(tmp) / "mask.mp4",
                    dimensions=(96, 54),
                    fps=3,
                    frame_count=3,
                )
            gc.collect()

        resource_warnings = [
            warning for warning in caught if warning.category is ResourceWarning
        ]
        self.assertEqual(resource_warnings, [])

    def test_mask_failure_cleanup_uses_unique_temporary_paths(self):
        """Negative cleanup mechanics only; this is not acceptance evidence."""

        temporary_paths = []

        class FailingWriter:
            def __init__(self, path: Path, failure: str):
                self.path = path
                self.failure = failure

            def send(self, _frame):
                if self.failure == "send":
                    raise RuntimeError("injected writer send failure")

            def close(self):
                if self.failure == "close":
                    raise RuntimeError("injected writer close failure")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mask.mp4"
            for failure in ("send", "close", "verification"):
                with self.subTest(failure=failure):

                    def fake_write_frames(path, *_args, **_kwargs):
                        temporary = Path(path)
                        temporary.write_bytes(b"injected partial output")
                        temporary_paths.append(temporary)
                        return FailingWriter(temporary, failure)

                    verification_error = (
                        RuntimeError("injected verification failure")
                        if failure == "verification"
                        else None
                    )
                    with mock.patch(
                        "videoactagent.vace_inputs.imageio_ffmpeg.write_frames",
                        side_effect=fake_write_frames,
                    ), mock.patch(
                        "videoactagent.vace_inputs._verify_full_generation_mask",
                        side_effect=verification_error,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "injected"):
                            write_full_generation_mask(
                                target,
                                dimensions=(96, 54),
                                fps=3,
                                frame_count=3,
                            )
                    self.assertFalse(target.exists())
                    self.assertFalse(temporary_paths[-1].exists())

        self.assertEqual(len(set(temporary_paths)), len(temporary_paths))

    def test_job_atomic_writer_survives_concurrent_publishers(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "vace_job.json"
            barrier = threading.Barrier(8)
            failures: list[BaseException] = []

            def publish(index: int) -> None:
                try:
                    barrier.wait()
                    _atomic_write_json(
                        target,
                        {"publisher": index, "payload": str(index) * 4096},
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    failures.append(exc)

            threads = [
                threading.Thread(target=publish, args=(index,)) for index in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(failures, [])
            self.assertFalse(any(thread.is_alive() for thread in threads))
            published = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(published["publisher"], range(8))
            self.assertEqual(
                published["payload"], str(published["publisher"]) * 4096
            )
            self.assertEqual(list(target.parent.glob(".vace_job.json.*.tmp")), [])

    def test_job_atomic_writer_fsyncs_and_cleans_up_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "vace_job.json"
            target.write_text('{"state":"old"}\n', encoding="utf-8")
            with mock.patch(
                "videoactagent.vace_inputs.os.fsync", wraps=os.fsync
            ) as fsync, mock.patch(
                "pathlib.Path.replace", side_effect=OSError("injected replace failure")
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    _atomic_write_json(target, {"state": "new"})

            self.assertGreaterEqual(fsync.call_count, 1)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"state": "old"}
            )
            self.assertEqual(list(target.parent.glob(".vace_job.json.*.tmp")), [])

    def test_builds_s01_job_only_from_hash_verified_stage2_media(self):
        source_bundle = json.loads(REAL_BUNDLE.read_text(encoding="utf-8"))
        source_shot = source_bundle["shots"][0]

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            job_path = build_vace_inputs(REAL_BUNDLE, "s01", output_dir)
            job = json.loads(job_path.read_text(encoding="utf-8"))
            mask_path = output_dir / "src_mask.mp4"
            self.assertTrue(mask_path.is_file())
            mask_metadata, mask_frames = decode_video(mask_path)
            self.assertEqual(mask_metadata["size"], (960, 540))
            self.assertAlmostEqual(mask_metadata["fps"], 3.0)
            self.assertEqual(len(mask_frames), 15)
            for index in (0, len(mask_frames) // 2, len(mask_frames) - 1):
                self.assertGreaterEqual(min(mask_frames[index]), 250)
                self.assertEqual(max(mask_frames[index]), 255)
            self.assertEqual(job["mask"]["bytes"], mask_path.stat().st_size)
            self.assertEqual(job["mask"]["sha256"], sha256_file(mask_path))

        self.assertEqual(job["evidence_source"], "real_stage2_blender")
        self.assertEqual(job["selected_shot_id"], "s01")
        self.assertEqual(job["vace"]["model_name"], "vace-1.3B")
        self.assertEqual(job["vace"]["size"], "480p")
        self.assertEqual(job["vace"]["seed"], 2025)
        self.assertEqual(job["vace"]["commit"], VACE_COMMIT)
        self.assertEqual(job["source"]["source_frame_count"], 15)
        self.assertEqual(job["source"]["expected_preprocessed_frames"], 13)
        self.assertEqual(job["source"]["fps"], 3)
        self.assertEqual(job["source"]["dimensions"], [960, 540])
        self.assertEqual(
            job["mapping"]["src_video"],
            "source/shots/s01/proxy.mp4",
        )
        self.assertEqual(job["mapping"]["src_mask"], "src_mask.mp4")
        self.assertEqual(
            job["mapping"]["src_ref_images"],
            ["source/shots/s01/first.png"],
        )
        self.assertEqual(job["mapping"]["prompt"], job["prompt"]["text"])
        self.assertEqual(
            set(job["mapping"]),
            {"src_video", "src_mask", "src_ref_images", "prompt"},
        )
        self.assertEqual(
            job["prompt"]["text"],
            source_shot["prompts"]["cinematic"]
            + " "
            + source_shot["prompts"]["timed"],
        )
        self.assertEqual(
            job["prompt"]["sha256"],
            hashlib.sha256(job["prompt"]["text"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            job["source_bundle"]["sha256"], sha256_file(REAL_BUNDLE)
        )
        self.assertEqual(job["mapping_path_bases"], {"source": "bundle_parent"})
        self.assertEqual(job["mask"]["path"], "src_mask.mp4")
        self.assertEqual(job["mask"]["frame_count"], 15)
        self.assertEqual(job["mask"]["dimensions"], [960, 540])
        self.assertEqual(job["mask"]["fps"], 3)
        self.assertEqual(
            job["mask"]["mask_semantics"], "white_generate_black_retain"
        )
        self.assertEqual(
            job["mask"]["mask_policy"],
            "full_frame_generate_from_real_stage2_dimensions",
        )
        self.assertFalse(job["mask"]["actor_segmentation_claimed"])
        for name, record in source_shot["controls"].items():
            self.assertEqual(job["source"]["controls"][name]["bytes"], record["bytes"])
            self.assertEqual(
                job["source"]["controls"][name]["sha256"], record["sha256"]
            )
            self.assertEqual(
                job["source"]["controls"][name]["path"],
                "source/" + record["path"],
            )
        self.assertFalse(job["evidence"]["source_validation_passed"])
        self.assertFalse(job["evidence"]["inference_success"])

    def test_rejects_tampered_real_control_copies(self):
        for control_name in ("proxy_video", "first_frame", "last_frame"):
            with self.subTest(control=control_name), tempfile.TemporaryDirectory() as tmp:
                bundle_path = self._copy_real_stage2(Path(tmp))
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                relative = bundle["shots"][0]["controls"][control_name]["path"]
                target = bundle_path.parent / relative
                contents = bytearray(target.read_bytes())
                contents[len(contents) // 2] ^= 0x01
                target.write_bytes(contents)
                with self.assertRaisesRegex(ProvenanceError, control_name):
                    build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_missing_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            target = bundle_path.parent / bundle["shots"][0]["controls"]["last_frame"]["path"]
            target.unlink()
            with self.assertRaisesRegex(ProvenanceError, "last_frame"):
                build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            self._mutate_bundle(
                bundle_path,
                lambda bundle: bundle["shots"][0]["controls"]["first_frame"].update(
                    {"path": "../outside.png"}
                ),
            )
            with self.assertRaisesRegex(ProvenanceError, "first_frame.*outside"):
                build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_duplicate_shot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            self._mutate_bundle(
                bundle_path,
                lambda bundle: bundle["shots"].append(copy.deepcopy(bundle["shots"][0])),
            )
            with self.assertRaisesRegex(ProvenanceError, "duplicate.*s01"):
                build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_duplicate_unselected_shot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            self._mutate_bundle(
                bundle_path,
                lambda bundle: bundle["shots"].append(copy.deepcopy(bundle["shots"][1])),
            )
            with self.assertRaisesRegex(ProvenanceError, "duplicate.*s02"):
                build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_missing_shot_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            with self.assertRaisesRegex(ProvenanceError, "missing shot.*s99"):
                build_vace_inputs(bundle_path, "s99", Path(tmp) / "output")

    def test_rejects_nonpositive_fps(self):
        for fps in (0, -3):
            with self.subTest(fps=fps), tempfile.TemporaryDirectory() as tmp:
                bundle_path = self._copy_real_stage2(Path(tmp))
                self._mutate_bundle(bundle_path, lambda bundle: bundle.update({"fps": fps}))
                with self.assertRaisesRegex(ProvenanceError, "fps"):
                    build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_invalid_frame_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            self._mutate_bundle(
                bundle_path,
                lambda bundle: bundle["shots"][0].update({"frame_range": [15, 1]}),
            )
            with self.assertRaisesRegex(ProvenanceError, "frame_range"):
                build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_rejects_frame_count_duration_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = self._copy_real_stage2(Path(tmp))
            self._mutate_bundle(
                bundle_path,
                lambda bundle: bundle["shots"][0].update({"frame_range": [1, 14]}),
            )
            with self.assertRaisesRegex(ProvenanceError, "duration.*frame count"):
                build_vace_inputs(bundle_path, "s01", Path(tmp) / "output")

    def test_prepare_cli_rechecks_manifest_before_success_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "prepared"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "videoactagent.vace_inputs",
                    "prepare",
                    "--bundle",
                    str(REAL_BUNDLE),
                    "--shot",
                    "s01",
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("VACE_INPUTS_PREPARED", completed.stdout)
            job_path = output_dir / "vace_job.json"
            self.assertTrue(job_path.is_file())
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(
                job["mask"]["sha256"], sha256_file(output_dir / "src_mask.mp4")
            )

    def test_prepared_job_rejects_mask_path_outside_job_directory(self):
        for unsafe_path in ("../src_mask.mp4", str(REPO_ROOT / "src_mask.mp4")):
            with self.subTest(path=unsafe_path), tempfile.TemporaryDirectory() as tmp:
                job_path = build_vace_inputs(REAL_BUNDLE, "s01", Path(tmp))
                job = json.loads(job_path.read_text(encoding="utf-8"))
                job["mask"]["path"] = unsafe_path
                job_path.write_text(
                    json.dumps(job, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ProvenanceError, "mask path.*outside"):
                    verify_prepared_job(job_path, REAL_BUNDLE)

    def test_prepared_job_rejects_contract_tampering_against_real_stage2(self):
        def set_top_level(field, value):
            return lambda job: job.__setitem__(field, value)

        def set_nested(section, field, value):
            return lambda job: job[section].__setitem__(field, value)

        def tamper_prompt_with_matching_hash(job):
            job["prompt"]["text"] = "tampered prompt"
            job["prompt"]["sha256"] = hashlib.sha256(
                job["prompt"]["text"].encode("utf-8")
            ).hexdigest()

        def remove_created_at(job):
            job.pop("created_at", None)

        tamperers = {
            "schema_version": set_top_level("schema_version", "9.9"),
            "evidence_source": set_top_level("evidence_source", "synthetic_fixture"),
            "selected_shot_id": set_top_level("selected_shot_id", "s02"),
            "source.shot_id": set_nested("source", "shot_id", "s02"),
            "source.fps": set_nested("source", "fps", 99),
            "source.duration_seconds": set_nested("source", "duration_seconds", 99),
            "source.frame_range": set_nested("source", "frame_range", [2, 16]),
            "source.source_frame_count": set_nested("source", "source_frame_count", 99),
            "source.expected_preprocessed_frames": set_nested(
                "source", "expected_preprocessed_frames", 99
            ),
            "source.dimensions": set_nested("source", "dimensions", [1, 1]),
            "mapping.src_video": set_nested(
                "mapping", "src_video", "source/shots/s02/proxy.mp4"
            ),
            "mapping.src_ref_images": set_nested(
                "mapping", "src_ref_images", ["source/shots/s01/last.png"]
            ),
            "mapping.prompt": set_nested("mapping", "prompt", "tampered prompt"),
            "prompt.text_with_recomputed_sha": tamper_prompt_with_matching_hash,
            "vace.commit": set_nested("vace", "commit", "deadbeef"),
            "vace.model_name": set_nested("vace", "model_name", "vace-14B"),
            "vace.size": set_nested("vace", "size", "720p"),
            "vace.seed": set_nested("vace", "seed", 7),
            "mask.actor_segmentation_claimed": set_nested(
                "mask", "actor_segmentation_claimed", True
            ),
            "mask.mask_semantics": set_nested(
                "mask", "mask_semantics", "black_generate_white_retain"
            ),
            "mask.mask_policy": set_nested(
                "mask", "mask_policy", "actor_segmentation"
            ),
            "mask.frame_count": set_nested("mask", "frame_count", 99),
            "mask.dimensions": set_nested("mask", "dimensions", [1, 1]),
            "mask.fps": set_nested("mask", "fps", 99),
            "created_at.malformed": set_top_level("created_at", "not-a-date"),
            "created_at.missing": remove_created_at,
            "created_at.non_utc": set_top_level(
                "created_at", "2026-07-29T08:00:00+08:00"
            ),
        }
        for label, tamper in tamperers.items():
            with self.subTest(field=label), tempfile.TemporaryDirectory() as tmp:
                job_path = build_vace_inputs(REAL_BUNDLE, "s01", Path(tmp))
                job = json.loads(job_path.read_text(encoding="utf-8"))
                tamper(job)
                job_path.write_text(
                    json.dumps(job, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                with self.assertRaises(ProvenanceError, msg=label):
                    verify_prepared_job(job_path, REAL_BUNDLE)

if __name__ == "__main__":
    unittest.main()
