# Stage 8 Environment Presets Implementation Plan

**Goal:** Replace the Blender proxy's hard-coded station environment with four small, deterministic scene presets and render one real five-second preview for each newly added environment before any generation API is used.

**Architecture:** New `ShotScript` files gain an explicit, allow-listed `environment_preset`. The byte-bound legacy `station_platform` example alone keeps a compatibility default of `station`, so Stage 1-7 evidence hashes remain valid. The existing Blender compiler dispatches that value to fixed Python scene builders (`station`, `city_crosswalk`, `forest_path`, `studio_room`). Camera and actor scheduling remain unchanged, so this stage changes only scene geometry and appearance. No LLM-generated Blender code is introduced.

**Verification boundary:** Parser tests prove schema propagation and rejection of unsupported presets. Real Blender integration renders and reopens one 15-frame MP4/BLEND pair per new preset; decoded frame counts, distinct frames, dimensions, output hashes, and visual keyframes are recorded. This stage makes no Seedance, Kling, or other paid API calls.

## Task 1: Extend the ShotScript contract

- Add a failing parser test for the explicit station preset.
- Add failing tests for missing presets on new scenes and unsupported presets.
- Add `environment_preset` to `ShotScript` and preserve it when trajectory compilation snapshots the script.
- Update the existing station example.

## Task 2: Add deterministic Blender preset builders

- Keep the existing station geometry unchanged.
- Add simple city-crosswalk, forest-path, and studio-room builders using Blender primitives and fixed materials.
- Dispatch only allow-listed presets from `configure_scene`.
- Record `scene_id` and `environment_preset` in the trajectory report.

## Task 3: Add minimal real examples

- Add one-shot, two-actor ShotScripts for the three new environments.
- Keep duration, fps, actor identities, and camera syntax compatible with the existing control bridge.
- Use visibly different camera/actor trajectories across the examples.

## Task 4: Run real local experiments

- Render each new example with `D:\blender\blender.exe` into a fresh run directory.
- Reopen each generated `.blend` in background mode.
- Decode every MP4 and require 15 frames, 960x540 resolution, and more than one unique frame.
- Inspect first/middle/last images and record exact hashes and any observed defects.

## Task 5: Report before later API work

- Write a stage report containing commands, artifacts, hashes, measured metadata, and visual findings.
- Run focused tests and the full unit suite.
- Report that API usage is zero and server usage for this stage is zero.
- Do not start a multi-scene Seedance/Kling matrix without a separately bounded approval.
