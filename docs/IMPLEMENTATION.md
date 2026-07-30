# Whole-Story Repository Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-shot user workflow with one offline whole-story runner that prepares eight distinct prompts, proxies, and backend bundles, quarantine invalid historical scoring claims, and simplify the repository and documentation.

**Architecture:** A new `whole_story` orchestrator validates a checked-in suite, invokes the existing real Blender renderer once per one-take story, normalizes outputs to generic story paths, extracts inspection frames, writes story-level hash-bound bundles, and emits one summary. Existing trajectory code remains available for preserved evidence but is not part of this path.

**Tech Stack:** Python 3.12 standard library, existing ShotScript/Blender renderer, imageio-ffmpeg, Pillow, unittest, Blender 5.1.2.

---

### Task 1: Freeze and correct historical evaluation claims

**Files:**
- Create: `docs/EXPERIMENTS.md`
- Modify: `examples/module_io_manifest.json` only if evidence paths migrate
- Test: `tests/test_whole_story.py`

- [ ] Record the actual 15-second proxy and four 5.04-second API video hashes.
- [ ] Record that requests contained duration 5 and text only.
- [ ] Mark revision causality, same-seed fairness, full-story completeness, and camera conclusions invalid.
- [ ] Preserve only descriptive s01 metrics and immutable real artifacts.

### Task 2: Define the whole-story suite with TDD

**Files:**
- Create: `videoactagent/whole_story.py`
- Create: `tests/test_whole_story.py`
- Create: `configs/whole_story_suite.json`
- Create: `stories/*.json`
- Create: `prompts/*.txt`

- [ ] Write failing tests for schema, eight unique cases, fixed profile and source existence.
- [ ] Run the focused test and confirm the expected failure.
- [ ] Implement immutable suite parsing and validation.
- [ ] Require unique story, prompt and ShotScript hashes and distinct motion signatures.
- [ ] Run focused tests and commit.

### Task 3: Implement one offline entry point

**Files:**
- Create: `run.py`
- Modify: `videoactagent/whole_story.py`
- Test: `tests/test_whole_story.py`

- [ ] Write failing tests for `submit=false`, `max_api_calls=0`, workspace-safe output and immutable runs.
- [ ] Implement `python run.py <config>` with no subcommands.
- [ ] Validate all cases before launching Blender.
- [ ] Refuse any network/API mode in this phase.
- [ ] Run focused tests and commit.

### Task 4: Render eight real complete proxies

**Files:**
- Modify: `videoactagent/whole_story.py`
- Test: `tests/test_whole_story_integration.py`
- Runtime: `runs/work/whole_story_v1/`

- [ ] Write a real Blender integration test for one case.
- [ ] Invoke the existing Blender runner for the whole one-take story.
- [ ] Normalize outputs to `proxy.blend`, `proxy.mp4`, `manifest.json`.
- [ ] Extract first, middle and last decoded frames from the full proxy.
- [ ] Require 15 frames, 3 fps, 5 seconds, 960×540 and non-identical keyframes.
- [ ] Render all eight cases and verify all prompt, ShotScript and MP4 hashes are unique.

### Task 5: Prepare honest story-level bundles

**Files:**
- Modify: `videoactagent/whole_story.py`
- Test: `tests/test_whole_story.py`

- [ ] Write failing tests that bundles contain no `shot_id`.
- [ ] Source duration from story profile rather than `shots[0]`.
- [ ] Emit Kling/Seedance `prompt_only` bundles with unsupported seed recorded.
- [ ] Emit VACE `source_video` bundle with the proxy hash.
- [ ] Write a suite summary and run module-level validation.

### Task 6: Add duration coverage gate

**Files:**
- Modify: `videoactagent/whole_story.py`
- Test: `tests/test_whole_story.py`

- [ ] Write failing tests for 5.04/5.0 accepted and 5.04/15.0 rejected.
- [ ] Implement coverage with real decoded duration and explicit threshold 0.95.
- [ ] Record `incomplete` before any movement metric is allowed.
- [ ] Report real annotated-frame count separately from interpolation sample count.

### Task 7: Simplify repository and docs

**Files:**
- Rewrite: `README.md`
- Create: `docs/USAGE.md`
- Keep: `docs/ARCHITECTURE.md`, `docs/EXPERIMENTS.md`
- Delete: old tracked `docs/reports/*`, `docs/superpowers/*`, `docs/DEBUGGING.md` after facts migrate
- Delete: safe ignored caches and scratch diagnostics only

- [ ] Replace the mojibake README with short UTF-8 Chinese documentation.
- [ ] Document one command, output layout and evidence boundaries.
- [ ] Move no user-owned `.idea/` or `artifacts/` content.
- [ ] Preserve ignored real artifacts required by tests and current forensic audit.
- [ ] Delete only dependency-audited debug/cache paths.

### Task 8: Verify and review

**Files:**
- Test: full `tests/`

- [ ] Decode and hash all eight real proxies.
- [ ] Run focused whole-story tests.
- [ ] Run the complete Python 3.12 test suite.
- [ ] Run `git diff --check` and inspect tracked/untracked state.
- [ ] Request an independent code and evidence review.
- [ ] Commit without `.idea/` or `artifacts/`; do not push without separate approval.
