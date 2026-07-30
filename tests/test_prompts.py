"""Planning/prompt compiler tests for ``videoactagent.prompts``.

Run: ``& $PY -m unittest tests.test_prompts -v`` (see ``docs/USAGE.md``).
Input is ``examples/station_shotscript.json`` and outputs are in-memory plain,
cinematic, and timed prompt strings. This validates prompt preservation only;
it does not submit the prompts or measure generated-video control quality.
"""

from pathlib import Path
import unittest

from videoactagent.shotscript import ShotScript


SHOT_SCRIPT = Path("examples/station_shotscript.json")


class ShotPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = ShotScript.from_path(SHOT_SCRIPT)

    def test_compiles_plain_cinematic_and_timed_prompts_from_real_first_shot(self):
        from videoactagent.prompts import compile_shot_prompts

        shot = self.script.shots[0]
        prompts = compile_shot_prompts(shot)

        self.assertEqual(prompts.plain, shot.prompt)
        self.assertIn("wide shot", prompts.cinematic)
        self.assertIn("35mm lens", prompts.cinematic)
        self.assertIn("truck right", prompts.cinematic)
        self.assertIn("look at actors midpoint", prompts.cinematic)
        self.assertIn("0.0-5.0s", prompts.timed)
        self.assertIn(
            "actor A moves from (-3.0, 0.0) to (-1.0, 0.0)",
            prompts.timed,
        )

    def test_preserves_camera_and_continuity_terms_for_every_shot(self):
        from videoactagent.prompts import compile_shot_prompts

        prompts = [compile_shot_prompts(shot) for shot in self.script.shots]

        self.assertIn("dolly in", prompts[1].cinematic)
        self.assertIn("arc clockwise", prompts[2].cinematic)
        self.assertIn("look at actor B", prompts[2].cinematic)
        self.assertIn("performs face actor B", prompts[2].timed)
        for compiled in prompts:
            self.assertIn("left to right", compiled.cinematic)
            self.assertIn("north side of the action axis", compiled.cinematic)
            self.assertIn("actor A", compiled.timed)
            self.assertIn("actor B", compiled.timed)


if __name__ == "__main__":
    unittest.main()
