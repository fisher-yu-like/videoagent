import unittest

from tests.test_multicam_plan import VALID_PLAN
from videoactagent.multicam_plan import load_multicam_plan
from videoactagent.multicam_rig import compile_camera_rig


ACTOR_KEYFRAMES = [
    {
        "id": f"K{index}",
        "t": t,
        "actors": {
            "actor_a": {"x": 0.16 + 0.28 * t, "y": 0.5},
            "actor_b": {"x": 0.67, "y": 0.5},
        },
    }
    for index, t in enumerate((0.0, 0.2, 0.5, 0.8, 1.0))
]


class MulticamRigTests(unittest.TestCase):
    def test_compiles_complete_independent_camera_states(self):
        plan = load_multicam_plan(
            VALID_PLAN,
            scene_id="station_reunion",
            actors=("actor_a", "actor_b"),
        )
        states = compile_camera_rig(
            plan=plan,
            world_bounds=(-5.0, 5.0, -4.0, 4.0),
            actor_keyframes=ACTOR_KEYFRAMES,
        )
        self.assertEqual(set(states), {"camera_a", "camera_b", "camera_c"})
        self.assertTrue(all(len(items) == 5 for items in states.values()))
        self.assertTrue(all(
            items[0].t == 0.0 and items[-1].t == 1.0
            for items in states.values()
        ))
        self.assertEqual(len({states[camera][0].position for camera in states}), 3)
        self.assertEqual(states["camera_b"][0].look_at[0], -3.4)
        self.assertEqual(states["camera_b"][-1].look_at[0], -0.6)

    def test_static_master_position_does_not_follow_actor_midpoint(self):
        plan = load_multicam_plan(
            VALID_PLAN,
            scene_id="station_reunion",
            actors=("actor_a", "actor_b"),
        )
        states = compile_camera_rig(
            plan=plan,
            world_bounds=(-5.0, 5.0, -4.0, 4.0),
            actor_keyframes=ACTOR_KEYFRAMES,
        )
        self.assertEqual(
            {state.position for state in states["camera_a"]},
            {states["camera_a"][0].position},
        )


if __name__ == "__main__":
    unittest.main()
