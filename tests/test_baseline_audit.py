import json
from pathlib import Path
import unittest


MANIFEST = Path("baselines/manifest.json")
THIRD_PARTY = Path("third_party")


class BaselineAuditTests(unittest.TestCase):
    def test_manifest_pins_official_baseline_commits(self):
        self.assertTrue(MANIFEST.is_file(), "baseline manifest is missing")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        baselines = {item["name"]: item for item in manifest["baselines"]}

        self.assertEqual(
            baselines["VACE"]["commit"],
            "48eb44f1c4be87cc65a98bff985a26976841e9f3",
        )
        self.assertEqual(
            baselines["ReCamMaster"]["commit"],
            "fcf98bc86e876bb534518cd99e8a65b282f0f16e",
        )
        self.assertEqual(baselines["VACE"]["role"], "primary_structural_control")
        self.assertEqual(
            baselines["ReCamMaster"]["role"],
            "camera_rerender_comparator",
        )

    def test_actual_checkouts_match_manifest_and_required_files(self):
        from videoactagent.baseline_audit import audit_baselines

        report = audit_baselines(MANIFEST, THIRD_PARTY)

        self.assertEqual(report["status"], "verified_source_only")
        self.assertFalse(report["weights_downloaded"])
        self.assertFalse(report["inference_verified"])
        for baseline in report["baselines"]:
            self.assertTrue(baseline["checkout_exists"], baseline["name"])
            self.assertTrue(baseline["commit_match"], baseline["name"])
            self.assertTrue(all(baseline["required_files"].values()))
            for record in baseline["hashed_files"].values():
                self.assertEqual(len(record["sha256"]), 64)
                self.assertGreater(record["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
