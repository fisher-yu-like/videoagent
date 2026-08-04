from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from videoactagent.codegen_contract import CodegenContractError, build_codegen_input, snapshot_codegen_inputs


ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "prompts" / "station_reunion.txt"
SCRIPT = ROOT / "stories" / "station_reunion.json"
TRAJECTORY = ROOT / "examples" / "station_codegen_trajectory.json"


class CodegenContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_snapshots_exact_five_keyframe_hash_bound_input(self):
        value = snapshot_codegen_inputs(
            prompt_path=PROMPT,
            shotscript_path=SCRIPT,
            trajectory_path=TRAJECTORY,
            destination=self.root / "source",
            fps=8,
            resolution=(640, 360),
        )
        self.assertEqual(value["schema_version"], "blender-codegen-input-1.0")
        self.assertEqual(value["render_contract"]["frame_end"], 40)
        self.assertEqual(
            [point["keyframe_id"] for point in value["trajectory"]["tracks"][0]["points"]],
            ["K0", "K1", "K2", "K3", "K4"],
        )
        self.assertTrue(all(record["sha256"] for record in value["source_bindings"].values()))
        self.assertTrue((self.root / "source" / "input.json").is_file())

    def test_rejects_a_four_point_track(self):
        shotscript = json.loads(SCRIPT.read_text(encoding="utf-8"))
        trajectory = json.loads(TRAJECTORY.read_text(encoding="utf-8"))
        trajectory["tracks"][0]["points"].pop()
        with self.assertRaisesRegex(CodegenContractError, "K0--K4"):
            build_codegen_input(
                prompt="station reunion",
                shotscript=shotscript,
                trajectory=trajectory,
                fps=8,
                resolution=(640, 360),
                source_bindings={},
            )

    def _base(self):
        return json.loads(SCRIPT.read_text(encoding="utf-8")), json.loads(TRAJECTORY.read_text(encoding="utf-8"))

    def test_rejects_identity_duration_time_and_actor_errors(self):
        cases = []
        script, traj = self._base()
        bad = copy.deepcopy(traj); bad["scene_id"] = "other"
        cases.append((bad, "scene identity"))
        bad = copy.deepcopy(traj); bad["shot_id"] = "s2"
        cases.append((bad, "shot identity"))
        bad = copy.deepcopy(traj); bad["duration_seconds"] = 4.0
        cases.append((bad, "duration"))
        bad = copy.deepcopy(traj); bad["tracks"][0]["points"][1]["t"] = 0.25
        cases.append((bad, "times"))
        bad = copy.deepcopy(traj); bad["tracks"].append(copy.deepcopy(bad["tracks"][0]))
        bad["tracks"][-1]["track_id"] = "duplicate_actor"; cases.append((bad, "duplicate"))
        bad = copy.deepcopy(traj); bad["tracks"].pop(); cases.append((bad, "actor"))
        for value, label in cases:
            with self.subTest(label=label), self.assertRaises(CodegenContractError):
                build_codegen_input(prompt="station", shotscript=script, trajectory=value, fps=8, resolution=(640, 360), source_bindings={})


if __name__ == "__main__":
    unittest.main()
