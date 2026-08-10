"""Deterministic complex-scene prompt/spec bank for the full-chain probe.

The specs are intentionally explicit: the Blender stage can render a shared
world without guessing hidden interactions, while the appearance prompt tells
Seedance to replace clay appearance only.
"""

from __future__ import annotations

from copy import deepcopy


def _point(frame: int, position: tuple[float, float, float], yaw: float = 0.0) -> dict:
    return {"frame": frame, "position": list(position), "rotation": [0.0, 0.0, yaw]}


def _camera(camera_id: str, role: str, target: str, start, middle, end) -> dict:
    early = tuple(start[index] * 0.66 + middle[index] * 0.34 for index in range(3))
    late = tuple(middle[index] * 0.66 + end[index] * 0.34 for index in range(3))
    return {
        "camera_id": camera_id,
        "role": role,
        "target": target,
        "lens_mm": 42.0 if camera_id == "master" else 48.0,
        "roll_deg": 0.0,
        "points": [
            _point(0, start),
            _point(30, early),
            _point(60, middle),
            _point(90, late),
            _point(119, end),
        ],
    }


def _entity(entity_id: str, kind: str, asset: str, asset_id: str | None = None) -> dict:
    entity = {"id": entity_id, "kind": kind, "asset": asset}
    if asset_id is not None:
        entity["asset_id"] = asset_id
    return entity


def _planner(route: str, family: str, landmarks: list[str], gates: list[str]) -> dict:
    return {
        "prompt_route": {
            "planning_mode": route,
            "route_confidence": "high",
            "route_evidence": ["multiple people and objects", "ordered motion phases", "camera coverage"],
            "target_capabilities": ["multi_character_action", "object_coupling", "camera_trajectory"],
            "prompt_critical_requirements": landmarks,
            "white_clay_fit": "medium",
            "white_clay_limitations": ["identity, clothing and material realism are appearance-only"],
            "do_not_invent": ["no extra people", "no cuts", "no independent worlds per camera"],
        },
        "render_policy": {
            "render_value": "secondary",
            "template_family": family,
            "template_requirements": landmarks,
            "preview_landmarks": landmarks[:5],
            "quality_gates": gates,
            "downweight_or_skip_reason": "human appearance is not represented by clay",
        },
    }


