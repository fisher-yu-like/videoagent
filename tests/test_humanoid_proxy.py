"""Pure contract tests for the neutral articulated humanoid proxy."""

from dataclasses import FrozenInstanceError
import math
import unittest

from videoactagent.humanoid_proxy import (
    HUMANOID_PARTS,
    HumanoidPartSpec,
    path_heading_degrees,
    unwrap_heading_degrees,
)


EXPECTED_PARTS = (
    "head",
    "torso",
    "pelvis",
    "upper_arm.L",
    "lower_arm.L",
    "upper_arm.R",
    "lower_arm.R",
    "upper_leg.L",
    "lower_leg.L",
    "upper_leg.R",
    "lower_leg.R",
)


class HumanoidPartSpecTests(unittest.TestCase):
    def test_parts_have_complete_stable_bilateral_structure(self):
        self.assertEqual(tuple(part.name for part in HUMANOID_PARTS), EXPECTED_PARTS)
        self.assertEqual(len({part.name for part in HUMANOID_PARTS}), 11)
        self.assertEqual(
            {part.name.removesuffix(".L") for part in HUMANOID_PARTS if part.name.endswith(".L")},
            {part.name.removesuffix(".R") for part in HUMANOID_PARTS if part.name.endswith(".R")},
        )

    def test_part_records_and_nested_coordinates_are_immutable(self):
        part = HUMANOID_PARTS[0]
        self.assertIsInstance(part, HumanoidPartSpec)
        with self.assertRaises(FrozenInstanceError):
            part.name = "changed"
        with self.assertRaises(TypeError):
            part.local_center[0] = 99.0

    def test_parts_define_supported_primitives_and_resolvable_parents(self):
        known = {part.name for part in HUMANOID_PARTS}
        for part in HUMANOID_PARTS:
            with self.subTest(part=part.name):
                self.assertIn(part.primitive, {"cube", "cylinder", "ico_sphere"})
                self.assertTrue(all(value > 0 for value in part.local_scale))
                self.assertTrue(part.parent == "root" or part.parent in known)

    def test_proportions_read_as_a_neutral_human(self):
        parts = {part.name: part for part in HUMANOID_PARTS}

        def world_anchor(name):
            part = parts[name]
            if part.parent == "root":
                return part.anchor
            parent = world_anchor(part.parent)
            return tuple(a + b for a, b in zip(parent, part.anchor))

        def world_center(name):
            anchor = world_anchor(name)
            return tuple(a + b for a, b in zip(anchor, parts[name].local_center))

        head = world_center("head")
        torso = world_center("torso")
        pelvis = world_center("pelvis")
        self.assertGreater(head[2], torso[2])
        self.assertGreater(torso[2], pelvis[2])
        for side, sign in (("L", 1), ("R", -1)):
            arm = world_center(f"upper_arm.{side}")
            upper_leg = world_center(f"upper_leg.{side}")
            lower_leg = world_center(f"lower_leg.{side}")
            self.assertGreater(sign * arm[0], sign * torso[0])
            self.assertLess(upper_leg[2], pelvis[2])
            self.assertLess(lower_leg[2], upper_leg[2])


class PathHeadingTests(unittest.TestCase):
    def test_forward_segment_is_zero_degrees(self):
        self.assertAlmostEqual(path_heading_degrees(((0, 0), (3, 0)), 0), 0.0)

    def test_up_segment_is_ninety_degrees(self):
        self.assertAlmostEqual(path_heading_degrees(((0, 0), (0, 3)), 0), 90.0)

    def test_next_non_stationary_segment_skips_duplicates(self):
        points = ((1, 2), (1, 2), (1, 2), (0, 2))
        self.assertAlmostEqual(path_heading_degrees(points, 0), 180.0)

    def test_prior_non_stationary_segment_is_fallback(self):
        points = ((0, 0), (0, -2), (0, -2), (0, -2))
        self.assertAlmostEqual(path_heading_degrees(points, 2), -90.0)

    def test_fully_stationary_path_returns_zero(self):
        self.assertEqual(path_heading_degrees(((4, 5), (4, 5), (4, 5)), 1), 0.0)

    def test_sub_tolerance_noise_is_stationary_deterministically(self):
        points = ((0.0, 0.0), (1e-12, -1e-12), (0.0, 2.0))
        self.assertAlmostEqual(path_heading_degrees(points, 0), 90.0)

    def test_non_finite_coordinates_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite"):
                path_heading_degrees(((0.0, 0.0), (value, 1.0)), 0)

    def test_heading_sequence_unwraps_across_atan_discontinuity(self):
        self.assertEqual(unwrap_heading_degrees((170.0, -170.0, -160.0)), (170.0, 190.0, 200.0))

    def test_heading_sequence_unwrap_is_finite_and_immutable(self):
        result = unwrap_heading_degrees((0, 90, 180))
        self.assertEqual(result, (0.0, 90.0, 180.0))
        with self.assertRaisesRegex(ValueError, "finite"):
            unwrap_heading_degrees((0.0, math.inf))


if __name__ == "__main__":
    unittest.main()
