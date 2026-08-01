"""Real local media fixtures; these tests are not model-quality evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import imageio_ffmpeg


def _video(path: Path, duration: float, *, size: str = "1280x720") -> None:
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={size}:rate=10:duration={duration}",
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


def _proxy_record(path: Path, root: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
        # Deliberately present but never sufficient without probing real bytes.
        "duration_seconds": 5.0,
    }


class ReferenceResultTests(unittest.TestCase):
    def test_proxy_drift_during_result_decode_fails_closed(self):
        import videoactagent.reference_result as module

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            proxy = root_path / "proxy.mp4"
            result = root_path / "result.mp4"
            _video(proxy, 5)
            _video(result, 5)
            original = module._decode_k_frames

            def mutate_proxy(*args, **kwargs):
                decoded = original(*args, **kwargs)
                proxy.write_bytes(proxy.read_bytes() + b"drift")
                return decoded

            with patch.object(module, "_decode_k_frames", side_effect=mutate_proxy):
                report = module.validate_reference_result(
                    result, root_path / "report",
                    approved_proxy=_proxy_record(proxy, root_path),
                    approved_proxy_root=root_path,
                )
            self.assertFalse(report["technical_media_pass"])
            self.assertIn("approved proxy changed", report["failure_reason"])
            self.assertEqual(report["k_frames"], {})

    def test_validates_real_mp4_and_decodes_exact_k0_k4(self):
        from videoactagent.reference_result import validate_reference_result

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "source.mp4"
            proxy = root_path / "approved" / "proxy.mp4"
            proxy.parent.mkdir()
            _video(proxy, 5)
            _video(source, 5)
            output = root_path / "validated"
            report = validate_reference_result(
                source,
                output,
                expected_result_sha256=_sha(source),
                approved_proxy=_proxy_record(proxy, root_path),
                approved_proxy_root=root_path,
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
            proxy = root_path / "approved-proxy.mp4"
            _video(proxy, 5)
            proxy_record = _proxy_record(proxy, root_path)
            short = root_path / "short.mp4"
            _video(short, 3)
            failed = validate_reference_result(
                short, root_path / "short-report", approved_proxy=proxy_record,
                approved_proxy_root=root_path,
            )
            self.assertFalse(failed["technical_media_pass"])
            self.assertIn("duration", failed["failure_reason"])
            self.assertIsNone(failed["human_restyle_pass"])
            self.assertFalse((root_path / "short-report" / "k_frames").exists())

            good = root_path / "good.mp4"
            _video(good, 5)
            mismatch = validate_reference_result(
                good, root_path / "hash-report", approved_proxy=proxy_record,
                approved_proxy_root=root_path,
                expected_result_sha256="0" * 64,
            )
            self.assertFalse(mismatch["technical_media_pass"])
            self.assertIn("SHA-256", mismatch["failure_reason"])
            self.assertFalse((root_path / "hash-report" / "k_frames").exists())

    def test_rejects_wrong_aspect_and_undecodable_mp4_without_fake_frames(self):
        from videoactagent.reference_result import validate_reference_result

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            proxy = root_path / "proxy.mp4"
            _video(proxy, 5)
            proxy_record = _proxy_record(proxy, root_path)
            wrong = root_path / "wrong.mp4"
            _video(wrong, 5, size="1024x768")
            bad = root_path / "bad.mp4"
            bad.write_bytes(b"not an mp4")
            for source, name, message in (
                (wrong, "wrong-report", "16:9"),
                (bad, "bad-report", "decode|ffmpeg|video"),
            ):
                with self.subTest(source=source.name):
                    report = validate_reference_result(
                        source, root_path / name,
                        approved_proxy=proxy_record,
                        approved_proxy_root=root_path,
                    )
                    self.assertFalse(report["technical_media_pass"])
                    self.assertRegex(report["failure_reason"], message)
                    self.assertEqual(report["k_frames"], {})
                    self.assertFalse((root_path / name / "k_frames").exists())

    def test_proxy_record_requires_real_safe_hash_bound_media(self):
        from videoactagent.reference_result import validate_reference_result

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            result = root_path / "result.mp4"
            _video(result, 5)
            for record in (
                {"path": "missing.mp4", "sha256": "a" * 64, "bytes": 1},
                {"path": "../escape.mp4", "sha256": "a" * 64, "bytes": 1},
            ):
                with self.subTest(path=record["path"]):
                    with self.assertRaisesRegex(ValueError, "approved proxy"):
                        validate_reference_result(
                            result, root_path / (Path(record["path"]).stem + "-report"),
                            approved_proxy=record, approved_proxy_root=root_path,
                        )
