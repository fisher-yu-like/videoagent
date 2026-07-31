# Trajectory-Derived Prompt Compiler Design

## Goal

Remove mandatory human K-frame motion text and prevent text instructions from contradicting the authored actor/camera trajectories. Generate one complete, non-empty VACE prompt deterministically from source-bound story context and the approved trajectory annotation.

## Authoring interface

- Editable K frames require actor points and camera state, but no free-form motion Prompt.
- The page displays a read-only generated-prompt preview.
- Optional creative input is limited to fixed visual-style and mood choices. It cannot contain free-form motion, direction, timing, or camera instructions.
- The original source story prompt and appearance instruction remain visible provenance inputs and are never replaced by generated text.

## Deterministic compiler

The compiler runs locally in Python and uses no LLM or external API. It emits concise English because the current generation backends consume English prompts.

For every K transition it computes only facts present in the annotation:

- which actor moved more than the fixed normalized-plane tolerance;
- whether pairwise actor distance increased, decreased, or stayed stable;
- whether camera position or look-at changed;
- shot size, focal length, and roll changes;
- continuous-take duration and the no-cut constraint from the source story.

It never invents gestures, emotions, contact, or action names. Actor motion is phrased as `follows the authored path`; pair-distance changes use `moves closer`, `moves farther apart`, or `keeps similar spacing`. Camera facts use the submitted camera states.

The final prompt is composed in this order:

1. hash-bound source story prompt;
2. hash-bound appearance instruction;
3. deterministic whole-shot actor/camera summary;
4. optional enumerated visual-style and mood phrase;
5. continuity constraints: one continuous take, no cuts, no teleporting.

## Preview and persistence

- A local `/api/prompt-preview` endpoint accepts the same annotation candidate used for generation, performs full validation, and returns the compiled prompt without writing an iteration or starting Blender.
- The browser refreshes the preview only on an explicit `预览自动 Prompt` action, avoiding hidden network or render activity.
- `prepare_iteration` calls the same Python compiler again. Browser preview text is never trusted as an input.
- The canonical annotation stores the selected style/mood enum values, prompt compiler version, and source hashes. The compiled prompt remains a separate hash-bound artifact.

## Schema changes

- Remove `visible_state` from submitted keyframe fields.
- Add top-level `visual_style` with allowed values `source_default`, `cinematic_realism`, and `documentary`.
- Add top-level `mood` with allowed values `source_default`, `warm`, `neutral`, and `tense`.
- Add `prompt_compiler_version` to the canonical annotation. The client does not choose this value; the server writes it.
- Inherited D0 semantic descriptions remain reference evidence but are not copied into editable K-frame motion instructions.

No completed human D1 exists in the current workspace, so this strict schema replacement needs no migration of approved results.

## Failure behavior

- Prompt preview lists missing actor/camera fields using the same exact validation as generation.
- Unknown enum values, non-finite trajectories, false camera provenance, or stale inheritance hashes are rejected.
- The compiler must always produce non-empty text. If source story or appearance bindings are missing, it fails instead of fabricating content.

## Acceptance checks

1. K3/K4 can be completed without entering motion text.
2. Moving actor_a closer to actor_b generates a decreasing-distance fact and cannot generate an increasing-distance fact.
3. A stationary camera generates a static-camera fact; a changed camera generates a camera-motion fact.
4. Repeating the same source and annotation produces byte-identical prompt output.
5. Preview creates no files, render jobs, Blender process, API call, or GPU work.
6. `prepare_iteration` persists exactly the prompt returned by the same compiler.
7. VACE director-job verification still requires and hash-verifies a non-empty compiled prompt.