PLAZA_DANCE = {
    "scene_id": "plaza_dance_circle",
    "title": "广场舞者与街头乐手",
    "prompt": (
        "One continuous five-second plaza performance in one shared world. Three people share the same timeline: "
        "a lead dancer moves from left to center, waves to the musician at the onset, performs two readable side-step "
        "dance phases with alternating arm positions, then turns toward a passerby; the musician remains beside a small "
        "speaker and raises one hand on the beat; a passerby crosses behind them and briefly waves back. A backpack "
        "stays attached to the dancer, the speaker stays on the ground, and no person or prop teleports. "
        "Use four synchronized camera responsibilities: master orbit, lateral follow, reverse continuity, and elevated wide."
    ),
    "entities": [
        _entity("person_a", "character", "adult_dancer_proxy", "human_male_quaternius_v1"),
        _entity("person_b", "character", "street_musician_proxy", "human_female_v1"),
        _entity("person_c", "character", "crossing_passerby_proxy", "human_female_v1"),
        _entity("backpack", "object", "backpack_proxy"),
        _entity("speaker", "object", "speaker_proxy"),
        _entity("bench", "object", "bench_proxy"),
    ],
    "tracks": [
        {"target_id": "person_a", "kind": "character", "points": [_point(0, (-3.6, -0.5, 0), 0.0), _point(30, (-1.8, -0.3, 0), 0.1), _point(60, (0.0, 0.0, 0), 0.2), _point(90, (1.0, 0.2, 0), 0.7), _point(119, (1.7, 0.4, 0), 1.2)]},
        {"target_id": "person_b", "kind": "character", "points": [_point(0, (1.8, 1.0, 0), 3.1), _point(30, (1.8, 1.0, 0), 3.1), _point(60, (1.8, 1.0, 0), 3.2), _point(90, (1.7, 1.0, 0), 3.2), _point(119, (1.7, 1.0, 0), 3.2)]},
        {"target_id": "person_c", "kind": "character", "points": [_point(0, (4.2, 2.2, 0), -1.4), _point(30, (2.4, 1.8, 0), -1.4), _point(60, (0.8, 1.5, 0), -1.5), _point(90, (-1.0, 1.4, 0), -1.6), _point(119, (-2.8, 1.3, 0), -1.6)]},
        {"target_id": "backpack", "kind": "object", "points": [_point(0, (-3.4, -0.35, 1.0), 0.0), _point(30, (-1.6, -0.15, 1.0), 0.1), _point(60, (0.2, 0.15, 1.0), 0.2), _point(90, (1.2, 0.35, 1.0), 0.7), _point(119, (1.9, 0.55, 1.0), 1.2)]},
        {"target_id": "speaker", "kind": "object", "points": [_point(0, (2.2, 1.0, 0.35)), _point(30, (2.2, 1.0, 0.35)), _point(60, (2.2, 1.0, 0.35)), _point(90, (2.2, 1.0, 0.35)), _point(119, (2.2, 1.0, 0.35))]},
        {"target_id": "bench", "kind": "object", "points": [_point(0, (-1.8, 2.8, 0.45)), _point(30, (-1.8, 2.8, 0.45)), _point(60, (-1.8, 2.8, 0.45)), _point(90, (-1.8, 2.8, 0.45)), _point(119, (-1.8, 2.8, 0.45))]},
    ],
    "cameras": [
        _camera("master", "front orbit of dancer and musician", "person_a", (0, -8.0, 3.4), (1.0, -7.4, 3.6), (2.5, -6.8, 3.9)),
        _camera("lateral", "left-to-right lateral follow", "person_a", (-8, -2, 3.0), (-6, -1, 3.2), (-4, 0, 3.4)),
        _camera("reverse", "reverse continuity across the plaza", "person_b", (7, 6, 4.2), (6, 5, 4.0), (5, 4, 3.8)),
        _camera("elevated", "elevated wide circle coverage", "person_a", (0, 4, 9), (1, 3, 9), (2, 2, 9)),
    ],
    "action_phases": [
        "K0: all three people and grounded props are visible",
        "K1: dancer waves toward musician while entering center",
        "K2: dancer performs first side-step with left arm raised",
        "K3: dancer performs second side-step and passerby waves back",
        "K4: dancer turns toward passerby; backpack remains coupled",
    ],
    "physical_events": [
        {"id": "dancer_wave", "frame": 30, "type": "gesture_onset", "participants": ["person_a", "person_b"]},
        {"id": "passerby_wave", "frame": 90, "type": "gesture_response", "participants": ["person_a", "person_c"]},
    ],
    "gesture_tracks": [
        {"target_id": "person_a", "limb": "left_arm", "points": [(0, 0.0), (30, -0.8), (60, 0.9), (90, -0.6), (119, 0.0)]},
        {"target_id": "person_a", "limb": "right_arm", "points": [(0, 0.0), (30, 0.4), (60, -0.4), (90, 0.5), (119, 0.0)]},
        {"target_id": "person_b", "limb": "right_arm", "points": [(0, 0.0), (30, 0.7), (60, -0.7), (90, 0.7), (119, 0.0)]},
        {"target_id": "person_c", "limb": "right_arm", "points": [(0, 0.0), (30, 0.0), (60, 0.0), (90, 0.8), (119, 0.0)]},
    ],
    "appearance_prompt": (
        "Appearance-only edit for one continuous five-second plaza performance. Keep exactly three people, the dancer's "
        "backpack, the grounded speaker and bench, all relative positions, trajectories, wave timing, two side-step dance "
        "phases, passerby crossing, occlusion order, and all four camera responsibilities. Replace clay geometry with "
        "natural live-action people and believable plaza objects. Keep identities and outfits stable; preserve the dancer's "
        "backpack coupling. No cuts, no extra people, no duplicate speaker, no white clay cylinders, posts, labels or guide lines."
    ),
    "planner": _planner("motion_or_action", "human_action_proxy", ["three people", "wave onset", "two dance phases", "backpack coupling", "four camera roles"], ["no identity count change", "no teleportation", "speaker stays grounded"]),
}


