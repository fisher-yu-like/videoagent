# Human Director Panel Design

## Goal

Build a guided K0–K4 director panel with a complete-video review loop. A human controls actor motion, camera motion, and keyframe-visible intent. Blender regenerates the complete proxy until the human approves one immutable version for VACE.

The feature does not train a model and does not claim that VACE directly consumes trajectory JSON.

## User workflow

The page first plays the complete current diagnostic proxy. Its seek bar exposes K0–K4 markers. The user selects a frozen boundary, reviews inherited trajectory points, edits only later keyframes, and generates the next complete Blender proxy.

The user then watches the new complete video and either starts another revision or selects **Approve current proxy**. Viewing only five still frames is insufficient for approval.

## Revision boundary

The selected K frame is a locked boundary. It and every earlier K frame are read-only. Selecting K2 locks K0, K1, and K2; editing starts at K3. To modify K2, the user must select K1 as the boundary.

The top-down editor always displays inherited actor positions, camera position, and camera look-at point at the boundary. The locked prefix is a muted dashed path, the boundary is labelled as the locked start, and newly authored points form an editable suffix connected to that start.

For D0, boundary values are derived deterministically from the hash-bound ShotScript and the exact Blender interpolation policy. For D1 and later, they come directly from the current human annotation. They are recorded as `inherited_locked_values` with source hashes, not as human clicks and not as detections guessed from video pixels.

## Data model

The existing actor-only `TrajectoryInstruction` remains valid. A versioned `DirectorAnnotation` records:

- author, iteration ID, parent iteration ID, and save time;
- source manifest, ShotScript, semantic plan, proxy, and reference-frame hashes;
- K0–K4 timestamps and frame indices;
- `frozen_through_keyframe` and inherited-prefix source hashes;
- exact inherited actor/camera/prompt values through the boundary;
- newly authored actor positions after the boundary;
- camera position, look-at point, focal length, shot size, interpolation, and roll;
- editable visible-state text after the boundary;
- proof that no editable values were silently invented.

The compiler rejects any change to inherited values at or before the boundary. Camera orientation is stored as a look-at point rather than Euler angles.

## Compilation and rendering

The annotation compiles to an actor `TrajectoryInstruction`, a separate camera trajectory, and one chronological backend prompt. The inherited prefix and new suffix are joined at the locked boundary without changing earlier motion.

Blender renders diagnostic and clay profiles with identical actor/camera animation. Diagnostic overlays are evidence-only and absent from clay. VACE receives only the approved clay proxy, a control mask, and the compiled whole-video prompt.

## Iterative proxy review

D0 is the automatic proxy. Human revisions create immutable D1, D2, and later versions. The browser starts real Blender jobs and reports actual queued/running/succeeded/failed states. A failed render never replaces the last valid proxy.

VACE is not called during proxy iterations. Only a media-valid version with a matching human approval record can become a VACE job.

## Chinese guidance

The page includes a Chinese guide below the editor and uses Chinese labels by default:

- **景别**: how large the subject appears; choices are extreme wide, wide, medium, close, and extreme close.
- **焦距**: larger values narrow the view and make subjects appear closer; 35 mm is a normal wide starting point.
- **插值**: how motion changes between keyframes; linear is the default continuous option, Bezier eases motion, and constant jumps at the next keyframe.
- **Roll**: horizon tilt; keep it at 0 unless a deliberately tilted shot is required.
- **Look-at**: the world point toward which the camera is aimed.

Advanced numeric controls remain available but collapsed. The ordinary workflow exposes Chinese recommended controls and buttons for copying the locked boundary camera state.

## Pipeline boundary

```text
Human boundary selection and suffix annotation
  -> actor/camera interpolation with frozen prefix
  -> Blender diagnostic + clay rerender
  -> complete-video human review
  -> revise until approved
  -> approval-bound VACE job
  -> one real VACE inference
```

Seedance and Kling remain prompt-only baselines unless their configured APIs are independently verified to accept full video conditioning.

## Validation and failure behavior

Saving is rejected when the boundary is missing, inherited values differ from their source, or any editable actor/camera/prompt value is incomplete. Values must be finite and within configured bounds. Camera look-at cannot equal camera position and focal length must be positive.

Every source and output is checked by relative path, byte count, and SHA-256. Output directories are never overwritten. No trajectory points, camera points, prompts, annotations, or result labels are invented to make a check pass.

## Minimal acceptance

- Selecting K2 displays locked actor and camera points for K0–K2 and disables their controls.
- K3 and K4 can connect new actor/camera paths from the visible K2 boundary.
- D0 inherited values are reproducible from ShotScript; later inherited values match the previous annotation hashes.
- The page includes the full proxy video and a Chinese explanation of every camera term.
- One real Blender revise/render/review loop succeeds before approval.
- VACE preparation fails before approval and binds the exact approved proxy afterward.

## Out of scope

- model training or fine-tuning;
- video-pixel detection used as authoritative trajectory truth;
- direct ATI, ReCamMaster, or CamTrol tensor injection;
- automatic human result annotations;
- a large Seedance/Kling matrix before proxy-conditioned support is verified.
