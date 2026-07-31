# Coded Draft Pipeline Design

## Goal

Adopt the useful part of VideoCoCo without copying its incomplete inference stack: convert an explicit semantic story plan into two native-frame-rate Blender drafts, keep a diagnostic render separate from the neutral conditioning render, and emit a backend-neutral video-edit bundle that can later feed VACE or another real V2V model.

## Evidence behind the design

VideoCoCo demonstrates that a 5-second, 24-fps neutral clay draft can preserve a physical event while a video-edit model changes appearance. Its public repository does not currently provide a runnable complete baseline: the tuned Hugging Face repository has no weights, the JSONL toy manifest is read with `json.load`, the training patch references an absent `v2v_data` module, and only three of the five README-listed skills are present. Its inference code is also governed by the Tencent HY license. We therefore reuse the architectural ideas and data contracts, not copied implementation code.

Our current whole-story pipeline has the opposite strengths: real Blender/API/VACE executions, immutable hashes, media completeness gates, explicit actors and cameras, and local trajectory tooling. Its main deficiencies are that the Blender source draft is only 15 frames at 3 fps, semantic intermediate states are absent, diagnostic colors and labels are sent into the VACE control branch, and Kling/Seedance receive only text rather than the proxy or trajectory.

## Scope

This increment builds one self-contained local pilot and reusable modules:

1. A strict `SemanticStoryPlan` with K0-K4 states, adjacent transitions, causal constraints, `must_show`, `must_avoid`, and a separate appearance/edit instruction.
2. Two Blender render profiles from the same ShotScript:
   - `diagnostic`: existing colors, labels, and guide axis for human inspection.
   - `clay`: grayscale materials, no actor labels or guide axis, native 24 fps, 120 frames, 960x540.
3. A `CodedDraftBundle` binding prompt, ShotScript, semantic plan, diagnostic video, clay video, edit instruction, media metadata, and SHA-256 values.
4. A real local `station_reunion` pilot with both videos and a semantic-keyframe contact sheet.

This increment does not call Kling, Seedance, or any other API; does not run VACE; does not download or vendor OmniWeaving/VideoCoCo weights; and does not claim that a neutral draft alone improves final generation.

## Components

### `videoactagent/semantic_plan.py`

Parses a strict JSON schema. A plan contains exactly one `story_id`, a 5-second duration, an appearance instruction, 4-6 ordered keyframes starting at `t=0` and ending at `t=1`, adjacent transitions, constraints, and audit lists. Keyframes describe visible story state rather than Blender primitives. The parser rejects duplicate keys, unknown keys, discontinuous transition chains, invalid times, and empty requirements.

### Blender render profiles

`blender_runner.py` forwards explicit style, fps, and resolution arguments to `blender_proxy.py`. `blender_proxy.py` uses the effective fps for animation frame scheduling. Clay mode converts every material to neutral grayscale, gives actors stable grayscale contrast, hides text labels and guide axes, and retains geometry, actor movement, camera movement, lighting, and background depth cues.

Both profiles are real Blender renders. Temporal upsampling or final-frame cloning is forbidden in this stage; the clay file must decode as exactly 120 frames at 24 fps.

### `videoactagent/coded_draft.py`

The orchestrator snapshots source files, runs Blender twice into a new output directory, decodes both MP4s, validates frame count/fps/duration/resolution, extracts frames aligned to semantic keyframe times, builds a comparison sheet, and writes an atomic manifest. Existing output directories are rejected.

The emitted V2V bundle uses `conditioning_mode=source_video_edit`, points only to the clay video, stores the appearance instruction separately from motion semantics, and records that no backend has consumed it yet. The diagnostic video is evidence only and is never declared as a model condition.

### Pilot input

`plans/station_reunion.semantic.json` describes the approach event: initial separation, approach onset, continued approach, near-arrival, and final reunion distance, with a locked camera and no premature contact. It is manually authored and is not represented as automatic LLM planning.

## Validation

- Unit tests prove strict plan parsing and render-profile argument propagation.
- Blender profile tests inspect scene/report metadata, labels/axis visibility, and clay material colors.
- The real pilot must produce two decodable, non-identical MP4s.
- The clay video must be 120 frames, 24 fps, 5.0 seconds, and 960x540 without interpolation.
- All semantic-keyframe images must be extracted from decoded real frames.
- The manifest must bind every source and output by SHA-256.

## Follow-up boundary

After this pilot, generation-quality diagnosis and the comparison annotator are separate increments. Existing evidence already establishes that Kling/Seedance did not consume the proxy, so their failures cannot be attributed to proxy appearance. VACE similarity must be measured against both the diagnostic source and the new clay source before changing inference settings.