PARK_BADMINTON = {
    "scene_id": "park_badminton", "title": "公园羽毛球练习",
    "prompt": (
        "One continuous five-second park practice in one shared world. Two players face each other across a short net: "
        "the left player steps forward and serves, the right player retreats and returns, then both recover to ready poses. "
        "A spectator on a bench raises a hand in a fine-grained wave. The shuttlecock follows a visible arc from racket to "
        "racket, never disappears, and the net and bench stay fixed. Four synchronized cameras cover master, side tracking, "
        "reverse player view, and high diagonal wide coverage without cuts."
    ),
    "entities": [
        _entity("person_a", "character", "left_player_proxy", "human_male_quaternius_v1"), _entity("person_b", "character", "right_player_proxy", "human_female_v1"),
        _entity("spectator", "character", "spectator_proxy", "human_female_v1"), _entity("racket_a", "object", "racket_proxy"),
        _entity("racket_b", "object", "racket_proxy"), _entity("shuttlecock", "object", "shuttlecock_proxy"),
        _entity("net", "object", "net_proxy"), _entity("bench", "object", "bench_proxy"),
    ],
    "tracks": [
        {"target_id": "person_a", "kind": "character", "points": [_point(0, (-2.6, 0, 0), 0.0), _point(30, (-1.5, 0, 0), 0.0), _point(60, (-1.1, 0, 0), 0.2), _point(90, (-1.8, 0, 0), 0.1), _point(119, (-2.0, 0, 0), 0.0)]},
        {"target_id": "person_b", "kind": "character", "points": [_point(0, (2.6, 0, 0), 3.14), _point(30, (2.9, 0, 0), 3.14), _point(60, (2.0, 0, 0), 3.0), _point(90, (1.4, 0, 0), 3.0), _point(119, (2.0, 0, 0), 3.14)]},
        {"target_id": "spectator", "kind": "character", "points": [_point(0, (-1.5, 3.0, 0), -1.2), _point(30, (-1.5, 3.0, 0), -1.2), _point(60, (-1.5, 3.0, 0), -1.2), _point(90, (-1.5, 3.0, 0), -1.2), _point(119, (-1.5, 3.0, 0), -1.2)]},
        {"target_id": "racket_a", "kind": "object", "points": [_point(0, (-2.3, -0.25, 1.0), 0.0), _point(30, (-1.4, -0.2, 1.5), 0.5), _point(60, (-1.0, -0.2, 1.3), -0.4), _point(90, (-1.7, -0.2, 1.0), 0.0), _point(119, (-1.9, -0.2, 1.0), 0.0)]},
        {"target_id": "racket_b", "kind": "object", "points": [_point(0, (2.3, -0.25, 1.0), 3.14), _point(30, (2.5, -0.25, 1.1), 2.7), _point(60, (1.9, -0.25, 1.5), 2.2), _point(90, (1.4, -0.25, 1.2), 2.8), _point(119, (1.9, -0.25, 1.0), 3.14)]},
        {"target_id": "shuttlecock", "kind": "object", "points": [_point(0, (-1.8, 0, 1.2)), _point(30, (-0.5, 0, 3.0)), _point(60, (0.8, 0, 2.7)), _point(90, (1.8, 0, 1.6)), _point(119, (1.9, 0, 1.3))]},
        {"target_id": "net", "kind": "object", "points": [_point(0, (0, 0, 0.8)), _point(30, (0, 0, 0.8)), _point(60, (0, 0, 0.8)), _point(90, (0, 0, 0.8)), _point(119, (0, 0, 0.8))]},
        {"target_id": "bench", "kind": "object", "points": [_point(0, (-1.5, 3.0, 0.45)), _point(30, (-1.5, 3.0, 0.45)), _point(60, (-1.5, 3.0, 0.45)), _point(90, (-1.5, 3.0, 0.45)), _point(119, (-1.5, 3.0, 0.45))]},
    ],
    "cameras": [
        _camera("master", "front master showing both players and net", "shuttlecock", (0, -8.2, 3.4), (0, -7.5, 3.5), (0, -6.8, 3.6)),
        _camera("lateral", "side tracking along the net", "shuttlecock", (-8, -2, 3.0), (-7, -1, 3.1), (-6, 0, 3.2)),
        _camera("reverse", "reverse player return view", "person_b", (7, 7, 3.5), (6, 6, 3.4), (5, 5, 3.3)),
        _camera("elevated", "high diagonal arc view", "shuttlecock", (0, 3, 9), (1, 2, 9), (2, 1, 9)),
    ],
    "action_phases": [
        "K0: two players, rackets, shuttlecock, net, bench and spectator visible",
        "K1: left player steps in and serves; shuttlecock rises",
        "K2: shuttlecock reaches apex while right player retreats",
        "K3: right player returns and spectator performs a visible wave",
        "K4: both players recover to ready poses; shuttlecock remains visible",
    ],
    "physical_events": [{"id": "serve_contact", "frame": 30, "type": "racket_contact", "participants": ["person_a", "racket_a", "shuttlecock"]}, {"id": "return_contact", "frame": 90, "type": "racket_contact", "participants": ["person_b", "racket_b", "shuttlecock"]}],
    "gesture_tracks": [
        {"target_id": "person_a", "limb": "right_arm", "points": [(0, 0.0), (30, -0.9), (60, 0.3), (90, 0.0), (119, 0.0)]},
        {"target_id": "person_b", "limb": "left_arm", "points": [(0, 0.0), (30, 0.0), (60, 0.8), (90, -0.9), (119, 0.0)]},
        {"target_id": "spectator", "limb": "right_arm", "points": [(0, 0.0), (30, 0.0), (60, 0.0), (90, 0.9), (119, 0.0)]},
    ],
    "appearance_prompt": (
        "Appearance-only edit for one continuous five-second badminton practice. Preserve exactly two players, one spectator, "
        "two rackets, one shuttlecock, one net and one bench; preserve the serve, arc, retreat, return, spectator wave, "
        "contact order, grounded net/bench and all four camera roles. Replace clay with natural live-action people and park "
        "materials. Keep the shuttlecock visible and physically continuous. No cuts, no extra rackets or balls, no duplicate "
        "spectator, no white clay geometry, labels or guide lines."
    ),
    "planner": _planner("motion_or_action", "human_action_proxy", ["two players", "serve contact", "shuttlecock arc", "return contact", "spectator wave"], ["one shuttlecock", "net fixed", "no teleportation"]),
}


