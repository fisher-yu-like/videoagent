# Scene Props and Toast Proxy Design

Date: 2026-08-04

## Goal

Make the deterministic Blender Reference Proxy visibly match a scene plan that
contains furniture, hand-held props, and the lightweight `toast` action. Keep
DeepSeek limited to validated JSON planning; it must never emit executable
Blender code.

## Confirmed defect

`ScenePlanDraft.to_shotscript()` currently drops `objects`. The trajectory
contract retains only target IDs and XY paths, so Blender loses primitive,
dimensions, elevation, and attachment information. It consequently creates a
small cube at floor height for every object. Actor action `toast` is also absent
from the deterministic action vocabulary and is degraded to `stand`.

## Contract changes

Scene objects retain the existing fields and may add:

- `dimensions`: three positive finite values `[width, depth, height]` in Blender
  world units.
- `base_z`: a finite non-negative world-space support height.
- `attached_to`: either `null` or an actor ID in the same scene.

New agent-generated scene plans must provide all three fields. The parser keeps
legacy object documents readable by assigning the existing generic proxy size,
`dimensions=[0.48, 0.36, 0.36]`, `base_z=0.10`, and no attachment when the
fields are absent. These defaults preserve the old cube's size and center
height. Unknown fields remain rejected.

`toast` is added to the closed actor-action set. It is a proxy-level pose label,
not a claim of articulated natural-motion generation.

ShotScript gains an optional root `objects` collection with the same executable
descriptors. Old ShotScripts without it remain valid. ScenePlan compilation must
preserve every object descriptor exactly.

## Deterministic Blender behavior

Before trajectory application, Blender creates one `prop__<id>` object for each
ShotScript object using its declared primitive and dimensions. Unattached
objects use `(start.x, start.y, base_z + height / 2)` as their initial center.
Trajectory application updates XY while retaining this computed Z and existing
geometry; it must not replace the object with the old fallback cube.

An object with `attached_to=<actor ID>` is parented to that actor's right lower
arm anchor with a fixed hand-relative offset. Its independent XY trajectory is
kept in evidence but does not override the attachment transform. The render
manifest records primitive, dimensions, base height, attachment, and whether the
object trajectory was applied or superseded by attachment.

For actor action `toast`, the existing humanoid anchors receive a restrained
raised-arm pose throughout the shot. Actor translation and facing continue to
use the existing motion compiler. Other actions retain current behavior.

## Frontend

The existing object editor remains in the seven-stage wizard. Each row adds
compact fields for width, depth, height, base height, and optional attached actor.
Small Chinese helper text explains that attachment makes the prop follow the
actor's hand. No second frontend or advanced asset browser is introduced.

## Current workspace migration

The implementation must not modify approved SP1 or failed/incorrect R1 in
`runs/work/my_story`. After the code passes verification, create a new local
unapproved ScenePlan version derived from SP1 with:

- three actors using `toast`;
- one round table at floor height with visible table dimensions;
- one drinking glass attached to each corresponding actor;
- no new DeepSeek API call.

The user must approve the new ScenePlan and render a new Reference from the
frontend. Historical hashes and results remain unchanged.

## Error handling

- Reject non-finite dimensions, dimensions outside `(0, 50]`, and `base_z`
  outside `[0, 50]` Blender world units.
- Reject attachments to missing actors.
- Reject duplicate actor/object IDs as before.
- Fail rendering when a declared prop or attachment anchor cannot be created;
  never silently drop it.

## Verification

Tests must first demonstrate the current failure, then cover:

- ScenePlan object metadata preservation into ShotScript;
- legacy ScenePlan and ShotScript compatibility;
- invalid dimensions, elevation, and attachment rejection;
- DeepSeek request enum/schema instructions for `toast` and object metadata;
- Blender creation of cylinder/cube/sphere props with declared dimensions;
- attached glass following the correct actor hand;
- toast arm-anchor transforms;
- manifest evidence for every declared object;
- existing director-wizard, trajectory, and Blender integration suites.

A real Blender smoke render must show a visible table, three hand-held glasses,
and three raised-arm actors before completion is reported.
