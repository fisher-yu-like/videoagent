"""Real local media fixtures; these tests are not model-quality evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import imageio_ffmpeg


def _video(path: Path, duration: float) -> None:
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
            f"testsrc2=size=1280x720:rate=10:duration={duration}",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode(errors="replace"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReferenceResultTests(unittest.TestCase):
    def test_validates_real_mp4_and_decodes_exact_k0_k4(self):
        from videoactagent.reference_result import validate_reference_result

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "source.mp4"
            _video(source, 5)
            output = root_path / "validated"
            report = validate_reference_result(
                source,
                output,
                expected_duration_seconds=5.0,
                expected_result_sha256=_sha(source),
                approved_proxy={"path": "approved/proxy.mp4", "sha256": "a" * 64,
                                "duration_seconds": 5.0},
            )

            self.assertTrue(report["technical_media_pass"])
            self.assertIsNone(report["human_restyle_pass"])
            self.assertEqual(report["human_restyle_status"], "pending")
            self.assertEqual(report["result"]["sha256"], _sha(source))
            self.assertEqual(report["result"]["dimensions"], [1280, 720])
            self.assertGreaterEqual(report["result"]["frame_count"], 49)
            self.assertTrue(report["result"]["decode_pass"])
            frames = report["k_frames"]
            self.assertEqual(list(frames), ["K0", "K1", "K2", "K3", "K4"])
            self.assertEqual(frames["K4"]["frame_index"], report["result"]["frame_count"] - 1)
            for frame in frames.values():
                target = output / frame["path"]
                self.assertTrue(target.is_file())
                self.assertEqual(frame["sha256"], _sha(target))
            self.assertEqual((output / "result.mp4").read_bytes(), source.read_bytes())
            stored = json.loads((output / "technical_report.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, report)

    def test_rejects_materially_truncated_and_hash_mismatched_media(self):
        from videoactagent.reference_result import validate_reference_result

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            short = root_path / "short.mp4"
            _video(short, 3)
            failed = validate_reference_result(
                short, root_path / "short-report", expected_duration_seconds=5.0
            )
            self.assertFalse(failed["technical_media_pass"])
            self.assertIn("duration", failed["failure_reason"])
            self.assertIsNone(failed["human_restyle_pass"])
            self.assertFalse((root_path / "short-report" / "k_frames").exists())

            good = root_path / "good.mp4"
            _video(good, 5)
            mismatch = validate_reference_result(
                good, root_path / "hash-report", expected_duration_seconds=5.0,
                expected_result_sha256="0" * 64,
            )
            self.assertFalse(mismatch["technical_media_pass"])
            self.assertIn("SHA-256", mismatch["failure_reason"])
            self.assertFalse((root_path / "hash-report" / "k_frames").exists())

