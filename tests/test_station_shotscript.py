from pathlib import Path
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
        self.assertEqual(script.fps, 3)
        self.assertEqual([shot.shot_id for shot in script.shots], ["s01", "s02", "s03"])
        self.assertEqual([shot.duration for shot in script.shots], [5.0, 5.0, 5.0])

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


if __name__ == "__main__":
    unittest.main()
