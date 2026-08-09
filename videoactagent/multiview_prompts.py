"""Controlled Prompt variants for the shared-world multi-view experiment."""

from __future__ import annotations


def station_multiview_prompts(appearance_prompt: str) -> list[str]:
    """Return four prompts that vary camera language, not scene identity."""
    base = appearance_prompt.strip()
    if not base:
        raise ValueError("appearance_prompt must be nonempty")
    shared = (
        "The three references are synchronized views from the same shared Blender world and the same timeline. "
        "[Video1] is the front establishing view, [Video2] is the lateral view, and [Video3] is the reverse view. "
        "Keep the same people, object identities, station layout, relative positions, lighting direction, and event order across views. "
    )
    return [
        base + " " + shared + "Use stable cinematic continuity: preserve the approved composition and readable human motion; no new events.",
        base + " " + shared + "Use a smooth lateral tracking interpretation: camera motion is horizontal and level, subject motion remains continuous, and the background landmarks stay coherent.",
        base + " " + shared + "Use a restrained half-orbit interpretation: move around the subject on a level arc while keeping the look-at target fixed, horizon stable, and roll at zero.",
        base + " " + shared + "Use a controlled push-in interpretation: begin with the wide establishing relation, then move gradually toward a medium framing while preserving the same trajectory and identity.",
    ]
