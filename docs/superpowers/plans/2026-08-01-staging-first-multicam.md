# Staging-First Multicam Director Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an approved actor/object trajectory stage before Agent camera planning while keeping one small three-step browser page.

**Architecture:** Store immutable staging versions as strict `TrajectoryInstruction` records. Bind planning and rendering to the approved staging hash, invalidate only active downstream pointers when staging changes, and preserve all historical evidence. Rework only the new multicamera page into three numbered cards.

**Tech Stack:** Python 3.12, standard library HTTP, existing trajectory dataclasses, vanilla HTML/JavaScript, unittest, Blender 5.1.

---

### Task 1: Immutable staging state and gates

**Files:**
- Modify: `videoactagent/director_multicam.py`
- Modify: `tests/test_director_multicam.py`

- [ ] Write failing tests asserting initial `S1`, plan rejection before staging approval, immutable `S2`, locked-prefix rejection, and approved-staging hash in render job.
- [ ] Run `py -3.12 -m unittest tests.test_director_multicam -v` and confirm the new assertions fail because staging APIs are absent.
- [ ] Add `save_staging()` and `approve_staging()`, strict actor/object validation, approved staging in `_scene_context()`, and binding in `prepare_render()`.
- [ ] Run the same suite and confirm all tests pass.
- [ ] Commit `feat: gate camera planning on approved staging`.

### Task 2: Three-card browser workflow

**Files:**
- Modify: `static/director_multicam_panel.html`
- Modify: `videoactagent/director_multicam.py`
- Modify: `tests/test_director_multicam_panel.py`

- [ ] Write failing static/API contract tests for `stage-staging`, `stage-camera`, `stage-result`, `target-select`, `add-object`, `save-staging`, `approve-staging` and concise `.hint` text.
- [ ] Run the panel tests and confirm missing elements/endpoints fail.
- [ ] Add staging GET data, `POST /api/staging`, `POST /api/staging/{id}/approve`, canvas actor/object editing, disabled downstream buttons, compact three-card styling, and state-driven automatic scroll to the first incomplete card.
- [ ] Run panel, workspace and legacy compatibility tests.
- [ ] Commit `feat: add staging-first multicam workflow`.

### Task 3: Real binding verification and documentation

**Files:**
- Modify: `tests/test_multicam_blender_integration.py`
- Modify: `docs/USAGE.md`
- Modify: `docs/MULTICAM_EXPERIMENT.md`

- [ ] Update the real integration setup to approve S1 before P1 and assert render job/evidence contains the S1 hash.
- [ ] Run the real Blender test and decode all three videos.
- [ ] Update the short Chinese instructions without adding routine CLI commands.
- [ ] Run the 64-test targeted suite and `git diff --check`.
- [ ] Commit `docs: explain staging-first director flow`.
