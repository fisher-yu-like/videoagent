# Camera Auto-Initialization Design

## Goal

Remove the ordering dependency between drawing camera points and inheriting camera parameters. When the user selects a frozen boundary, every editable keyframe after that boundary receives a complete camera state from the current source-bound Proxy.

## User-visible behavior

- Selecting K2 freezes K0--K2 and opens K3.
- K3 and K4 immediately contain the current Proxy camera position, look-at point, height, focal length, shot size, interpolation, and roll.
- The page labels these values `来自当前 Proxy，可修改`.
- Drawing a camera position or look-at point replaces only that point's X/Y while retaining all other initialized parameters.
- Editing any camera field changes the label for that keyframe to `人工修改`.
- Actor points and visible-state Prompt remain blank for editable keyframes and must still be supplied by the user.
- Saving an incomplete keyframe lists every missing item by name.

## Provenance and validation

Automatic camera values are source-derived defaults, not human clicks. Each submitted editable keyframe carries a `camera_source` value:

- `inherited`: the complete camera object must exactly equal the matching `session.inherited_keyframes` camera object.
- `human_modified`: at least one camera field must differ from the inherited camera object.

The server recomputes this relationship and rejects false provenance. The existing inheritance SHA-256 remains the source binding. `auto_filled_values` stays zero because no synthetic or guessed value is created; inherited camera values are separately and explicitly identified by `camera_source`.

## Scope

This change affects only the browser director loop, director annotation contract, focused tests, and concise usage text. It does not render Blender, call VACE, use a GPU, or call a paid API.

## Acceptance checks

1. Select K2, draw K3 camera points without clicking an inherit button, and retain complete Z/style parameters.
2. Draw first and then use the inherit action without losing the drawn X/Y values.
3. An unchanged initialized camera is saved as `inherited`.
4. A changed camera is saved as `human_modified`.
5. The server rejects a mismatched `camera_source`.
6. Missing actor or Prompt values are reported by exact field name.
