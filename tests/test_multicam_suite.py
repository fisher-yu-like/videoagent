import hashlib
import json
from pathlib import Path
import unittest

from videoactagent.shotscript import ShotScript


EXPECTED = {
    "station_reunion", "city_crosswalk", "forest_path",
    "studio_room", "cafe_handoff", "warehouse_chase",
}


class MulticamSuiteTests(unittest.TestCase):
    def test_six_sources_are_distinct_and_one_take(self):
        config_path = Path("configs/multicam_suite.json")
        value = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual({case["scene_id"] for case in value["cases"]}, EXPECTED)
        prompt_hashes, script_hashes, motion_hashes = set(), set(), set()
        for case in value["cases"]:
            prompt = (config_path.parent / case["prompt"]).resolve()
            script_path = (config_path.parent / case["shotscript"]).resolve()
            script = ShotScript.from_path(script_path)
            self.assertEqual(script.scene_id, case["scene_id"])
            self.assertEqual(len(script.shots), 1)
            self.assertEqual(script.shots[0].duration, 5.0)
            prompt_hashes.add(hashlib.sha256(prompt.read_bytes()).hexdigest())
            script_hashes.add(hashlib.sha256(script_path.read_bytes()).hexdigest())
            motion = {
                "camera": [script.shots[0].camera.start.as_list(), script.shots[0].camera.end.as_list()],
                "actors": [[a.start.as_list(), a.end.as_list()] for a in script.shots[0].actors],
            }
            motion_hashes.add(hashlib.sha256(json.dumps(motion, sort_keys=True).encode()).hexdigest())
        self.assertEqual(len(prompt_hashes), 6)
        self.assertEqual(len(script_hashes), 6)
        self.assertEqual(len(motion_hashes), 6)


if __name__ == "__main__":
    unittest.main()