INDOOR_MARKET = {
    "scene_id": "indoor_market_exchange", "title": "室内市场推车交接",
    "prompt": (
        "One continuous five-second indoor market exchange in one shared world. A vendor stands behind a counter while a "
        "customer pushes a handcart from left to right with two boxes; a helper walks behind, turns, and gestures toward the "
        "counter. The customer pauses, raises one hand before the exchange, then resumes the cart path. Two loose paper sheets "
        "lift from the cart, drift briefly in a small granular-like flutter, and settle near the counter; the cart remains in "
        "contact with its wheels and the boxes stay on it. Four synchronized cameras cover master, cart-side follow, reverse "
        "vendor view, and overhead layout without cuts or layout changes."
    ),
    "entities": [
        _entity("vendor", "character", "vendor_proxy", "human_female_v1"), _entity("customer", "character", "customer_proxy", "human_male_quaternius_v1"), _entity("helper", "character", "helper_proxy", "human_female_v1"),
        _entity("handcart", "object", "handcart_proxy"), _entity("box_a", "object", "box_proxy"), _entity("box_b", "object", "box_proxy"),
        _entity("paper_a", "object", "paper_sheet_proxy"), _entity("paper_b", "object", "paper_sheet_proxy"), _entity("counter", "object", "counter_proxy"),
    ],
    "tracks": [
        {"target_id": "vendor", "kind": "character", "points": [_point(0, (3.2, 1.6, 0), 3.14), _point(30, (3.2, 1.6, 0), 3.14), _point(60, (3.2, 1.6, 0), 3.0), _point(90, (3.2, 1.6, 0), 3.0), _point(119, (3.2, 1.6, 0), 3.0)]},
        {"target_id": "customer", "kind": "character", "points": [_point(0, (-4.0, -0.8, 0), 0.0), _point(30, (-2.8, -0.8, 0), 0.0), _point(60, (-1.4, -0.8, 0), 0.2), _point(90, (0.0, -0.8, 0), 0.2), _point(119, (1.2, -0.8, 0), 0.2)]},
        {"target_id": "helper", "kind": "character", "points": [_point(0, (-3.0, 2.0, 0), -1.2), _point(30, (-2.0, 2.0, 0), -1.0), _point(60, (-1.0, 2.1, 0), -0.8), _point(90, (0.2, 2.0, 0), -0.5), _point(119, (1.3, 2.0, 0), -0.4)]},
        {"target_id": "handcart", "kind": "object", "points": [_point(0, (-3.8, -0.8, 0.5)), _point(30, (-2.6, -0.8, 0.5)), _point(60, (-1.2, -0.8, 0.5)), _point(90, (0.2, -0.8, 0.5)), _point(119, (1.4, -0.8, 0.5))]},
        {"target_id": "box_a", "kind": "object", "points": [_point(0, (-3.8, -0.8, 1.2)), _point(30, (-2.6, -0.8, 1.2)), _point(60, (-1.2, -0.8, 1.2)), _point(90, (0.2, -0.8, 1.2)), _point(119, (1.4, -0.8, 1.2))]},
        {"target_id": "box_b", "kind": "object", "points": [_point(0, (-3.8, -0.1, 1.2)), _point(30, (-2.6, -0.1, 1.2)), _point(60, (-1.2, -0.1, 1.2)), _point(90, (0.2, -0.1, 1.2)), _point(119, (1.4, -0.1, 1.2))]},
        {"target_id": "paper_a", "kind": "object", "points": [_point(0, (-3.4, -0.5, 1.7)), _point(30, (-2.2, -0.5, 1.7)), _point(60, (-1.2, -0.2, 2.8)), _point(90, (0.2, 0.2, 1.4)), _point(119, (1.0, 0.4, 0.05))]},
        {"target_id": "paper_b", "kind": "object", "points": [_point(0, (-3.2, -0.2, 1.8)), _point(30, (-2.0, -0.2, 1.8)), _point(60, (-0.8, 0.0, 2.5)), _point(90, (0.4, 0.4, 1.3)), _point(119, (1.3, 0.6, 0.06))]},
        {"target_id": "counter", "kind": "object", "points": [_point(0, (3.0, 1.6, 1.0)), _point(30, (3.0, 1.6, 1.0)), _point(60, (3.0, 1.6, 1.0)), _point(90, (3.0, 1.6, 1.0)), _point(119, (3.0, 1.6, 1.0))]},
    ],
    "cameras": [
        _camera("master", "front master showing cart and counter", "handcart", (0, -9.0, 3.6), (1, -8.2, 3.7), (2, -7.4, 3.8)),
        _camera("lateral", "cart-side follow", "handcart", (-8, -3, 3.0), (-6, -2, 3.1), (-4, -1, 3.2)),
        _camera("reverse", "reverse vendor-facing view", "vendor", (8, 6, 4.0), (7, 5, 4.0), (6, 4, 3.8)),
        _camera("elevated", "overhead layout and paper flutter", "handcart", (0, 2, 10), (1, 1, 10), (2, 0, 10)),
    ],
    "action_phases": [
        "K0: vendor, customer, helper, cart, two boxes, two papers and counter visible",
        "K1: customer pushes cart and raises a hand before exchange",
        "K2: helper turns and gestures toward counter while cart continues",
        "K3: two papers flutter upward/outward from cart and begin settling",
        "K4: cart reaches counter; papers rest on floor; boxes remain on cart",
    ],
    "physical_events": [{"id": "customer_gesture", "frame": 30, "type": "gesture_onset", "participants": ["customer", "handcart"]}, {"id": "paper_flutter", "frame": 60, "type": "granular_flutter", "participants": ["paper_a", "paper_b", "handcart"]}],
    "gesture_tracks": [
        {"target_id": "customer", "limb": "right_arm", "points": [(0, 0.0), (30, 0.8), (60, 0.2), (90, 0.0), (119, 0.0)]},
        {"target_id": "helper", "limb": "right_arm", "points": [(0, 0.0), (30, 0.0), (60, 0.7), (90, -0.5), (119, 0.0)]},
    ],
    "appearance_prompt": (
        "Appearance-only edit for one continuous five-second indoor market exchange. Preserve exactly three people, one handcart, "
        "two boxes, two loose paper sheets and one counter; preserve the cart and boxes coupling, customer hand gesture, helper "
        "turn, paper flutter/settling order, all relative layout and all four camera roles. Replace clay with natural live-action "
        "people and believable market materials. Keep paper motion small and causal, no cuts, no extra people or boxes, no "
        "duplicate cart, no white clay posts/cubes/labels or guide lines."
    ),
    "planner": _planner("motion_or_action", "granular_or_powder", ["three people", "cart trajectory", "two boxes stay coupled", "hand gesture", "paper flutter and settle"], ["paper source stays at cart", "no teleportation", "counter fixed"]),
}


