from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import imageio_ffmpeg

from videoactagent.full_chain import compile_matrix
from videoactagent.vace_inputs import sha256_file


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "full_chain_matrix.json"


class VaceFullChainTests(unittest.TestCase):
    def _vace_job(self):
        matrix = compile_matrix(CONFIG)
        return next(job for job in matrix.jobs if job.backend == "vace")

    def test_export_snapshots_whole_story_inputs_and_decodes_81_frame_mask(self):
        from videoactagent.vace_full_chain import export_vace_job, validate_vace_job

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "station_reunion"
            exported = export_vace_job(self._vace_job(), job_dir)
            validated = validate_vace_job(job_dir)

            self.assertEqual(exported, validated)
            self.assertEqual(exported["story_id"], "station_reunion")
            self.assertEqual(exported["conditioning_mode"], "source_video")
            self.assertNotIn("shot_id", json.dumps(exported, sort_keys=True))
            self.assertEqual(
                exported["inference"],
                {
                    "model_name": "vace-1.3B",
                    "size": "480p",
                    "frame_num": 81,
                    "fps": 16,
                    "seed": 2026,
                    "sample_steps": 20,
                },
            )
            self.assertEqual(
                exported["server_contract"],
                {
                    "vace_commit": "48eb44f1c4be87cc65a98bff985a26976841e9f3",
                    "wan_commit": "9737cba9c1c3c4d04b33fcad41c111989865d315",
                },
            )
            for name in ("proxy_video", "prompt", "first_frame"):
                record = exported["snapshots"][name]
                path = job_dir / record["path"]
                self.assertTrue(path.is_file())
                self.assertEqual(record["bytes"], path.stat().st_size)
                self.assertEqual(record["sha256"], sha256_file(path))
            mask = job_dir / "src_mask.mp4"
            self.assertEqual(exported["mask"]["sha256"], sha256_file(mask))
            self.assertEqual(imageio_ffmpeg.count_frames_and_secs(str(mask))[0], 81)

    def test_validate_rejects_tampered_snapshot_hash_and_inference_contract(self):
        from videoactagent.vace_full_chain import VaceFullChainError, export_vace_job, validate_vace_job

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "station_reunion"
            export_vace_job(self._vace_job(), job_dir)
            path = job_dir / "vace_job.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["snapshots"]["proxy_video"]["sha256"] = "0" * 64
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(VaceFullChainError, "proxy_video.*hash"):
                validate_vace_job(job_dir)

            document = json.loads(path.read_text(encoding="utf-8"))
            document["snapshots"]["proxy_video"]["sha256"] = sha256_file(
                job_dir / document["snapshots"]["proxy_video"]["path"]
            )
            document["inference"]["sample_steps"] = 21
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(VaceFullChainError, "inference"):
                validate_vace_job(job_dir)

    def test_runner_is_fail_closed_and_records_exact_vace_invocation_contract(self):
        script = (ROOT / "scripts" / "run_vace_full_chain.sh").read_text(encoding="utf-8")
        self.assertIn('"$#" -ne 2', script)
        self.assertIn("output directory already exists", script)
        self.assertIn("--query-compute-apps=pid", script)
        self.assertIn("vace_wan_inference.py", script)
        self.assertIn("--frame_num", script)
        self.assertIn("--sample_steps", script)
        self.assertIn("--base_seed", script)
        self.assertIn("--query-gpu=timestamp,index,memory.used", script)
        self.assertIn("ffprobe", script)
        self.assertIn("sha256sum", script)
        self.assertIn('if [ "$EXIT_CODE" -ne 0 ]; then', script)
        self.assertIn('exit 1', script)
        self.assertIn("trap - EXIT", script)
        self.assertNotIn("retry", script.lower())


if __name__ == "__main__":
    unittest.main()
