"""Prompt variants for the complex-story multiview capability probe."""

from __future__ import annotations


VIEW_ROLES = {
    3: (
        "[Video1] master front tracking view, [Video2] lateral follow view, "
        "[Video3] reverse continuity view"
    ),
    5: (
        "[Video1] master front tracking view, [Video2] lateral follow view, "
        "[Video3] reverse continuity view, [Video4] wide establishing view, "
        "[Video5] high three-quarter view"
    ),
    8: (
        "[Video1] master front tracking view, [Video2] lateral follow view, "
        "[Video3] reverse continuity view, [Video4] wide front establishing view, "
        "[Video5] low lateral follow view, [Video6] high three-quarter view, "
        "[Video7] front-left high three-quarter view, [Video8] elevated front-right transition view"
    ),
}


def complex_station_story_prompt(view_count: int) -> str:
    if view_count not in VIEW_ROLES:
        raise ValueError("complex story probe supports 3, 5, or 8 views")
    roles = VIEW_ROLES[view_count]
    return (
        "Appearance-only edit instruction for one continuous five-second railway-platform event. "
        "The references are synchronized renders of exactly one shared Blender world and one timeline; "
        f"there are exactly {view_count} synchronized references: {roles}. "
        "Preserve the same friend, traveler, rolling luggage, platform edge, back wall, columns, sign, "
        "relative positions, walking direction, contact timing, occlusion order, lighting direction, and horizon. "
        "Story event order must remain: the traveler walks along the platform with the luggage, approaches the waiting friend, "
        "the friend turns toward the traveler, and the luggage stays coupled to the traveler; no cuts, no new actions, no time jump. "
        "Use the camera roles as coordinated coverage of the same event, not separate scene generations. "
        "Keep camera motion smooth and physically plausible; keep look-at targets stable during each move; keep roll at zero; "
        "do not invent a new camera, person, prop, room layout, or background. "
        "Change appearance only from clay to coherent live-action railway-platform footage; use natural anatomy, cloth, skin, "
        "metal and contact shadows; remove clay geometry, labels, guide lines, and CG overlays."
    )
