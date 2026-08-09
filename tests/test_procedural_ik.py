import math

from videoactagent.procedural_ik import clamp_target_to_reach, solve_two_link_ik


def distance(a, b):
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def test_two_link_ik_preserves_bone_lengths_for_reachable_target():
    result = solve_two_link_ik((0.0, 0.0, 2.0), (0.6, 0.2, 1.0), 0.7, 0.6, pole_sign=1.0)
    assert result["reachable"] is True
    assert abs(distance(result["root"], result["elbow"]) - 0.7) < 1e-6
    assert abs(distance(result["elbow"], result["target"]) - 0.6) < 1e-6
    assert all(math.isfinite(value) for point in (result["root"], result["elbow"], result["target"]) for value in point)


def test_two_link_ik_clamps_unreachable_target_without_nan():
    result = solve_two_link_ik((0.0, 0.0, 2.0), (5.0, 0.0, 2.0), 0.7, 0.6, pole_sign=-1.0)
    assert result["reachable"] is False
    assert abs(distance(result["root"], result["elbow"]) - 0.7) < 1e-6
    assert abs(distance(result["elbow"], result["target"]) - 0.6) < 1e-6
    assert distance(result["root"], result["target"]) <= 1.3 + 1e-6


def test_clamp_target_to_reach_handles_zero_length_direction():
    target, reachable = clamp_target_to_reach((1.0, 2.0, 3.0), (1.0, 2.0, 3.0), 0.7, 0.6)
    assert reachable is False
    assert target[2] < 3.0
    assert all(math.isfinite(value) for value in target)
