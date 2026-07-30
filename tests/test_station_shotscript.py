"""Planning-stage ShotScript parser tests for ``videoactagent.shotscript``.

Run: ``& $PY -m unittest tests.test_station_shotscript -v`` (see
``docs/USAGE.md``). Input is the real project example
``examples/station_shotscript.json`` and parsed objects are held in memory.
This validates schema/continuity fields only, not Blender or video generation.
"""

from pathlib import Path
import json
import unittest


EXAMPLE = Path("examples/station_shotscript.json")


class StationShotScriptTests(unittest.TestCase):
    def load_script(self):
        try:
            from videoactagent.shotscript import ShotScript
        except ModuleNotFoundError as exc:
            self.fail(f"ShotScript parser is missing: {exc}")
        return ShotScript.from_path(EXAMPLE)

    def test_actual_station_example_has_expected_shot_sequence(self):
        script = self.load_script()
        self.assertEqual(script.environment_preset, "station")
        self.assertEqual(script.fps, 3)
        self.assertEqual([shot.shot_id for shot in script.shots], ["s01", "s02", "s03"])
        self.assertEqual([shot.duration for shot in script.shots], [5.0, 5.0, 5.0])

    def test_from_dict_matches_path_parser(self):
        from videoactagent.shotscript import ShotScript

        document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(ShotScript.from_dict(document), ShotScript.from_path(EXAMPLE))

    def test_actual_station_example_preserves_actor_and_shot_continuity(self):
        script = self.load_script()
        for shot in script.shots:
            self.assertEqual({actor.actor_id for actor in shot.actors}, {"actor_a", "actor_b"})
            self.assertEqual(shot.continuity.screen_direction, "left_to_right")
            self.assertEqual(shot.continuity.axis_side, "north")
        self.assertIsNone(script.shots[0].continuity.previous_shot)
        self.assertEqual(script.shots[1].continuity.previous_shot, "s01")
        self.assertEqual(script.shots[2].continuity.previous_shot, "s02")

        actor_a = [
            next(actor for actor in shot.actors if actor.actor_id == "actor_a")
            for shot in script.shots
        ]
        self.assertEqual(actor_a[0].end, actor_a[1].start)
        self.assertEqual(actor_a[1].end, actor_a[2].start)

    def test_environment_preset_is_required_and_allow_listed(self):
        from videoactagent.shotscript import ShotScript, ShotScriptError

        document = json.loads(
            Path("examples/city_crosswalk_shotscript.json").read_text(encoding="utf-8")
        )
        document.pop("environment_preset")
        with self.assertRaisesRegex(ShotScriptError, "environment_preset"):
            ShotScript.from_dict(document)

        document["environment_preset"] = "freeform_llm_scene"
        with self.assertRaisesRegex(ShotScriptError, "unsupported environment_preset"):
            ShotScript.from_dict(document)

    def test_all_checked_in_environment_examples_parse(self):
        from videoactagent.shotscript import ShotScript

        expected = {
            "city_crosswalk_shotscript.json": "city_crosswalk",
            "forest_path_shotscript.json": "forest_path",
            "studio_room_shotscript.json": "studio_room",
        }
        for filename, preset in expected.items():
            with self.subTest(filename=filename):
                script = ShotScript.from_path(Path("examples") / filename)
                self.assertEqual(script.scene_id, preset)
                self.assertEqual(script.environment_preset, preset)
                self.assertEqual(len(script.shots), 1)


if __name__ == "__main__":
    unittest.main()
