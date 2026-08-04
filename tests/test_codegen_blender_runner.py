from __future__ import annotations

import unittest
from pathlib import Path

from videoactagent.codegen_blender_entry import frame_for_time, world_xy
from videoactagent.codegen_safety import validate_generated_code


class CodegenBlenderEntryTests(unittest.TestCase):
    def test_frame_and_world_mapping(self):
        self.assertEqual(frame_for_time(1, 40, 0.0), 1)
        self.assertEqual(frame_for_time(1, 40, 1.0), 40)
        self.assertEqual(world_xy([-0.5, 0.5, -1.0, 1.0], 0.25, 0.25), (-0.25, 0.5))

    def test_handwritten_fixture_passes_same_gate_as_model_code(self):
        path = Path(__file__).parent / "fixtures" / "codegen" / "safe_station_scene.py"
        evidence = validate_generated_code(path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
