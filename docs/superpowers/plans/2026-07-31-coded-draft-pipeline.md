# Coded Draft Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a strict semantic-plan-to-dual-Blender-draft pipeline and produce one real, hash-bound 24-fps clay pilot without API or GPU inference.

**Architecture:** A new semantic-plan parser supplies auditable story states. Existing Blender scene construction gains explicit diagnostic/clay render profiles, and a new orchestrator renders, validates, snapshots, and bundles both outputs while exposing only the clay video as future V2V conditioning.

**Tech Stack:** Python 3.10+, dataclasses, `imageio-ffmpeg`, Pillow, Blender 5.x Python API, `unittest`.

---

### Task 1: Strict semantic story plan

**Files:**
- Create: `videoactagent/semantic_plan.py`
- Create: `tests/test_semantic_plan.py`
- Create: `plans/station_reunion.semantic.json`

- [ ] Write tests that require exact keys, 4-6 strictly ordered K0-K4-style keyframes, `t=0/1` endpoints, adjacent transitions, non-empty appearance instruction, constraints, and duplicate-key rejection.
- [ ] Run `python -m unittest tests.test_semantic_plan -v` and confirm import/schema failures occur because the module is absent.
- [ ] Implement immutable plan/keyframe/transition dataclasses plus `from_path`, `from_dict`, and canonical `to_dict` methods.
- [ ] Add the manually authored station reunion plan and test it parses with five keyframes.
- [ ] Run the focused tests and commit the schema and pilot input.

### Task 2: Explicit Blender diagnostic and clay profiles

**Files:**
- Modify: `videoactagent/blender_proxy.py`
- Modify: `videoactagent/blender_runner.py`
- Create: `tests/test_blender_render_profiles.py`

- [ ] Write failing tests for runner forwarding of `--render-style`, `--fps`, and `--resolution`, and for rejecting invalid profile values before Blender launch.
- [ ] Run `python -m unittest tests.test_blender_render_profiles -v` and confirm the new arguments are absent.
- [ ] Add validated runner arguments and pass them through to Blender.
- [ ] Add a small render-profile value object inside `blender_proxy.py`; use effective fps/resolution for frame scheduling and report metadata.
- [ ] Implement clay material neutralization, stable grayscale actor contrast, and render hiding of actor labels/action axes without changing geometry or motion.
- [ ] Run profile tests plus `tests.test_blender_proxy_integration` and commit.

### Task 3: Coded draft orchestrator and bundle

**Files:**
- Create: `videoactagent/coded_draft.py`
- Create: `tests/test_coded_draft.py`
- Modify: `pyproject.toml`

- [ ] Write failing tests for immutable source snapshots, dual runner commands, exact media gates, semantic-frame selection, SHA binding, non-identical videos, existing-output rejection, and `source_video_edit` bundle semantics.
- [ ] Run `python -m unittest tests.test_coded_draft -v` and confirm the orchestrator is absent.
- [ ] Implement safe path resolution, atomic JSON writes, real MP4 decoding, semantic frame extraction, contact-sheet construction, and manifest/bundle creation.
- [ ] Add one console entry point `videoactagent-coded-draft`; do not add backend submission commands.
- [ ] Run the focused tests and commit.

### Task 4: Real Blender pilot and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/USAGE.md`
- Runtime output: `runs/work/coded_draft_v1/station_reunion/`

- [ ] Run the real local command against `stories/station_reunion.json`, `prompts/station_reunion.txt`, and `plans/station_reunion.semantic.json` using `D:\blender\blender.exe`.
- [ ] Decode both outputs and assert 120 frames, 24 fps, 5 seconds, 960x540, nonzero bytes, distinct SHA-256, and keyframe images for every semantic state.
- [ ] Visually inspect the contact sheet and record bounded observations without claiming photorealistic quality or downstream control success.
- [ ] Document the single entry command, artifacts, and backend boundary in concise Chinese usage text.
- [ ] Run all new tests plus whole-story/Blender focused regression tests and commit documentation.

### Task 5: Baseline dependency audit

**Files:**
- Create: `docs/BASELINE_TEST_AUDIT.md`

- [ ] Record the fresh baseline command and the fact that deleted historical `runs/stage*` paths cause pre-existing failures.
- [ ] Identify tests that are self-contained versus tests that incorrectly require mutable runtime artifacts.
- [ ] Do not recreate or fake deleted evidence to make those tests pass.
- [ ] Run the self-contained focused suite and record its exact result.
- [ ] Commit the audit separately from feature code.
