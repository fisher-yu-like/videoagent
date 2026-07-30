from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "whole_story_suite.json"
BLENDER = Path(r"D:\blender\blender.exe")


@unittest.skipUnless(BLENDER.is_file(), "real Blender is not installed at D:\\blender")
class WholeStoryBlenderIntegrationTests(unittest.TestCase):
    def test_real_blender_renders_one_complete_story_proxy(self):
        from videoactagent.whole_story import load_suite, render_story_case

        suite = load_suite(CONFIG)
        with tempfile.TemporaryDirectory() as root:
            result = render_story_case(suite, suite.cases[0], Path(root) / "case")
            case_dir = Path(result["case_dir"])
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue((case_dir / "proxy.mp4").is_file())
            self.assertTrue((case_dir / "proxy.blend").is_file())
            self.assertTrue((case_dir / "sources" / "prompt.txt").is_file())
            self.assertTrue((case_dir / "sources" / "shotscript.json").is_file())
            self.assertEqual(
                manifest["source_hashes"]["prompt_file_sha256"],
                hashlib.sha256((case_dir / "sources" / "prompt.txt").read_bytes()).hexdigest(),
            )
            self.assertEqual(manifest["media"]["frame_count"], 15)
            self.assertAlmostEqual(manifest["media"]["duration_seconds"], 5.0, places=1)
            self.assertEqual(manifest["media"]["size"], [960, 540])
            self.assertEqual(manifest["media"]["fps"], 3.0)
            self.assertEqual(manifest["status"], "media_complete")
            self.assertEqual(manifest["scheduling"]["status"], "matched")
            self.assertTrue(manifest["scheduling"]["camera_rotation_locked"])
            self.assertEqual(set(manifest["inspection_frames"]), {"first", "middle", "last"})
            for entry in manifest["inspection_frames"].values():
                self.assertTrue((case_dir / entry["path"]).is_file())
            frame_hashes = {
                entry["sha256"] for entry in manifest["inspection_frames"].values()
            }
            self.assertGreater(len(frame_hashes), 1)
            self.assertNotIn("shot_id", json.dumps(manifest["bundles"]))
            self.assertEqual(
                manifest["bundles"]["vace"]["source_video"]["path"], "proxy.mp4"
            )
            self.assertEqual(
                manifest["bundles"]["vace"]["source_video"]["path_base"], "case_dir"
            )
            for backend, record in manifest["bundle_files"].items():
                bundle_path = case_dir / record["path"]
                self.assertTrue(bundle_path.is_file())
                self.assertEqual(
                    record["sha256"], hashlib.sha256(bundle_path.read_bytes()).hexdigest()
                )
                self.assertEqual(
                    json.loads(bundle_path.read_text(encoding="utf-8")),
                    manifest["bundles"][backend],
                )


if __name__ == "__main__":
    unittest.main()
