# Human Director Panel Design

## Goal

Upgrade the existing human actor-trajectory author into a guided K0–K4 director panel with a complete-video review loop. A human specifies actor motion, camera motion, and keyframe-visible intent. These annotations regenerate the full Blender proxy, return it to the same panel for review, and repeat until the human explicitly approves one version for VACE.

The feature must remain a small extension of the current end-to-end pipeline. It does not train a model and does not claim that VACE directly consumes trajectory JSON.

## User workflow

The page first presents the complete current diagnostic proxy in a video player. Its seek bar contains K0–K4 markers; selecting a marker seeks to that timestamp and opens its editor. For each K0–K4 state, the user:

1. reads the diagnostic reference frame and semantic state;
2. places every actor on the top-down world plane;
3. places the camera, sets its look-at point, and selects focal length and shot size;
4. edits the visible-state prompt for that keyframe;
5. saves the keyframe and advances to the next one.

The page shows completed/incomplete status for every keyframe, draws actor and camera paths in distinct colors, and keeps advanced numeric camera fields collapsed by default. After editing, the user selects **Generate next proxy**. Blender renders the next version and the complete new video replaces the current review video only after media validation succeeds.

The user then either starts another edit/render iteration or selects **Approve current proxy**. Approval freezes the exact version and enables VACE job creation. Viewing only the five still frames is never sufficient for approval.

## Data model

The existing actor-only `TrajectoryInstruction` remains valid. A separate versioned `DirectorAnnotation` records:

- author identity and save timestamp;
- source manifest, ShotScript, semantic-plan, and reference-frame hashes;
- K0–K4 timestamps and frame indices;
- actor world positions for every actor and keyframe;
- camera position, look-at point, focal length, shot size, and interpolation mode for every keyframe;
- editable keyframe visible-state text;
- proof that no required values were automatically filled.

Each annotation also records its iteration ID, parent iteration ID, source proxy record, output diagnostic/clay proxy records, render status, and optional human approval event. The approval event contains author identity, time, approved iteration ID, and hashes of the annotation and both proxy videos.

Camera orientation is stored as a look-at point rather than Euler angles in the default interface. Optional roll and exact XYZ fields are advanced controls. Actor and camera records are independent so neither can silently overwrite the other.

## Compilation and rendering

The director annotation is validated against the current story inputs and then compiled into two artifacts:

1. an actor `TrajectoryInstruction` interpolated over the full 120-frame timeline;
2. a camera trajectory interpolated over the same timeline, including focal-length changes.

Blender consumes both artifacts and renders a new diagnostic proxy and a new clay proxy. Both render profiles use identical actor and camera animation. Diagnostic overlays remain evidence-only and are absent from the clay proxy.

The five keyframe descriptions are compiled in chronological order into one backend prompt. Structured camera values are authoritative: derived camera wording is appended by the compiler so free text cannot silently contradict the rendered camera path.

VACE receives the newly rendered clay proxy, a control mask, and the compiled whole-video prompt. VACE does not receive the trajectory JSON as a native model input. The JSON remains available for provenance and post-generation adherence measurements.

## Iterative proxy review

The automatically generated proxy is iteration `D0`. A saved human revision creates `D1`, the next revision creates `D2`, and so on. Iterations are immutable and an output directory is never reused.

The browser starts Blender as a local background job through the existing application service; the user does not need to run a CLI command. The page displays actual job state rather than simulated progress. If Blender fails, the failed iteration is recorded and the last valid proxy remains active.

During an active loop, the current and immediately previous proxy videos remain available for comparison. All older annotations, manifests, hashes, and failure evidence remain available, but their large video files may be removed after approval. The approved video and its immediate predecessor are retained.

VACE is not called during proxy iterations. Only an immutable, media-valid iteration with a matching human approval record can be converted into a VACE job.

## Pipeline boundary

```text
Human Director Annotation
  -> actor/camera interpolation
  -> Blender diagnostic + clay rerender
  -> complete-video human review
  -> revise and rerender until approved
  -> freeze approved proxy version
  -> VACE control preprocessing
  -> one real VACE inference
  -> media validation and trajectory evaluation
```

Seedance and Kling are not described as full-proxy V2V backends unless their configured API payloads are independently verified to accept video conditioning. Their prompt-only results remain separate baselines.

## Validation and failure behavior

Saving is rejected when any actor, camera position, look-at point, focal length, or keyframe description is missing. Values must be finite and within configured world/camera bounds. Camera look-at cannot equal camera position, focal length must be positive, and K IDs/timestamps must exactly match the semantic plan.

Every referenced source file, full source proxy, and reference frame is checked by relative path, byte count, and SHA-256. Existing output directories are not overwritten. A failed render or inference publishes failure evidence but never a success bundle. The approval endpoint rejects an incomplete or undecodable video, a stale iteration, an annotation/proxy hash mismatch, or a missing author identity.

No trajectory points, camera points, prompts, annotations, or evaluation labels are automatically invented to make a check pass.

## Minimal acceptance

- The UI saves one complete, human-authored station annotation covering two actors and K0–K4.
- The saved annotation contains five complete camera states and five non-empty keyframe prompts.
- The UI plays the complete current proxy, exposes K0–K4 seek markers, and returns a successful next Blender render to the player as a new immutable iteration.
- Blender produces decodable diagnostic and clay videos with the expected duration, frame count, FPS, and resolution.
- The two render profiles have identical actor/camera animation bindings but non-identical pixels.
- At least one real revise/rerender/review loop is completed before approval.
- VACE job creation is rejected before approval and succeeds only for the exact approved proxy hashes.
- The generated VACE job is bound to the new clay proxy and director-annotation hashes.
- One real VACE output is retained with request/job provenance, server log, media metadata, and SHA-256.

## Out of scope

- model training or fine-tuning;
- direct ATI, ReCamMaster, or CamTrol tensor injection;
- automatic camera planning that replaces human choices;
- automatic human result annotations;
- a large Seedance/Kling matrix before proxy-conditioned backend support is verified.