WAREHOUSE_LOADING = {
    "scene_id": "warehouse_loading_maneuver",
    "title": "Warehouse loading maneuver",
    "prompt": (
        "One continuous five-second loading-bay maneuver in one shared world. A worker grips a loaded handcart and rolls it "
        "from left to right along a marked floor lane; the cart wheels stay grounded and two boxes remain coupled to the cart. "
        "At the braking point the worker stops the cart, raises one hand to signal the supervisor, then resumes the push. An "
        "assistant walks behind the cart, turns toward the receiving counter, and points to the drop-off area. The braking event "
        "causes two lightweight packing slips to lift from the top box, drift in a short irregular flutter, and settle beside the "
        "counter; they do not teleport and the boxes never leave the cart. Four synchronized cameras use a pronounced dolly-in "
        "master, a cart-side tracking shot, a reverse arc toward the counter, and a descending overhead pull-in, with continuous "
        "look-at changes and no cuts."
    ),
    "entities": [
        _entity("vendor", "character", "supervisor_proxy", "human_female_v1"),
        _entity("customer", "character", "worker_proxy", "human_male_quaternius_v1"),
        _entity("helper", "character", "assistant_proxy", "human_female_v1"),
        _entity("handcart", "object", "loading_cart_proxy"),
        _entity("box_a", "object", "large_box_proxy"),
        _entity("box_b", "object", "small_box_proxy"),
        _entity("paper_a", "object", "packing_slip_proxy"),
        _entity("paper_b", "object", "packing_slip_proxy"),
        _entity("counter", "object", "receiving_counter_proxy"),
    ],
    "tracks": [
        {"target_id": "vendor", "kind": "character", "points": [_point(0, (1.8, 1.0, 0), 3.14), _point(30, (1.8, 1.0, 0), 3.14), _point(60, (1.8, 1.0, 0), 3.0), _point(90, (1.8, 1.0, 0), 3.0), _point(119, (1.8, 1.0, 0), 3.0)]},
        {"target_id": "customer", "kind": "character", "points": [_point(0, (-4.0, -0.8, 0), 0.0), _point(30, (-2.8, -0.8, 0), 0.0), _point(60, (-1.8, -0.8, 0), 0.15), _point(90, (-1.8, -0.8, 0), 0.15), _point(119, (1.5, -0.8, 0), 0.2)]},
        {"target_id": "helper", "kind": "character", "points": [_point(0, (-3.4, 1.1, 0), -1.2), _point(30, (-2.6, 1.0, 0), -1.0), _point(60, (-1.8, 1.0, 0), -0.8), _point(90, (-0.2, 0.7, 0), -0.45), _point(119, (0.8, 0.5, 0), -0.25)]},
        {"target_id": "handcart", "kind": "object", "points": [_point(0, (-3.8, -0.8, 0)), _point(30, (-2.6, -0.8, 0)), _point(60, (-1.6, -0.8, 0)), _point(90, (-1.6, -0.8, 0)), _point(119, (1.7, -0.8, 0))]},
        {"target_id": "box_a", "kind": "object", "points": [_point(0, (-3.8, -0.8, 0.72)), _point(30, (-2.6, -0.8, 0.72)), _point(60, (-1.6, -0.8, 0.72)), _point(90, (-1.6, -0.8, 0.72)), _point(119, (1.7, -0.8, 0.72))]},
        {"target_id": "box_b", "kind": "object", "points": [_point(0, (-3.8, -0.1, 0.72)), _point(30, (-2.6, -0.1, 0.72)), _point(60, (-1.6, -0.1, 0.72)), _point(90, (-1.6, -0.1, 0.72)), _point(119, (1.7, -0.1, 0.72))]},
        {"target_id": "paper_a", "kind": "object", "points": [_point(0, (-3.4, -0.45, 1.18)), _point(30, (-2.2, -0.45, 1.18)), _point(60, (-1.3, -0.35, 1.65)), _point(90, (-0.3, 0.0, 1.05)), _point(119, (0.9, 0.5, 0.06))]},
        {"target_id": "paper_b", "kind": "object", "points": [_point(0, (-3.2, -0.1, 1.20)), _point(30, (-2.0, -0.1, 1.20)), _point(60, (-1.0, -0.1, 1.85)), _point(90, (0.0, 0.2, 1.10)), _point(119, (1.1, 0.6, 0.08))]},
        {"target_id": "counter", "kind": "object", "points": [_point(0, (1.8, 1.0, 0)), _point(30, (1.8, 1.0, 0)), _point(60, (1.8, 1.0, 0)), _point(90, (1.8, 1.0, 0)), _point(119, (1.8, 1.0, 0))]},
    ],
    "cameras": [
        _camera("master", "pronounced dolly-in from loading-bay wide to the handcart brake and counter", "handcart", (0, -12, 4.0), (0, -8.0, 3.6), (1.0, -4.5, 3.0)),
        _camera("lateral", "cart-side tracking shot matching the left-to-right wheel travel", "handcart", (-10, -2.5, 3.2), (-6, -1.5, 3.0), (-1.5, -0.6, 2.8)),
        _camera("reverse", "reverse arc that moves from behind the worker toward the receiving counter", "counter", (8, 7, 4.4), (5, 4, 3.8), (2.5, 1.5, 3.1)),
        _camera("elevated", "descending crane pull-in showing cart lane, assistant turn and paper landing", "handcart", (0, 8, 12), (1.5, 5, 9), (1.5, 0.5, 6.0)),
    ],
    "action_phases": [
        "K0: worker grips loaded cart; supervisor, assistant, counter, boxes and packing slips are visible",
        "K1: cart rolls left-to-right with continuous wheel contact and assistant follows behind",
        "K2: cart brakes and holds; worker raises one hand before the receiving exchange",
        "K3: assistant turns and points; slips lift from the top box, flutter briefly and begin to descend",
        "K4: worker resumes to the counter; boxes remain coupled and both slips rest beside the counter",
    ],
    "physical_events": [
        {"id": "cart_brake_signal", "frame": 60, "type": "grounded_brake_and_gesture", "participants": ["customer", "handcart"]},
        {"id": "packing_slip_release", "frame": 66, "type": "source_connected_flutter", "participants": ["paper_a", "paper_b", "box_a", "handcart"]},
    ],
    "gesture_tracks": [
        {"target_id": "customer", "limb": "right_arm", "points": [(0, 0.0), (48, 0.0), (60, 1.2), (84, 1.2), (96, 0.0), (119, 0.0)]},
        {"target_id": "helper", "limb": "left_arm", "points": [(0, 0.0), (60, 0.0), (78, 1.0), (96, 1.0), (108, 0.0), (119, 0.0)]},
    ],
    "appearance_prompt": (
        "Appearance-only edit for one continuous five-second loading-bay maneuver. Preserve exactly three people, one handcart, "
        "two coupled boxes, two packing slips and one receiving counter; preserve the left-to-right cart travel, true brake pause, "
        "worker hand signal, assistant turn/point, source-connected slip flutter and final landing, plus all four moving camera roles. "
        "Replace the proxy with natural live-action people and believable loading-bay materials. No cuts, no extra people, no duplicate "
        "cart or boxes, no mannequin geometry, no guide lines or storyboard overlays."
    ),
    "planner": _planner("motion_or_action", "human_action_proxy", ["grounded cart roll", "brake pause", "worker hand signal", "assistant turn", "source-connected packing-slip flutter", "four moving camera paths"], ["wheels stay grounded", "boxes remain coupled", "pause precedes signal", "slips originate on box", "no teleportation", "camera positions and look-at continuity are auditable"]),
}


SCENES = [PLAZA_DANCE, PARK_BADMINTON, INDOOR_MARKET, WAREHOUSE_LOADING]
_BY_ID = {scene["scene_id"]: scene for scene in SCENES}


def scene_spec(scene_id: str) -> dict:
    try:
        return deepcopy(_BY_ID[scene_id])
    except KeyError as exc:
        raise KeyError(f"unknown complex scene {scene_id!r}") from exc


def iter_scene_specs():
    for scene in SCENES:
        yield scene_spec(scene["scene_id"])
